"""视频超分：ffmpeg 抽帧 → realesrgan-ncnn-vulkan 批量放大 → 重新编码 + 复制原音轨。

这是**逐帧 2D 放大**，不是二采：像素由超分模型重绘，不重新采样，也不参考前后帧。
所以快镜头/大幅运动会有轻微闪烁（每帧独立放大），慢镜头和静态构图没问题。

进度按「批」打印百分比：CliAdapter 从日志尾部抓最后一个 xx.xx%，所以这里每个
阶段都要吐百分比，否则前端一直显示 0。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 每次调用 realesrgan 处理的帧数：太少会反复加载模型，太多就没有进度
BATCH = 12


def pct(value: float) -> None:
    print(f"{max(0.0, min(100.0, value)):.2f}%", flush=True)


def run(argv: list[str]) -> None:
    """子进程出错时把命令原样带出去，别只报一个 returncode。"""
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        tail = proc.stdout.decode("utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"命令失败 rc={proc.returncode}: {' '.join(argv)}\n{tail}")


def probe_fps(ffprobe: str, src: Path) -> float:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    try:
        num, _, den = out.stdout.strip().splitlines()[0].partition("/")
        fps = float(num) / float(den or 1)
    except (IndexError, ValueError, ZeroDivisionError):
        return 24.0
    return fps if fps > 0 else 24.0


def has_audio(ffprobe: str, src: Path) -> bool:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="源视频路径")
    ap.add_argument("--output", required=True, help="目标 mp4 路径")
    ap.add_argument("--scale", type=int, default=2, choices=[2, 3, 4])
    ap.add_argument("--model", default="realesr-animevideov3")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--tool", default="", help="realesrgan-ncnn-vulkan.exe；留空从 tools_dir 推")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    out = Path(args.output).resolve()
    if not src.is_file():
        print(f"[!!] 源视频不存在: {src}")
        return 2

    ffmpeg = shutil.which(args.ffmpeg) or args.ffmpeg
    ffprobe = shutil.which(args.ffprobe) or args.ffprobe
    tool = Path(args.tool) if args.tool else Path(__file__).resolve().parents[2] / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
    if not tool.is_file():
        print(f"[!!] 找不到 realesrgan: {tool}（先跑 scripts/download-tools.ps1）")
        return 2

    fps = probe_fps(ffprobe, src)
    audio = has_audio(ffprobe, src)
    print(f"[--] fps={fps:.3f} audio={audio} scale={args.scale} model={args.model}", flush=True)

    with tempfile.TemporaryDirectory(prefix="mbai_vupscale_") as tmp:
        tmp_dir = Path(tmp)
        frames = tmp_dir / "frames"
        frames.mkdir()
        upscaled = tmp_dir / "up"
        upscaled.mkdir()

        # 1) 抽帧。不重采样时间轴（passthrough），保证帧数/顺序和源一致
        pct(1)
        run([ffmpeg, "-y", "-v", "error", "-i", str(src), "-fps_mode", "passthrough",
             "-qscale:v", "1", str(frames / "%08d.png")])
        names = sorted(p.name for p in frames.glob("*.png"))
        if not names:
            print("[!!] 抽帧结果为空，源可能不是视频")
            return 3
        total = len(names)
        print(f"[--] frames={total}", flush=True)

        # 2) 分批放大。批内用硬链接避免复制大文件（同卷），失败再退回复制
        done = 0
        for start in range(0, total, BATCH):
            batch = names[start:start + BATCH]
            batch_in = tmp_dir / f"b_in_{start:05d}"
            batch_out = tmp_dir / f"b_out_{start:05d}"
            batch_in.mkdir()
            batch_out.mkdir()
            for name in batch:
                link = batch_in / name
                try:
                    os.link(frames / name, link)
                except OSError:
                    shutil.copy2(frames / name, link)
            run([str(tool), "-i", str(batch_in), "-o", str(batch_out),
                 "-n", args.model, "-s", str(args.scale), "-f", "png"])
            for name in batch:
                produced = batch_out / name
                if not produced.is_file():
                    print(f"[!!] 放大失败，缺帧: {name}")
                    return 4
                produced.replace(upscaled / name)
            shutil.rmtree(batch_in, ignore_errors=True)
            shutil.rmtree(batch_out, ignore_errors=True)
            done += len(batch)
            # 放大占整体进度的 5%~90%
            pct(5 + 85 * done / total)

        # 3) 重编码：视频重新编码，音频直接复制（超分不动声音）
        pct(92)
        cmd = [ffmpeg, "-y", "-v", "error", "-framerate", f"{fps:.6f}",
               "-i", str(upscaled / "%08d.png"), "-i", str(src),
               "-map", "0:v:0", "-map", "1:a?"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
                "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)]
        run(cmd)
        pct(100)

    if not out.is_file():
        print("[!!] 没有产出文件")
        return 5
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
