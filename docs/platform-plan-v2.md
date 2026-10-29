# 一站式 AI 平台方案 v0.2

- 日期：2026-10-23
- 变更原因：
  1. 平台代码与模型池分离（代码跟着仓库走，权重按机器配置）
  2. 认识到"超分没有唯一解"——同一任务不同内容要用不同模型（animevideov3 / x4plus / RealCUGAN / waifu2x）
  3. 接受"平台不是第一天就完整"，第一阶段以**统一登记 + 去重 + 统一调度**为目标
- 标注：`[实测]` 本机实测 / `[已知]` 调研结论 / `[推断]` 推导 / `[建议]` 决策

---

## 0. 相对 v0.1 的三个结构性修改

| 项 | v0.1 | v0.2 |
|---|---|---|
| 落地方式 | 先建平台，再搬能力 | **先做"联邦调度"**：现有引擎原封不动，套一层统一队列/API/产物库；之后逐个收编 |
| 模型观 | 一个能力一个主模型 | **一个能力 N 个模型变体 + 预设 + 按内容路由**，模型是数据不是代码 |
| 目标 | 18 项能力一次定义完 | **能力有成熟度等级**，平台随项目推进逐级点亮 |

v0.1 里"6 周做完 v1"的估时作废。v0.2 的第一阶段是 **1~2 周**，结束时就能用上统一队列。

---

## 1. 第一阶段的真目标（别再加码）

> 把散在多个目录里的权重、多套 Python 环境、几类引擎，收敛成：
> **一个入口（API + 网页）+ 一个队列 + 一个产物库 + 一个模型池**，
> 引擎本身保持原样，能跑就行，不重装、不重构、不迁模型格式。

第一阶段**不做**：蒙版画布、图层分离、ControlNet 预设、人脸换脸调优、视频超分调优。
第一阶段**只做**：

1. 真实模型台账（谁在哪、多大、重复没有、许可是什么）
2. 物理去重（同盘硬链接 / 跨盘联接），回收空间
3. 统一队列 + 统一产物 + 统一 API
4. 把现有 4 个引擎挂上去：ComfyUI、FramePack、CosyVoice、realesrgan-ncnn-vulkan

验收：**在网页上提交一个任务，不管它落到哪个引擎，都能在同一个队列里看到进度，
产物进同一个图库，Agent 用同一套 API 调用。**

---

## 2. 超分这件事说明的问题（方案的核心修正）

一次实测（同一个动画片段的两种超分）：

```
realesr-animevideov3  → 1 分钟，帧间稳定，画风不崩
realesrgan-x4plus     → 11 分钟，纹理更锐，但对平涂画会发脆、加彩边
```

结论：**"超分"不是一个能力，是一族能力。** 平台不能只暴露一个 `upscale` 按钮，必须暴露变体：

```yaml
capability: video.upscale
variants:
  - id: anime-video        # 动画/平涂/线稿
    model: realesr-animevideov3
    engine: ncnn-vulkan
    speed: fast
    artifacts: [sharp_edges_ok, no_color_fringe]
  - id: live-action        # 实拍/纹理
    model: realesrgan-x4plus
    engine: ncnn-vulkan
    speed: slow
  - id: restore            # 老片/噪点
    model: seedvr2-3b
    engine: comfyui
    speed: very_slow
  - id: illustration       # 插画放大
    model: realcugan
    engine: ncnn-vulkan
```

每个产物记录 `model_id` + `variant`，图库里能直接对比"这张是哪个方案出的"。
下次遇到同类素材，平台能推荐上次效果最好的那个。

**这条规则推广到全部能力**：出图也有 `anime / photo / text-poster / lineart` 变体，
换脸也有 `fast / quality` 变体。模型是注册表里的一行数据，不是代码里的 if-else。

---

## 3. 存储布局：热池 / 冷池

代码仓库只放代码与配置，权重复用已有目录，不复制：

```
<仓库>\                      代码 + 配置 + 前端 + data/
<PLATFORM_MODELS_DIR>\       热池：近期在用、单卡能跑的模型
<冷池目录>\                  冷池：大模型、低频、旧版本（首次调用要等搬运）
```

- 热池位置由 `.env` 的 `PLATFORM_MODELS_DIR` 指定，换机器只改这一行
- 冷池不进注册表的主路径，靠 `PLATFORM_EXTRA_MODEL_DIRS` 只读挂载
- 冷池的代价是首次调用要等 1~2 分钟搬运，manifest 里可标 `cold`，UI 提前提示
- **去重优先于扩容**：同盘用硬链接（0 占用），跨盘用 junction / 符号链接（脚本入库、链接不入库）

---

## 4. 架构（联邦式）

```
                    ┌──────────── 调用方 ────────────┐
                    │ 局域网网页（React）  Agent(MCP)  │
                    └───────────────┬────────────────┘
                                    │ 统一 API / SSE
                    ┌───────────────▼────────────────┐
                    │ 控制平面 FastAPI（无 torch）      │
                    │ queue / artifacts / registry     │
                    └───┬────────┬────────┬───────────┘
                        │        │        │            ← 统一调度
        ┌───────────────▼──┐ ┌───▼─────┐ ┌▼──────────────┐
        │ ComfyUI 适配器    │ │FramePack│ │ audio 适配器   │
        │ (HTTP)           │ │ 适配器   │ │ (CosyVoice)   │
        └───────┬──────────┘ └───┬─────┘ └┬──────────────┘
                │                │         │
        ┌───────▼────────────────▼─────────▼──────────────┐
        │ CLI 适配器：realesrgan-ncnn-vulkan / ffmpeg       │
        └───────────────────┬─────────────────────────────┘
                            │ junction / extra_model_paths
                ┌───────────▼───────────┐
                │ <PLATFORM_MODELS_DIR> │ 热池（唯一真相）
                └───────────────────────┘
```

