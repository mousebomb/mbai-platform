"""CLI 适配器：把本地命令行程序（realesrgan-ncnn-vulkan 等）包装成 Adapter 契约。

manifest 的 cli.command 里用 {参数名} 引用 constants/params，另有
{tools_dir}（工具根目录）和 {out}（产物路径）两个内置占位符；
替换不到的占位符在启动前直接报错，不静默传空串。
进程用 Popen 启动（CREATE_NO_WINDOW），stdout+stderr 合并写
data/logs/<job_id>.log，句柄按 job_id 存在适配器单例里。
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from ..assets import image_size
from ..contracts import Artifact
from ..media import KIND_BY_SUFFIX, media_duration
from ..registry import VariantSpec
from ..resources import release_comfy
from ..settings import settings
from ..voices import find
from .base import Adapter, AdapterError, EngineState, EngineStatus

_PLACEHOLDER = re.compile(r"\{([\w:]+)\}")
# 产物扩展名 → Artifact.kind；不认识的扩展名直接跳过，不当产物收
_MEDIA_KINDS = KIND_BY_SUFFIX
# 日志里的百分比进度（realesrgan 按 tile 打 "xx.xx%"）；取最后一个匹配
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
# 执行中进度上限：1.0 只留给 SUCCEEDED
_PROGRESS_CEIL = 0.99


def _render(template: str, values: dict[str, Any]) -> str:
    """替换 {name} 占位符；缺值直接报错，不静默传空串。"""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise AdapterError(f"cli.command 里的占位符 {{{name}}} 没有对应的参数/常量/内置值")
        return str(values[name])

    return _PLACEHOLDER.sub(repl, template)


class CliAdapter(Adapter):
    kind = "cli"

    def __init__(self) -> None:
        # ref == job_id：进程句柄按任务保存，取消/收产物时取出
        self._procs: dict[str, subprocess.Popen] = {}
        self._deadlines: dict[str, float] = {}
        self._cancelled: set[str] = set()

    async def health(self) -> dict:
        # CLI 引擎没有常驻服务，任务即起即用；工具是否装好由任务启动报错兜底
        return {"ok": True, "url": str(settings.tools_dir), "devices": []}

    async def submit(self, spec: VariantSpec, params: dict[str, Any], job_id: str) -> str:
        cli = spec.cli or {}
        command = cli.get("command") or []
        if not command:
            raise AdapterError("manifest 缺少 cli.command")

        out_dir = settings.artifacts_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        output_ext = str(cli.get("output_ext", "png")).lstrip(".")
        out_path = out_dir / f"output.{output_ext}"

        # 占位符取值：settings 字段打底（引擎路径因机器而异，manifest 不写死）、
        # constants 覆盖、params 最高优先级（与 manifest 约定一致）
        scalar_settings = {
            k: v
            for k, v in settings.model_dump().items()
            if isinstance(v, (str, int, float, Path))
        }
        values: dict[str, Any] = {
            **scalar_settings,
            **spec.constants,
            **params,
            "out": str(out_path),
            "tools_dir": str(settings.tools_dir),
        }
        # voice 类型参数展开成 {<参数名>:wav} / {<参数名>:prompt}：
        # 音色清单在平台侧统一解析（config.txt + 目录扫描），脚本只收路径和文本
        for name, desc in spec.inputs.items():
            if desc.get("type") == "voice" and name in params:
                voice = find(str(params[name]))
                if voice is None:
                    raise AdapterError(f"音色 {params[name]!r} 不存在")
                values[f"{name}:wav"] = str(voice.wav)
                values[f"{name}:prompt"] = voice.prompt_text
        argv = [_render(str(arg), values) for arg in command]

        # 单卡机器：声明了 free_comfy_vram 的大显存任务，开跑前先让 ComfyUI 卸载模型
        if cli.get("free_comfy_vram"):
            await release_comfy()

        log_path = settings.logs_dir / f"{job_id}.log"
        logf = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                argv,
                stdout=logf,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as e:
            raise AdapterError(
                f"启动命令行进程失败（工具缺失先跑 scripts/download-tools.ps1）: {e}"
            ) from e
        finally:
            logf.close()  # 子进程已复制句柄，父进程这里就可以关

        # 重新入队的同 id 任务要清掉上次的取消标记
        self._cancelled.discard(job_id)
        self._procs[job_id] = proc
        self._deadlines[job_id] = time.monotonic() + float(cli.get("timeout_sec", 900))
        return job_id

    async def status(self, ref: str) -> EngineStatus:
        if ref in self._cancelled:
            return EngineStatus(state=EngineState.CANCELLED)
        proc = self._procs.get(ref)
        if proc is None:
            # 平台重启后句柄丢失；任务会被 requeue_running 重新提交
            return EngineStatus(state=EngineState.FAILED, error="进程句柄不存在")
        code = proc.poll()
        if code is None:
            deadline = self._deadlines.get(ref)
            if deadline is not None and time.monotonic() > deadline:
                self._kill_tree(proc)
                return EngineStatus(state=EngineState.FAILED, error="执行超时，已终止进程")
            return EngineStatus(state=EngineState.RUNNING, progress=self._log_progress(ref))
        # 进程已退出：立刻释放句柄，避免失败任务在 _procs 里越积越多
        self._release(ref)
        if code == 0:
            return EngineStatus(state=EngineState.SUCCEEDED, progress=1.0)
        return EngineStatus(state=EngineState.FAILED, error=self._error_tail(ref, code))

    def _release(self, ref: str) -> None:
        self._procs.pop(ref, None)
        self._deadlines.pop(ref, None)

    def _log_progress(self, ref: str) -> float:
        """从日志末尾解析最后一个 "xx.xx%"，映射到 0~0.99；解析不到返回 0。"""
        try:
            data = (settings.logs_dir / f"{ref}.log").read_bytes()
        except OSError:
            return 0.0
        # 只看末尾 64 KB：进度是滚动追加的，全读浪费
        text = data[-65536:].decode("utf-8", errors="replace")
        matches = _PCT_RE.findall(text)
        if not matches:
            return 0.0
        try:
            return min(_PROGRESS_CEIL, max(0.0, float(matches[-1]) / 100.0))
        except ValueError:
            return 0.0

    async def cancel(self, ref: str) -> None:
        proc = self._procs.get(ref)
        if proc is not None:
            self._kill_tree(proc)
        # 无论进程是否还在，都记取消：status/collect 据此收尾
        self._cancelled.add(ref)

    async def collect(self, ref: str, job_id: str, params: dict[str, Any]) -> list[Artifact]:
        self._procs.pop(ref, None)
        self._deadlines.pop(ref, None)
        self._cancelled.discard(ref)

        out_dir = settings.artifacts_dir / job_id
        artifacts: list[Artifact] = []
        for path in sorted(out_dir.iterdir()):
            if not path.is_file():
                continue
            kind = _MEDIA_KINDS.get(path.suffix.lower())
            if kind is None:
                continue
            if kind == "image":
                size = image_size(path.read_bytes())
                artifacts.append(
                    Artifact(
                        job_id=job_id,
                        kind="image",
                        path=str(path),
                        width=size[0] if size else None,
                        height=size[1] if size else None,
                    )
                )
            else:
                artifacts.append(
                    Artifact(
                        job_id=job_id,
                        kind=kind,
                        path=str(path),
                        duration=media_duration(path),
                    )
                )
        if not artifacts:
            raise AdapterError("CLI 执行完成但 out_dir 下没有可识别的产物（png/wav/mp4…）")
        return artifacts

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        """杀整个进程树（Windows 专用），防止子进程残留成孤儿。"""
        if proc.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _error_tail(self, ref: str, code: int) -> str:
        """失败时带日志末尾 200 字符，方便不翻文件就定位原因。"""
        try:
            data = (settings.logs_dir / f"{ref}.log").read_bytes()
        except OSError:
            return f"进程退出码 {code}（无日志）"
        text = data.decode("utf-8", errors="replace").strip()
        return f"进程退出码 {code}: {text[-200:]}"
