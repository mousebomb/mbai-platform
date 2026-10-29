# AGENTS.md

给接手这个仓库的 Agent 看。读完这页再动代码；踩过的坑都写在最后，别重复踩。

- 远程仓库：`git@github.com:mousebomb/mbai-platform.git`
- 目标机器：Windows + 单卡 RTX 3060 12GB，局域网访问
- 方案文档：`docs/platform-plan-v2.md`（有效）；v1 已作废，只在作者本机、不入库
- 接入细节与实测数据：`docs/moss-image-integration.md`
- 历史开发日志：在作者本地、不随仓库分发（所以文中偶尔出现 `devlog` 引用属正常）

---

## 1. 这个项目是什么

**ComfyUI 当模型运行时，本仓库只做控制平面**：能力注册表（YAML）、任务队列（SQLite）、产物库、引擎适配器，对外一个 HTTP 接口 + 一个网页。

一句话的分层：

```
浏览器 / Agent  ──HTTP──▶  FastAPI(8000)  ──队列──▶  worker  ──HTTP──▶  ComfyUI(8188)  ──▶  GPU
                                │                        │
                             SQLite                   产物落盘 data/artifacts/
```

代码不认识任何具体模型。加模型 = 加 YAML + workflow.json。

## 2. 铁律

违反其中任何一条，之前的实测结论就作废。

1. **不自研推理内核**。采样、VAE、文本编码全交给 ComfyUI；本仓库不写 torch 代码。
2. **新增能力不改 Python**。`capabilities/` 的 `manifest.yaml` + `workflow.json` 是唯一入口。
3. **torch 必须 cu130**。cu128 下 `comfy_kitchen` 的原生 int8/w4a8 算子被禁用退回 eager，MOSS-Image 从 1.5 s/it 掉到 6.2 s/it。
4. **永远不要给引擎加 `--lowvram`**。12GB 显存上直觉该加，实测慢 4 倍，DynamicVRAM 自己调度更快。
5. **ComfyUI 固定 commit `d07dff45`**。MOSS-Image 原生节点只进了 master 的某点之后，浮动分支会让节点消失。
6. **权重只在 `PLATFORM_MODELS_DIR`**（热池）。历史权重用 `engines/comfy/extra_model_paths.yaml` **只读挂载**，从不复制。
7. **不提交**权重、产物图、`.venv`、`node_modules`、`dist`、`.env`、`data/`。`.gitignore` 已覆盖，别用 `git add -f` 绕。
8. **杀进程要精确**。`Get-CimInstance ... | Stop-Process` 按命令行匹配会误伤同命令行的 PowerShell 包装进程，连自己的 shell 一起杀。
9. **中文/JSON 文件不要用 PowerShell `Set-Content` 写**（会变 GBK 乱码 + BOM）。要用工具直接写，读取统一 `encoding="utf-8-sig"`。
10. **Windows 控制台是 GBK**：脚本别输出 `✓/✗`，用 `[OK]`/`[!!]`。
11. **链接（junction / 硬链接 / 符号链接）只允许在本机、且只在被 gitignore 的目录里**（`engines/*/`、`data/`）。跨机器复用靠脚本重下，不靠链接传递，见 §4.1。

## 3. 目录

```
apps/api/main.py       FastAPI 入口（同时托管前端静态文件）
apps/web/              React + Vite 前端（产物 dist/ 不入库）
platform_core/         控制平面
  contracts.py         Job / Artifact / JobState / VariantInfo
  registry.py          扫 models/*.yaml 与 capabilities/*/*/*/manifest.yaml
  workflow.py          参数校验 + bindings 注入
  adapters/            引擎适配器层（控制平面只认这里的契约）
    base.py            Adapter(ABC) / EngineStatus / EngineState / AdapterError
    comfy.py           ComfyUI 实现（包 comfy_client.py 的 HTTP 传输）
    cli.py             本地命令行引擎（ffmpeg / realesrgan / CosyVoice 之类）
  media.py             产物时长解析（wav RIFF / mp4 mvhd，纯标准库）
  voices.py            音色清单（config.txt + 目录扫描），给 TTS 下拉用
  comfy_client.py      ComfyUI HTTP 传输（submit/history/cancel/download）
  store.py             SQLite 任务表
  worker.py            队列循环、轮询、产物收集、取消
  service.py           单例 store + worker
  settings.py          全部路径走 PLATFORM_ 环境变量
capabilities/<能力>/<变体>/<版本>/{manifest.yaml,workflow.json}
models/<id>.yaml       模型元数据（files/license/vram_profile），不含权重
docs/images/           README 里的界面截图（敏感文字已打码）
scripts/               引导、下载、启动、验证脚本
docs/                  方案与实测记录
engines/               ComfyUI 本体（clone，不入库）
data/                  SQLite + 产物 + 日志（不入库）
```

被忽略的目录**都是可重建的**，重建方式见下一节。

## 4. 从零到能跑

```powershell
# 0. 平台依赖
uv sync

# 1. 引擎：clone 固定 commit + 生成模型挂载 + 建 venv + 装 cu130 torch 与依赖
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-comfy.ps1

# 2. 权重：按 models/*.yaml 的 source 字段还原（约 18.5 GB，走 hf-mirror）
uv run python scripts\download-models.py

# 3. 前端
npm --prefix apps/web install
npm --prefix apps/web run build

# 4. 预检（注册表 / 模型文件 / 引擎 / MOSS 节点）
uv run mbai doctor
```

