"""HTTP 入口：前端和 Agent 都只调这一套接口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from platform_core.comfy_client import ComfyClient
from platform_core.contracts import Job, JobRequest, JobState
from platform_core.registry import registry
from platform_core.service import store, worker
from platform_core.settings import settings
from platform_core.workflow import resolve_params


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
    engine: dict = {"url": settings.comfy_url, "ok": False}
    try:
        async with ComfyClient(settings.comfy_url, timeout=5) as comfy:
            stats = await comfy.health()
        engine.update(ok=True, devices=stats.get("devices"))
    except Exception as e:  # noqa: BLE001
        engine["error"] = f"{type(e).__name__}: {e}"

    missing = []
    for model in registry.models.values():
        if model.resolve_path() is None or not model.resolve_path().exists():
            missing.append(model.id)

    return {
        "engine": engine,
        "registry_errors": registry.errors,
        "missing_models": missing,
        "variants": len(registry.variants),
    }


@app.get("/v1/capabilities")
async def capabilities() -> list[dict]:
    return [v.info().model_dump() for v in registry.list_variants()]


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
        resolve_params(spec, req.params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

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


@app.post("/v1/jobs/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
        return job

    job.state = JobState.CANCELLED
    store.save(job)

    # 已提交到引擎的，顺手中断
    if job.engine_ref:
        try:
            async with ComfyClient(settings.comfy_url, timeout=5) as comfy:
                await comfy.cancel(job.engine_ref)
        except Exception:  # noqa: BLE001 - 取消失败不影响本地状态
            pass
    return job


@app.get("/v1/jobs/{job_id}/file/{filename}")
async def get_file(job_id: str, filename: str) -> FileResponse:
    # 只允许读该任务目录下的直接子文件，挡掉路径穿越
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = settings.artifacts_dir / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)
