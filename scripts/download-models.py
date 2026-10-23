"""按模型注册表还原权重：新机器 clone 下来跑一遍就有全部模型。

数据来源是 `models/*.yaml` 的 `source` 字段，所以新模型只要在 YAML 里写清楚从哪下，
脚本不需要改。同一份权重被多个模型引用（比如 MOSS 的文本编码器）只会下一次。

用法：
    uv run python scripts\\download-models.py            # 缺什么下什么
    uv run python scripts\\download-models.py --check     # 只体检，不联网
    uv run python scripts\\download-models.py --dry-run   # 只打印将要执行的命令
    uv run python scripts\\download-models.py --models-dir E:\\models
    uv run python scripts\\download-models.py --endpoint https://huggingface.co
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from platform_core.registry import SourceFile, registry
from platform_core.settings import settings

GB = 1024**3


@dataclass
class Missing:
    repo: str
    spec: SourceFile
    target: Path
    reason: str


def collect(models_dir: Path) -> tuple[list[Missing], list[str], list[str]]:
    """返回 (待下载, 已就绪, 无 source 的模型)。

    去重键是 (repo, 本地路径)：同一文件被 base/sft 共用时只下一次。
    """
    missing: dict[tuple[str, str], Missing] = {}
    ready: dict[str, str] = {}
    no_source: list[str] = []

    for model in registry.models.values():
        if model.source is None:
            # files 为空的模型（权重随工具包分发）不算「无法还原」，跳过
            if model.files:
                no_source.append(model.id)
            continue
        for f in model.source.files:
            target = models_dir / f.local_path
            if target.exists() and target.stat().st_size >= f.min_gb * GB:
                # 同一文件被多个模型共用，只报一次
                ready[str(target)] = f"{target}  ({target.stat().st_size / GB:.2f} GiB)"
                continue
            reason = "缺失" if not target.exists() else "大小异常"
            missing.setdefault(
                (model.source.repo, f.local_path), Missing(model.source.repo, f, target, reason)
            )

    return list(missing.values()), list(ready.values()), no_source


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", default=str(settings.models_dir))
    ap.add_argument("--check", action="store_true", help="只体检，不下载")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令")
    ap.add_argument("--only", default="", help="只处理本地路径包含该子串的文件，例如 --only vae")
    ap.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com")
    args = ap.parse_args()

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    if registry.errors:
        print("[!!] 注册表有错误，先修 models/*.yaml：")
        for e in registry.errors:
            print(f"     {e}")
        return 1

    tasks, ready, no_source = collect(models_dir)
    if args.only:
        tasks = [m for m in tasks if args.only in str(m.target)]

    print(f"模型池: {models_dir}")
    for line in ready:
        print(f"  [OK] {line}")
    for m in tasks:
        print(f"  [--] {m.reason}: {m.target}  <-  {m.repo}/{m.spec.remote}")
    if no_source:
        # 这类模型只能人工搬，脚本不该假装能还原
        print(f"  [!!] 无下载来源，需人工放置: {', '.join(no_source)}")

    if not tasks:
        return 0 if not no_source else 1
    if args.check:
        print(f"缺 {len(tasks)} 个文件（--check 模式不下载）")
        return 1

    # 关键：Comfy-Org 的部分仓库走 HF 的 Xet 存储，hf-mirror 不代理 Xet，
    # 开着 Xet 会「下载成功」但拿到 388 字节的 LFS 指针文件。禁用后退回普通 LFS。
    env = dict(os.environ, HF_ENDPOINT=args.endpoint, HF_HUB_DISABLE_XET="1")
    for m in tasks:
        cmd = [
            "uvx", "--from", "huggingface_hub", "hf", "download",
            m.repo, "--include", m.spec.remote,
            "--local-dir", str(models_dir), "--max-workers", "4",
        ]
        print(f"\n[dl] {m.target.name}")
        print("     " + " ".join(cmd))
        if args.dry_run:
            continue
        rc = subprocess.call(cmd, env=env)
        if rc != 0:
            print(f"[!!] 下载失败 rc={rc}: {m.spec.remote}")
            return rc
        # remote 与 local 不同时（HF 仓库带 split_files/ 前缀），hf download 按远端路径
        # 镜像到 local-dir，需挪到注册表声明的本地位置；同一文件多模型共用时只挪一次
        src = models_dir / m.spec.remote
        dst = models_dir / m.spec.local_path
        if src != dst and src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
            # 顺手清掉搬空后的远端目录层级，别留空壳目录
            for p in src.parents:
                if p != models_dir and p.exists() and not any(p.iterdir()):
                    p.rmdir()
                else:
                    break

    if args.dry_run:
        return 0

    # 下完再体检一次，避免「命令成功但文件不对」
    tasks2, ready2, _ = collect(models_dir)
    print(f"\n[verify] 就绪 {len(ready2)} 个，仍缺 {len(tasks2)} 个")
    return 0 if not tasks2 else 1


if __name__ == "__main__":
    sys.exit(main())