**可选：音频引擎（CosyVoice2）**。图片/视频之外的 `audio.tts` / `audio.voice_clone` /
`audio.voice_design` 三个变体都要它，独立 conda 环境 + 4.8 GB 模型：

```powershell
# clone 固定 commit + 建 conda 环境 + 装 torch cu121 与锁定依赖 + 下模型 + 写 .env
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-cosyvoice.ps1
```

装完 `mbai doctor` 不需要重启就能用（脚本会往 `.env` 写 `PLATFORM_COSYVOICE_*`）。
只要环境不要模型：`-SkipModels`；环境已经好了只重新对齐路径：`-SkipEnv -SkipModels`。

> **为什么别的引擎都用 uv、只有它要 conda**：CosyVoice 的文本正则化 → WeTextProcessing →
> pynini → openfst，而 pynini 在 Windows 上**没有 pip wheel**，只能 conda-forge。这是硬约束，
> 不是懒得统一。
>
> **模型必须钉 revision**：`9d5c22a8`（2024-12-13）。2025-04 之后模型仓库把 `cosyvoice.yaml`
> 改成 `cosyvoice2.yaml` 并换了 cache 版权重，需要更新的代码才认，而这里钉的 commit 是 2025-01 的。
> 踩坑全过程见日志仓库的 `2026-10-26_PC_cosyvoice-bootstrap.md`。

跑起来需要**两个进程**：

```powershell
# 窗口 A：引擎（127.0.0.1:8188）
powershell -ExecutionPolicy Bypass -File scripts\start-engine.ps1

# 窗口 B：平台（网页 + API 同一个端口）
uv run mbai serve --host 0.0.0.0 --port 8000
```

局域网地址：`http://<本机IP>:8000`。

### 换机器时要改的东西

**只有仓库根目录的 `.env`**（模板 `.env.example`，已被 git 忽略）。`settings.py` 的默认值只保证 clone 下来能起服务，不含任何人的盘符：

| 设置 | 默认 | 环境变量 |
|---|---|---|
| 模型热池 | `~/ai-models` | `PLATFORM_MODELS_DIR` |
| 数据目录 | `<仓库>/data`（跟着仓库走） | `PLATFORM_DATA_DIR` |
| 只读挂载 | `[]` | `PLATFORM_EXTRA_MODEL_DIRS`（JSON 数组） |
| 本地 CLI 工具 | `<仓库>/tools` | `PLATFORM_TOOLS_DIR` |
| 引擎地址 | `http://127.0.0.1:8188` | `PLATFORM_COMFY_URL` |

列表型环境变量要写 JSON：

```powershell
$env:PLATFORM_MODELS_DIR = 'E:\models'
$env:PLATFORM_EXTRA_MODEL_DIRS = '["F:\AI\ComfyUI\models","G:\old-models"]'
```

> `data_dir` 默认是 `<仓库>/data`，clone 到任何盘都不用改；只有模型池位置需要按机器指定。

## 4.1 跨机器还原与链接（junction / 硬链接 / 符号链接）

**原则：仓库里只放「怎么重建」，不放「重建出来的东西」。**

| 方式 | 指本地其他盘 | 指网络 UNC | 需要管理员 | 跨机器复用 |
|---|---|---|---|---|
| 硬链接 | ❌ 只能同卷 | ❌ | 否 | ❌ |
| junction（`mklink /J`） | ✅ | ❌ | 否 | ❌ |
| 符号链接（`mklink /D`） | ✅ | ✅ | 是 / 开发者模式 | ❌ |
| git 记录 | — | — | — | **不记录任何 reparse point** |

规则：

1. **链接只允许存在于本机**，并且只能落在**已被 gitignore 的目录**里：`engines/*/`、`data/`。
   禁止放进 `models/`、`platform_core/`、`apps/` 等被跟踪的位置。
2. **原因**：git 不存 reparse point，别的机器 clone 下来什么都没有；而 Git for Windows 会把 junction
   当**普通目录递归**，一旦落在跟踪路径里，`git add .` 会把整个权重池塞进仓库。
3. **跨机器复用只能靠三件套**：清单（`models/*.yaml` 的 `files` + `source`）、脚本
   （`bootstrap-comfy.ps1` / `download-models.py`）、参数化路径（`-ModelsDir` / `PLATFORM_MODELS_DIR`）。
4. **同机多引擎共享权重**，优先用引擎自己的多目录配置（ComfyUI 的 `extra_model_paths.yaml`，由
   `bootstrap-comfy.ps1` 生成）；只有引擎不支持多目录时才退到 junction，且 **脚本入库、链接不入库**。
5. 需要跨设备共享同一份权重的唯一现实做法是 SMB 共享 + **符号链接**（junction 指不了 UNC），
   代价是首次加载走网络；这条不在仓库里体现，属于各机器自己的部署选择。

### 新机器还原整套权重的验收口径

