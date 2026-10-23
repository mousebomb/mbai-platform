"""ComfyUI 适配器：把 ComfyClient 的 HTTP 传输包装成 Adapter 契约。

HTTP 细节全部留在 comfy_client.py；这里只做状态映射与产物落盘。
工作流构建（build_workflow）的逻辑在 workflow.py，这里只是编排调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..comfy_client import ComfyClient
from ..contracts import Artifact
from ..registry import VariantSpec
from ..settings import settings
from ..workflow import build_workflow
from .base import Adapter, AdapterError, EngineState, EngineStatus


class ComfyAdapter(Adapter):
    kind = "comfy"

    def __init__(self, url: str | None = None, timeout: float = 30.0) -> None:
        self.url = (url or settings.comfy_url).rstrip("/")
        # 任务操作要容忍长执行；健康检查单独用短超时快速失败
        self.timeout = timeout

    async def health(self) -> dict:
        """探活。失败也返回结构化 dict，调用方不用 try/except。"""
        try:
            async with ComfyClient(self.url, timeout=5) as client:
                stats = await client.health()
            return {"ok": True, "url": self.url, "devices": stats.get("devices")}
        except Exception as e:  # noqa: BLE001 - 探活失败要原样上报
            return {"ok": False, "url": self.url, "error": f"{type(e).__name__}: {e}"}

    async def submit(self, spec: VariantSpec, params: dict[str, Any], job_id: str) -> str:
        """构建工作流并提交，返回 prompt_id 作为 ref。

        job_id 目前用不上（ComfyUI 用 client_id 区分提交方），
        接口里保留它是为了让别的引擎能按任务命名/分组。
        """
        workflow = build_workflow(spec, params)
        async with ComfyClient(self.url, timeout=self.timeout) as client:
            return await client.submit(workflow)

    async def status(self, ref: str) -> EngineStatus:
        async with ComfyClient(self.url, timeout=self.timeout) as client:
            history = await client.history(ref)
        status = history.get("status") or {}
        # 还没出现在 history = 仍在排队
        if not status:
            return EngineStatus(state=EngineState.QUEUED)
        if status.get("completed") or status.get("status_str") in {"success", "error"}:
            if status.get("status_str") == "error":
                return EngineStatus(
                    state=EngineState.FAILED,
                    error=_format_status_error(status),
                    detail=history,
                )
            return EngineStatus(state=EngineState.SUCCEEDED, progress=1.0, detail=history)
        return EngineStatus(state=EngineState.RUNNING, detail=history)

    async def cancel(self, ref: str) -> None:
        async with ComfyClient(self.url, timeout=self.timeout) as client:
            await client.cancel(ref)

    async def collect(self, ref: str, job_id: str, params: dict[str, Any]) -> list[Artifact]:
        """把产物写进 artifacts_dir/<job_id>/，返回产物清单。"""
        out_dir = settings.artifacts_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[Artifact] = []
        async with ComfyClient(self.url, timeout=self.timeout) as client:
            history = await client.history(ref)
            for node_output in (history.get("outputs") or {}).values():
                for image in node_output.get("images") or []:
                    if image.get("type") != "output":
                        continue
                    data = await client.download(
                        image["filename"], image.get("subfolder", ""), image.get("type", "output")
                    )
                    path = out_dir / _safe_name(image["filename"])
                    path.write_bytes(data)
                    artifacts.append(
                        Artifact(
                            job_id=job_id,
                            kind="image",
                            path=str(path),
                            width=params.get("width"),
                            height=params.get("height"),
                        )
                    )
        if not artifacts:
            raise AdapterError("工作流执行完成但没有产出文件")
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
