"""rembg 抠图（默认 isnet-anime），输出带 alpha 的 PNG。

为什么单独一条链路而不是继续用 ComfyUI 核心节点：
1. 核心的 LoadBackgroundRemovalModel + RemoveBackground + JoinImageWithAlpha 只改 alpha、
   保留原 RGB，边缘会留下白背景的混合色（straight alpha 的合成数学，不是模型精度问题）；
2. 核心节点没有 decontaminate / 边缘内缩这类后处理；
3. isnet-anime 对「被前景包围的白色空隙」（脸颊两侧、头发下缘）判断明显更准。

能力边界（很重要，写进 manifest 让调用方自己判断）：
  只适合动漫 / 二次元角色立绘。场景、静物、建筑这类图会让模型把主体整块删掉——
  实测草地、花拱门、森林小屋各一张，输出几乎全透明。这类输入请用 image.matting/birefnet。

参数：
  --decontaminate white  假设原背景是纯白，反解前景色 F=(C-(1-a))/a。白底生成图最有效
  --decontaminate auto   用 pymatting 估计前景色，不假设背景颜色，通用但效果弱一些
  --decontaminate none   不处理（保留模型原始输出）
  --edge-shrink N        alpha 内缩 N px，削掉残留硬边环

控制台只用 [OK]/[!!]（Windows 是 GBK）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 反混合只在「确实半透明」的过渡带生效。下限给 0.08：再低的话分母趋零，
# 会把发丝尖端的噪声放大成彩色斑点（实测马尾会泛粉）。
_ALPHA_LO = 0.08
_ALPHA_HI = 0.995


def decompose_white(rgba):
    """白背景反混合：F = (C - (1-a)*255) / a，逐通道 clamp 回 [0,1]。"""
    import numpy as np

    out = rgba.copy()
    a = out[..., 3]
    mask = (a > _ALPHA_LO) & (a < _ALPHA_HI)
    a_safe = np.where(mask, a, 1.0)[..., None]
    fg = (out[..., :3] - (1 - a_safe)) / a_safe
    out[..., :3] = np.where(mask[..., None], np.clip(fg, 0.0, 1.0), out[..., :3])
    return out


def decompose_auto(rgba):
    """pymatting 估计前景色：用 alpha 造 trimap，只替换过渡带的 RGB。"""
    import numpy as np
    from pymatting import estimate_foreground_ml

    a = rgba[..., 3]
    trimap = np.full(a.shape, 0.5)
    trimap[a >= 0.97] = 1.0
    trimap[a <= 0.02] = 0.0
    fg = estimate_foreground_ml(rgba[..., :3], trimap, return_background=False)
    out = rgba.copy()
    edge = (a > 0.02) & (a < 0.97)
    out[..., :3] = np.where(edge[..., None], np.clip(fg, 0.0, 1.0), out[..., :3])
    return out


def shrink_alpha(rgba, px: int):
    """最小值滤波内缩 alpha，削掉外圈 1~2px 的硬边残留。"""
    if px <= 0:
        return rgba
    import numpy as np
    from scipy import ndimage

    out = rgba.copy()
    out[..., 3] = ndimage.grey_erosion(out[..., 3], size=(px * 2 + 1, px * 2 + 1))
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="rembg 抠图")
    p.add_argument("--image", required=True, help="输入图路径")
    p.add_argument("--out", required=True, help="输出 PNG 路径")
    p.add_argument("--model", default="isnet-anime", help="rembg 模型名")
    p.add_argument("--model-dir", default=None, help="模型缓存目录（rembg 的 U2NET_HOME）")
    p.add_argument(
        "--decontaminate",
        default="white",
        choices=["white", "auto", "none"],
        help="去色散方式",
    )
    p.add_argument("--edge-shrink", type=int, default=0, help="alpha 内缩像素数 0~3")
    p.add_argument("--alpha-matting", action="store_true", help="启用 rembg 的 alpha matting")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    src = Path(args.image)
    if not src.is_file():
        print(f"[!!] 输入图不存在: {src}")
        return 2

    if args.model_dir:
        # rembg 从 U2NET_HOME 找权重；必须设在 import rembg 之前才生效
        os.environ["U2NET_HOME"] = str(Path(args.model_dir))

    import io

    import numpy as np
    from PIL import Image
    from rembg import new_session, remove

    try:
        session = new_session(args.model)
    except Exception as e:  # noqa: BLE001 - 模型没下下来要给出可操作的提示
        print(f"[!!] 加载模型 {args.model} 失败: {type(e).__name__}: {e}")
        print("[!!] 先跑 scripts\\download-models.py 或在 .env 配 PLATFORM_MATTING_MODEL_DIR")
        return 3

    raw = src.read_bytes()
    cut = remove(
        raw,
        session=session,
        alpha_matting=args.alpha_matting,
        post_process_mask=False,
    )
    im = Image.open(io.BytesIO(cut)).convert("RGBA")
    arr = np.asarray(im).astype(np.float32) / 255.0

    if args.decontaminate == "white":
        arr = decompose_white(arr)
    elif args.decontaminate == "auto":
        arr = decompose_auto(arr)

    arr = shrink_alpha(arr, max(0, min(3, args.edge_shrink)))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), "RGBA").save(out_path)

    a = arr[..., 3]
    print(f"[OK] {src.name} -> {out_path.name}  {im.size[0]}x{im.size[1]}")
    print(
        f"[OK] 模型={args.model} 去色散={args.decontaminate} 内缩={args.edge_shrink}px "
        f"不透明={float((a > 0.98).mean()):.1%} 半透明={float(((a > 0.02) & (a < 0.98)).mean()):.2%}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