```powershell
git clone git@github.com:mousebomb/mbai-platform.git; cd mbai-platform
uv sync
$env:PLATFORM_MODELS_DIR = 'E:\models'                    # 允许与源机器不同位置
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-comfy.ps1 -ModelsDir $env:PLATFORM_MODELS_DIR
uv run python scripts\download-models.py                   # 缺什么下什么，下完自检
npm --prefix apps/web install; npm --prefix apps/web run build
uv run mbai doctor
```

判定标准：`download-models.py --check` 退出码 0，且 `mbai doctor` 没有任何 `[!!]`。

## 4.2 模型绑定与 profile（换权重不动代码）

**变体只声明「要哪些角色」，不声明文件名。** 具体文件由本机 profile 决定：

```
优先级：profiles/local.yaml  →  profiles/default.yaml  →  models/*.yaml 的 files
```

`manifest.yaml` 里用 `model_bind` 把角色绑到工作流字段：

```yaml
model_bind:
  diffusion_model: "1.inputs.unet_name"
  text_encoder: "3.inputs.clip_name"
  vae: "8.inputs.vae_name"
```

`models/*.yaml` 里用 `files` 声明角色 → 默认相对路径：

```yaml
files:
  diffusion_model: diffusion_models/z_image_turbo_int8_convrot.safetensors
kind: quantized          # upstream / quantized / merged / local
lineage: {base: z_image_turbo_bf16, quant: int8_convrot}
```

`profiles/local.yaml`（**不入库**）覆盖任意角色：

```yaml
z-image-turbo:
  diffusion_model: diffusion_models/zImageTurboQuantized_fp8E4m3fn.safetensors
```

换量化档、换自己 merge 的模型 = 改这一行。变体、工作流、前端、Agent 一行不动。

三条配套规则：

1. **文件必须在引擎扫得到的目录里**（扩散模型 `diffusion_models/`、检查点 `checkpoints/`、编码器 `text_encoders/`、VAE `vae/`），
   否则 ComfyUI 的加载器根本看不见。别的盘的文件用 `scripts/link-local-models.ps1` 建**文件符号链接**，不要复制。
2. **`kind` 要诚实**：`merged` 的模型（社区 merge、魔改）写 `restorable: false`，`doctor` 会标「需自备」——
   不要给它编 `source`，编了就是骗下一个人。
3. **不同档位要对比**就新建 `model_id` + 变体（产物里的 `model_id` 是溯源依据）；只是「我这儿只有这个版本」用 profile。

`doctor` 现在按模型输出：`kind`、能否还原、每个角色的实际路径和绑定来源（`model` / `profile`）。

### 带输入图的能力（image.edit / image.img2img / image.inpaint）

`type: image` 的参数在 worker 里被解析成本机绝对路径，`comfy` 适配器提交前会把它**上传到引擎的
`input/` 目录**并换成文件名——因为 `LoadImage` 只认自己目录里的文件名，不认绝对路径。
判定条件很窄（字符串 + 绝对路径 + 文件存在），普通文本参数不会误伤。

两类容易踩的点：

1. **尺寸要发给模型的不是原图尺寸**。`z-image-turbo-i2i` 前面挂了 `ImageScaleToTotalPixels`
   （`resolution_steps: 32`），否则 1000×750 这种图在 `VAEEncode` 那步尺寸不整除。
2. **inpaint 的蒙版必须和原图同尺寸**。前端画布按原图像素尺寸建，所以天然对齐；
   外部传进来的蒙版尺寸不一致时 ComfyUI 会插值，边界会糊。

### 运行时选权重（SD1.5 检查点 + LoRA 下拉）

`image.txt2img/sd15-community` 和别的变体不一样：它不靠 `model_bind` 绑定固定文件，
而是靠参数 `checkpoint` / `lora` 在运行时选，候选由模型池目录现扫（`platform_core/pool.py`）。

```
<PLATFORM_MODELS_DIR>\checkpoints\sd15\   ← scripts\link-local-models.ps1 从 A1111 建的符号链接
<PLATFORM_MODELS_DIR>\loras\sd15\         ← 同上（整目录镜像）
```

三条要点：

1. **必须放进子目录**。模型池里还有 Qwen / Wan 的全量检查点，混在一个目录里下拉就会选错；
   `options_from: checkpoints/sd15` 把候选锁在子目录，同时引擎列出的名字是 `sd15\xxx`。
2. **链接是单文件符号链接**，源目录（A1111）完全不动、不复制、不占空间。断链了重跑脚本即可；
   换机器重跑脚本时源路径不存在就跳过，不会报错中断。
3. **LoRA 不选就摘节点**。`lora` 空串时 `optional_nodes` 把 `LoraLoader` 从图里移除，
   不是塞一个空文件名（ComfyUI 的 `lora_name` 不接受空串）。

删掉不想要的检查点/ LoRA：直接删 `<PLATFORM_MODELS_DIR>\checkpoints\sd15` 或 `loras\sd15` 下的链接，
刷新页面候选就少了，不用动任何 YAML。

### 音频引擎为什么走 `cli` 而不是常驻服务（CosyVoice）

CosyVoice 有官方 FastAPI 服务，但**没接**：它的环境是 torch 2.3.1+cu121，与平台的 cu130 冲突；
常驻还要在 12 GB 卡上一直占 3~4 GB，跟 FramePack 抢。所以做成一次性 CLI：

