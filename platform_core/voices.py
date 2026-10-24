"""音色目录：给 TTS 变体提供「下拉可选」的音色清单。

两个来源，按名字去重（不区分大小写，先到先得）：
1. `config.txt` —— 老项目的格式，`名字|wav路径|参考文本`，路径可以指向任意位置
2. 音色目录下的 `*.wav` + 同名 `.txt`（参考文本）

只收录「wav 存在 + 有参考文本」的：零样本合成缺参考文本会跑偏，
这种东西出现在下拉里比不出现更糟。所以 `laurel1.wav` 这种没 .txt 的，
只有在 config.txt 里登记过才会出现。

清单每次调用现读，用户往目录里丢一个 wav+txt，刷新页面就能选到。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .settings import settings

# 仓库自带参考音色的参考文本（CosyVoice 官方 demo 用的那句）
DEFAULT_PROMPT_TEXT = "希望你以后能够做得比我还好呦。"


@dataclass(frozen=True)
class Voice:
    name: str
    wav: Path
    prompt_text: str
    # 来源：config / dir / builtin，排障时用
    source: str


def _from_config(path: Path) -> list[Voice]:
    """解析 config.txt：跳过注释/空行/全局键值行，只认三段式。"""
    if not path.is_file():
        return []
    voices: list[Voice] = []
    # 相对路径按 config.txt 所在目录解析（老项目就是 ./voiceclone/xxx.wav 这么写的）
    base = path.parent
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split("|")[0]:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, wav, text = (p.strip() for p in parts)
        if not name or not wav or not text:
            continue
        wav_path = Path(wav)
        if not wav_path.is_absolute():
            wav_path = base / wav_path
        if wav_path.is_file():
            voices.append(Voice(name=name, wav=wav_path, prompt_text=text, source="config"))
    return voices


def _from_dir(path: Path) -> list[Voice]:
    """扫目录：`<名字>.wav` 必须配 `<名字>.txt`。"""
    if not path.is_dir():
        return []
    voices: list[Voice] = []
    for wav in sorted(path.glob("*.wav")):
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            continue
        text = txt.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        voices.append(Voice(name=wav.stem, wav=wav, prompt_text=text, source="dir"))
    return voices


def _builtin_default() -> list[Voice]:
    wav = settings.cosyvoice_home / "asset" / "zero_shot_prompt.wav"
    if not wav.is_file():
        return []
    return [Voice(name="default", wav=wav, prompt_text=DEFAULT_PROMPT_TEXT, source="builtin")]


def list_voices() -> list[Voice]:
    """所有可用的音色，按名字排序。每次现读，不缓存。"""
    found: dict[str, Voice] = {}
    for v in [*_from_config(settings.cosyvoice_config), *_from_dir(settings.cosyvoice_voices)]:
        found.setdefault(v.name.lower(), v)
    if "default" not in found:
        for v in _builtin_default():
            found.setdefault(v.name.lower(), v)
    return sorted(found.values(), key=lambda v: v.name.lower())


def find(name: str) -> Voice | None:
    """按名字找音色，不区分大小写。"""
    if not name:
        return None
    key = name.lower()
    for v in list_voices():
        if v.name.lower() == key:
            return v
    return None
