"""CosyVoice2 一次性合成脚本：加载模型 → 合成 → 写 wav → 退出。

两种模式，靠 `--instruct` / `--prompt-text` 二选一：
- 零样本（默认）：参考音频 + 它的逐字文本，复刻参考音色
- 情感指令：参考音频 + 自然语言指令（"用兴奋的语气说"），音色来自参考、语气来自指令

instruct 模式的原理（`frontend_instruct2`）：把指令 + `<|endofprompt|>` 放进 prompt_text，
并删掉 llm 侧 prompt token——不让参考音频的语调压住指令。所以它**不需要参考文本**。

为什么用独立脚本而不是常驻服务：
- CosyVoice 自己的环境是 torch 2.3.1+cu121，平台的 uv 环境是 cu130，混用会互相污染
- 12 GB 卡上常驻会一直占着显存，跟 FramePack 抢；TTS 任务短，加载开销可以接受
  （实测：加载约 35 s，合成本身 5 s 左右）

参考音色由平台解析好再传进来（`--prompt-wav` + `--prompt-text`）：
音色清单是平台侧的 `platform_core/voices.py` 管的（config.txt + 目录扫描），
脚本不重复实现一遍，只认路径和文本。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="CosyVoice2 合成 wav（零样本 / 情感指令）")
    ap.add_argument("--repo", required=True, help="CosyVoice 仓库根目录（含 cosyvoice/ 与 pretrained_models/）")
    ap.add_argument("--model", default="pretrained_models/CosyVoice2-0.5B", help="模型目录，相对 repo 或绝对路径")
    ap.add_argument("--text", required=True, help="要合成的文本")
    ap.add_argument("--prompt-wav", required=True, help="参考音频（提供音色）")
    ap.add_argument("--prompt-text", help="参考音频的逐字文本（零样本模式必填）")
    ap.add_argument("--instruct", help="自然语言指令，如\"用兴奋、语速稍快的语气说\"（给了它就不需要 --prompt-text）")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--out", required=True, help="输出 wav 路径")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "cosyvoice").is_dir():
        print(f"[!!] {repo} 不像 CosyVoice 仓库（缺 cosyvoice/ 子目录）", file=sys.stderr)
        return 2

    # 两条推理路径二选一：instruct 模式下 prompt 位置放的是指令，不是参考文本
    if args.instruct:
        mode = "instruct"
    elif args.prompt_text:
        mode = "zero_shot"
    else:
        print("[!!] 必须给 --instruct 或 --prompt-text 之一", file=sys.stderr)
        return 2

    prompt_wav = Path(args.prompt_wav)
    if not prompt_wav.is_file():
        print(f"[!!] 参考音频不存在: {prompt_wav}", file=sys.stderr)
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
    prompt_speech = load_wav(str(prompt_wav), 16000)
    if mode == "instruct":
        # infer_instruct2 内部会把 instruct + <|endofprompt|> 放进 prompt_text，
        # 并删掉 llm 侧 prompt token —— 目的是不让参考音频的语调压住指令
        gen = tts.inference_instruct2(
            args.text, args.instruct, prompt_speech, stream=False, speed=args.speed
        )
    else:
        gen = tts.inference_zero_shot(
            args.text, args.prompt_text, prompt_speech, stream=False, speed=args.speed
        )

    # 长文本前端会自动切句，模型按句返回，这里拼回一条
    chunks = [chunk["tts_speech"] for chunk in gen]
    if not chunks:
        print("[!!] 模型没有返回任何音频", file=sys.stderr)
        return 3
    speech = torch.cat(chunks, dim=1) if len(chunks) > 1 else chunks[0]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(out), speech, tts.sample_rate)
    print(
        f"[OK] {out}  {speech.shape[1] / tts.sample_rate:.2f}s  {tts.sample_rate} Hz  mode={mode}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