```
platform_cli → {cosyvoice_python} scripts\engines\cosyvoice_tts.py → 加载模型 → 合成 → 写 wav → 退出
```

代价是每次多付约 35 秒模型加载（合成本身 5 秒左右），换来的是「任务跑完显存立刻还回去」。
真嫌慢再改常驻 + 空闲卸载，那时要同时处理它的卸载时机。

音色清单由 `platform_core/voices.py` 统一解析，**两个来源按名字去重**：

1. `cosyvoice_config`（就是老项目的 `PLATFORM_COSYVOICE_CONFIG` 指向的文件）：`名字|wav路径|参考文本`，
   路径可以指向任意位置，相对路径按 config 所在目录解析
2. 音色目录 `cosyvoice_voices` 下成对的 `<名字>.wav` + `<名字>.txt`

只收录「wav 存在 + 有参考文本」的——缺参考文本的零样本合成会跑偏，
出现在下拉里比不出现更糟（比如 `laurel1.wav` 没有 txt，靠 config.txt 登记才出现）。
清单**每次请求现读**：往目录里丢一个 wav+txt，刷新页面就能选到。

CosyVoice2-0.5B **没有内置音色**（缺 `spk2info.pt`），只能零样本，
所以「音色」永远是「某个参考音频 + 它的逐字文本」。
**怎么加音色、参考音频有什么要求，见 README 的「配置音色（语音合成）」一节**——
那是给人看的操作说明，这里只记实现约束。

### 音色插值为什么能只加两个文件（`audio.voice_design/blend`）

需求来自作者本地沙盒的实验结论（沙盒不入库、也不被本仓库 import）：
两段参考音频 → 声纹向量 slerp → 一个新音色。接入方式是**纯加文件**：
`capabilities/audio.voice_design/blend/v1/manifest.yaml` + `scripts/engines/cosyvoice_blend.py`，
`platform_core` 一行没动。

能零改动的原因：两个 `audio` 参数在平台侧本来就是通用能力——worker 按 `spec.inputs`
逐个把 asset_id 解析成绝对路径（`worker.py` 第 54 行那个循环），CLI 适配器按 `cli.command`
逐个渲染占位符，前端按 `inputs` 逐个生成上传控件。**不是为这个能力特批的。**

四条必须记住的约束：

1. **策略不能改回「只换 embedding」**。实测只换 `flow_embedding` 时 F0 跨度只有 4 Hz，
   音色被 prompt token 压制；现在是「两端 prompt 拼接 + 等长截断」，F0 跨度 101 Hz。
2. **ratio 的听感中点不是 0.5**，是 0.38~0.40（F0 校准出来的，两端 132 Hz / 233 Hz）。
   别按直觉改默认值。
3. **它调的是 CosyVoice 内部接口**：`frontend_zero_shot()` 的返回结构、
   `_extract_spk_embedding()`、`model.tts(**model_input)`。升级 CosyVoice 仓库
   可能改签名，届时先看这里，别怀疑平台。
4. **中间态不稳**是声纹空间的固有性质（t=0.5 是 210 Hz、t=0.6 掉回 137 Hz），
   不是 bug。要稳定就把某一档合成一次、固化进音色目录，别每次重新插值。

`float` 类型在入队时只转数字、**不校验 min/max**（`workflow._check_input` 只对 int 校验），
所以 `ratio` 越界由引擎脚本自己拦。

### 情感指令（`audio.tts/cosyvoice2-instruct`）

`CosyVoice2.inference_instruct2()` 是 zero_shot 的变体：把 `instruct + <|endofprompt|>`
放进 `prompt_text`，然后**删掉 llm 侧 prompt token**——不让参考音频的语调压住指令。
所以它**不需要参考文本**，音色仍来自参考音频，语气来自指令。

实测（同一句 × 三个指令）：F0 跨度 63.7 Hz，语速差近 2 倍，方向符合直觉
（兴奋最快最响、疲惫最低最轻）。

`instruct` 是自由文本，不是枚举——指令空间本来就包含情绪/语速/方言/年龄，
枚举会把模型锁死，而"这句用什么情绪"是内容判断，平台不该替调用方做。
**平台只提供能力，情绪由调用方（人或多模态 Agent）决定。**

### 音色放哪、什么不进库

音色目录与登记表都在仓库外（由 `.env` 的 `PLATFORM_COSYVOICE_VOICES` / `_CONFIG` 指定），**自建音色不随仓库走**。
比如 blend 出来的 `合成1dcvj_t0.45`：它是插值产物，没有下载源，换机器必须手工拷贝
wav + 逐字一致的 txt。`doctor` 也查不到它（不在注册表里）。

### FramePack 为什么也是 `cli`（视频）

FramePack 是个 Gradio 应用，但它把推理都放在 `diffusers_helper/` 里，所以接入方式是
**把官方 `demo_gradio.py` 的 worker 循环原样搬成脚本**（`scripts/engines/framepack_i2v.py`），
去掉 UI，采样仍然调用它自己的 `sample_hunyuan`——不自研推理。

三个必须知道的点：

