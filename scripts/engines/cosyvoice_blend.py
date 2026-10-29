"""CosyVoice2 双音色插值：两段参考音频 → 一个新音色 → 合成一段 wav。

来源：一组本地沙盒实验的实测结论（沙盒不入库，本文件只保留生产路径）。
实验代码不随仓库分发，两边不互相依赖。

策略固定为「concat + 等长截断」，理由（都是实测数据，不是推测）：
    只换 flow_embedding 时 F0 跨度仅 4 Hz —— 音色被 prompt token 压制；
    两端 prompt 拼接并等长截断后 F0 跨度 101 Hz，声纹双向单调过渡。

设计约束（遵守 AGENTS.md 铁律）：
    · 不自研推理内核，只调 CosyVoice 官方 API（frontend_zero_shot + model.tts）
    · 不写 torch 训练/采样代码，只做向量插值 + 注入
    · 控制台按 GBK 输出，用 [OK]/[!!]，不用 ✓/✗

已知脆弱点：`frontend_zero_shot()` 的返回结构、`_extract_spk_embedding()`、
`model.tts(**model_input)` 都是 CosyVoice 的内部接口，升级仓库可能改签名。
断了先看这条，不要怀疑平台。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def slerp(a: list, b: list, t: float) -> list:
    """球面线性插值。保持模长，避免线性插值在中间态把向量缩短、声音发虚。"""
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        raise ValueError("零向量无法插值")
    cos_w = max(-1.0, min(1.0, dot / (na * nb)))
    omega = math.acos(cos_w)

    # 夹角极小：退化为线性插值，避免除零
    if omega < 1e-6:
        return [(1.0 - t) * x + t * y for x, y in zip(a, b)]

    s1 = math.sin((1.0 - t) * omega) / math.sin(omega)
    s2 = math.sin(t * omega) / math.sin(omega)
    return [s1 * x + s2 * y for x, y in zip(a, b)]


def main() -> int:
    ap = argparse.ArgumentParser(description="CosyVoice2 双音色插值（concat 策略）")
    ap.add_argument("--repo", required=True, help="CosyVoice 仓库根目录")
    ap.add_argument("--model", default="pretrained_models/CosyVoice2-0.5B", help="模型目录，相对 repo 或绝对路径")
    ap.add_argument("--voice-a", required=True, help="A 的参考音频")
    ap.add_argument("--prompt-a", required=True, help="A 的逐字文本")
    ap.add_argument("--voice-b", required=True, help="B 的参考音频")
    ap.add_argument("--prompt-b", required=True, help="B 的逐字文本")
    ap.add_argument("--ratio", type=float, default=0.4, help="0=A, 1=B；听感中点约 0.38-0.40")
    ap.add_argument("--text", required=True, help="要合成的文本")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--out", required=True, help="输出 wav 路径")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "cosyvoice").is_dir():
        print(f"[!!] {repo} 不像 CosyVoice 仓库（缺 cosyvoice/ 子目录）", file=sys.stderr)
        return 2
    for p in (args.voice_a, args.voice_b):
        if not Path(p).is_file():
            print(f"[!!] 参考音频不存在: {p}", file=sys.stderr)
            return 2
    # 平台的 float 类型不强制 min/max，越界在这里拦（ratio=1.2 会静默出怪声）
    if not 0.0 <= args.ratio <= 1.0:
        print(f"[!!] ratio 必须落在 [0,1]，收到 {args.ratio}", file=sys.stderr)
        return 2

    # 仓库要求把根目录和 Matcha-TTS 都加进 sys.path
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

    wav_a = load_wav(str(args.voice_a), 16000)
    wav_b = load_wav(str(args.voice_b), 16000)

    # 同一条目标文本，分别按 A、B 的参考音构造两份输入
    mi_a = tts.frontend.frontend_zero_shot(args.text, args.prompt_a, wav_a, sr)
    mi_b = tts.frontend.frontend_zero_shot(args.text, args.prompt_b, wav_b, sr)

    emb_a = tts.frontend._extract_spk_embedding(wav_a).flatten().tolist()
    emb_b = tts.frontend._extract_spk_embedding(wav_b).flatten().tolist()
    # 两端直接取原向量，不做插值（t=0 时 slerp 也会有浮点误差）
    emb = emb_a if args.ratio <= 0.0 else emb_b if args.ratio >= 1.0 else slerp(emb_a, emb_b, args.ratio)

    # 等长截断：两端各取同样多的 token / feat，消除「谁说得久谁权重高」的偏置
    n = min(int(mi_a["flow_prompt_speech_token"].shape[1]),
            int(mi_b["flow_prompt_speech_token"].shape[1]))

    def side(mi):
        return {
            "llm_prompt_speech_token": mi["llm_prompt_speech_token"][:, :n],
            "llm_prompt_speech_token_len": torch.tensor([n], dtype=torch.int32),
            "flow_prompt_speech_token": mi["flow_prompt_speech_token"][:, :n],
            "flow_prompt_speech_token_len": torch.tensor([n], dtype=torch.int32),
            "prompt_speech_feat": mi["prompt_speech_feat"][:, : 2 * n],
            "prompt_speech_feat_len": torch.tensor([2 * n], dtype=torch.int32),
            "prompt_text": mi["prompt_text"],
            "prompt_text_len": mi["prompt_text_len"],
        }

    # 音色三条通道：prompt token（A+B 拼接，让模型没有单一音色可抄）、
    # mel feat（同上）、embedding（插值结果，模型此时只能从这儿找答案）
    a, b = side(mi_a), side(mi_b)
    model_input = {
        "text": mi_a["text"], "text_len": mi_a["text_len"],
        "prompt_text": torch.cat([a["prompt_text"], b["prompt_text"]], dim=1),
        "prompt_text_len": a["prompt_text_len"] + b["prompt_text_len"],
        "llm_prompt_speech_token": torch.cat([a["llm_prompt_speech_token"], b["llm_prompt_speech_token"]], dim=1),
        "llm_prompt_speech_token_len": a["llm_prompt_speech_token_len"] + b["llm_prompt_speech_token_len"],
        "flow_prompt_speech_token": torch.cat([a["flow_prompt_speech_token"], b["flow_prompt_speech_token"]], dim=1),
        "flow_prompt_speech_token_len": a["flow_prompt_speech_token_len"] + b["flow_prompt_speech_token_len"],
        "prompt_speech_feat": torch.cat([a["prompt_speech_feat"], b["prompt_speech_feat"]], dim=1),
        "prompt_speech_feat_len": a["prompt_speech_feat_len"] + b["prompt_speech_feat_len"],
    }

    tensor = torch.tensor([emb], dtype=torch.float32)
    model_input["flow_embedding"] = tensor
    model_input["llm_embedding"] = tensor

    chunks = [o["tts_speech"] for o in
              tts.model.tts(**model_input, stream=False, speed=args.speed)]
    if not chunks:
        print("[!!] 模型没有返回任何音频", file=sys.stderr)
        return 3
    speech = torch.cat(chunks, dim=1) if len(chunks) > 1 else chunks[0]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(out), speech, sr)
    print(f"[OK] {out}  {speech.shape[1] / sr:.2f}s  {sr} Hz  ratio={args.ratio:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
