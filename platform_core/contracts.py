"""平台契约：所有能力的输入输出都用这里的类型说话。

新增能力时不需要改这里，只需要新增 capability manifest；
只有出现全新产物类型（视频/音频）时才扩展 Artifact.kind。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class Artifact(BaseModel):
    """产物：真实文件落在 data/artifacts/<job_id>/ 下。"""

    id: str = Field(default_factory=lambda: new_id("art"))
    job_id: str
    kind: Literal["image", "video", "audio"] = "image"
    path: str
    width: int | None = None
    height: int | None = None
    # 音视频时长（秒）；图片为 None。从文件头读，不信参数
    duration: float | None = None
    created_at: float = Field(default_factory=time.time)


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    capability: str
    variant: str
    model_id: str
    params: dict[str, Any] = Field(default_factory=dict)

    state: JobState = JobState.QUEUED
    progress: float = 0.0
    # ComfyUI 侧的 prompt_id，用于取消和排障
    engine_ref: str | None = None
    error: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)

    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


class JobRequest(BaseModel):
    capability: str = "image.txt2img"
    variant: str
    params: dict[str, Any] = Field(default_factory=dict)


class VariantInfo(BaseModel):
    capability: str
    variant: str
    version: str
    model_id: str
    title: str = ""
    description: str = ""
    # 引擎适配器 kind：前端据此判断「提交按钮要不要等 ComfyUI 在线」
    engine: str = "comfy"
    inputs: dict[str, Any] = Field(default_factory=dict)