1. **不要传 HF repo id，要传本地缓存目录。** 分片模型走 repo id 时 diffusers 会调
   `model_info()` 确认分片清单，离线直接 `OfflineModeIsEnabled`。脚本里 `_snapshot()`
   把 `models--org--name/snapshots/<rev>` 解析出来再喂给 `from_pretrained`。
2. **它和 ComfyUI 抢显存。** 12 GB 卡上 ComfyUI 空闲也占 8 GB+，FramePack 要 10 GB+，
   所以 manifest 里写 `cli.free_comfy_vram: true`，适配器在启动进程前先
   `POST /free {"unload_models": true, "free_memory": true}`（`platform_core/resources.py`）。
3. **速度是分钟级。** 实测 2 秒片（2 节 × 25 步）约 14 分钟，1 秒片（1 节 × 10 步）约 6 分钟
   （其中模型加载 ~2 分钟）。`timeout_sec` 给了 3600。

## 4.3 两个 CosyVoice 并存（2.0 做 blend，3.0 做 TTS）

| 用途 | 引擎 | conda 环境 | 变体 |
|---|---|---|---|
| 声纹插值 | CosyVoice2 | `cosyvoice` | `audio.voice_design/blend` |
| 凭空造音色 | Qwen3-TTS VoiceDesign | `qwen3tts` | `audio.voice_design/qwen3-voice-design` |
| 长文朗读 | CosyVoice3 | `cosyvoice3` | `audio.tts/cosyvoice3-segmented` |
| 存量朗读 | CosyVoice2 | `cosyvoice` | `audio.tts/cosyvoice2*` |

- **不需要新增适配器**：`cli` 适配器只负责起进程，用哪个模型是命令里的路径问题。
  引擎由 manifest 的占位符选：`{cosyvoice_python}` 或 `{cosyvoice3_python}`（settings 提供）。
- 两边是**不同仓库 + 不同 conda 环境**，互不覆盖；换机器用 `.env` 覆盖
  `PLATFORM_COSYVOICE3_PYTHON` / `_HOME` / `_MODEL`。
- **桥是「声音卡」**：blend 产出固化成 `ref.wav + .txt`，3.0 当普通参考音频用。
  所以换 TTS 引擎不用重做 blend（今天 v3 批次里的 `f_stu_2` 就是 2.0 blend 出来的卡 + 3.0 渲染）。
- **3.0 的注音是替换语义**：`微[x][ūn]` 对；`微醺[x][ūn]` 会把原字和注音各念一遍（「微熏熏」）；
  `[xi][ūn]` 也不行——x 组韵母是 ü，声母只写 `x`。
- **坑**：3.0 的指令必须走 `inference_instruct2`；把指令文案塞进 `inference_zero_shot` 的
  `prompt_text` 会因 prompt 文本与语音 token 错位而输出整段乱码。
- **还没迁移**：3.0 的 blend 没标定（`CosyVoice3` 继承 `CosyVoice2`，`frontend_zero_shot` 同签名，
  理论上可移植，但插值中点/变暗行为要重测）；3.0 没有入库的还原脚本，
  `models/cosyvoice3-0.5b.yaml` 标了 `restorable: false`（手工步骤写在 notes 里）。

### VoiceDesign 造卡的两个硬约束

1. **产物是声音卡，不是 embedding**：`output.wav` + 同目录 `output.txt`（逐字文本）。
   复制成 `<名字>.wav` / `<名字>.txt` 放进音色目录，CosyVoice3 就能直接念。
2. **描述里的性别/音高不可靠，必须量化复核**。实测同一批描述：
   「19 岁男生，清亮干净的少年音」→ F0 **251 Hz**（生成成女声）；
   「19 岁女生，音调偏高」→ **341 Hz**（童声）。改成「男性，音高偏低，胸腔共鸣」→ 172 Hz、
   「成年女性，音高中等偏低（不是童声）」→ 259 Hz 才正常。
   所以造完卡必须量一次 F0，男生 < 190 Hz、女主 240~280 Hz 才算落在预期区间。
3. **它不读任何参考音频**：`generate_voice_design(text, language, instruct)` 只有一个文字描述，
   和音色库里的真人素材无关。带真人来源的只有 `blend`（2.0 声纹插值）那几个音色——
   被问到「这声音是不是用我录的素材做的」时，答案是「不是」。

## 5. 契约

### 5.1 HTTP

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 引擎状态、注册表错误、缺模型、变体数 |
| GET | `/v1/capabilities` | 所有变体及 `inputs` 元数据（前端据此生成表单） |
| POST | `/v1/jobs` | 入队，**202**；参数非法当场 422（不等执行到一半才炸） |
| GET | `/v1/jobs?limit=&state=` | 列表，按创建时间倒序 |
| GET | `/v1/jobs/{id}` | 详情 |
| POST | `/v1/jobs/{id}/cancel` | 取消；已提交引擎的会顺手中断 |
| DELETE | `/v1/jobs/{id}` | 删除任务记录 + 产物目录，**204**；非终态返回 409 |
| GET | `/v1/jobs/{id}/file/{filename}` | 取产物，只允许任务目录下的直接子文件 |
| POST | `/v1/assets` | 上传输入图（multipart 字段 `file`，png/jpg/jpeg/webp ≤ 20 MB），返回 asset_id + 宽高 |
| GET | `/v1/assets/{asset_id}` | 取回素材（前端预览用） |

