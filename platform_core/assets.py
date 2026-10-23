"""素材（上传的输入图）落盘与解析。

素材只落盘、不进 SQLite：asset_id 就是文件名的一部分，任务参数里存 id，
worker 执行前再解析成绝对路径交给适配器。宽高用标准库从文件头解析
（PNG IHDR / JPEG SOF / WebP VP8X），刻意不引入 Pillow 等图像库。
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from .settings import settings

# 允许上传的图片扩展名
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# 上传大小上限（20 MB）
MAX_ASSET_BYTES = 20 * 1024 * 1024
_ID_RE = re.compile(r"^ast_[0-9a-f]{12}$")

# JPEG 的 SOF 段标记（C0~CF 去掉 DHT/JPG/DAC 三个非 SOF 段）
_SOF = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def detect_format(data: bytes) -> str | None:
    """按魔数识别图片家族：png / jpeg / webp；识别不了返回 None。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def ext_matches(ext: str, fmt: str) -> bool:
    """上传扩展名与文件头探测结果是否一致（jpg/jpeg 是同一家族）。"""
    if fmt == "jpeg":
        return ext in {".jpg", ".jpeg"}
    return ext == f".{fmt}"


def image_size(data: bytes) -> tuple[int, int] | None:
    """从文件头解析宽高；解析不了返回 None（调用方按 null 处理）。"""
    fmt = detect_format(data)
    if fmt == "png":
        return _png_size(data)
    if fmt == "jpeg":
        return _jpeg_size(data)
    if fmt == "webp":
        return _webp_size(data)
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    # IHDR 是签名后的第一个 chunk：偏移 16..24 是宽/高大端 32 位
    if len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """扫描标记段直到 SOF；段内布局：长度(2) 精度(1) 高(2) 宽(2)。"""
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1  # 填充字节
            continue
        if marker in _SOF:
            if i + 9 > n:
                return None
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            return (w, h)
        if marker == 0xD8 or 0xD0 <= marker <= 0xD9:
            i += 2  # SOI / RST / EOI：无长度段
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    """只解析 VP8X（扩展格式）的画布尺寸；VP8/VP8L 简单格式给 None。"""
    if len(data) < 30 or data[12:16] != b"VP8X":
        return None
    # 24 位画布尺寸是「真实值 - 1」，小端，宽高各 3 字节
    w = int.from_bytes(data[24:27], "little") + 1
    h = int.from_bytes(data[27:30], "little") + 1
    return (w, h)


def asset_path(asset_id: str) -> Path | None:
    """asset_id → 磁盘路径；id 格式非法或文件不存在返回 None。"""
    if not _ID_RE.fullmatch(asset_id):
        return None
    for ext in IMAGE_EXTS:
        p = settings.uploads_dir / f"{asset_id}{ext}"
        if p.exists():
            return p
    return None
