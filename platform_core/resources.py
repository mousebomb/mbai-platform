"""单卡显存调度：让别的引擎先让位。

这台机器只有一张 12 GB 卡，ComfyUI 空闲时也会把模型留在显存里（实测 8 GB+）。
FramePack 这种要 10 GB 的引擎一开就会 OOM，所以大显存任务启动前先请 ComfyUI 释放：
`POST /free {"unload_models": true, "free_memory": true}`。

只影响缓存，不影响正在跑的任务——worker 本来就是串行的。
"""

from __future__ import annotations

from .comfy_client import ComfyClient
from .settings import settings


async def release_comfy() -> bool:
    """让 ComfyUI 卸载模型并清空缓存；引擎不可达返回 False（不阻塞任务）。"""
    try:
        async with ComfyClient(settings.comfy_url, timeout=30) as client:
            return await client.free()
    except Exception:  # noqa: BLE001 - 释放失败不该让任务失败，最多慢/爆显存
        return False