另外 `/` 挂的是前端构建产物（`apps/web/dist`），所以网页和 API 同源同端口，没有 CORS 问题。

### 5.2 任务状态机

`queued → running → succeeded | failed | cancelled`

- 进程启动时 `requeue_running()` 会把上次被杀的 `running` 任务**放回队列**（不是直接标失败）
- 用户取消后，执行路径的异常**不能**把状态覆盖成 `failed`（`worker._execute` 里有显式判断）
- 删除只允许终态：排队/执行中删掉后执行器仍会回写该任务，变成「删了又冒出来」的幽灵记录，所以返回 409 让用户先取消
- `progress` 是 0~0.99 的实时值：comfy 适配器另起一个 websocket watcher 按 `progress` 消息折算，
  连不上就降级为 0（不影响执行）；CLI 引擎仍只有 0/1

### 5.3 manifest.yaml

```yaml
capability: image.txt2img      # 能力，点号分隔
variant: moss-image-sft        # 变体，同一能力的某个实现
version: v1
model_id: moss-image-sft-int8  # 必须存在于 models/*.yaml
workflow: workflow.json        # 同目录下的 ComfyUI API 格式工作流（cli 引擎可省略）
engine: comfy                  # 引擎适配器 kind，见 platform_core/adapters/；默认 comfy
inputs:                        # 对外暴露的参数，前端据此生成表单
  prompt: {type: prompt, required: true}
  steps:  {type: int, default: 25, min: 8, max: 100}
bindings:                      # "节点ID.inputs.字段" → 参数名（只对 comfy 有意义）
  "7.inputs.steps": steps
constants:                     # 固定注入，不对外暴露
  batch_size: 1
```

CLI 引擎的变体不写 `workflow`，改在 `cli` 块里声明命令模板：

```yaml
engine: cli
cli:
  command: ["{tools_dir}\\realesrgan\\realesrgan-ncnn-vulkan.exe",
            "-i", "{image}", "-o", "{out}", "-n", "{model_name}", "-s", "{scale}"]
  timeout_sec: 900
  output_ext: png
inputs:
  image: {type: image, required: true}   # 值 = asset_id，worker 解析成绝对路径
  scale: {type: int, default: 4, min: 2, max: 4}
constants:
  model_name: realesr-animevideov3
```

占位符取值顺序是 `settings` 字段打底（引擎路径因机器而异，manifest 里不写死）、`constants` 覆盖、
`params` 最高优先级，另有 `{out}`（产物路径）和 `{tools_dir}` 两个内置；
替换不到的占位符会在启动前直接报错，不会静默传空串。

外部引擎的解释器路径都在 `settings.py` 里（`cosyvoice_python` / `framepack_python` 等），
换机器用 `.env` 覆盖，例如 `PLATFORM_COSYVOICE_HOME=D:/engines/CosyVoice/CosyVoice`。

`inputs.type` 支持 `string` / `int` / `float` / `prompt` / `text` / `image` / `audio` / `voice` / `mask` /
`checkpoint` / `lora`，
可选 `required`、`default`、`min`、`max`、`multiple_of`、`options_from`。校验在 `workflow._check_input`，
**未知参数直接拒绝**（防前端塞字段改工作流）。`image` / `audio` / `mask` 的值是 `asset_id`，
worker 执行前解析成绝对路径；上传白名单见 `assets.ASSET_EXTS`。

`voice` 类型的值是音色名，必须能在这个机器的音色清单里找到（拼错直接拒绝，不白跑 40 秒）。
CLI 适配器会把它展开成两个占位符：`{voice:wav}`（参考音频绝对路径）和 `{voice:prompt}`（参考文本），
所以脚本只认路径和文本，不重复实现音色发现。
`options_from: voices` 表示候选值由 `/v1/capabilities` 在请求时注入，前端据此渲染成下拉。

`checkpoint` / `lora` 是「运行时选权重」：候选值来自模型池目录（`platform_core/pool.py`），
`options_from: checkpoints/sd15` 这种写法把候选缩到一个子目录，避免和 Qwen / Wan 的全量检查点混在一起。
值必须与引擎列出的文件名**逐字符一致**（Windows 上是 `sd15\xxx.safetensors`，斜杠打反就 `value_not_in_list` 400）。
`lora` 允许空串 = 不挂 LoRA，此时 `optional_nodes` 会把对应节点从图里摘掉（见 §5.4）。

`mask` 是 `image.edit` 之外唯一需要画布的输入：值是**黑白 PNG**的 asset_id（白 = 要重绘），
前端渲染成涂抹画布（`apps/web/src/MaskEditor.tsx`），CLI/Agent 直接上传一张 PNG 就行。
它和 `image` 走完全相同的素材链路，后端只多认一个类型名。

`cli.output_ext` 决定产物扩展名，产物 `kind` 由扩展名反推（`media.KIND_BY_SUFFIX`）：
png/webp → image，wav/mp3/flac/ogg/m4a → audio，mp4/webm/mov → video。音视频时长从文件头读，不信参数。

