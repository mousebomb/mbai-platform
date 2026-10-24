"""模型池目录扫描：给 `checkpoint` / `lora` 这类「运行时选文件」的参数提供下拉候选。

和 voices.py 一个思路：清单每次现读，往目录里丢文件、刷新页面就能选到。

两个和音色不同的点：

1. 扫的是模型池（`models_dir` + 只读挂载），并且**按子目录过滤**（`options_from:
   checkpoints/sd15`）。模型池里混着 Qwen / Wan 的全量检查点，不隔离的话下拉里混进
   26 GB 的异族模型，选错一次就是几十秒白跑。
2. 返回的是**相对路径**（`sd15\aaa.safetensors`），不是 basename。ComfyUI 的加载器按
   相对路径列文件名，注入时必须逐字符一致（分隔符也算），否则提交直接 400。
"""

from __future__ import annotations

from pathlib import Path

from .settings import settings
from .voices import list_voices

# ComfyUI 认的权重后缀；`.ckpt` 是 pickle，能列出来但不建议用（加载侧有兼容与安全坑）
SUFFIXES = {".safetensors", ".ckpt", ".pt", ".bin"}


def _scan(role: str) -> list[str]:
    """扫一个角色目录，返回相对路径清单；同名（不区分大小写）先到先得。

    `role` 可以是 `checkpoints/sd15`：只扫子目录，但返回值仍相对 `checkpoints` 根，
    因为 ComfyUI 加载器认的就是那个相对路径（`sd15\\aaa.safetensors`）。

    分隔符必须跟引擎一致（Windows 上是反斜杠）：ComfyUI 提交时按 `value_not_in_list`
    严格比对候选清单，`sd15/aaa` 和 `sd15\\aaa` 是两个不同的字符串。
    """
    top, _, sub = role.partition("/")
    found: dict[str, str] = {}
    for base in [settings.models_dir, *settings.extra_model_dirs]:
        root = Path(base) / top
        scope = root / sub if sub else root
        if not scope.is_dir():
            continue
        for path in scope.rglob("*"):
            if path.suffix.lower() not in SUFFIXES or not path.is_file():
                continue
            rel = str(path.relative_to(root))
            found.setdefault(rel.lower(), rel)
    return sorted(found.values(), key=str.lower)


def list_checkpoints(role: str = "checkpoints") -> list[str]:
    return _scan(role)


def list_loras(role: str = "loras") -> list[str]:
    return _scan(role)


def _find(items: list[str], name: str) -> str | None:
    key = name.lower()
    for item in items:
        if item.lower() == key:
            return item
    return None


def find_checkpoint(name: str, role: str = "checkpoints") -> str | None:
    return _find(list_checkpoints(role), name) if name else None


def find_lora(name: str, role: str = "loras") -> str | None:
    return _find(list_loras(role), name) if name else None


def resolve_options(inputs: dict) -> dict:
    """把 `options_from` 展开成具体候选值，供 API 下发给前端。

    `options_from` 的写法：`voices` / `checkpoints` / `checkpoints/sd15` / `loras` /
    `loras/sd15`，斜杠后面是模型池里的子目录，用来把候选缩到一族模型。

    清单里的默认值如果不在候选里（比如换了机器），退到第一个，避免下拉空白；
    但 `default: ""` 是显式声明「不使用」（LoRA 可选项），空串本身就在候选里，不会被顶掉。
    """
    out: dict = {}
    for key, spec in inputs.items():
        spec = dict(spec)
        source = spec.get("options_from")
        if not source:
            out[key] = spec
            continue
        kind, _, sub = source.partition("/")
        role = f"{kind}/{sub}" if sub else kind
        if kind == "voices":
            names = [v.name for v in list_voices()]
        elif kind == "checkpoints":
            names = list_checkpoints(role)
        elif kind == "loras":
            # 空串排第一：不用 LoRA 是最常用的选择
            names = ["", *list_loras(role)]
        else:
            out[key] = spec
            continue
        spec["options"] = names
        if names and spec.get("default") not in names:
            spec["default"] = names[0]
        out[key] = spec
    return out
