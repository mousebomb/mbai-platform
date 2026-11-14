"""OpenAI 兼容层：把平台队列包装成 /v1/audio/speech 与 /v1/images/generations。

为什么是「提交 → 阻塞等终态 → 回读产物」：平台的执行路径只有一条
（队列 → worker → 适配器），绕开它会丢作品墙记录、取消能力和产物溯源。
而 OpenAI 的这两个接口是同步返回的，所以这里只能等任务跑完再回结果。

参数映射不写死变体：全部按 manifest 的 inputs 声明来（text / voice / speed /
instruct / prompt / width / height），新增 TTS 或 txt2img 变体不用改这个文件。

不支持流式（`stream_format=sse` 直接 400）：TTS 产物是引擎进程退出后一次性落盘的，
「边生成边播」需要引擎侧逐句 flush + 绕开队列的执行路径，不在这一层的范围内。
返回 400 而不是静默忽略，是为了让请求 sse 的客户端立刻暴露问题，而不是拿到
二进制音频后解析失败。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .adapters import get_adapter
from .contracts import TERMINAL_STATES, Job, JobState
from .registry import VariantSpec, registry
from .service import store
from .settings import settings
from .voices import find as find_voice, list_voices
from .workflow import resolve_params

log = logging.getLogger("mbai.openai")

router = APIRouter(tags=["openai"])

# 兼容层只暴露这两类能力；将来接 /v1/images/edits、/v1/audio/transcriptions
# 时在这里加能力名，/v1/models 会自动跟着变
_SPEECH_CAPS = {"audio.tts"}
_IMAGE_CAPS = {"image.txt2img"}

# OpenAI 官方模型名：客户端把 tts-1 / dall-e-3 写死在代码里很常见，
# 不认这些名字它连一次都跑不通。按前缀匹配，兼容带日期后缀的快照名
_TTS_ALIASES = ("tts-1", "gpt-4o-mini-tts", "gpt-4o-tts")
_IMAGE_ALIASES = ("dall-e-", "gpt-image-")

# n>1 时逐张串行提交（单 worker、单卡），上限压住排队时间
_MAX_N = 4

# response_format → (ffmpeg 参数, MIME)。wav 直接发原文件，不在这里
_TRANSCODE = {
    "mp3": (["-codec:a", "libmp3lame", "-qscale:a", "2", "-f", "mp3"], "audio/mpeg"),
    "opus": (["-codec:a", "libopus", "-b:a", "48k", "-f", "ogg"], "audio/ogg"),
    "aac": (["-codec:a", "aac", "-b:a", "128k", "-f", "adts"], "audio/aac"),
    "flac": (["-codec:a", "flac", "-f", "flac"], "audio/flac"),
    # pcm 是裸采样（16-bit 有符号小端），CosyVoice 本来就是 24 kHz 单声道
    "pcm": (["-codec:a", "pcm_s16le", "-f", "s16le"], "audio/pcm"),
}


class CompatError(Exception):
    """按 OpenAI 的错误信封返回：{"error": {message, type, param, code}}。"""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        type_: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.param = param
        self.code = code


def _error_response(
    status: int,
    message: str,
    *,
    type_: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "param": param, "code": code}},
    )


async def _handle_compat_error(request: Request, exc: CompatError) -> JSONResponse:
    return _error_response(exc.status, exc.message, type_=exc.type, param=exc.param, code=exc.code)


async def _handle_validation(request: Request, exc: RequestValidationError) -> Response:
    """兼容层的请求体校验错误翻成 OpenAI 信封；平台原生接口保持 FastAPI 的 422 形状。"""
    if not request.url.path.startswith(("/v1/audio/", "/v1/images/", "/v1/models")):
        return await request_validation_exception_handler(request, exc)
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(p) for p in first.get("loc", ()) if p != "body"]
    msg = str(first.get("msg", "请求体不合法"))
    detail = f"{'.'.join(loc)}: {msg}" if loc else msg
    return _error_response(422, detail, param=loc[0] if loc else None, code="invalid_body")


class SpeechRequest(BaseModel):
    # 客户端爱传 user / stream / 各种扩展字段，宽容接受，别为多一个键拒掉整个请求
    model_config = ConfigDict(extra="ignore")

    input: str
    model: str | None = None
    # OpenAI 允许 voice 是字符串或 {"id": "voice_xxx"}，两种都收
    voice: str | dict[str, Any] | None = None
    response_format: str = "mp3"
    speed: float = 1.0
    instructions: str | None = None
    stream_format: str = "audio"


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt: str
    model: str | None = None
    n: int = 1
    size: str | None = None
    response_format: str = "url"


def _is_alias(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name.startswith(p) for p in prefixes)


def _default_variant(pool: list[VariantSpec], default_name: str) -> VariantSpec:
    for v in pool:
        if v.variant == default_name:
            return v
    # 配置里写的默认变体不在注册表时退回名字序第一个：宁可用错也不要整条接口不可用
    fallback = sorted(pool, key=lambda v: v.variant)[0]
    log.warning("默认变体 %r 不在注册表里，改用 %r", default_name, fallback.variant)
    return fallback


def _pick_variant(
    caps: set[str],
    aliases: tuple[str, ...],
    model: str | None,
    default_name: str,
    label: str,
) -> VariantSpec:
    """把 OpenAI 的 model 字段解析成平台变体：全名 → 裸变体名 → 官方别名 → 默认变体。"""
    pool = [v for v in registry.list_variants() if v.capability in caps]
    if not pool:
        raise CompatError(501, f"平台没有注册任何 {label} 变体", type_="server_error")
    name = (model or "").strip()
    if not name:
        return _default_variant(pool, default_name)

    # capability/variant 全名
    spec = registry.variants.get(name)
    if spec is not None and spec.capability in caps:
        return spec
    # 裸变体名（z-image-turbo、cosyvoice3-segmented）
    for v in pool:
        if v.variant == name:
            return v
    if _is_alias(name, aliases):
        selected = _default_variant(pool, default_name)
        log.info("模型名 %r 是 OpenAI 官方名，按默认变体处理 → %s", name, selected.variant)
        return selected

    available = ", ".join(sorted(v.variant for v in pool))
    raise CompatError(
        400, f"未知模型 {name!r}；可用: {available}", param="model", code="model_not_found"
    )


def _resolve_voice(raw: str | dict[str, Any] | None) -> str:
    """音色名解析：命中本机音色清单就用；否则一律落到默认音色。

    OpenAI 客户端发的是 alloy / onyx 这类名字，本机音色是自定义中文名，
    两套名字没有天然对应关系。硬报错会让这类客户端一次都跑不通，
    所以按约定静默替换，只在日志里留一行。
    """
    voices = list_voices()
    if not voices:
        raise CompatError(
            501,
            "本机没有可用音色：检查 PLATFORM_COSYVOICE_CONFIG / PLATFORM_COSYVOICE_VOICES",
            type_="server_error",
        )
    name = raw if isinstance(raw, str) else str((raw or {}).get("id") or "")
    if name:
        hit = find_voice(name)
        if hit is not None:
            return hit.name

    target = settings.openai_default_voice
    if target:
        hit = find_voice(target)
        if hit is None:
            # 配错的默认音色不能静默换一个：那会让「声音不对」变成排障事故
            raise CompatError(
                500,
                f"PLATFORM_OPENAI_DEFAULT_VOICE={target!r} 不在音色清单里",
                type_="server_error",
            )
        resolved = hit.name
    else:
        resolved = next((v.name for v in voices if v.name.lower() == "default"), voices[0].name)
    log.info("音色 %r 未命中，按默认音色处理 → %s", name or "(未指定)", resolved)
    return resolved


def _text_key(spec: VariantSpec) -> str:
    """TTS 的正文参数名：优先 text，其次 input，再退到第一个 text 类型输入。"""
    for key in ("text", "input"):
        if key in spec.inputs:
            return key
    for key, desc in spec.inputs.items():
        if desc.get("type") == "text":
            return key
    raise CompatError(
        501, f"{spec.capability}/{spec.variant} 没有文本输入，不能用作 TTS", type_="server_error"
    )


def _parse_size(raw: str) -> tuple[int, int]:
    parts = raw.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise CompatError(400, f"size 格式应为 WxH，收到 {raw!r}", param="size")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError as e:
        raise CompatError(400, f"size 格式应为 WxH，收到 {raw!r}", param="size") from e
    if width <= 0 or height <= 0:
        raise CompatError(400, f"size 必须为正数，收到 {raw!r}", param="size")
    return width, height


def _submit(spec: VariantSpec, params: dict[str, Any]) -> Job:
    try:
        # 入队前先校验一遍，别把明显非法的参数排进队列（worker 里还会再校验一次）
        resolve_params(spec, params)
    except ValueError as e:
        raise CompatError(400, str(e)) from e
    job = Job(
        capability=spec.capability,
        variant=spec.variant,
        model_id=spec.model_id,
        params=params,
    )
    return store.add(job)


async def _cancel(job: Job) -> None:
    """与 /v1/jobs/{id}/cancel 同语义：先落状态，再顺手中断已提交的引擎任务。"""
    job.state = JobState.CANCELLED
    store.save(job)
    if job.engine_ref:
        try:
            spec = registry.get(job.capability, job.variant)
            await get_adapter(spec.engine).cancel(job.engine_ref)
        except Exception:  # noqa: BLE001 - 取消失败不影响本地状态
            pass


async def _await_job(job: Job, request: Request) -> Job:
    """阻塞等任务到终态。客户端断开就取消任务，别让它白占 GPU。"""
    deadline = time.monotonic() + max(1.0, settings.openai_wait_sec)
    while True:
        fresh = store.get(job.id)
        if fresh is None:
            raise CompatError(500, f"任务 {job.id} 记录丢失", type_="server_error")
        if fresh.state in TERMINAL_STATES:
            break
        if await request.is_disconnected():
            await _cancel(fresh)
            raise CompatError(499, f"客户端断开，任务 {job.id} 已取消")
        if time.monotonic() >= deadline:
            raise CompatError(
                504,
                f"等待超时（{settings.openai_wait_sec:.0f}s）。任务 {job.id} 仍在队列中，"
                f"可用 GET /v1/jobs/{job.id} 跟进；延长上限改 PLATFORM_OPENAI_WAIT_SEC",
                type_="server_error",
                code="timeout",
            )
        await asyncio.sleep(0.4)

    if fresh.state == JobState.FAILED:
        raise CompatError(
            500, fresh.error or "引擎执行失败", type_="server_error", code="engine_error"
        )
    if fresh.state == JobState.CANCELLED:
        raise CompatError(409, f"任务 {job.id} 已取消", code="cancelled")
    return fresh


def _ffmpeg() -> str | None:
    # 显式配置优先；没配就从 PATH 找（跨机器不保证装在哪）
    return settings.ffmpeg or shutil.which("ffmpeg")


async def _audio_response(path: Path, fmt: str) -> Response:
    fmt = (fmt or "mp3").lower()
    if fmt == "wav":
        return Response(path.read_bytes(), media_type="audio/wav")
    if fmt not in _TRANSCODE:
        raise CompatError(
            400,
            f"response_format={fmt!r} 不支持；可选: wav, {', '.join(_TRANSCODE)}",
            param="response_format",
        )
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        raise CompatError(
            501,
            f"response_format={fmt!r} 需要 ffmpeg 转码（TTS 产物是 wav）。"
            "装一份 ffmpeg 并把 PLATFORM_FFMPEG 指向它，或改用 response_format=wav",
            type_="server_error",
            code="ffmpeg_missing",
        )
    args, mime = _TRANSCODE[fmt]
    with tempfile.TemporaryDirectory(prefix="mbai-speech-") as tmp:
        out = Path(tmp) / f"speech.{fmt}"
        proc = await asyncio.to_thread(
            subprocess.run,
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), *args, str(out)],
            capture_output=True,
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", errors="replace").strip()[-400:]
            raise CompatError(
                500, f"ffmpeg 转码失败: {tail}", type_="server_error", code="ffmpeg_failed"
            )
        data = out.read_bytes()
    return Response(data, media_type=mime)


@router.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """已适配的 OpenAI 端点对应的模型；客户端配对地址后靠它列候选。"""
    caps = _SPEECH_CAPS | _IMAGE_CAPS
    return {
        "object": "list",
        "data": [
            {"id": v.variant, "object": "model", "created": 0, "owned_by": "mbai"}
            for v in registry.list_variants()
            if v.capability in caps
        ],
    }


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str) -> dict[str, Any]:
    caps = _SPEECH_CAPS | _IMAGE_CAPS
    for v in registry.list_variants():
        if v.capability not in caps:
            continue
        if model_id in {v.variant, registry.key(v.capability, v.variant)}:
            return {"id": v.variant, "object": "model", "created": 0, "owned_by": "mbai"}
    raise CompatError(404, f"未知模型 {model_id!r}", code="model_not_found")


@router.post("/v1/audio/speech")
async def create_speech(body: SpeechRequest, request: Request) -> Response:
    if (body.stream_format or "audio").lower() == "sse":
        raise CompatError(
            400,
            "本平台不支持 stream_format=sse：TTS 产物在引擎进程结束后一次性落盘，"
            "无法边生成边播。请用 stream_format=audio（默认）或省略该字段",
            param="stream_format",
            code="stream_format_not_supported",
        )
    if not (body.input or "").strip():
        raise CompatError(400, "input 不能为空", param="input", code="invalid_input")

    spec = _pick_variant(
        _SPEECH_CAPS, _TTS_ALIASES, body.model, settings.openai_tts_variant, "TTS"
    )
    params: dict[str, Any] = {_text_key(spec): body.input}
    if "voice" in spec.inputs:
        params["voice"] = _resolve_voice(body.voice)
    if abs(body.speed - 1.0) > 1e-6:
        # 不支持 speed 的变体上静默忽略会让调用方以为生效了
        if "speed" not in spec.inputs:
            raise CompatError(400, f"{spec.variant} 不支持 speed", param="speed")
        params["speed"] = body.speed
    # OpenAI 的 instructions → instruct；instruct 型变体把它声明成必填，缺省给中性指令
    instruct_key = next((k for k in ("instruct", "instructions") if k in spec.inputs), None)
    if instruct_key:
        params[instruct_key] = body.instructions or settings.openai_default_instruct

    job = await _await_job(_submit(spec, params), request)
    audio = next((a for a in job.artifacts if a.kind == "audio"), None)
    if audio is None:
        raise CompatError(
            500, f"任务 {job.id} 没有音频产物", type_="server_error", code="no_artifact"
        )
    return await _audio_response(Path(audio.path), body.response_format)


@router.post("/v1/images/generations")
async def create_image(body: ImageRequest, request: Request) -> dict[str, Any]:
    if not (body.prompt or "").strip():
        raise CompatError(400, "prompt 不能为空", param="prompt", code="invalid_prompt")
    if not 1 <= body.n <= _MAX_N:
        raise CompatError(
            400, f"n 只支持 1~{_MAX_N}（单卡串行，n>1 会逐张生成）", param="n"
        )

    spec = _pick_variant(
        _IMAGE_CAPS, _IMAGE_ALIASES, body.model, settings.openai_image_variant, "文生图"
    )
    if "prompt" not in spec.inputs:
        raise CompatError(
            501, f"{spec.capability}/{spec.variant} 没有 prompt 输入", type_="server_error"
        )

    params: dict[str, Any] = {"prompt": body.prompt}
    # 变体没声明 width/height 时忽略 size（比如固定尺寸的模型），不报错
    if body.size and "width" in spec.inputs and "height" in spec.inputs:
        width, height = _parse_size(body.size)
        params["width"] = width
        params["height"] = height
    fmt = (body.response_format or "url").lower()
    if fmt not in {"url", "b64_json"}:
        raise CompatError(
            400, f"response_format={fmt!r} 不支持；可选: url, b64_json", param="response_format"
        )
    base_url = str(request.base_url).rstrip("/")

    data: list[dict[str, Any]] = []
    for _ in range(body.n):
        job = await _await_job(_submit(spec, dict(params)), request)
        art = next((a for a in job.artifacts if a.kind == "image"), None)
        if art is None:
            raise CompatError(
                500, f"任务 {job.id} 没有图片产物", type_="server_error", code="no_artifact"
            )
        path = Path(art.path)
        if fmt == "b64_json":
            data.append({"b64_json": base64.b64encode(path.read_bytes()).decode("ascii")})
        else:
            # 局域网直连：用请求自身的 base_url 拼绝对地址，客户端换机器也能取到
            data.append({"url": f"{base_url}/v1/jobs/{job.id}/file/{path.name}"})
    return {"created": int(time.time()), "data": data}


def install(app: FastAPI) -> None:
    """挂到 FastAPI：路由 + 两个异常处理器（只影响兼容层路径）。"""
    app.include_router(router)
    app.add_exception_handler(CompatError, _handle_compat_error)
    app.add_exception_handler(RequestValidationError, _handle_validation)