### 5.4 workflow.json

必须是 **ComfyUI 的 API 格式**（`{节点ID: {class_type, inputs}}`），不是 UI 导出的整包 frontend 格式。从跑通的 ComfyUI 里导出，不要手写猜节点。

可选的 `optional_nodes` 让「参数为空就不用这个节点」成立（目前只有 LoRA 用到）：

```yaml
optional_nodes:
  lora:
    node: "8"
    rewires:
      - ["8", 0, "1", 0]   # 原 ["8",0] 改指 ["1",0]（MODEL）
      - ["8", 1, "1", 1]   # 原 ["8",1] 改指 ["1",1]（CLIP）
```

摘节点的时机在 `bindings` 之后（先按常规注入值，再整体移除）。`rewires` 必须显式写，
**不要按「输入声明顺序推输出槽位」**——猜错不会报错，只会静默出一张错图。

### 5.5 prompt 的两种形态

`type: prompt` 既接受普通字符串，也接受 **MOSS Layout JSON 对象**：

```json
{"summary": {"caption": "..."},
 "items": [{"label": "text", "text": "NOCTURNE", "bbox": [-470, -420, -330, 420]}],
 "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
```

- bbox 以**画布中心为原点**、像素为单位，顺序 `yxyx`
- 注入时 `_set_path` 发现目标字段是字符串 → 自动 `json.dumps`（节点只认字符串）
- 不指定 bbox 的小字会糊/乱码，这是模型行为，别当 bug 修

### 5.6 前端

- 不写死表单：读 `/v1/capabilities` 的 `inputs` 生成控件
- `/v1/capabilities` 返回 `engine` 字段：**只有 `comfy` 变体等 ComfyUI 在线**，
  `cli` 变体（TTS / 超分 / FramePack）起本地进程，ComfyUI 没开也能提交
- 2 秒轮询 `/v1/jobs`，排队中可取消
- prompt 输入框以 `{` 开头就按 Layout JSON 解析，否则当普通文本
- 产物 URL：后端只存磁盘绝对路径，前端取 basename 拼 `/v1/jobs/{id}/file/{name}`

### 5.7 MCP 入口

`mbai mcp` 起一个 stdio MCP 服务（`platform_core/mcp_server.py`），工具：
`list_capabilities` / `capability_matrix` / `upload_file` / `submit` / `job_status` /
`list_jobs` / `wait_job` / `cancel_job`。

它是**薄客户端**：只走 HTTP 调 `/v1/*`，不 import store / worker。好处是能跨机器连、
平台重启不打断会话；代价是没法绕过 HTTP 直接动数据库（这是故意的）。
`wait_job` 返回产物的**绝对路径**，Agent 可以直接读文件。

## 6. 加一个新能力

以「接一个快速出图变体」为例：

1. `models/<id>.yaml`：写清 `files`（相对 `PLATFORM_MODELS_DIR` 的路径）、`license`、`commercial_ok`、`vram_profile`
2. 下载权重：`scripts/` 下加下载脚本或在现有脚本里加条目（**别手动拷，路径要可复现**）
3. `capabilities/image.txt2img/<变体>/v1/workflow.json`：从 ComfyUI 导出 API 格式
4. 同目录 `manifest.yaml`：`inputs` 只暴露真正要调的参数，其余进 `constants`
5. 验证：

```powershell
uv run mbai doctor                        # 应全绿
uv run python scripts\smoke-test.py <变体>    # 端到端
```

> `doctor` 里对模型文件的检查会**同时**看热池和 `extra_model_dirs`，所以历史权重也能通过。

## 7. 验证手段

| 命令 | 用途 |
|---|---|
| `uv run mbai doctor` | 注册表 / 模型文件 / 引擎 / MOSS 节点总检 |
| `uv run mbai run --variant <变体> --prompt "..."` | 不走队列直跑一次，看真实耗时 |
| `uv run mbai gc` | 按 TTL 清理中间产物，**默认只预演**；`--apply` 才真删 |
| `uv run mbai mcp` | 起 MCP 服务（stdio），Agent 用；薄客户端，走 HTTP 调 `/v1/*` |
| `uv run python scripts\smoke-test.py [变体]` | 走 HTTP + 队列的端到端冒烟 |
| `uv run python scripts\test-layout.py [base_url]` | Layout JSON 版式落位验证 |
| `uv run python scripts\check-nodes.py` | 检查引擎里的 MOSS 相关节点是否存在 |
| `uv run python scripts\job-status.py <job_id>` | 查单个任务 |
| `powershell -File scripts\download-tools.ps1` | 装/校验 Real-ESRGAN 工具包（CLI 适配器用） |
| `powershell -File scripts\link-local-models.ps1` | 把别处的权重用符号链接挂进模型池（不复制、不入库） |

冷却参考（RTX 3060 12GB）：

| 变体 | 分辨率 / 步数 | 耗时 |
|---|---|---|
| `z-image-turbo` | 1024×1024 / 8 步 | **16.6 s**（首跑，含加载）/ 768×768 端到端 7.2 s |
| `moss-image-sft` | 1024×1024 / 25 步 | 46 s |
| `moss-image-sft` | 1088×1472 / 25 步 | 66 s |
| `moss-image-base` | 1024×1024 / 50 步 | 79 s |

