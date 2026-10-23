"""引擎适配器契约：控制平面只认这里的接口，不关心引擎是谁。

接新引擎 = 实现一个 Adapter 子类并在 adapters/__init__.py 注册；
worker / API / CLI 都不改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from ..contracts import Artifact
from ..registry import VariantSpec


class EngineState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class EngineStatus:
    state: EngineState
    progress: float = 0.0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)   # 引擎原始状态，排障用


class AdapterError(RuntimeError):
    """适配器边界上的失败：引擎执行错误、取消中止等。

    worker 用它区分「正常流程要停」和「意外异常」，不再 import Comfy 侧的异常类型。
    """


class Adapter(ABC):
    kind: ClassVar[str]

    @abstractmethod
    async def health(self) -> dict: ...

    @abstractmethod
    async def submit(self, spec: VariantSpec, params: dict[str, Any], job_id: str) -> str: ...

    @abstractmethod
    async def status(self, ref: str) -> EngineStatus: ...

    @abstractmethod
    async def cancel(self, ref: str) -> None: ...

    @abstractmethod
    async def collect(self, ref: str, job_id: str, params: dict[str, Any]) -> list[Artifact]: ...
