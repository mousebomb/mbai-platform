"""CLI：doctor 预检、run 跑一张图、serve 起服务。"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .adapters import get_adapter
from .comfy_client import ComfyClient
from .contracts import Job, JobState
from .gc import apply as gc_apply
from .gc import plan as gc_plan
from .registry import MATURITY_LEVELS, registry
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

    print("[矩阵]")
    matrix = registry.matrix()
    counts = {lv: 0 for lv in MATURITY_LEVELS}
    for row in matrix:
        counts[row["level"]] += 1
    print(
        f"  {OK} {len(matrix)} 项能力：L0 {counts['L0']} · L1 {counts['L1']} "
        f"· L2 {counts['L2']} · L3 {counts['L3']}"
    )
    print()

    print("[模型文件]")
    missing = 0
    for model in registry.models.values():
        roles = sorted(set(model.files) | set(registry.profiles.get(model.id, {})))
        if not roles:
            print(f"  {OK} {model.id} ({model.kind}): 无固定权重文件（工具包分发或运行时选择）")
            continue
        # 可还原性直接影响「换机器能不能自动补齐」，必须显式打出来
        tag = model.kind + ("，可还原" if model.can_restore else "，需自备")
        print(f"  {OK} {model.id} ({tag})")
        for role in roles:
            path = registry.resolve_file(model.id, role)
            src = registry.binding_source(model.id, role)
            if path is not None and path.exists():
                print(
                    f"      {OK} {role}: {path}  "
                    f"({path.stat().st_size / 1e9:.2f} GB, 绑定={src})"
                )
            else:
                ok = False
                missing += 1
                hint = "" if model.can_restore else "（无下载来源，需人工放置）"
                print(f"      {ERR} {role}: 缺失 {path}{hint}")
    if missing:
        print(f"     补下载: uv run python scripts\\download-models.py --models-dir {settings.models_dir}")
    print()

    print("[引擎]")
    # 探活走适配器；MOSS 节点检查是 ComfyUI 特有项，仍直接走 ComfyClient
    health = await get_adapter("comfy").health()
    if not health.get("ok"):
        ok = False
        print(f"  {ERR} 引擎不可达: {health.get('error')}")
        print("    启动命令: powershell -ExecutionPolicy Bypass -File scripts\\start-engine.ps1")
    else:
        for d in health.get("devices") or []:
            print(f"  {OK} {d.get('name')}  VRAM {d.get('vram_total', 0) / 1e9:.1f} GB")
        try:
            async with ComfyClient(settings.comfy_url, timeout=5) as comfy:
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
    # --set key=value：给不认识的变体补参数（TTS 的 text、声音克隆的素材 id 等）
    for item in args.set or []:
        if "=" not in item:
            print(f"{ERR} --set 需要 key=value 形式，收到 {item!r}")
            return 1
        key, value = item.split("=", 1)
        # --set 拿到的永远是字符串，按 manifest 声明的类型转换，否则 float/int 参数会被拒
        kind = (spec.inputs.get(key) or {}).get("type")
        if kind in {"int", "float"}:
            try:
                params[key] = int(value) if kind == "int" else float(value)
            except ValueError:
                print(f"{ERR} --set {key} 需要{'整数' if kind == 'int' else '数字'}，收到 {value!r}")
                return 1
        else:
            params[key] = value

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
        detail = (
            f"{a.width}x{a.height}"
            if a.kind == "image"
            else (f"{a.duration:.2f}s" if a.duration else "时长未知")
        )
        print(f"  {a.path}  [{a.kind}] {detail}")
    return 0


def _gc(args: argparse.Namespace) -> int:
    items = gc_plan(
        store,
        artifacts_days=args.artifacts_days,
        uploads_days=args.uploads_days,
        logs_days=args.logs_days,
        engine_input_days=args.engine_input_days,
        engine_output_days=args.engine_output_days,
    )
    if not items:
        print(f"{OK} 没有可清理的内容")
        return 0

    by_cat: dict[str, list] = {}
    for it in items:
        by_cat.setdefault(it.category, []).append(it)
    for cat in sorted(by_cat):
        lst = by_cat[cat]
        total = sum(i.size for i in lst)
        print(f"[{cat}] {len(lst)} 项，{total / 1024 / 1024:.2f} MB")
        for it in lst[:10]:
            print(f"   - {it.path.name}  ({it.size / 1024 / 1024:.2f} MB)  {it.reason}")
        if len(lst) > 10:
            print(f"   ... 其余 {len(lst) - 10} 项")

    total = sum(i.size for i in items)
    if not args.apply:
        print(f"\n{OK} 预演：共 {len(items)} 项，可释放 {total / 1024 / 1024:.2f} MB")
        print("     加 --apply 才会真的删除；用户产物默认不动（--artifacts-days N 才清）")
        return 0

    done, freed = gc_apply(items)
    print(f"\n{OK} 已删除 {done} 项，释放 {freed / 1024 / 1024:.2f} MB")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="platform")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境与模型预检")

    p_run = sub.add_parser("run", help="本地直接跑一个任务")
    p_run.add_argument("--capability", default="image.txt2img")
    p_run.add_argument("--variant", required=True)
    p_run.add_argument("--prompt", default=None)
    p_run.add_argument("--width", type=int)
    p_run.add_argument("--height", type=int)
    p_run.add_argument("--steps", type=int)
    p_run.add_argument("--cfg", type=float)
    p_run.add_argument("--seed", type=int)
    p_run.add_argument("--sampler", default=None)
    # 给参数不固定的变体补值（TTS 的 text、克隆的素材 id 等），可重复
    p_run.add_argument("--set", action="append", default=[], metavar="K=V")

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    # MCP 入口：给 Agent 用的 stdio 服务，内部走 HTTP 调本平台
    p_mcp = sub.add_parser("mcp", help="MCP 服务（stdio），把平台能力暴露给 Agent")
    p_mcp.add_argument("--url", default=None, help="平台地址，默认 settings.api_url")
    p_mcp.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse", "streamable-http"]
    )

    p_gc = sub.add_parser("gc", help="按 TTL 清理中间产物（默认只预演）")
    p_gc.add_argument("--apply", action="store_true", help="真的删除；不加只打印计划")
    p_gc.add_argument(
        "--artifacts-days", type=int, default=0, help="用户产物保留天数，0=不清理（默认）"
    )
    p_gc.add_argument("--uploads-days", type=int, default=7, help="上传素材保留天数")
    p_gc.add_argument("--logs-days", type=int, default=7, help="CLI 日志保留天数")
    p_gc.add_argument("--engine-input-days", type=int, default=7, help="引擎 input 副本保留天数")
    p_gc.add_argument("--engine-output-days", type=int, default=7, help="引擎 output 副本保留天数")

    args = parser.parse_args()
    if args.cmd == "doctor":
        sys.exit(asyncio.run(_doctor()))
    if args.cmd == "run":
        sys.exit(asyncio.run(_run(args)))
    if args.cmd == "gc":
        sys.exit(_gc(args))
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("apps.api.main:app", host=args.host, port=args.port, reload=False)
    if args.cmd == "mcp":
        # 延迟导入：不用 MCP 的人不必付 mcp 包的启动开销
        from .mcp_server import main as mcp_main

        sys.argv = ["platform mcp"]
        if args.url:
            sys.argv += ["--url", args.url]
        sys.argv += ["--transport", args.transport]
        mcp_main()


if __name__ == "__main__":
    main()
