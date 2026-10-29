"""CosyVoice2 逐句分段合成：一次加载模型，按句合成，再按设计好的节奏拼接。

为什么要分段（来自实测数据，不是拍脑袋）：
1. 整段一次生成时，末尾常被 EOS 硬切 —— 66 字那段"你都有我"的尾静音只剩 20 ms，
   等于切在音节中间。短句单独生成，EOS 判定准得多。
2. 整段生成的停顿不可控 —— 实测停顿占比 27%，其中 3 处超过 700 ms，听起来"一句一顿"。
   分段后停顿长度由本脚本指定（句末 380 ms、逗号 200 ms），模型没有机会乱插。

另外，逐句合成还有一个隐性收益：每句都有自己的起调，整段的韵律不会塌成一条直线。

两种模式与 cosyvoice_tts.py 一致：
- 给 --instruct：音色来自参考音频，语气来自指令（不需要 --prompt-text）
- 给 --prompt-text：零样本，音色和语调都复刻参考音频

设计约束（同 cosyvoice_tts.py）：不自研推理内核，只调官方 API；控制台 GBK 友好。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def split_segments(text: str, max_len: int, min_len: int = 6) -> list[str]:
    """按句号切；单句超过 max_len 再按逗号/分号切，并回拼到接近 max_len。

    切完把过短的碎片（< min_len 字）并回上一段：单字句合成出来只有一个音节，
    既没有韵律也给模型更多机会往里面塞东西。
    """
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
    # 开头也可能是碎片（第一段没有前一段可并）
    if len(merged) > 1 and len(merged[0]) < min_len:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    return merged


def tail_silence_ms(speech, sr: int) -> tuple[float, int]:
    """返回（末尾静音毫秒数, 最后一个有声帧的位置样本）。"""
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
    ap = argparse.ArgumentParser(description="CosyVoice2 逐句分段合成")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--model", default="pretrained_models/CosyVoice2-0.5B")
    ap.add_argument("--text", required=True, help="整段文本，脚本自己按句切")
    ap.add_argument("--prompt-wav", required=True, help="参考音频（提供音色）")
    ap.add_argument("--prompt-text", help="参考音频的逐字文本（零样本模式必填）")
    ap.add_argument("--instruct", help="自然语言指令；给了它就不需要 --prompt-text")
    ap.add_argument("--max-len", type=int, default=30, help="每段最多多少字")
    ap.add_argument("--gap-sentence", type=float, default=0.38, help="句号/问号/感叹号后的停顿(s)")
    ap.add_argument("--gap-comma", type=float, default=0.20, help="逗号/分号后的停顿(s)")
    ap.add_argument("--fade", type=float, default=0.02, help="每段末尾淡出(s)，防硬切爆音")
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
    if args.instruct:
        mode = "instruct"
    elif args.prompt_text:
        mode = "zero_shot"
    else:
        print("[!!] 必须给 --instruct 或 --prompt-text 之一", file=sys.stderr)
        return 2

    segs = split_segments(args.text, args.max_len)
    if not segs:
        print("[!!] 文本切完是空的", file=sys.stderr)
        return 2
    print(f"[..] mode={mode}  切出 {len(segs)} 句（≤{args.max_len} 字/句）")

    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "third_party" / "Matcha-TTS"))

    import torch
    import torchaudio
    from cosyvoice.cli.cosyvoice import CosyVoice2
    from cosyvoice.utils.file_utils import load_wav

    model_dir = Path(args.model)
    if not model_dir.is_absolute():
        model_dir = repo / model_dir

    tts = CosyVoice2(str(model_dir), load_jit=False, load_trt=False, fp16=False)
    sr = tts.sample_rate
    prompt_speech = load_wav(str(prompt_wav), 16000)

    # 用第一句先跑一遍 frontend，拿到「提示音部分」，后面每句只替换 text token。
    # 这样模型只加载一次、prompt 只编码一次，30 句的开销约等于 1 句。
    if mode == "instruct":
        first = tts.frontend.frontend_instruct2(segs[0], args.instruct, prompt_speech, sr)
    else:
        first = tts.frontend.frontend_zero_shot(segs[0], args.prompt_text, prompt_speech, sr)
    prompt_only = {k: v for k, v in first.items() if k not in ("text", "text_len")}

    def synth(seg: str):
        """按同一份 prompt 合成一句，返回 (speech, 有效时长秒)。"""
        tok, tok_len = tts.frontend._extract_text_token(seg)
        mi = dict(prompt_only)
        mi["text"] = tok
        mi["text_len"] = tok_len
        chunks = [o["tts_speech"] for o in tts.model.tts(**mi, stream=False, speed=args.speed)]
        if not chunks:
            return None, 0.0
        speech = torch.cat(chunks, dim=1) if len(chunks) > 1 else chunks[0]
        raw_sil, cut = tail_silence_ms(speech, sr)
        keep = min(speech.shape[1], cut + int(0.03 * sr))
        return speech[:, :keep].clone(), keep / sr

    # ---- 第一轮：逐句合成 ----
    takes: list[list] = []
    for seg in segs:
        speech, dur = synth(seg)
        takes.append([seg, speech, dur])

    # ---- 泄漏检测 ----
    # instruct 模式偶发会把指令当正文念出来（实测：22 句里约 1 句），症状是这一句
    # 凭空长出几秒。同段内语速应当大致一致，所以用「字/秒」的中位数当基准，
    # 低于中位数 30% 的句子重合成一次（模型带随机性，重试大概率能拿到正常结果）。
    cps = [len(t[0]) / t[2] if t[2] > 0 else 0.0 for t in takes]
    graded = [c for t, c in zip(takes, cps) if len(t[0]) >= 6 and c > 0]
    if len(graded) >= 3:
        med = float(sorted(graded)[len(graded) // 2])
        for t, c in zip(takes, cps):
            if len(t[0]) < 6 or c <= 0 or c >= med * 0.7:
                continue
            for _ in range(2):
                speech, dur = synth(t[0])
                if speech is None:
                    continue
                new_c = len(t[0]) / dur
                if new_c > c:  # 变快了说明这次没把指令念进去
                    t[1], t[2], c = speech, dur, new_c
                if c >= med * 0.7:
                    break
            print(f"[..] 命速异常已重合成: {t[0][:12]}… {c:.1f} 字/s（基准 {med:.1f}）")

    pieces = []
    gaps = []
    for i, (seg, body, _dur) in enumerate(takes):
        if body is None:
            print(f"[!!] 第 {i + 1} 句没有返回音频: {seg}", file=sys.stderr)
            return 3
        nf = max(1, int(args.fade * sr))
        body[0, -nf:] *= torch.linspace(1.0, 0.0, nf)

        gap = args.gap_sentence if seg.rstrip()[-1:] in "。！？" else args.gap_comma
        if i == len(segs) - 1:
            gap = 0.3  # 末段留一点尾巴，避免播放器一停就断
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
