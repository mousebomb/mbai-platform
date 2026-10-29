"""Qwen3-TTS VoiceDesign：用一句自然语言描述造一个新音色。

这不是 TTS 变体，是「音色工厂」：产出的是**声音卡**（wav + 同名的 .txt 逐字文本），
把卡丢进音色目录，CosyVoice3 就能拿它念书（平台里所有 TTS 变体的 voice 参数都吃这个格式）。

为什么卡片要单独再合成一段：
VoiceDesign 的产物是一段语音，不是 embedding。要让它能被别的引擎复用一个稳定音色，
就得把「一段中性的、内容已知的语音」固化成参考音频 + 准确逐字文本。
文本是我们自己给的，所以不存在转写误差。

用法：
    python qwen3_voice_design.py --models D:\\engines\\qwen3tts\\models \
        --model Qwen3-TTS-12Hz-1.7B-VoiceDesign \
        --instruct "19岁女生，清脆甜美，带笑意" --out out.wav
产物：out.wav + out.txt（.txt 就是喂给模型的原文，供 TTS 零样本对齐用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 声音卡标准文本：含常见韵母和四声，长度约 10 秒（CosyVoice 的 prompt 甜区 8~12 s）
CARD_TEXT = ("花看半开，酒饮微醺。总有人会带着那颗热忱的心，穿越人海拥抱你。"
             "从此山河过往，凛冬天明，你都有我。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3-TTS VoiceDesign 造声音卡")
    ap.add_argument("--models", required=True, help="模型根目录")
    ap.add_argument("--model", default="Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    ap.add_argument("--instruct", required=True, help="音色描述（自然语言）")
    ap.add_argument("--text", default=CARD_TEXT, help="用来固化音色的文本")
    ap.add_argument("--language", default="Chinese")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    model_dir = Path(args.models) / args.model
    if not model_dir.is_dir():
        print(f"[!!] 模型目录不存在: {model_dir}", file=sys.stderr)
        return 2

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained(
        str(model_dir), device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
    wavs, sr = model.generate_voice_design(
        text=args.text, language=args.language, instruct=args.instruct)
    if not wavs:
        print("[!!] 模型没有返回音频", file=sys.stderr)
        return 3

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), wavs[0], sr)
    # 逐字文本写在 wav 旁边：声音卡 = <名>.wav + <名>.txt，平台音色目录认这个命名
    out.with_suffix(".txt").write_text(args.text, encoding="utf-8")
    print(f"[OK] {out}  {len(wavs[0]) / sr:.2f}s  {sr} Hz")
    print(f"[OK] 声音卡文本 -> {out.with_suffix('.txt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
