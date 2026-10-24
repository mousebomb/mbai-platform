"""垃圾回收：产物、上传素材、CLI 日志、引擎 input 副本的 TTL 清理。

安全第一的三条规则：
1. **用户产物默认不删**（`artifacts_days=0`）。那是作品，不是缓存，要删必须显式传天数。
2. 排队/执行中的任务，产物和素材一律跳过。
3. 被任何任务引用过的上传素材不删——删了任务就再也复现不了。

默认只清「中间产物」：上传素材、CLI 日志、引擎 input 里的副本，7 天。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import TERMINAL_STATES, Job
from .settings import settings
from .store import JobStore

# 上传素材的文件名形如 ast_9b517e283611.png；引擎 input 里的副本同名
_ASSET_RE = re.compile(r"ast_[0-9a-f]{12}")


@dataclass
class GcItem:
    category: str  # orphan / artifacts / uploads / logs / engine_input
    path: Path
    size: int
    reason: str


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _older_than(path: Path, days: int) -> bool:
    cutoff = time.time() - days * 86400
    return path.stat().st_mtime < cutoff


def _referenced_assets(jobs: list[Job]) -> set[str]:
    """所有任务参数里出现过的 asset_id —— 这些素材不能删。"""
    refs: set[str] = set()
    for job in jobs:
        try:
            refs.update(_ASSET_RE.findall(json.dumps(job.params, ensure_ascii=False)))
        except (TypeError, ValueError):
            continue
    return refs


def plan(
    store: JobStore,
    *,
    artifacts_days: int = 0,
    uploads_days: int = 7,
    logs_days: int = 7,
    engine_input_days: int = 7,
    engine_output_days: int = 7,
    include_orphans: bool = True,
) -> list[GcItem]:
    jobs = store.list(limit=100_000)
    by_id = {j.id: j for j in jobs}
    live_assets = _referenced_assets(jobs)
    items: list[GcItem] = []

    # ---- 产物目录 ----
    if settings.artifacts_dir.exists():
        for d in sorted(settings.artifacts_dir.iterdir()):
            if not d.is_dir():
                continue
            job = by_id.get(d.name)
            if job is None:
                if include_orphans:
                    items.append(GcItem("orphan", d, _dir_size(d), "任务记录已不存在"))
                continue
            if job.state not in TERMINAL_STATES:
                continue  # 排队/执行中，绝不碰
            if artifacts_days > 0:
                # 用任务完成时间判断年龄，而不是目录 mtime（目录 mtime 会被后续读写刷新）
                ts = job.finished_at or job.created_at
                if ts < time.time() - artifacts_days * 86400:
                    items.append(
                        GcItem("artifacts", d, _dir_size(d), f"终态任务超过 {artifacts_days} 天")
                    )

    # ---- 上传素材 ----
    if settings.uploads_dir.exists():
        for f in sorted(settings.uploads_dir.iterdir()):
            if not f.is_file():
                continue
            asset_id = f.stem
            if asset_id in live_assets:
                continue
            if uploads_days > 0 and _older_than(f, uploads_days):
                items.append(GcItem("uploads", f, f.stat().st_size, f"未被任务引用且超过 {uploads_days} 天"))

    # ---- CLI 日志 ----
    if settings.logs_dir.exists():
        for f in sorted(settings.logs_dir.glob("*.log")):
            if logs_days > 0 and _older_than(f, logs_days):
                items.append(GcItem("logs", f, f.stat().st_size, f"超过 {logs_days} 天"))

    # ---- 引擎 input 里的素材副本（上传时复制过去的）----
    engine_input = settings.root / "engines" / "comfy" / "input"
    if engine_input_days > 0 and engine_input.exists():
        for f in sorted(engine_input.iterdir()):
            if not f.is_file() or not _ASSET_RE.fullmatch(f.stem):
                continue  # 只清我们自己传的，引擎自带的示例图不动
            if _older_than(f, engine_input_days):
                items.append(
                    GcItem("engine_input", f, f.stat().st_size, f"引擎 input 副本超过 {engine_input_days} 天")
                )

    # ---- 引擎 output 里的副本（平台已把它们收进 data/artifacts）----
    engine_output = settings.root / "engines" / "comfy" / "output"
    if engine_output_days > 0 and engine_output.exists():
        for f in sorted(engine_output.iterdir()):
            if f.is_file() and _older_than(f, engine_output_days):
                items.append(
                    GcItem("engine_output", f, f.stat().st_size, f"引擎 output 副本超过 {engine_output_days} 天")
                )

    return items


def apply(items: list[GcItem]) -> tuple[int, int]:
    """执行删除，返回 (删除数量, 释放字节)。单个失败不影响其它。"""
    done = 0
    freed = 0
    for it in items:
        try:
            if it.path.is_dir():
                shutil.rmtree(it.path)
            else:
                it.path.unlink(missing_ok=True)
            done += 1
            freed += it.size
        except OSError:
            continue
    return done, freed
