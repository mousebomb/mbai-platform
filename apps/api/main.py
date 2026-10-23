"""HTTP 入口：前端和 Agent 都只调这一套接口。"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from platform_core.adapters import get_adapter
from platform_core.assets import (
    IMAGE_EXTS,
    MAX_ASSET_BYTES,
    asset_path,
    detect_format,
    ext_matches,
    image_size,
)
from platform_core.contracts import TERMINAL_STATES, Job, JobRequest, JobState
from platform_core.registry import registry
from platform_core.service import store, worker
from platform_core.settings import settings
from platform_core.workflow import resolve_params

# 素材预览用的 MIME（Windows 的 mimetypes 不认识 webp，显式指定）
_ASSET_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 上次进程被杀时留下的 running 任务放回队列
    store.requeue_running()
    worker.start()
    try:
        yield
    finally:
        await worker.stop()


app = FastAPI(title="AI Platform", version="0.1.0", lifespan=lifespan)

# 局域网前端直连，先放开；后续收敛到固定来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/v1/health")
async def health() -> dict:
    # 引擎按注册表声明的 kind 走适配器探活；前端顶栏展示的主引擎优先 comfy
    #（有 GPU 信息），CLI 引擎没有常驻服务，逐个探活结果放进 engines
    kinds = {spec.engine for spec in registry.list_variants()} or {"comfy"}
    engines = {k: await get_adapter(k).health() for k in kinds}
    engine = engines.get("comfy") or next(iter(engines.values()))

    missing = []
    for model in registry.models.values():
        # files 为空的模型（权重随工具包分发）没有可检查的文件，跳过
        if not model.files:
            continue
        path = model.resolve_path()
        if path is None or not path.exists():
            missing.append(model.id)

    return {
        "engine": engine,
        "engines": engines,
        "registry_errors": registry.errors,
        "missing_models": missing,
        "variants": len(registry.variants),
    }


@app.post("/v1/assets")
async def upload_asset(request: Request, file: UploadFile = File(...)) -> dict:
    """上传输入图：只认 png/jpg/jpeg/webp，≤ 20 MB，落盘 data/uploads/<id>.<ext>。"""
    # 先看 Content-Length 挡掉明显超限的请求，避免大文件先整个落进临时文件
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_ASSET_BYTES + 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 20 MB 上限")

    original = Path(file.filename or "").name
    ext = Path(original).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的图片类型 {ext or '(无扩展名)'}，只允许 png/jpg/jpeg/webp",
        )

    data = await file.read(MAX_ASSET_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > MAX_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 20 MB 上限")

    # 文件头必须和扩展名对上，防止文本/伪造文件混进来
    fmt = detect_format(data)
    if fmt is None or not ext_matches(ext, fmt):
        raise HTTPException(status_code=400, detail="文件内容不是有效的 PNG/JPEG/WebP 图片")

    size = image_size(data)
    asset_id = "ast_" + uuid.uuid4().hex[:12]
    (settings.uploads_dir / f"{asset_id}{ext}").write_bytes(data)
    return {
        "id": asset_id,
        "filename": original,
        "bytes": len(data),
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "url": f"/v1/assets/{asset_id}",
    }


@app.get("/v1/assets/{asset_id}")
async def get_asset(asset_id: str) -> FileResponse:
    path = asset_path(asset_id)
    if path is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    return FileResponse(path, media_type=_ASSET_MEDIA[path.suffix.lower()])


@app.get("/v1/capabilities")
async def capabilities() -> list[dict]:
    return [v.info().model_dump() for v in registry.list_variants()]


@app.get("/v1/matrix")
async def matrix() -> list[dict]:
    """能力矩阵：目标全集 18 项 × 成熟度等级（= 变体 maturity 最大值），前端「矩阵」tab 用。"""
    return registry.matrix()


@app.post("/v1/jobs", response_model=Job, status_code=202)
async def create_job(req: JobRequest) -> Job:
    spec = registry.get(req.capability, req.variant)
    job = Job(
        capability=req.capability,
        variant=req.variant,
        model_id=spec.model_id,
        params=req.params,
    )
    # 参数错误在入队前就暴露，而不是等执行到一半才失败
    try:
        resolved = resolve_params(spec, req.params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # image 参数在入队前校验素材存在，避免任务排到一半才发现缺图
    for name, desc in spec.inputs.items():
        if desc.get("type") == "image" and name in resolved:
            if asset_path(resolved[name]) is None:
                raise HTTPException(
                    status_code=422, detail=f"参数 {name} 引用的素材不存在: {resolved[name]}"
                )

    return store.add(job)


@app.get("/v1/jobs", response_model=list[Job])
async def list_jobs(limit: int = 50, state: JobState | None = None) -> list[Job]:
    return store.list(limit=limit, state=state)


@app.get("/v1/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get("/v1/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    """SSE 任务事件流：state / progress 变化时推一帧，15 秒一次心跳，终态推完即断。

    任务不存在在流开始前就 404；客户端断开由 is_disconnected() 感知，
    不会留下僵尸协程。
    """
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def stream():
        # 首帧一定是快照；(state, 4 位小数进度) 相同就不推
        last_key: tuple[str, float] | None = None
        last_ping = time.monotonic()
        while True:
            job = store.get(job_id)
            if job is None:
                return
            key = (job.state.value, round(job.progress, 4))
            if key != last_key:
                last_key = key
                yield f"data: {job.model_dump_json()}\n\n"
                if job.state in TERMINAL_STATES:
                    return
            if time.monotonic() - last_ping >= 15:
                last_ping = time.monotonic()
                yield ": ping\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/jobs/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
        return job

    job.state = JobState.CANCELLED
    store.save(job)

    # 已提交到引擎的，顺手中断；引擎 kind 从注册表拿
    if job.engine_ref:
        try:
            spec = registry.get(job.capability, job.variant)
            await get_adapter(spec.engine).cancel(job.engine_ref)
        except Exception:  # noqa: BLE001 - 取消失败不影响本地状态
            pass
    return job


@app.delete("/v1/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> Response:
    """删除任务及其产物文件。

    只允许删终态任务：排队/执行中的先取消。否则删除后执行器仍会回写该任务，
    变成「删了又冒出来」的幽灵记录。
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.state not in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="任务还在排队或执行中，请先取消")

    store.delete(job_id)
    # 产物目录即使不存在也不报错：失败任务可能压根没产出
    shutil.rmtree(settings.artifacts_dir / job_id, ignore_errors=True)
    return Response(status_code=204)


@app.get("/v1/jobs/{job_id}/file/{filename}")
async def get_file(job_id: str, filename: str) -> FileResponse:
    # 只允许读该任务目录下的直接子文件，挡掉路径穿越
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = settings.artifacts_dir / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)


# 前端构建产物由同一个进程托管：局域网只需 8000 一个地址
WEB_DIST = settings.root / "apps" / "web" / "dist"

if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
else:

    @app.get("/")
    async def index_missing() -> dict:
        return {"detail": "前端未构建：npm --prefix apps/web run build"}
