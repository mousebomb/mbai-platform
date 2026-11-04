"""SenseVoice 转写：把音频变成文本，供 Agent 校验 TTS 输出（是否混入 instruct、是否乱码）。

为什么默认跑 CPU：
ASR 模型小（234M）、句子短，推理时间远小于模型加载时间。CPU 跑**不碰显存**，
ComfyUI 的模型留在卡上，后续出图省一次 10~15 秒的重载。GPU 版只在批量时划算，
由 manifest 里的 free_comfy_vram 显式卸载 ComfyUI 模型，代价写进 guidance。

输出 .jsonl 而不是 .txt：批量时一个解析器搞定，单条也只有一行。
字段：
    index      输入顺序（0 起）
    filename   文件名，便于和上传的素材对应
    text       去掉 <|zh|><|NEUTRAL|><|Speech|> 标签后的干净文本，比对用这个
    raw        模型原始输出，带语言/情感/事件标签，排障用
    duration_s 音频时长（soundfile 读）
    elapsed_s  这条的推理耗时
    rtf        耗时 / 时长
    error      出错时才有，此时 text 为空

控制台只用 [OK]/[!!]（Windows 控制台是 GBK）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# SenseVoice 输出形如 <|zh|><|NEUTRAL|><|Speech|><|woitn|>后面才是正文
_TAG_RE = re.compile(r"<\|[^|]*\|>")


def strip_tags(raw: str) -> str:
    """去掉 SenseVoice 的 <|zh|><|NEUTRAL|><|Speech|><|withitn|> 标签。

    刻意不调 funasr 的 rich_transcription_postprocess：它会把情感/事件标签转成
    emoji 插进正文（"根本停不下来。😡"），这对「和期望文本比对」是噪音。
    需要情感/语言信息时读 raw 字段。
    """
    return _TAG_RE.sub("", raw).strip()


def audio_duration(path: Path) -> float | None:
    """读时长；读不出返回 None，不中断任务。"""
    try:
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:  # noqa: BLE001
        return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SenseVoice 语音转写")
    p.add_argument("--audios", nargs="+", required=True, help="一个或多个音频路径")
    p.add_argument("--out", required=True, help="输出 .jsonl 路径")
    p.add_argument("--model-dir", required=True, help="SenseVoiceSmall 本地目录")
    p.add_argument("--device", default="cpu", help="cpu 或 cuda:0")
    p.add_argument("--language", default="auto", help="auto/zh/en/ja/ko/yue")
    p.add_argument("--no-itn", action="store_true", help="关掉逆文本正则化（数字保持汉字）")
    p.add_argument("--batch-size-s", type=int, default=60, help="按总时长切的批大小（秒）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    model_dir = Path(args.model_dir)
    if not model_dir.is_dir():
        print(f"[!!] 模型目录不存在: {model_dir}")
        print("[!!] 先跑 scripts\\download-models.py 或在 .env 设 PLATFORM_ASR_MODEL")
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 模型加载是一次性成本，也是 CLI 适配器每次任务都要付的那部分；
    # --device cpu 时不初始化 CUDA context，省 2~5 秒
    from funasr import AutoModel

    t0 = time.monotonic()
    model = AutoModel(
        model=str(model_dir),
        # funasr 1.4 已内置 SenseVoice，不需要仓库里的 model.py；
        # 开着 True 会打一条 "Loading remote code failed" 的噪音警告
        trust_remote_code=False,
        device=args.device,
        disable_update=True,
        disable_pbar=True,
    )
    load_s = time.monotonic() - t0
    print(f"[OK] 模型已加载 {load_s:.1f}s ({args.device})")

    rows: list[dict] = []
    for i, raw_path in enumerate(args.audios):
        path = Path(raw_path)
        row: dict = {"index": i, "filename": path.name}
        if not path.is_file():
            row.update(text="", error=f"音频不存在: {path}")
            rows.append(row)
            continue
        t1 = time.monotonic()
        try:
            res = model.generate(
                input=str(path),
                cache={},
                language=args.language,
                use_itn=not args.no_itn,
                batch_size_s=args.batch_size_s,
            )
            raw = (res[0].get("text") if res else "") or ""
            row["text"] = strip_tags(raw)
            row["raw"] = raw
        except Exception as e:  # noqa: BLE001 - 单条失败不拖垮整批
            row.update(text="", error=f"{type(e).__name__}: {e}")
            rows.append(row)
            continue
        elapsed = time.monotonic() - t1
        dur = audio_duration(path)
        row["duration_s"] = round(dur, 3) if dur else None
        row["elapsed_s"] = round(elapsed, 3)
        row["rtf"] = round(elapsed / dur, 4) if dur else None
        rows.append(row)

    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    ok = sum(1 for r in rows if not r.get("error"))
    total = time.monotonic() - t0
    print(f"[OK] {ok}/{len(rows)} 条转写完成，端到端 {total:.1f}s（含加载 {load_s:.1f}s）")
    print(f"[OK] 结果: {out_path}")
    # 全部失败才算任务失败，部分成功也交付（Agent 自己决定怎么处理）
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
