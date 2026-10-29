"""MCP 入口：把平台的 HTTP 接口包成一组 MCP 工具，让 Agent 直接发现能力、提任务、取产物。

为什么是**薄客户端**（走 HTTP）而不是直接 import platform_core：
- Agent 可能在另一台机器上连过来，HTTP 是唯一稳定的边界
- 平台服务重启不影响已连上的 MCP 会话
所以这里只依赖 httpx + mcp，不碰 store / worker。

用法（stdio，主流客户端都支持）：

    mbai mcp                      # 连 http://127.0.0.1:8000
    mbai mcp --url http://<本机IP>:8000

客户端配置示例（Claude Desktop / opencode 之类）：

    {"mcpServers": {"mbai-platform": {
        "command": "uv",
        "args": ["run", "--directory", "D:/AI/platform", "platform", "mcp"]
    }}}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from .settings import settings

# 单个工具调用的默认上限：提交/查询都是毫秒级，600 秒留给 wait_job
_TIMEOUT = 60.0
# wait_job 的轮询间隔：队列是 2s 刷新的，再密也没意义
_POLL_SEC = 1.5

srv = MCPServer(
    name="mbai-platform",
    title="本机 AI 能力平台",
    version="0.1.0",
    instructions=(
        "这台机器上的统一 AI 队列（图片/音频/视频）。"
        "先用 list_capabilities 看有哪些变体和参数，再用 submit 提交、wait_job 等结果。"
        "带图片/音频参数的能力：先用 upload_file 把本地文件换成 asset_id，再放进 params。"
        "capability_matrix 能看到哪些能力还没接（L0）。"
    ),
)

_base_url = settings.api_url

# httpx 每个请求都打一行 INFO 到 stderr；stdio 模式下 stderr 只是日志，
# 但客户端会把它们当服务输出展示，太吵，压到 WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_base_url.rstrip("/"), timeout=_TIMEOUT)


def _error(resp: httpx.Response) -> str:
    """把后端的 detail 提出来，别让 Agent 只看到 422。"""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:400]}"
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return f"HTTP {resp.status_code}: {json.dumps(detail, ensure_ascii=False)}"


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@srv.tool(description="列出全部能力变体及其参数定义（params 允许的键、类型、默认值、范围都在里面）")
async def list_capabilities() -> str:
    async with _client() as c:
        r = await c.get("/v1/capabilities")
        if r.status_code != 200:
            return _error(r)
        return _dump(r.json())


@srv.tool(description="能力成熟度矩阵：L0 未接 / L1 能跑 / L2 有预设 / L3 已验证，看有哪些坑还没填")
async def capability_matrix() -> str:
    async with _client() as c:
        r = await c.get("/v1/matrix")
        if r.status_code != 200:
            return _error(r)
        return _dump(r.json())


@srv.tool(description="上传本地图片/音频作为任务输入，返回 asset_id（放进 params 里用）")
async def upload_file(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return f"文件不存在: {path}"
    async with _client() as c:
        r = await c.post("/v1/assets", files={"file": (p.name, p.read_bytes())})
        if r.status_code != 200:
            return _error(r)
        asset = r.json()
        return _dump({"asset_id": asset["id"], **asset})


@srv.tool(description="把任务提交进队列，立刻返回 job_id；不等结果，用 wait_job 或多个 job_status 跟进")
async def submit(capability: str, variant: str, params: dict[str, Any]) -> str:
    async with _client() as c:
        r = await c.post(
            "/v1/jobs", json={"capability": capability, "variant": variant, "params": params}
        )
        if r.status_code not in (200, 202):
            return _error(r)
        job = r.json()
        return _dump({"job_id": job["id"], "state": job["state"]})


@srv.tool(description="查单个任务：状态、进度、错误信息、产物绝对路径")
async def job_status(job_id: str) -> str:
    async with _client() as c:
        r = await c.get(f"/v1/jobs/{job_id}")
        if r.status_code != 200:
            return _error(r)
        return _dump(_brief(r.json()))


@srv.tool(description="列出最近的任务（按创建时间倒序），默认 20 条")
async def list_jobs(limit: int = 20) -> str:
    async with _client() as c:
        r = await c.get("/v1/jobs", params={"limit": limit})
        if r.status_code != 200:
            return _error(r)
        return _dump([_brief(j) for j in r.json()])


@srv.tool(description="阻塞等任务结束（succeeded/failed/cancelled），返回终态与产物路径；超时只返回当时的状态")
async def wait_job(job_id: str, timeout_sec: int = 600) -> str:
    deadline = time.monotonic() + max(5, timeout_sec)
    last: dict[str, Any] | None = None
    async with _client() as c:
        while True:
            r = await c.get(f"/v1/jobs/{job_id}")
            if r.status_code != 200:
                return _error(r)
            last = r.json()
            if last["state"] in {"succeeded", "failed", "cancelled"}:
                return _dump(_brief(last))
            if time.monotonic() > deadline:
                return _dump({**_brief(last), "note": "等待超时，任务仍在队列里"})
            await asyncio.sleep(_POLL_SEC)


@srv.tool(description="取消排队中或执行中的任务（已提交引擎的会顺手中断）")
async def cancel_job(job_id: str) -> str:
    async with _client() as c:
        r = await c.post(f"/v1/jobs/{job_id}/cancel")
        if r.status_code != 200:
            return _error(r)
        return _dump(_brief(r.json()))


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    """只留 Agent 用得上的字段，别把整条记录（含 60 条历史）灌进上下文。"""
    return {
        "job_id": job["id"],
        "capability": job["capability"],
        "variant": job["variant"],
        "state": job["state"],
        "progress": round(float(job.get("progress") or 0), 3),
        "error": job.get("error"),
        # 产物给绝对路径，Agent 可以直接读文件
        "artifacts": [
            {
                "kind": a["kind"],
                "path": a["path"],
                "width": a["width"],
                "height": a["height"],
                "duration": a["duration"],
            }
            for a in job.get("artifacts") or []
        ],
        "created_at": job["created_at"],
        "finished_at": job.get("finished_at"),
    }


def main() -> None:
    global _base_url
    parser = argparse.ArgumentParser(prog="mbai mcp", description="MCP 入口（stdio）")
    parser.add_argument("--url", default=None, help=f"平台地址，默认 {settings.api_url}")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse", "streamable-http"]
    )
    args = parser.parse_args()
    if args.url:
        _base_url = args.url
    srv.run(transport=args.transport)


if __name__ == "__main__":
    main()
