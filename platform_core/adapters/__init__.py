"""引擎适配器层：控制平面通过 get_adapter 拿引擎单例，不认识任何具体引擎。

接新引擎 = 实现 Adapter 子类 + 在 _REGISTRY 注册一行，
worker / API / CLI 的核心路径都不动。
"""

from __future__ import annotations

from .base import Adapter
from .cli import CliAdapter
from .comfy import ComfyAdapter

# kind → 适配器类。控制平面只从注册表拿 kind 字符串，永远不 import 具体引擎
_REGISTRY: dict[str, type[Adapter]] = {
    "comfy": ComfyAdapter,
    "cli": CliAdapter,
}

# 按 kind 缓存单例：适配器无状态（只有 url/timeout），进程内复用即可
_instances: dict[str, Adapter] = {}


def get_adapter(kind: str) -> Adapter:
    if kind not in _REGISTRY:
        raise KeyError(f"未注册的引擎: {kind}")
    if kind not in _instances:
        _instances[kind] = _REGISTRY[kind]()
    return _instances[kind]
