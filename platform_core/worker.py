"""执行器：从队列取任务 → 提交 ComfyUI → 收产物 → 落库。

单进程内跑一个 asyncio 任务，GPU 串行；API 线程只读写数据库，永远不阻塞请求。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .comfy_client import ComfyClient, ComfyError
from .contracts import Artifact, Job, JobState
from .registry import registry
from .settings import settings
from .store import JobStore
from .workflow import build_workflow, resolve_params


class Worker:
    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="platform-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            job = self.store.claim_next()
            if job is None:
                await asyncio.sleep(1.0)
                continue
            await self._execute(job)

    async def _execute(self, job: Job) -> None:
        # 队列路径已由 claim_next 置好状态；CLI 直跑时在这里补上
        if job.started_at is None:
            job.started_at = time.time()
            job.state = JobState.RUNNING
        try:
            spec = registry.get(job.capability, job.variant)
            # 只接受注册表中已声明的参数，防止前端塞入任意字段改工作流
            params = resolve_params(spec, job.params)
            workflow = build_workflow(spec, params)

            async with ComfyClient(settings.comfy_url) as comfy:
                job.engine_ref = await comfy.submit(workflow)
                self.store.save(job)

                history = await self._wait(comfy, job)
                status = history.get("status") or {}
                if status.get("status_str") == "error":
                    raise ComfyError(_format_status_error(status))

                job.artifacts = await self._collect(comfy, job, history, params)
            job.state = JobState.SUCCEEDED
        except Exception as e:  # noqa: BLE001 - 任何失败都要落库，不能吞掉
            # 用户主动取消的任务不能被覆盖成 failed
            fresh = self.store.get(job.id)
            cancelled = fresh is not None and fresh.state == JobState.CANCELLED
            job.state = JobState.CANCELLED if cancelled else JobState.FAILED
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.finished_at = time.time()
            job.progress = 1.0 if job.state == JobState.SUCCEEDED else job.progress
            self.store.save(job)

    async def _wait(self, comfy: ComfyClient, job: Job) -> dict[str, Any]:
        """轮询任务状态，同时响应取消请求。"""
        while True:
            history = await comfy.history(job.engine_ref or "")
            status = history.get("status") or {}
            if status.get("completed") or status.get("status_str") in {"success", "error"}:
                return history
            fresh = self.store.get(job.id)
            if fresh and fresh.state == JobState.CANCELLED:
                await comfy.cancel(job.engine_ref or "")
                raise ComfyError("任务已取消")
            # TODO: 接 ComfyUI 的 websocket 拿真实进度，目前进度只在开始时置 0
            await asyncio.sleep(2.0)

    async def _collect(
        self, comfy: ComfyClient, job: Job, history: dict[str, Any], params: dict[str, Any]
    ) -> list[Artifact]:
        out_dir = settings.artifacts_dir / job.id
        out_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[Artifact] = []
        for node_output in (history.get("outputs") or {}).values():
            for image in node_output.get("images") or []:
                if image.get("type") != "output":
                    continue
                data = await comfy.download(
                    image["filename"], image.get("subfolder", ""), image.get("type", "output")
                )
                path = out_dir / _safe_name(image["filename"])
                path.write_bytes(data)
                artifacts.append(
                    Artifact(
                        job_id=job.id,
                        kind="image",
                        path=str(path),
                        width=params.get("width"),
                        height=params.get("height"),
                    )
                )
        if not artifacts:
            raise ComfyError("工作流执行完成但没有产出文件")
        return artifacts


def _safe_name(name: str) -> str:
    return Path(name).name


def _format_status_error(status: dict[str, Any]) -> str:
    messages = status.get("messages") or []
    for item in messages:
        if isinstance(item, list) and len(item) == 2 and item[0] == "execution_error":
            detail = item[1]
            return f"{detail.get('node_type')}: {detail.get('exception_message')}"
    return "ComfyUI 返回执行错误"
