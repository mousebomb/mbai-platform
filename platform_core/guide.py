"""给调用方（尤其是 LLM / Agent）看的能力指南，走 `GET /llms.txt`。

为什么动态生成而不是仓库里放一个静态 llms.txt：
变体数量会变（现在 19 个，加一个就多一个）。静态文件必然和注册表漂移，
而漂移的文档比没有文档更糟——Agent 会照着它提交一个已经不存在的变体，
拿到 422，然后重试，再失败。这里每次请求都从 registry 现拼，永不脱节。

内容取舍：只写「怎么调」和「什么时候该选哪个」。具体参数不用在这里重复，
`GET /v1/capabilities` 才是结构化真相（带 type/default/min/max/options）。
"""

from __future__ import annotations

from .registry import registry

_GUIDANCE_LABELS = {
    "when_to_use": "什么时候用",
    "avoid_when": "什么时候别用",
    "cost": "代价",
}


def _render_variant(v: dict) -> list[str]:
    lines: list[str] = [f"### {v['capability']}/{v['variant']}", ""]
    if v.get("title"):
        lines.append(f"**{v['title']}**")
        lines.append("")
    if v.get("description"):
        lines.append(v["description"].strip())
        lines.append("")
    guidance = v.get("guidance") or {}
    if guidance:
        for key, label in _GUIDANCE_LABELS.items():
            value = guidance.get(key)
            if value:
                lines.append(f"- {label}：{' '.join(str(value).split())}")
        lines.append("")
    inputs = v.get("inputs") or {}
    if inputs:
        # 只列参数名和类型，默认值/范围以 /v1/capabilities 为准
        parts = []
        for name, spec in inputs.items():
            kind = spec.get("type", "string")
            mark = "*" if spec.get("required") else ""
            multi = "[]" if spec.get("multiple") else ""
            parts.append(f"`{name}{mark}: {kind}{multi}`")
        lines.append("参数：" + "、".join(parts))
        lines.append("")
    return lines


def render_llms_txt() -> str:
    """拼出整份指南。顺序稳定（能力名字典序），便于 diff。"""
    out: list[str] = [
        "# MBAI Platform",
        "",
        "> 局域网内的本地推理平台。图像生成/编辑/抠图/放大、语音合成/克隆/音色插值、"
        "语音转写、视频生成，全部走同一个任务队列。",
        "",
        "## 怎么调",
        "",
        "1. `GET /v1/capabilities` —— 所有能力与变体，含每个参数的 type/default/min/max/options。"
        "**这是选定参数的唯一依据**，本文只是选型指南。",
        "2. `POST /v1/assets` —— 需要图片/音频作为输入时先上传（multipart 字段 `file`），"
        "拿到 `id` 后作为参数值传回去。",
        "3. `POST /v1/jobs` —— 提交任务，返回 202 与 job 对象。"
        "参数非法会当场 422，不会排队到一半才失败。",
        "4. `GET /v1/jobs/{id}` 轮询，或 `GET /v1/jobs/{id}/events` 用 SSE 等推送。",
        "5. `GET /v1/jobs/{id}/file/{filename}` 取产物。文本产物是 `.jsonl`/`.txt`，"
        "用 `application/x-ndjson` / `text/plain` 直接返回，不是下载附件。",
        "",
        "任务状态：`queued → running → succeeded | failed | cancelled`。"
        "失败原因在 `error` 字段；`progress` 是 0~0.99 的实时值（CLI 引擎只有 0/1）。",
        "",
    ]

    variants = [v.info().model_dump() for v in registry.list_variants()]
    by_cap: dict[str, list[dict]] = {}
    for v in variants:
        by_cap.setdefault(v["capability"], []).append(v)

    for cap in sorted(by_cap):
        group = sorted(by_cap[cap], key=lambda x: x["variant"])
        out.append(f"## {cap}")
        out.append("")
        for v in group:
            out.extend(_render_variant(v))

    out.extend(
        [
            "## 几条通用约定",
            "",
            "- **同一能力的多个变体是「实现不同」，不是「质量不同」**。按 guidance 里的边界选，"
            "不要默认挑名字最长或带 batch 的那个。",
            "- **模型文件按角色绑定**，换量化档不改调用方参数。",
            "- **参数名写错会被直接拒绝**（未知参数 422），不会静默忽略。",
            "- **音色名必须存在于本机音色目录**，拼错当场拒绝，不会白跑一次合成。",
            "- **CLI 引擎不依赖 ComfyUI 在线**；只有 `engine: comfy` 的变体需要引擎起来。",
            "",
            "## 平台自身的限制",
            "",
            "- 单 worker 串行，一次只跑一个任务。",
            "- 无鉴权，仅限局域网，不要映射到公网。",
            "- 产物默认不自动清理（GC 需要手动跑），所以历史结果会一直在。",
            "",
        ]
    )
    return "\n".join(out) + "\n"
