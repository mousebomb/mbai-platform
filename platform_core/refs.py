"""素材引用解析：`ast_`（上传素材）或 `art_`（历史产物）。

为什么两种都认：把已有作品再喂给下一个能力（视频超分、拿生成图做图生视频）
不该要求用户先下载再上传——服务器上已经有那份文件，多存一份纯属浪费。

引用只存 id，不存路径：产物目录可能被 `mbai gc` 清理，删掉的引用必须能被
明确拒绝，而不是指到一个已经被别的东西占用的路径。
"""

from __future__ import annotations

from pathlib import Path

from .assets import asset_path
from .media import KIND_BY_SUFFIX
from .store import JobStore

# inputs.type → 产物 kind。mask 用的也是图片素材
KIND_FOR_TYPE = {"image": "image", "mask": "image", "audio": "audio", "video": "video"}


def ref_path(store: JobStore, ref: str, input_type: str) -> Path | None:
    """把参数值解析成磁盘路径；不存在或类型不符返回 None。"""
    want = KIND_FOR_TYPE.get(input_type)
    if want is None:
        return None
    if ref.startswith("ast_"):
        path = asset_path(ref)
        # 素材的 kind 由扩展名决定，和产物走同一张表
        if path is None:
            return None
        return path if _asset_kind(path) == want else None
    if ref.startswith("art_"):
        found = store.find_artifact(ref)
        if found is None:
            return None
        _, artifact = found
        if artifact.kind != want:
            return None
        path = Path(artifact.path)
        return path if path.is_file() else None
    return None


def _asset_kind(path: Path) -> str | None:
    return KIND_BY_SUFFIX.get(path.suffix.lower())
