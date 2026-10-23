"""注册表：能力(capability) → 变体(variant) → 模型(model)，全部以磁盘上的 YAML 为唯一事实来源。

新增一个模型/能力 = 新增一个 YAML（+ 一个 workflow.json），不改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .contracts import VariantInfo
from .settings import settings


class ModelSpec(BaseModel):
    id: str
    name: str = ""
    family: str = ""
    license: str = "unknown"
    commercial_ok: bool = False
    # 12GB 显存下的可用性：comfortable / degraded / infeasible
    vram_profile: str = "unknown"
    # 逻辑名 → 相对 models 根目录的路径
    files: dict[str, str] = Field(default_factory=dict)
    notes: str = ""

    def resolve_path(self) -> Path | None:
        """返回主权重文件的真实路径（优先热池，其次额外挂载的目录）。"""
        main = self.files.get("diffusion_model") or next(iter(self.files.values()), None)
        if not main:
            return None
        for base in [settings.models_dir, *settings.extra_model_dirs]:
            p = base / main
            if p.exists():
                return p
        return settings.models_dir / main


class VariantSpec(BaseModel):
    capability: str
    variant: str
    version: str = "v1"
    title: str = ""
    description: str = ""
    model_id: str
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    # "节点ID.inputs.字段" → 参数名，工作流里只换值，不动结构
    bindings: dict[str, str] = Field(default_factory=dict)
    # 固定注入的值（比如模型文件名），不对外暴露
    constants: dict[str, Any] = Field(default_factory=dict)
    # 加载时回填，不写进 YAML
    base: Path | None = Field(default=None, exclude=True)

    @property
    def dir(self) -> Path:
        if self.base is None:
            raise RuntimeError(f"{self.capability}/{self.variant} 未绑定磁盘目录")
        return self.base

    @property
    def workflow_path(self) -> Path:
        return self.dir / self.workflow

    def info(self) -> VariantInfo:
        return VariantInfo(
            capability=self.capability,
            variant=self.variant,
            version=self.version,
            model_id=self.model_id,
            title=self.title,
            description=self.description,
            inputs=self.inputs,
        )


class Registry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.root
        self.capabilities_dir = self.root / "capabilities"
        self.models_dir = self.root / "models"
        self.models: dict[str, ModelSpec] = {}
        self.variants: dict[str, VariantSpec] = {}
        self.errors: list[str] = []

    def load(self) -> "Registry":
        self.models.clear()
        self.variants.clear()
        self.errors.clear()
        self._load_models()
        self._load_variants()
        self._validate()
        return self

    def _load_models(self) -> None:
        for f in sorted(self.models_dir.glob("*.yaml")):
            try:
                spec = ModelSpec(**yaml.safe_load(f.read_text(encoding="utf-8")))
                self.models[spec.id] = spec
            except Exception as e:  # noqa: BLE001 - 注册表要能容忍坏文件并报出来
                self.errors.append(f"模型文件解析失败 {f.name}: {e}")

    def _load_variants(self) -> None:
        for f in sorted(self.capabilities_dir.glob("*/*/*/manifest.yaml")):
            try:
                spec = VariantSpec(**yaml.safe_load(f.read_text(encoding="utf-8")))
                spec.base = f.parent
                self.variants[self.key(spec.capability, spec.variant)] = spec
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"能力清单解析失败 {f}: {e}")

    def _validate(self) -> None:
        for key, v in self.variants.items():
            if v.model_id not in self.models:
                self.errors.append(f"{key}: 引用了不存在的模型 {v.model_id}")
            if not v.workflow_path.exists():
                self.errors.append(f"{key}: 工作流文件缺失 {v.workflow_path}")

    @staticmethod
    def key(capability: str, variant: str) -> str:
        return f"{capability}/{variant}"

    def get(self, capability: str, variant: str) -> VariantSpec:
        spec = self.variants.get(self.key(capability, variant))
        if spec is None:
            raise KeyError(f"未注册的能力变体: {capability}/{variant}")
        return spec

    def list_variants(self) -> list[VariantSpec]:
        return list(self.variants.values())


registry = Registry().load()