## 8. 调试手册

| 症状 | 先查什么 |
|---|---|
| 局域网打开报 **502** | 不是服务挂了。客户端代理连不上上游会返回 502。查 Windows 防火墙的**程序级**规则：`python.exe` 首次监听会弹窗，没人点同意就在 `Public` 下是 Block，端口规则覆盖不了它。交叉验证用 `Test-NetConnection <IP> -Port 8000`，**必须从第二台机器发起**（本机自测不过入站检查，等于没测） |
| 出图 6 s/it | torch 是不是 cu128？是不是加了 `--lowvram`？ |
| `doctor` 报 MOSS 节点为「无」 | 引擎 commit 不是 `d07dff45`，或跑的不是 `engines/comfy` 那份 |
| 注册表错误 / 缺模型 | `models/*.yaml` 的 `files` 路径与磁盘实际路径不一致 |
| 产物中文乱码 | 文件被 PowerShell 写过；重新用工具写，读取用 `utf-8-sig` |
| 任务卡在 `running` | 引擎进程死了。重启平台服务会 `requeue_running()` |
| `git push` 卡住 | git 全局代理指向没运行的 `127.0.0.1:7890`，用 `git -c http.proxy= -c https.proxy= ...` |
| `hf download` 少文件 | 已修：`download-models.py` 一次下一个文件；别手写多 `--include` |
| `hf download` 退出码 0 但文件只有几百字节 | 那是 LFS **指针文件**：该仓库走 HF Xet 存储，hf-mirror 不代理 Xet。设 `HF_HUB_DISABLE_XET=1` 退回普通 LFS（脚本已内置） |

## 9. 已知限制

- `progress` 只有 comfy 引擎是实时的，CLI 引擎（超分 / TTS / FramePack）还是 0/1
- 无鉴权、`CORS allow_origins=["*"]`：内网单人用，**不要映射到公网**
- 单 worker、单卡串行（`worker_concurrency = 1`）
- CLI 子进程不随平台服务退出：服务被强杀时 realesrgan 可能残留，`taskkill /IM realesrgan-ncnn-vulkan.exe /F` 清一下
- 上传的素材（`data/uploads/`）只增不删，没有 TTL；任务删除不会连带删素材
- 输入图会被复制进 `engines/comfy/input/`（引擎的上传接口行为），同样没有清理
- **GC 不会自动跑**：`mbai gc --apply` 需要手动或自己挂计划任务；用户产物默认永不清理
- **FramePack 跑的时候独占 GPU**：ComfyUI 用不了，且开跑前会被强制 `/free` 卸载模型
- 外部引擎（CosyVoice / FramePack）的解释器路径**默认不设**（`settings.py` 里是 `None`），只能在 `.env` 里配
  `PLATFORM_COSYVOICE_*` / `PLATFORM_FRAMEPACK_*`。CosyVoice 那四项由
  `scripts\bootstrap-cosyvoice.ps1` 自动写入（幂等，重跑即对齐）；**FramePack 还没有引导脚本**，
  只能手工配 `.env`
- 抠图（`image.matting`）走 ComfyUI 核心的 `LoadBackgroundRemovalModel` + `RemoveBackground` +
  `JoinImageWithAlpha`，有两个必须记住的点：
  1. 模型放 `models/background_removal/`，**该目录键必须写进 `extra_model_paths.yaml`**（bootstrap 的
     `$keys` 里已加），漏了的话节点候选是空的，看起来像「模型没下下来」
  2. `JoinImageWithAlpha` 内部是 `alpha = 1.0 - mask`，即**它要的是「变透明的区域」**，
     所以要接一个 `InvertMask`，直接把 `RemoveBackground` 的 mask 连上去会抠出背景、留下前景的洞

## 9.1 垃圾回收（TTL）

`uv run mbai gc` 默认**只预演**，打印将删什么、能释放多少。加 `--apply` 才真删。

| 类别 | 默认 TTL | 说明 |
|---|---|---|
| `orphan` | 立即 | 任务记录已不存在的产物目录 |
| `uploads` | 7 天 | 未被任何任务引用过的上传素材 |
| `logs` | 7 天 | `data/logs/*.log`（CLI 引擎输出） |
| `engine_input` | 7 天 | 引擎 `input/` 里我们上传的副本（`ast_*`） |
| `engine_output` | 7 天 | 引擎 `output/` 副本（平台已收进 `data/artifacts`） |
| `artifacts` | **0 = 不清理** | 用户作品。要清必须显式 `--artifacts-days 30` |

三条硬规则（写在 `platform_core/gc.py` 顶部）：
1. 用户产物默认不删，那是作品不是缓存
2. 排队/执行中的任务，产物和素材一律跳过
3. 被任何任务参数引用过的素材不删——删了任务就再也复现不了
- 产物没有定期清理，`data/artifacts/` 会一直涨

## 10. 提交规范

- 中文 commit message，一句话说清「改了什么 / 为什么」
- 一次提交只做一件事；动了运行行为就同步 `docs/` 或统一日志仓库
- 提交前自查：`git status` 里不该出现 `data/`、权重、`dist/`、`node_modules/`
