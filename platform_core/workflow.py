"""工作流模板：JSON 里只留结构，参数通过 manifest 的 bindings 注入。

为什么不用字符串占位符：占位符一旦被写进 prompt 文本就会被误替换，
路径式绑定（"6.inputs.text" → prompt）没有这个歧义。
"""

from __future__ import annotations

import json
import random
from typing import Any

from .registry import VariantSpec, registry
from .voices import find, list_voices


def _check_input(name: str, spec: dict[str, Any], value: Any) -> Any:
    """按 manifest 里的 inputs 描述做最小校验，错误信息要说清哪个参数。"""
    kind = spec.get("type", "string")
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"参数 {name} 需要整数，收到 {value!r}")
        if "min" in spec and value < spec["min"]:
            raise ValueError(f"参数 {name} 不能小于 {spec['min']}")
        if "max" in spec and value > spec["max"]:
            raise ValueError(f"参数 {name} 不能大于 {spec['max']}")
        if spec.get("multiple_of") and value % spec["multiple_of"]:
            raise ValueError(f"参数 {name} 必须是 {spec['multiple_of']} 的倍数")
    elif kind == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"参数 {name} 需要数字，收到 {value!r}")
        value = float(value)
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"参数 {name} 需要字符串，收到 {value!r}")
    elif kind == "text":
        # 长文本（TTS 台词等）：前端用 textarea，后端只做字符串校验
        if not isinstance(value, str):
            raise ValueError(f"参数 {name} 需要字符串，收到 {value!r}")
    elif kind in {"image", "audio"}:
        # 值就是 asset_id 字符串；文件存在性在入队时（API）校验
        if not isinstance(value, str) or not value:
            raise ValueError(f"参数 {name} 需要素材 ID（字符串），收到 {value!r}")
    elif kind == "voice":
        # 音色名必须在音色目录里真实存在，避免拼错后白跑一次 40 秒的合成
        if not isinstance(value, str) or not value:
            raise ValueError(f"参数 {name} 需要音色名（字符串），收到 {value!r}")
        if find(value) is None:
            names = [v.name for v in list_voices()]
            raise ValueError(f"参数 {name} 的音色 {value!r} 不存在，可选: {names}")
    elif kind == "prompt":
        # 提示词可以是纯文本，也可以是 MOSS Layout JSON（结构化版式）
        if isinstance(value, str):
            pass
        elif isinstance(value, dict):
            if not isinstance(value.get("items"), list):
                raise ValueError(f"参数 {name} 的布局对象缺少 items 数组")
        else:
            raise ValueError(f"参数 {name} 需要字符串或布局对象，收到 {type(value).__name__}")
    return value


def resolve_params(spec: VariantSpec, params: dict[str, Any]) -> dict[str, Any]:
    unknown = set(params) - set(spec.inputs)
    if unknown:
        raise ValueError(f"未知参数: {', '.join(sorted(unknown))}")

    resolved: dict[str, Any] = {}
    for name, desc in spec.inputs.items():
        if name in params:
            value = params[name]
        elif "default" in desc:
            value = desc["default"]
        elif desc.get("required"):
            raise ValueError(f"缺少必填参数: {name}")
        else:
            continue
        resolved[name] = _check_input(name, desc, value)
    return resolved


def build_workflow(spec: VariantSpec, params: dict[str, Any]) -> dict[str, Any]:
    # CLI 引擎没有工作流，走 cli.command；这里只服务 comfy
    path = spec.workflow_path
    if path is None:
        raise ValueError(f"{spec.capability}/{spec.variant} 没有声明 workflow（CLI 引擎走 cli.command）")
    # utf-8-sig：Windows 上被记事本/PowerShell 写过的文件常带 BOM
    workflow = json.loads(path.read_text(encoding="utf-8-sig"))

    # 模型文件按「角色」注入：profile 能换文件而不动 manifest。
    # 只传 basename —— ComfyUI 的加载器只接受它自己目录列表里的文件名。
    for role, node_path in spec.model_bind.items():
        file_path = registry.resolve_file(spec.model_id, role)
        if file_path is None or not file_path.exists():
            raise ValueError(
                f"模型 {spec.model_id} 的角色 {role} 找不到文件；"
                f"检查 models/*.yaml 的 files 或 profiles/local.yaml"
            )
        _set_path(workflow, node_path, file_path.name)

    # seed 传 -1 表示随机；ComfyUI 的 seed 是 64 位整数
    if params.get("seed", 0) is not None and params.get("seed", 0) < 0:
        params["seed"] = random.randint(0, 2**53)

    merged = {**params, **spec.constants}
    for path, param_name in spec.bindings.items():
        if param_name not in merged:
            continue
        _set_path(workflow, path, merged[param_name])
    return workflow


def _set_path(workflow: dict[str, Any], path: str, value: Any) -> None:
    node_id, _, rest = path.partition(".")
    node = workflow.get(node_id)
    if node is None:
        raise KeyError(f"工作流里没有节点 {node_id}（绑定 {path}）")
    cursor: Any = node
    keys = rest.split(".")
    for k in keys[:-1]:
        cursor = cursor[k]
    # 目标是字符串而传入结构化对象（Layout JSON）时序列化，节点只认字符串
    if isinstance(cursor.get(keys[-1]), str) and isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cursor[keys[-1]] = value
