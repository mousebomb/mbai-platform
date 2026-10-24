"""执行器：从队列取任务 → 交给引擎适配器 → 收产物 → 落库。

引擎细节全部在 adapters/ 里：接新引擎只加适配器，这里的核心路径不动。
"""

from __future__ import annotations

import asyncio
import time

from .adapters import get_adapter
from .adapters.base import Adapter, AdapterError, EngineState
from .assets import asset_path
from .contracts import Job, JobState
from .registry import registry
from .store import JobStore
from .workflow import resolve_params


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
            # image / audio 参数存的是 asset_id，这里解析成绝对路径再交给适配器；
            # 适配器不碰数据库/资产目录，只认文件路径
            for name, desc in spec.inputs.items():
                if desc.get("type") in {"image", "audio"} and name in params:
                    p = asset_path(params[name])
                    if p is None:
                        raise ValueError(f"参数 {name} 的素材不存在: {params[name]}")
                    params[name] = str(p)
            adapter = get_adapter(spec.engine)

            job.engine_ref = await adapter.submit(spec, params, job.id)
            self.store.save(job)

            status = await self._wait(adapter, job)
            if status.state == EngineState.FAILED:
                raise AdapterError(status.error or "引擎执行失败")
            # 引擎自己报取消（例如 CLI 适配器的进程被 kill）也要停，不能往下收产物
            if status.state == EngineState.CANCELLED:
                raise AdapterError(status.error or "任务已取消")

            job.artifacts = await adapter.collect(job.engine_ref or "", job.id, params)
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

    async def _wait(self, adapter: Adapter, job: Job) -> EngineStatus:
        """轮询引擎状态，同时响应取消请求、把实时进度写进库。"""
        while True:
            status = await adapter.status(job.engine_ref or "")
            if status.state in {EngineState.SUCCEEDED, EngineState.FAILED, EngineState.CANCELLED}:
                return status
            # 进度变化 ≥0.01 才落库，避免每 2 秒刷一次 SQLite
            p = min(0.99, max(0.0, status.progress))
            if abs(p - job.progress) >= 0.01:
                job.progress = p
                self.store.save(job)
            fresh = self.store.get(job.id)
            if fresh and fresh.state == JobState.CANCELLED:
                await adapter.cancel(job.engine_ref or "")
                raise AdapterError("任务已取消")
            await asyncio.sleep(2.0)