关键点：

- **"一个后端"对外成立，对内是 N 个适配器进程**。每个适配器自带 venv，互不污染。
- 适配器协议只有 4 个接口：

```
POST /run       {capability, variant, params, inputs[]} → {ref}
GET  /status/{ref}   → {state, progress, artifacts[], error}
POST /cancel/{ref}
GET  /health         → {engine, models_loaded, vram_used}
```

- 新引擎 = 新适配器（100~200 行），控制平面不动。
- 非 AI 步骤（ffmpeg 抽帧/合帧/xfade/编码）也是一等公民，作为 `pipeline stage`
  串在 AI 步骤前后——真正决定成片质量的一半是 ffmpeg 参数，不是超分模型。

---

## 5. 能力成熟度（回答"不是第一天完整"）

每个能力在注册表里有等级，网页上直接显示：

| 等级 | 含义 | 例子 |
|---|---|---|
| **L0 declared** | 记了清单，没接 | `image.layer_split`（模型在，12 GB 跑不动） |
| **L1 wired** | 能跑，参数裸露，无预设 | `video.upscale` 第一版 |
| **L2 tuned** | 有预设/默认参数/失败处理 | `image.txt2img` 接 Z-Image 后 |
| **L3 validated** | golden test + 质量对比 + 文档 | `video.motion_transfer` |

平台首页就是一张能力矩阵：全部能力 × 等级。**它诚实地告诉你现在还缺什么**，也是迭代看板。

追加新模型的流程不变：manifest → 工作流/脚本 → schema → golden test → 打版本，不改 Python。

---

## 6. 分阶段

| 阶段 | 时间 | 交付 | 验收 |
|---|---|---|---|
| **M0 台账与回收** | 3~5 天 | 只读扫描 → `models.yaml` + 去重 dry-run → 执行 hardlink/junction → 空间账单 | 重复文件清零；每个模型有唯一 canonical 路径；引擎仍能启动 |
| **M1 联邦调度** | 1~2 周 | 控制平面 + 队列 + 产物库 + 4 个适配器 + 统一 API + 最简前端 | 引擎任务都进同一队列；重启不丢任务；Agent 可提交并取回产物 |
| **M2 图像收敛** | 2~3 周 | A1111 常用能力搬进 ComfyUI；Z-Image / MOSS 接入；蒙版重绘；MCP | 停掉旧 WebUI 不再影响任何工作流 |
| **M3 视频与预设** | 3 周 | 超分 4 变体、动作迁移、FramePack 延展、首尾帧 | 同一条素材能一键切 4 种超分方案并对比 |
| **M4 高级编辑** | 3~4 周 | 图层分离（大卡/远程）、ControlNet 预设、人脸修复链路 | 全部能力 ≥ L2 |
| **M5 归整收尾** | 1 周 | 旧目录 junction 化 → 验证 → 物理删除 | 连续 2 周无回退才删 |

M1 结束就能"用起来"，不用等到 M4。

---

## 7. 第一阶段的具体任务清单

**M0**

1. 权重扫描：输出 `models.yaml`（路径 / sha256 / 大小 / 来源 / 许可 / 引用引擎）
2. 去重计划：同卷走硬链接，跨卷走 junction；先 dry-run 再执行
3. 回收顺序：回收站 → 中间帧 → 孤儿副本 → 跨盘重复 → 引擎仓库里的 `.git`
4. 验收：四个引擎逐个启动一次，确认模型仍能加载

**M1**

1. `apps/api`：`POST /v1/jobs`、`GET /v1/jobs/{id}`、`GET /v1/jobs/{id}/events`、`POST /v1/assets`
2. 契约：capability / variant / model 三张表的 Pydantic 定义
3. 适配器：`comfy`（HTTP）、`framepack`（Python 包装）、`audio`（CosyVoice）、`cli`（realesrgan/ffmpeg）
4. 队列：SQLite WAL，单 GPU 串行
5. 前端：Queue / Gallery / Run（按 schema 自动生成表单）
6. 产物：`data/artifacts/{job_id}/`，中间帧 TTL 7 天

---

## 8. 风险

| 风险 | 说明 | 对策 |
|---|---|---|
| 联邦层变成"又一个壳" | 如果适配器质量差，故障排查要跨两层 | 适配器必须自带 `/health` 和原始日志；控制平面只转发不吞错 |
| 模型变体爆炸 | 一个能力 5 个变体，参数不统一 | 变体必须有 schema 继承 + 默认预设，UI 只暴露 3~5 个关键参数 |
| 磁盘再次被塞满 | 产物 + 中间帧增长快 | 中间帧 TTL、成片配额、每月容量报告 |
| 引擎升级炸依赖 | ComfyUI 节点腐烂 | 固定版本 + lock + 禁用 Manager 自动装 |
| 12 GB 天花板 | 20B 级模型不可用 | 能力表标 `remote_only`，再接远程 worker |

---

## 9. 许可（进注册表）

Apache/MIT 可商用：Z-Image、MOSS-Image、Qwen-Image、Wan2.2、SeedVR2、CosyVoice2、BiRefNet、
realesrgan（BSD-3 类）。
非商用或未核实：FLUX.1 dev 系列、Qwen-Image-2.1、inswapper、RMBG-2.0、FramePackI2V_HY、
社区 SD1.5 检查点。

注册表字段 `license` + `commercial_ok`，UI 打标，提交时警告，不拦截。
`unknown` 表示未核实，不是允许使用。
