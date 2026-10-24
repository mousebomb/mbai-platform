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
    # 单个文件可以来自别的仓库（比如 LoRA 在另一个 repo），省略则用模型级 repo
    repo: str | None = None

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
    # 资产来源类型：上游官方 / 量化衍生 / 合并魔改 / 纯本地
    kind: Literal["upstream", "quantized", "merged", "local"] = "upstream"
    # 血缘：base / patches / quant 方法等，魔改模型必须写清从哪来
    lineage: dict[str, Any] = Field(default_factory=dict)
    # 逻辑名 → 相对 models 根目录的路径
    files: dict[str, str] = Field(default_factory=dict)
    # 下载来源：scripts/download_models.py 按它还原整套权重
    source: ModelSource | None = None
    # 能否在新机器上还原；None = 由 source 推导（有 source 就能）
    restorable: bool | None = None
    notes: str = ""

    @property
    def can_restore(self) -> bool:
        if self.restorable is not None:
            return self.restorable
        return self.source is not None

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
    # 模型角色 → 工作流字段（如 "1.inputs.unet_name"）。有它就能靠 profile 换权重文件，
    # 不用改 manifest；没写则退回 constants 里写死的文件名（兼容旧变体）
    model_bind: dict[str, str] = Field(default_factory=dict)
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
        # model_id → {角色: 相对路径}，来自 profiles/default.yaml 与 profiles/local.yaml
        # （local 覆盖 default，两者都覆盖 models/*.yaml 的 files）
        self.profiles: dict[str, dict[str, str]] = {}
        self.errors: list[str] = []

    def load(self) -> "Registry":
        self.models.clear()
        self.variants.clear()
        self.catalog.clear()
        self.profiles.clear()
        self.errors.clear()
        self._load_models()
        self._load_variants()
        self._load_catalog()
        self._load_profiles()
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

    def _load_profiles(self) -> None:
        """加载本机绑定覆盖：profiles/default.yaml（入库）→ profiles/local.yaml（不入库）。

        作用只有一个：让「同一套能力定义」在不同机器上用不同的权重文件。
        换量化档、换魔改模型，改这里一行，变体/工作流/前端都不动。
        """
        for name in ("default.yaml", "local.yaml"):
            path = self.root / "profiles" / name
            if not path.exists():
                continue
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"profile 解析失败 {path}: {e}")
                continue
            if not isinstance(raw, dict):
                self.errors.append(f"profile 格式错误 {path}: 顶层必须是 mapping")
                continue
            for model_id, roles in raw.items():
                if not isinstance(roles, dict):
                    self.errors.append(f"profile 格式错误 {path}: {model_id} 必须是 mapping")
                    continue
                self.profiles.setdefault(str(model_id), {}).update(
                    {str(k): str(v) for k, v in roles.items()}
                )

        for model_id in self.profiles:
            if model_id not in self.models:
                self.errors.append(f"profile 引用了不存在的模型 {model_id}")

    def resolve_file(self, model_id: str, role: str) -> Path | None:
        """角色 → 真实文件路径。

        优先级：profiles/local.yaml → profiles/default.yaml → models/*.yaml 的 files。
        返回「优先命中已存在的那个目录」，都不存在则返回热池下的期望路径（调用方用 exists() 判断）。
        """
        model = self.models.get(model_id)
        rel = self.profiles.get(model_id, {}).get(role)
        if not rel and model is not None:
            rel = model.files.get(role)
        if not rel:
            return None
        for base in [settings.models_dir, *settings.extra_model_dirs]:
            candidate = base / rel
            if candidate.exists():
                return candidate
        return settings.models_dir / rel

    def binding_source(self, model_id: str, role: str) -> str:
        """这个角色的文件是从哪来的：local / default / model。doctor 用来标注。"""
        if role in self.profiles.get(model_id, {}):
            # 无法区分 default 与 local（已合并），统一标 profile
            return "profile"
        model = self.models.get(model_id)
        return "model" if model and role in model.files else "-"

    def _validate(self) -> None:
        for key, v in self.variants.items():
            if v.model_id not in self.models:
                self.errors.append(f"{key}: 引用了不存在的模型 {v.model_id}")
            # CLI 引擎没有 workflow.json，只在声明了文件时检查存在
            if v.workflow and not v.workflow_path.exists():
                self.errors.append(f"{key}: 工作流文件缺失 {v.workflow_path}")
            # model_bind 里的角色必须能在模型或 profile 里找到
            for role in v.model_bind:
                if self.resolve_file(v.model_id, role) is None:
                    self.errors.append(f"{key}: model_bind 引用了未知角色 {role}（模型 {v.model_id} 没定义）")

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
