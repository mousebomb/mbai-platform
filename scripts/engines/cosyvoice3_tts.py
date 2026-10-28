"""CosyVoice 3.0 逐句分段合成（与 cosyvoice_tts_segmented.py 对齐的能力，换 3.0 引擎）。

为什么单独一个脚本而不是改旧的：
- 3.0 的入口是 CosyVoice3 + inference_instruct2，指令要包成
  `You are a helpful assistant. …<|endofprompt|>`；
- 2.0 的脚本还要继续给 blend 和存量变体用，两条线互不影响。

和 2.0 版一样的三个设计：
1. 按句切（≤max_len 字），逐句合成，裁掉模型自带尾静音，补固定停顿；
2. 语速异常重合成：frontend_instruct2 仍然会删 llm prompt token，
   instruct 被念进正文的毛病没消失，靠「字/秒 低于中位数 72%」兜底；
3. 控制台 GBK 友好，只用 [OK]/[!!]。

注音：3.0 支持在文本里用 `[声母][带调韵母]` 覆盖读音，写法是「替换」不是「追加」：
    酒饮微[x][ūn]     -> wēi xūn（对）
    酒饮微醺[x][ūn]   -> 微熏熏（错，原字和注音各念一遍）
文本原样透传，脚本不做改写。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

INSTRUCT_PREFIX = "You are a helpful assistant. "


def split_segments(text: str, max_len: int, min_len: int = 6) -> list[str]:
    """按句号切；超长按逗号再切；<min_len 的碎片并回上一句。"""
    import re

    rough = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    out: list[str] = []
    for s in rough:
        if len(s) <= max_len:
            out.append(s)
            continue
        buf = ""
        for sub in re.split(r"(?<=[，；：])", s):
            if len(buf) + len(sub) <= max_len:
                buf += sub
            else:
                if buf:
                    out.append(buf)
                buf = sub
        if buf:
            out.append(buf)

    merged: list[str] = []
    for s in out:
        if merged and len(s) < min_len:
            merged[-1] += s
        else:
            merged.append(s)
    if len(merged) > 1 and len(merged[0]) < min_len:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def tail_silence_ms(speech, sr: int) -> tuple[float, int]:
    """返回（末尾静音毫秒数, 最后一个有声帧的样本位置）。"""
    import numpy as np

    x = speech.squeeze().cpu().numpy()
    hop = max(1, int(0.01 * sr))
    n_fr = len(x) // hop
    if n_fr == 0:
        return 0.0, 0
    rms = np.array([np.sqrt(np.mean(x[i * hop:(i + 1) * hop] ** 2)) for i in range(n_fr)])
    thr = max(rms.max() * 0.03, 1e-6)
    voiced = np.where(rms > thr)[0]
    if not len(voiced):
        return len(x) / sr * 1000, 0
    last = int(voiced[-1])
    return (len(x) - last * hop) / sr * 1000, last * hop


def main() -> int:
    ap = argparse.ArgumentParser(description="CosyVoice 3.0 逐句分段合成")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--model", default="pretrained_models/Fun-CosyVoice3-0.5B")
    ap.add_argument("--text", required=True)
    ap.add_argument("--prompt-wav", required=True)
    ap.add_argument("--instruct", required=True)
    ap.add_argument("--max-len", type=int, default=30)
    ap.add_argument("--gap-sentence", type=float, default=0.38)
    ap.add_argument("--gap-comma", type=float, default=0.20)
    ap.add_argument("--fade", type=float, default=0.02)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "cosyvoice").is_dir():
        print(f"[!!] {repo} 不像 CosyVoice 仓库（缺 cosyvoice/ 子目录）", file=sys.stderr)
        return 2
    prompt_wav = Path(args.prompt_wav)
    if not prompt_wav.is_file():
        print(f"[!!] 参考音频不存在: {prompt_wav}", file=sys.stderr)
        return 2

    segs = split_segments(args.text, args.max_len)
    if not segs:
        print("[!!] 文本切完是空的", file=sys.stderr)
        return 2
    print(f"[..] CosyVoice3 切出 {len(segs)} 句（≤{args.max_len} 字/句）")

    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "third_party" / "Matcha-TTS"))

    import torch
    import torchaudio
    from cosyvoice.cli.cosyvoice import CosyVoice3

    model_dir = Path(args.model)
    if not model_dir.is_absolute():
        model_dir = repo / model_dir

    tts = CosyVoice3(str(model_dir), fp16=False)
    sr = tts.sample_rate
    instruct = INSTRUCT_PREFIX + args.instruct + "<|endofprompt|>"

    def synth(text: str):
        chunks = [o["tts_speech"] for o in tts.inference_instruct2(
            text, instruct, str(prompt_wav), stream=False, speed=args.speed)]
        if not chunks:
            return None, 0.0
        speech = torch.cat(chunks, dim=1) if len(chunks) > 1 else chunks[0]
        _sil, cut = tail_silence_ms(speech, sr)
        keep = min(speech.shape[1], cut + int(0.03 * sr))
        return speech[:, :keep].clone(), keep / sr

    takes = []
    for s in segs:
        speech, dur = synth(s)
        takes.append([s, speech, dur])

    # 泄漏兜底：同段内语速应当一致，低于中位数 72% 的重合成
    cps = [len(t[0]) / t[2] if t[2] > 0 else 0.0 for t in takes]
    graded = [c for t, c in zip(takes, cps) if len(t[0]) >= 6 and c > 0]
    if len(graded) >= 3:
        med = float(sorted(graded)[len(graded) // 2])
        for t, c in zip(takes, cps):
            if len(t[0]) < 6 or c <= 0 or c >= med * 0.72:
                continue
            for _ in range(2):
                speech, dur = synth(t[0])
                if speech is None:
                    continue
                new_c = len(t[0]) / dur
                if new_c > c:
                    t[1], t[2], c = speech, dur, new_c
                if c >= med * 0.72:
                    break
            print(f"[..] 语速异常已重合成: {t[0][:12]}… {c:.1f} 字/s（基准 {med:.1f}）")

    pieces, gaps = [], []
    for i, (s, body, _d) in enumerate(takes):
        if body is None:
            print(f"[!!] 第 {i + 1} 句没有返回音频: {s}", file=sys.stderr)
            return 3
        nf = max(1, int(args.fade * sr))
        body[0, -nf:] *= torch.linspace(1.0, 0.0, nf)
        gap = args.gap_sentence if s.rstrip()[-1:] in "。！？" else args.gap_comma
        if i == len(takes) - 1:
            gap = 0.3
        pieces.append(torch.cat([body, torch.zeros(1, int(gap * sr))], dim=1))
        gaps.append(gap)

    full = torch.cat(pieces, dim=1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(out), full, sr)
    total = full.shape[1] / sr
    print(f"[OK] {out}  {total:.2f}s  {sr} Hz  {len(segs)} 句  "
          f"设计停顿合计 {sum(gaps):.2f}s（{sum(gaps) / total * 100:.1f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
