"""配置：路径与引擎地址全部走环境变量，默认值只保证「能 clone、能起服务」。

跨机器约定：
- 所有可变的路径都写在仓库根目录的 `.env` 里（已被 git 忽略），模板见 `.env.example`
- `data_dir` 默认跟着仓库走（root/data），clone 到任何位置都成立
- `models_dir` 默认 `~/ai-models`，是本机的模型热池，**必须按实际情况改**
- 环境变量前缀 `PLATFORM_`；列表型用 JSON，例如
  `PLATFORM_EXTRA_MODEL_DIRS='["F:/AI/ComfyUI/models"]'`
- 外部引擎解释器（CosyVoice / FramePack 等）默认不设：没有配置时，
  对应变体在启动前直接报错，不会静默跑一个空路径
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录，从本文件位置推出来，不写死盘符
ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # env_file 钉在仓库根：从任何工作目录启动都读同一份 .env
    model_config = SettingsConfigDict(
        env_prefix="PLATFORM_",
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    root: Path = ROOT
    # 热模型池：ComfyUI 的目录约定，权重可就地引用
    models_dir: Path = Path.home() / "ai-models"
    # 产物/SQLite：默认随仓库走，保证 clone 到别的机器不用改
    data_dir: Path = ROOT / "data"
    # 需要额外挂载的现存权重目录（只读引用，不复制）
    extra_model_dirs: list[Path] = []
    # 本地 CLI 工具根目录：scripts/download-tools.ps1 把 realesrgan 之类放这里
    tools_dir: Path = ROOT / "tools"

    comfy_url: str = "http://127.0.0.1:8188"
    # 平台自己的 HTTP 接口：MCP 入口（mbai mcp）当客户端连它
    api_url: str = "http://127.0.0.1:8000"
    # GPU 单卡，串行执行；将来多卡/多引擎再调整
    worker_concurrency: int = 1

    # ---- 外部引擎：各自独立的 Python 环境，路径因机器而异，只能写 .env ----
    # 未配置（None）时，用到该引擎的变体会在启动进程前报错并提示配置哪个变量。
    # CosyVoice：torch 2.3.1+cu121，跟平台的 cu130 不能混用同一个解释器。
    # 这四项的还原由 scripts\bootstrap-cosyvoice.ps1 负责（建环境 + 下模型 + 写 .env）
    cosyvoice_python: Path | None = None
    cosyvoice_home: Path | None = None
    cosyvoice_model: Path | None = None
    # 音色目录：<名字>.wav + <名字>.txt（参考文本），文件名即音色名
    cosyvoice_voices: Path | None = None
    # 音色登记表：`名字|wav路径|参考文本`；没有这个文件就只扫目录
    cosyvoice_config: Path | None = None
    # ---- CosyVoice 3.0：与 2.0 并存，不覆盖 ----
    # 分工：3.0 只做 TTS（发音更准、支持拼音注音），2.0 保留做声纹插值（blend）。
    cosyvoice3_python: Path | None = None
    cosyvoice3_home: Path | None = None
    cosyvoice3_model: Path | None = None
    # ---- Qwen3-TTS VoiceDesign：只用来「造音色卡」，不参与念书 ----
    qwen3_tts_python: Path | None = None
    qwen3_tts_models: Path | None = None
    # FramePack：自带便携 Python（cu126 + torch 2.6），不装进平台环境
    framepack_python: Path | None = None
    framepack_home: Path | None = None
    # HF_HOME：模型缓存在它下面的 hub/ 里
    framepack_hf_home: Path | None = None

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "platform.db"

    def ensure_dirs(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
