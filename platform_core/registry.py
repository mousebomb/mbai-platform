"""注册表：能力(capability) → 变体(variant) → 模型(model)，全部以磁盘上的 YAML 为唯一事实来源。

新增一个模型/能力 = 新增一个 YAML（+ 一个 workflow.json），不改代码。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .contracts import VariantInfo
from .settings import settings


class SourceFile(BaseModel):
    """远端文件 → 本地相对路径。local 省略时与 remote 相同。"""

    remote: str
    local: str | None = None
    # 完整性下限，单位 GiB；只用来抓「下到一半断掉」，不是精确值
    min_gb: float = 0.0

    @property
    def local_path(self) -> str:
        return self.local or self.remote


class ModelSource(BaseModel):
    """权重从哪来。没有 source 的模型视为「本机已有、不可自动还原」。"""

    type: Literal["huggingface"] = "huggingface"
    repo: str
    files: list[SourceFile] = Field(default_factory=list)


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
    # 下载来源：scripts/download_models.py 按它还原整套权重
    source: ModelSource | None = None
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


class CapabilitySpec(BaseModel):
    """catalog.yaml 里声明的一项能力。

    目标全集：没接的能力也写在这里，首页矩阵才能诚实地显示「缺什么」。
    """

    id: str
    title: str = ""
    # 计划阶段 M1~M4，对应 docs/platform-plan-v2.md §6 的分阶段
    stage: str = "M3"
    note: str = ""


# 等级排序权重：数值越大越成熟，matrix 按它降序
MATURITY_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
MATURITY_LEVELS = ("L0", "L1", "L2", "L3")


class VariantSpec(BaseModel):
    capability: str
    variant: str
    version: str = "v1"
    title: str = ""
    description: str = ""
    model_id: str
    # ComfyUI 工作流文件名；CLI 引擎没有工作流，允许为空
    workflow: str | None = None
    # 引擎适配器 kind：接第二个引擎时新变体写别的值，控制平面不动
    engine: str = "comfy"
    # 成熟度等级（能力矩阵的核心字段，非法值由 Literal 拒绝并落进 registry.errors）：
    #   L0 declared   记了清单，没接
    #   L1 wired      能跑，参数裸露，无预设
    #   L2 tuned      有预设/默认参数/失败处理
    #   L3 validated  golden test + 质量对比 + 文档
    maturity: Literal["L0", "L1", "L2", "L3"] = "L1"
    # CLI 引擎的启动配置（command 占位符列表 / timeout_sec / output_ext）
    cli: dict[str, Any] | None = None
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
    def workflow_path(self) -> Path | None:
        if not self.workflow:
            return None
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
        self.catalog: dict[str, CapabilitySpec] = {}
        self.errors: list[str] = []

    def load(self) -> "Registry":
        self.models.clear()
        self.variants.clear()
        self.catalog.clear()
        self.errors.clear()
        self._load_models()
        self._load_variants()
        self._load_catalog()
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

    def _load_catalog(self) -> None:
        """能力目标全集：只声明「存在什么能力」，不负责接线，等级由变体推导。"""
        path = self.capabilities_dir / "catalog.yaml"
        if not path.exists():
            return
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for item in raw.get("capabilities") or []:
                spec = CapabilitySpec(**item)
                self.catalog[spec.id] = spec
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"能力清单解析失败 {path}: {e}")

    def _validate(self) -> None:
        for key, v in self.variants.items():
            if v.model_id not in self.models:
                self.errors.append(f"{key}: 引用了不存在的模型 {v.model_id}")
            # CLI 引擎没有 workflow.json，只在声明了文件时检查存在
            if v.workflow and not v.workflow_path.exists():
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

    def matrix(self) -> list[dict[str, Any]]:
        """首页能力矩阵：catalog 目标全集 + 注册表里有变体但 catalog 没写的兜底。

        某能力的等级 = 它所有变体 maturity 的最大值；没有变体的 = L0。
        排序：等级降序，同等级按 capability 名升序。
        """
        rows: dict[str, dict[str, Any]] = {
            cid: {
                "capability": cid,
                "title": spec.title or cid,
                "stage": spec.stage,
                "note": spec.note,
                "variants": [],
            }
            for cid, spec in self.catalog.items()
        }
        # 有变体但 catalog 没声明的能力也要出现在矩阵里，别丢数据
        by_cap: dict[str, list[VariantSpec]] = {}
        for v in self.variants.values():
            by_cap.setdefault(v.capability, []).append(v)
        for cid in by_cap:
            if cid not in rows:
                rows[cid] = {
                    "capability": cid,
                    "title": cid,
                    "stage": "",
                    "note": "",
                    "variants": [],
                }

        for cid, row in rows.items():
            vs = sorted(by_cap.get(cid, []), key=lambda v: v.variant)
            row["variants"] = [
                {
                    "variant": v.variant,
                    "maturity": v.maturity,
                    "engine": v.engine,
                    "model_id": v.model_id,
                }
                for v in vs
            ]
            best = max((MATURITY_RANK[v.maturity] for v in vs), default=0)
            row["level"] = MATURITY_LEVELS[best]

        return sorted(rows.values(), key=lambda r: (-MATURITY_RANK[r["level"]], r["capability"]))


registry = Registry().load()
