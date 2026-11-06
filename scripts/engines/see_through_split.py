"""See-through 拆层：单张动漫立绘 → 分层 PSD + 合成预览 + 层包。

为什么走 cli 适配器而不是 ComfyUI：
1. 它是独立仓库 + 独立环境（torch 2.8 cu128 / diffusers 0.37），跟平台的 cu130 冲突；
2. 12 GB 卡上要 --group_offload（实测峰值约 10 GB），而 ComfyUI 空闲就占 8 GB，
   所以 manifest 里声明 free_comfy_vram，开跑前先把 ComfyUI 的模型卸掉；
3. 它的推理不是节点图（SDXL 双管线 13+11 组 + Marigold 深度 + PSD 后处理），
   搬进 ComfyUI 等于重写一遍推理，违背「不自研推理内核」。

产物（全部平铺在 --out 所在目录，因为 cli 适配器只收顶层文件）：
  output.png   合成预览（--out 本身）
  layers.psd   分层 PSD，主交付物
  layers.zip   PSD + 每层 PNG + report.json，给不吃 PSD 的工具用
  report.txt   质检摘要（层数 / 覆盖率 / 重建 PSNR / 可疑项）

画布说明：上游会把输入按中心 pad 成正方形再推理，所以 PSD 画布是方形
（1024×1024），不是原图比例。每层都按 PSD 画布坐标导出，位置关系原样保留；
原始尺寸换算和每层 bbox 写在 report.json 里。

控制台只输出 ASCII 标记（[OK]/[!!]），Windows 控制台是 GBK。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# 图层覆盖率高于这个值就提示「疑似多部件粘连」（与交接包 eval_layers.py 同阈值）
_COVER_WARN = 0.55
# 重建 PSNR 低于这个值提示检查（实测 1024/30 步约 21~22 dB，不是像素级复刻）
_PSNR_WARN = 18.0


def _flatten(layers):
    """递归展开图层组，只要叶子图层。"""
    for layer in layers:
        if layer.is_group():
            yield from _flatten(layer)
        else:
            yield layer


def _compose(leaves, size):
    """按 PSD 坐标把图层叠起来（psd.composite() 会把透明区摊成黑底，不能用）。"""
    from PIL import Image

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    for layer in leaves:
        if layer.width == 0 or layer.height == 0:
            continue
        canvas.alpha_composite(layer.topil().convert("RGBA"), (layer.left, layer.top))
    return canvas


def _psnr_vs_input(comp, src_img_path: str | None) -> float | None:
    """合成图 vs 上游自己写出的 pad 后输入（src_img.png）。

    参考值必须是这张：原始输入是 832×1248 这种非方形比例，PSD 画布被 pad 成方形，
    拿原始图直接 resize 过来比没有意义（交接包的 eval_layers.py 就踩了这个坑，
    对非方形输入算出的 PSNR 恒为 1 dB 级）。

    透明区域不计入：只在合成图 alpha>8 的人物区域上比。
    """
    import numpy as np
    from PIL import Image

    if not src_img_path or not Path(src_img_path).exists():
        return None
    ref = Image.open(src_img_path).convert("RGBA")
    if ref.size != comp.size:
        return None
    white = Image.new("RGBA", comp.size, (255, 255, 255, 255))
    a = np.array(Image.alpha_composite(white, comp).convert("RGB")).astype(np.float32)
    b = np.array(Image.alpha_composite(white, ref).convert("RGB")).astype(np.float32)
    mask = np.array(comp)[..., 3] > 8
    if not mask.any():
        return None
    mse = float(((a[mask] - b[mask]) ** 2).mean())
    return 99.0 if mse == 0 else round(float(10 * np.log10(255.0**2 / mse)), 2)


def _export_layers(leaves, canvas_size, layer_dir: Path) -> list[dict]:
    """每层导出成「整幅画布大小」的 RGBA PNG（位置已烘进去，下游不用再对 bbox）。"""
    from PIL import Image

    total = canvas_size[0] * canvas_size[1]
    entries = []
    for layer in leaves:
        if layer.width == 0 or layer.height == 0:
            continue
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        rgba = layer.topil().convert("RGBA")
        canvas.alpha_composite(rgba, (layer.left, layer.top))
        import numpy as np

        alpha = np.array(canvas)[..., 3]
        coverage = float((alpha > 8).sum()) / total
        # 层名里可能有空格，换成下划线，同时去掉 Windows 非法字符
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in layer.name)
        canvas.save(layer_dir / f"{safe}.png")
        entries.append(
            {
                "name": layer.name,
                "file": f"layers/{safe}.png",
                "coverage": round(coverage, 4),
                "bbox": [int(layer.left), int(layer.top), int(layer.right), int(layer.bottom)],
            }
        )
    return entries


def main() -> int:
    # 日志统一 UTF-8：平台按 utf-8 读 data/logs/*.log，中文否则会变成乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python / 非重定向流，忽略
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="输入立绘（asset 解析后的绝对路径）")
    ap.add_argument("--out", required=True, help="预览图路径（cli 适配器的 {out}）")
    ap.add_argument("--home", required=True, help="See-through 仓库根目录")
    ap.add_argument("--hf-home", default=None, help="HF 缓存根（权重在这里，别放 C 盘）")
    ap.add_argument("--res", type=int, default=1024, help="推理分辨率（12 GB 上限 1280）")
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mode", default="offload", choices=["offload", "full", "nf4"])
    args = ap.parse_args()

    image = Path(args.image).resolve()
    home = Path(args.home).resolve()
    out_path = Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if not image.exists():
        print(f"[!!] input image not found: {image}")
        return 1
    if not (home / "inference" / "scripts").is_dir():
        print(f"[!!] See-through repo not found at {home} (expect {home}\\inference\\scripts)")
        return 1

    # 权重走 HF 镜像；Xet 存储镜像不代理，关掉能避免只拿到 LFS 指针文件
    env = dict(os.environ)
    env["HF_ENDPOINT"] = env.get("HF_ENDPOINT", "https://hf-mirror.com")
    env["HF_HUB_DISABLE_XET"] = "1"
    if args.hf_home:
        env["HF_HOME"] = args.hf_home
    # 减少显存碎片
    env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    env["PYTHONIOENCODING"] = "utf-8"

    stem = image.stem
    work = home / "workspace" / "layerdiff_output"
    psd_path = work / f"{stem}.psd"
    layer_src_dir = work / stem

    script = "inference/scripts/inference_psd.py"
    extra: list[str] = []
    if args.mode == "nf4":
        script = "inference/scripts/inference_psd_quantized.py"
    elif args.mode == "offload":
        extra.append("--group_offload")

    argv = [
        sys.executable,
        script,
        "--srcp",
        str(image),
        "--save_to_psd",
        "--resolution",
        str(args.res),
        "--inference_steps",
        str(args.steps),
        "--seed",
        str(args.seed),
        *extra,
    ]
    print(f"[..] see-through: {image.name}  res={args.res} steps={args.steps} mode={args.mode}")
    started = time.time()
    # 子进程继承 stdout，进度条（xx%）会被 cli 适配器解析成任务进度
    code = subprocess.call(argv, cwd=str(home), env=env)
    elapsed = time.time() - started
    if code != 0:
        print(f"[!!] inference failed, exit {code} after {elapsed / 60:.1f} min")
        return code

    if not psd_path.exists() or psd_path.stat().st_mtime < started - 5:
        print(f"[!!] PSD not produced (stale or missing): {psd_path}")
        return 1
    print(f"[OK] inference {elapsed / 60:.1f} min -> {psd_path.name}")

    from psd_tools import PSDImage

    psd = PSDImage.open(psd_path)
    leaves = [layer for layer in _flatten(psd) if layer.width > 0 and layer.height > 0]
    canvas = (psd.width, psd.height)

    tmp = Path(tempfile.mkdtemp(prefix="seethrough_layers_"))
    layer_dir = tmp / "layers"
    layer_dir.mkdir()
    entries = _export_layers(leaves, canvas, layer_dir)

    comp = _compose(leaves, canvas)
    comp.save(out_path)  # 预览 = --out

    src_img = layer_src_dir / "src_img.png"
    psnr = _psnr_vs_input(comp, str(src_img))

    warnings: list[str] = []
    if len(entries) < 15:
        warnings.append(f"[层数少] 只有 {len(entries)} 层（1024 分辨率正常 15~20 层）")
    for e in entries:
        if e["coverage"] > _COVER_WARN:
            warnings.append(f"[粘连] {e['name']} 覆盖 {e['coverage']:.0%} 画布，疑似多部件合在一层")
    if psnr is not None and psnr < _PSNR_WARN:
        warnings.append(f"[重建差] PSNR {psnr} dB，合成结果与原图差异大")
    verdict = "WARN" if warnings else "PASS"

    report = {
        "canvas": list(canvas),
        "layer_count": len(entries),
        "recon_psnr_db": psnr,
        "recon_psnr_note": "合成图 vs 上游 pad 后输入，只在人物区域（alpha>8）计算；不是像素级复刻",
        "score_note": "非像素级复刻：实测 1024/30 步约 21~22 dB，差异集中在层边界",
        "verdict": verdict,
        "warnings": warnings,
        "params": {"resolution": args.res, "steps": args.steps, "seed": args.seed, "mode": args.mode},
        "elapsed_min": round(elapsed / 60, 1),
        "layers": entries,
    }
    (tmp / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # layers.psd：主交付物，单独一份方便直接下载
    shutil.copyfile(psd_path, out_dir / "layers.psd")

    # layers.zip：PSD + 每层 PNG + report.json
    with zipfile.ZipFile(out_dir / "layers.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(psd_path, arcname="layers.psd")
        zf.write(tmp / "report.json", arcname="report.json")
        for p in sorted(layer_dir.glob("*.png")):
            zf.write(p, arcname=f"layers/{p.name}")

    # report.txt：平台没有「看日志」的入口，质检结论只能靠这个产物传出去
    lines = [
        f"判定: {verdict}   层数: {len(entries)}   画布: {canvas[0]}x{canvas[1]}",
        f"重建 PSNR: {psnr if psnr is not None else 'N/A'} dB   "
        f"耗时: {elapsed / 60:.1f} min   "
        f"参数: res={args.res} steps={args.steps} seed={args.seed} mode={args.mode}",
        "",
        f"{'#':>3}  {'图层名':<16} {'覆盖率':>7}  bbox",
    ]
    for i, e in enumerate(entries):
        lines.append(f"{i:>3}  {e['name'][:16]:<16} {e['coverage']:>7.1%}  {e['bbox']}")
    if warnings:
        lines += ["", "可疑项:"] + [f"  - {w}" for w in warnings]
    lines += [
        "",
        "画布是方形 pad 后的尺寸，不是原图比例；每层已按 PSD 坐标导出为整幅画布大小。",
        "空层（模型判为不存在，如 headwear / tail）没有进 PSD，属正常。",
    ]
    (out_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 清掉引擎侧中间产物，避免 workspace 无限增长（要的都拷到 out_dir 了）
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(layer_src_dir, ignore_errors=True)
    for p in work.glob(f"{stem}*.psd*"):
        p.unlink(missing_ok=True)

    print(f"[OK] {len(entries)} layers -> {out_dir}  ({elapsed / 60:.1f} min, verdict {verdict})")
    if warnings:
        for w in warnings:
            print(f"     {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
