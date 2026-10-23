"""CLI：doctor 预检、run 跑一张图、serve 起服务。"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .comfy_client import ComfyClient
from .contracts import Job, JobState
from .registry import registry
from .service import store, worker
from .settings import settings

OK = "[OK]"
ERR = "[!!]"


async def _doctor() -> int:
    ok = True
    print(f"平台根目录 : {settings.root}")
    print(f"模型池     : {settings.models_dir}")
    print(f"产物目录   : {settings.artifacts_dir}")
    print(f"引擎地址   : {settings.comfy_url}")
    print()

    print("[注册表]")
    if registry.errors:
        ok = False
        for e in registry.errors:
            print(f"  {ERR} {e}")
    else:
        print(f"  {OK} {len(registry.variants)} 个能力变体，{len(registry.models)} 个模型")
    print()

    print("[模型文件]")
    for model in registry.models.values():
        for role, rel in model.files.items():
            path = next(
                (
                    base / rel
                    for base in [settings.models_dir, *settings.extra_model_dirs]
                    if (base / rel).exists()
                ),
                None,
            )
            if path:
                print(f"  {OK} {model.id}/{role}: {path}  ({path.stat().st_size / 1e9:.2f} GB)")
            else:
                ok = False
                print(f"  {ERR} {model.id}/{role}: 缺失 {rel}")
    print()

    print("[引擎]")
    try:
        async with ComfyClient(settings.comfy_url, timeout=5) as comfy:
            stats = await comfy.health()
            for d in stats.get("devices") or []:
                print(f"  {OK} {d.get('name')}  VRAM {d.get('vram_total', 0) / 1e9:.1f} GB")
            info = await comfy.object_info()
            moss = [k for k in info if "MOSS" in k or "moss" in k.lower()]
            print(f"  {OK} 节点总数 {len(info)}，MOSS 相关节点: {moss or '无'}")
            if not moss:
                ok = False
                print(f"  {ERR} 当前 ComfyUI 不含 MOSS-Image 原生节点（需 pinned commit d07dff45）")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  {ERR} 引擎不可达: {type(e).__name__}: {e}")
        print("    启动命令: powershell -ExecutionPolicy Bypass -File scripts\\start-engine.ps1")

    return 0 if ok else 1


async def _run(args: argparse.Namespace) -> int:
    spec = registry.get(args.capability, args.variant)
    raw = {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
        "cfg": args.cfg,
        "seed": args.seed,
        "sampler_name": args.sampler,
    }
    params = {k: v for k, v in raw.items() if v is not None}

    job = store.add(
        Job(capability=args.capability, variant=args.variant, model_id=spec.model_id, params=params)
    )
    print(f"任务 {job.id} 入队，开始执行…")
    await worker._execute(job)  # noqa: SLF001 - CLI 直跑复用执行路径，不经过队列

    job = store.get(job.id) or job
    if job.state != JobState.SUCCEEDED:
        print(f"{ERR} {job.state.value}: {job.error}")
        return 1
    print(f"{OK} 完成，用时 {job.finished_at - job.started_at:.1f}s")
    for a in job.artifacts:
        print(f"  {a.path}  {a.width}x{a.height}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="platform")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境与模型预检")

    p_run = sub.add_parser("run", help="本地直接跑一个任务")
    p_run.add_argument("--capability", default="image.txt2img")
    p_run.add_argument("--variant", required=True)
    p_run.add_argument("--prompt", required=True)
    p_run.add_argument("--width", type=int)
    p_run.add_argument("--height", type=int)
    p_run.add_argument("--steps", type=int)
    p_run.add_argument("--cfg", type=float)
    p_run.add_argument("--seed", type=int)
    p_run.add_argument("--sampler", default=None)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.cmd == "doctor":
        sys.exit(asyncio.run(_doctor()))
    if args.cmd == "run":
        sys.exit(asyncio.run(_run(args)))
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("apps.api.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
