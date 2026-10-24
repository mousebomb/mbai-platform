"""媒体元信息：只从文件头读，不引 ffmpeg / Pillow 之类依赖。

目前只需要「时长」——产物墙要显示音频/视频多长，参数里没有（TTS 文本长度推不出秒数，
视频的 total_second_length 也不等于成片时长）。
"""

from __future__ import annotations

from pathlib import Path

# 扩展名 → 产物类型；CLI 适配器靠它给产物打 kind
KIND_BY_SUFFIX = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".wav": "audio",
    ".mp3": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".m4a": "audio",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
}


def media_duration(path: Path) -> float | None:
    """返回时长（秒）；解析不了返回 None，调用方按 null 处理。"""
    suffix = path.suffix.lower()
    if suffix == ".wav":
        return _wav_duration(path)
    if suffix in {".mp4", ".mov", ".m4a"}:
        return _mp4_duration(path)
    return None


def _wav_duration(path: Path) -> float | None:
    """手工走 RIFF 块，不用 wave 模块：torchaudio 写的是 WAVE_FORMAT_EXTENSIBLE，
    Python 3.11 的 wave 只认纯 PCM，会直接抛 unknown format。
    """
    try:
        with path.open("rb") as f:
            if f.read(4) != b"RIFF":
                return None
            f.read(4)  # RIFF 长度，用不上
            if f.read(4) != b"WAVE":
                return None
            byte_rate = 0
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                cid = head[:4]
                csize = int.from_bytes(head[4:8], "little")
                if cid == b"fmt ":
                    body = f.read(csize)
                    if len(body) < 12:
                        return None
                    byte_rate = int.from_bytes(body[8:12], "little")
                elif cid == b"data":
                    if byte_rate <= 0:
                        return None
                    return csize / byte_rate
                else:
                    # 块按 2 字节对齐，奇数长度要补 1 字节
                    f.seek(csize + (csize & 1), 1)
    except OSError:
        return None


def _mp4_duration(path: Path) -> float | None:
    """按 ISO BMFF 盒子结构找 moov/mvhd。

    只读盒子头，不解析完整文件；mvhd 里 timescale 和 duration 就是全片时长。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            pos = 0
            while pos + 8 <= size:
                f.seek(pos)
                head = f.read(8)
                if len(head) < 8:
                    return None
                box_size = int.from_bytes(head[0:4], "big")
                box_type = head[4:8]
                payload = pos + 8
                if box_size == 1:  # 64 位长度：紧跟在类型后面
                    ext = f.read(8)
                    if len(ext) < 8:
                        return None
                    box_size = int.from_bytes(ext, "big")
                    payload = pos + 16
                elif box_size == 0:  # 0 表示一直到文件末尾
                    box_size = size - pos
                if box_size < 8:
                    return None
                if box_type == b"moov":
                    return _mvhd_duration(f, payload, pos + box_size)
                pos += box_size
    except OSError:
        return None
    return None


def _mvhd_duration(f, start: int, end: int) -> float | None:
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return None
        box_size = int.from_bytes(head[0:4], "big")
        box_type = head[4:8]
        if box_size < 8:
            return None
        if box_type == b"mvhd":
            f.seek(pos + 8)
            body = f.read(min(box_size - 8, 32))
            if len(body) < 20:
                return None
            version = body[0]
            if version == 1:
                if len(body) < 32:
                    return None
                timescale = int.from_bytes(body[20:24], "big")
                duration = int.from_bytes(body[24:32], "big")
            else:
                timescale = int.from_bytes(body[12:16], "big")
                duration = int.from_bytes(body[16:20], "big")
            if timescale <= 0:
                return None
            return duration / timescale
        pos += box_size
    return None
