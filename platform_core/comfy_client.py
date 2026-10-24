"""ComfyUI 适配器：平台只通过 HTTP 与引擎对话，不 import ComfyUI 的代码。

对应方案里的四接口：run / status / cancel / health。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx


class ComfyError(RuntimeError):
    pass


_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _guess_mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex

    async def __aenter__(self) -> "ComfyClient":
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def health(self) -> dict[str, Any]:
        r = await self._http.get("/system_stats")
        r.raise_for_status()
        return r.json()

    async def object_info(self, node: str | None = None) -> dict[str, Any]:
        path = f"/object_info/{node}" if node else "/object_info"
        r = await self._http.get(path)
        r.raise_for_status()
        return r.json()

    async def submit(self, workflow: dict[str, Any]) -> str:
        """提交 API 格式工作流，返回 prompt_id。"""
        payload = {"prompt": workflow, "client_id": self.client_id}
        r = await self._http.post("/prompt", json=payload)
        if r.status_code != 200:
            raise ComfyError(f"提交失败 {r.status_code}: {r.text[:800]}")
        data = r.json()
        if data.get("node_errors"):
            raise ComfyError(f"工作流节点校验失败: {data['node_errors']}")
        return data["prompt_id"]

    async def history(self, prompt_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/history/{prompt_id}")
        r.raise_for_status()
        return r.json().get(prompt_id, {})

    async def wait(self, prompt_id: str, interval: float = 2.0, timeout: float = 3600.0) -> dict[str, Any]:
        """轮询直到任务结束，返回该 prompt 的 history 记录。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            h = await self.history(prompt_id)
            status = h.get("status") or {}
            if status.get("completed") or status.get("status_str") in {"success", "error"}:
                return h
            if asyncio.get_event_loop().time() > deadline:
                raise ComfyError(f"任务超时: {prompt_id}")
            await asyncio.sleep(interval)

    async def cancel(self, prompt_id: str) -> None:
        """取消排队中的任务；已在执行则中断当前采样。

        注意：/interrupt 会中断引擎当前正在跑的那个任务。平台 worker 是单并发，
        所以这里中断的一定是我们自己的任务；将来并发 >1 时要先查 /queue 再决定。
        """
        await self._http.post("/queue", json={"delete": [prompt_id]})
        await self._http.post("/interrupt", json={})

    async def upload_image(self, path: Path) -> str:
        """把本机图片传到引擎的 input 目录，返回引擎认的文件名。

        ComfyUI 的 LoadImage 只认自己 input/ 目录下的文件名，不认绝对路径，
        所以带输入图的工作流必须先把文件传上去。
        """
        data = {"type": "input", "overwrite": "true"}
        files = {"image": (path.name, path.read_bytes(), _guess_mime(path))}
        r = await self._http.post("/upload/image", files=files, data=data)
        if r.status_code != 200:
            raise ComfyError(f"上传输入图失败 {r.status_code}: {r.text[:400]}")
        payload = r.json()
        name = payload.get("name") or path.name
        subfolder = payload.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    async def download(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        r = await self._http.get(
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type},
        )
        r.raise_for_status()
        return r.content
