"""ComfyUI 适配器：把 ComfyClient 的 HTTP 传输包装成 Adapter 契约。

HTTP 细节全部留在 comfy_client.py；这里只做状态映射与产物落盘。
工作流构建（build_workflow）的逻辑在 workflow.py，这里只是编排调用。

真实进度：submit() 后起一个后台 watcher 任务连引擎 websocket
（ws://<host>/ws?clientId=<提交时的 client_id>），按 prompt_id 过滤
progress / progress_state 消息维护 0~1 进度。ws 连不上只降级为 0，
绝不影响执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from ..assets import image_size
from ..comfy_client import ComfyClient
from ..contracts import Artifact
from ..registry import VariantSpec
from ..settings import settings
from ..workflow import build_workflow
from .base import Adapter, AdapterError, EngineState, EngineStatus

# 进度上限：1.0 只留给终态（SUCCEEDED），执行中最多 0.99
_PROGRESS_CEIL = 0.99
# watcher 最长存活时间：正常任务一两分钟内结束，超时说明消息流断了
_WATCH_TIMEOUT_SEC = 600

_logger = logging.getLogger("platform.adapters.comfy")


class ComfyAdapter(Adapter):
    kind = "comfy"

    def __init__(self, url: str | None = None, timeout: float = 30.0) -> None:
        self.url = (url or settings.comfy_url).rstrip("/")
        # 任务操作要容忍长执行；健康检查单独用短超时快速失败
        self.timeout = timeout
        # prompt_id → 实时进度（0~0.99）与后台 watcher 任务
        self._progress: dict[str, float] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}

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
            # 带输入图的工作流：把本机绝对路径先传给引擎，换成它能认的文件名
            await self._upload_local_inputs(workflow, client)
            ref = await client.submit(workflow)
            cid = client.client_id
        # 提交成功就起后台 watcher 连 ws 跟踪真实进度；连不上只是降级为 0。
        # 注意：引擎把 progress/progress_state/execution_* 定向发给提交时
        # 携带的 client_id，watcher 必须用同一个 clientId 连接才能收到。
        self._progress.setdefault(ref, 0.0)
        self._watchers[ref] = asyncio.create_task(
            self._watch_progress(ref, cid), name=f"comfy-progress-{ref}"
        )
        return ref

    async def _upload_local_inputs(self, workflow: dict[str, Any], client: ComfyClient) -> None:
        """把工作流里「本机绝对路径」形式的输入换成引擎里的文件名。

        LoadImage 这类节点只接受 ComfyUI input/ 目录下的文件名，不认绝对路径。
        判定条件很窄：值必须是字符串、是绝对路径、且文件真实存在——普通的文本参数
        不会满足，所以不会误伤 prompt。
        """
        for node in workflow.values():
            inputs = node.get("inputs") or {}
            for key, value in list(inputs.items()):
                if not isinstance(value, str):
                    continue
                p = Path(value)
                if not p.is_absolute() or not p.is_file():
                    continue
                inputs[key] = await client.upload_image(p)

    async def _watch_progress(self, ref: str, cid: str) -> None:
        """后台任务：连引擎 websocket，把 ref 的实时进度写进 self._progress。

        - 连不上/中途断线只降级（进度停在缓存值），绝不影响任务执行
        - 收到 execution_success / execution_error 或超过 10 分钟自动退出
        - 进度一律 clamp 到 0~_PROGRESS_CEIL 且只增不减
        """
        ws_url = (
            self.url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            + f"/ws?clientId={cid}"
        )
        try:
            ws = await websockets.connect(
                ws_url, open_timeout=5, ping_interval=20, ping_timeout=10
            )
        except Exception as e:  # noqa: BLE001 - ws 只是进度增强，连不上就降级
            _logger.warning("ComfyUI websocket 连接失败，进度降级为 0: %s", e)
            self._watchers.pop(ref, None)
            return
        try:
            async with ws, asyncio.timeout(_WATCH_TIMEOUT_SEC):
                while True:
                    raw = await ws.recv()
                    if self._handle_progress_message(ref, raw):
                        break
        except asyncio.TimeoutError:
            pass  # 超时说明消息流断了；status() 走 HTTP 兜底
        except ConnectionClosed:
            pass  # 引擎侧断开（含任务被取消），静默退出
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _logger.warning("ComfyUI 进度 watcher 异常退出: %s", e)
        finally:
            self._watchers.pop(ref, None)

    def _handle_progress_message(self, ref: str, raw: str | bytes) -> bool:
        """解析一条 ws 消息并更新进度；返回 True 表示任务在引擎侧已结束。

        只认 `progress`（value/max，采样器按步上报，粒度最细）。
        `progress_state` 是节点级状态，实测只在节点边界推送、value 常驻 0，
        用它算总进度会让进度条长时间停在 0 然后跳到 0.99 —— 只用来兜底判终态。
        """
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return False
        if not isinstance(msg, dict):
            return False
        mtype = msg.get("type")
        # 引擎侧终局：execution_success / execution_error / execution_interrupted
        if mtype in {"execution_success", "execution_error", "execution_interrupted"}:
            data = msg.get("data") or {}
            return isinstance(data, dict) and data.get("prompt_id") == ref
        data = msg.get("data")
        if not isinstance(data, dict) or data.get("prompt_id") != ref:
            return False
        if mtype == "progress":
            self._apply(ref, _ratio_progress(data))
        return False

    def _apply(self, ref: str, p: float | None) -> None:
        """clamp 到 [0, _PROGRESS_CEIL] 且只增不减，写入缓存。"""
        if p is None:
            return
        prev = self._progress.get(ref, 0.0)
        self._progress[ref] = min(_PROGRESS_CEIL, max(prev, p))

    def _drop(self, ref: str) -> None:
        """清掉 ref 的进度缓存与 watcher，任务收尾/取消时调用，防止泄漏。"""
        self._progress.pop(ref, None)
        task = self._watchers.pop(ref, None)
        if task is not None and not task.done():
            task.cancel()

    async def status(self, ref: str) -> EngineStatus:
        async with ComfyClient(self.url, timeout=self.timeout) as client:
            history = await client.history(ref)
        status = history.get("status") or {}
        progress = self._progress.get(ref, 0.0)
        # 还没出现在 history = 仍在排队
        if not status:
            return EngineStatus(state=EngineState.QUEUED, progress=progress)
        if status.get("completed") or status.get("status_str") in {"success", "error"}:
            if status.get("status_str") == "error":
                return EngineStatus(
                    state=EngineState.FAILED,
                    progress=progress,
                    error=_format_status_error(status),
                    detail=history,
                )
            return EngineStatus(state=EngineState.SUCCEEDED, progress=1.0, detail=history)
        return EngineStatus(state=EngineState.RUNNING, progress=progress, detail=history)

    async def cancel(self, ref: str) -> None:
        self._drop(ref)
        async with ComfyClient(self.url, timeout=self.timeout) as client:
            await client.cancel(ref)

    async def collect(self, ref: str, job_id: str, params: dict[str, Any]) -> list[Artifact]:
        """把产物写进 artifacts_dir/<job_id>/，返回产物清单。"""
        out_dir = settings.artifacts_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        artifacts: list[Artifact] = []
        try:
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
                        # 没有 width/height 参数的变体（比如改图）就从文件头读真实尺寸
                        size = image_size(data)
                        artifacts.append(
                            Artifact(
                                job_id=job_id,
                                kind="image",
                                path=str(path),
                                width=params.get("width") or (size[0] if size else None),
                                height=params.get("height") or (size[1] if size else None),
                            )
                        )
        finally:
            # 收尾后清掉进度缓存与 watcher，不管收产物成功与否
            self._drop(ref)
        if not artifacts:
            raise AdapterError("工作流执行完成但没有产出文件")
        return artifacts


def _ratio_progress(data: dict[str, Any]) -> float | None:
    """progress 消息的单节点进度：value/max。"""
    try:
        total_max = float(data.get("max") or 0)
        if total_max <= 0:
            return None
        return float(data.get("value") or 0) / total_max
    except (TypeError, ValueError):
        return None


def _safe_name(name: str) -> str:
    return Path(name).name


def _format_status_error(status: dict[str, Any]) -> str:
    messages = status.get("messages") or []
    for item in messages:
        if isinstance(item, list) and len(item) == 2 and item[0] == "execution_error":
            detail = item[1]
            return f"{detail.get('node_type')}: {detail.get('exception_message')}"
    return "ComfyUI 返回执行错误"
