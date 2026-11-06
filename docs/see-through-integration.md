# See-through 拆层接入记录（`image.layer_split`）

> 单张动漫立绘 → 语义分层 PSD。平台第二条「独立环境 + 独立仓库」的 CLI 能力（第一条是 CosyVoice）。
> 接进来的代价是**独占 GPU 约 12 分钟**，换来的是 16 个可独立平移的图层。

## 结论

能跑，效果达标，但**慢且吃显存**：1024 分辨率 / 30 步 / `--group_offload` 单图 **9.3~11.9 分钟**，
峰值显存 **10.3 GB**（12 GB 卡可用，期间别开别的）。

| 项 | 值 |
|---|---|
| 上游 | `shitagaki-lab/see-through`，钉 commit `df019de`（2026-10-06） |
| 代码许可 | Apache-2.0 |
| 权重许可 | openrail++（`layerdifforg/seethroughv0.0.2_layerdiff3d` + `24yearsold/seethroughv0.0.1_marigold`） |
| 权重体积 | 约 9.5 GB，走 HF 缓存，**不进模型热池** |
| 环境 | `uv venv` python 3.12 + torch 2.8.0+cu128 + diffusers 0.37.0 + psd_tools 1.14.2 |
| 平台变体 | `image.layer_split/see-through/v1`（`engine: cli`） |
| 单图耗时 | 9.3 分钟（无下载）/ 11.9 分钟（首跑含 9.5 GB 下载） |
| 峰值显存 | 10.3 GB |
| 层数 | 24 个 tag 中 **16 层非空**（标准正面立绘） |

## 为什么是 `cli`，不是 ComfyUI

三条都成立，缺一条就会想搬进节点图：

1. **依赖冲突**：torch 2.8 cu128 / diffusers 0.37，平台的 ComfyUI 是 cu130。同一份 venv 装不下。
2. **显存**：12 GB 上必须 `--group_offload` 才压到 10.3 GB，而 ComfyUI 空闲就占 8 GB+。
   所以 manifest 写 `cli.free_comfy_vram: true`，适配器在起进程前先 `POST /free` 卸模型。
3. **不是节点图**：SDXL 双管线（13+11 组）+ Marigold 深度 + PSD 后处理，搬进 ComfyUI 等于重写推理，
   违背「不自研推理内核」。

代价：**ComfyUI 出图任务会在拆层结束后多付 10~15 秒重载模型**。这是设计取舍，不是 bug。

## 上游调用细节

```powershell
# 包装脚本实际执行的（scripts/engines/see_through_split.py）
python inference/scripts/inference_psd.py `
  --srcp <立绘.png> --save_to_psd `
  --resolution 1024 --inference_steps 30 --seed 42 --group_offload
```

| 参数 | 作用 | 平台默认 |
|---|---|---|
| `--resolution` | 推理边长 | 1024（12 GB 平衡点；1280 更慢，再高 OOM） |
| `--inference_steps` | 采样步数 | 30 |
| `--group_offload` | 分组换入换出 | 12 GB 卡**必须**，否则 OOM |
| `--save_to_psd` | 出分层 PSD | 必带 |

### 产物落位（上游行为，别猜）

```
workspace/layerdiff_output/<stem>.psd          ← PSD 在这里（父目录）
workspace/layerdiff_output/<stem>/*.png        ← 24 个 tag 的 PNG（子目录）
workspace/layerdiff_output/<stem>/src_img.png  ← 上游自己写出的「pad 后输入」，PSNR 要用它
```

包装脚本把 PSD / 每层 PNG / `report.json` 收进 job 目录后**删掉上游这两处**，
否则 `workspace/` 会无限增长（cli 适配器只收顶层文件，中间态没必要留）。

### PSD 画布是方形 pad 后的尺寸

上游先 `center_square_pad_resize` 再推理，所以 832×1248 的立绘出来是 **1024×1024 的画布**，
人物两侧带透明边。每层已按 PSD 画布坐标导出成整幅大小（位置烘进图层，平移不露背景），
每层 bbox 写在 `report.json` 里。

**要回到原图比例必须自己按 pad 信息反算**，平台没做——因为「要不要裁」取决于调用方。

空层属正常：`headwear / eyewear / earwear / neckwear / tail / wings / objects`
在没有对应部件时会被模型判为「不存在」，不进 PSD。所以「24 个 tag」不等于「24 层」。

## 实测

| 跑法 | 输入 | 耗时 | 层数 | 备注 |
|---|---|---|---|---|
| 手工首跑 | 832×1248 正面全身立绘 | **11.9 min** | 16 | 含 9.5 GB 权重下载 |
| 手工二跑 | 仓库样图 `common/assets/test_image.png` | **9.3 min** | 16 | 权重已缓存 |
| 平台端到端 | 同一张立绘（asset → job） | **9.2 min** | 16 | verdict PASS，4 个产物齐全 |

稳定值：**无下载时约 9.3 分钟**，其中模型加载约 2 分钟、采样 6~7 分钟。
所以「12 分钟」这个数字要按含下载的首次体验来理解。

### 质量：21.9 dB，差异集中在层接缝

| 指标 | 值 |
|---|---|
| 合成图 vs 上游 pad 后输入（`src_img.png`），人物区域 | **21.9 dB** |
| `|diff| > 16` 的像素占比 | **2.57%** |
| 整像素偏移 | 无 |
| 差异位置 | 裙腰线、左手、领口 |

21.9 dB 不是「接入失败」——这是扩散模型**重新生成**遮挡区域的结果，不是像素级复刻。
层边界必然有轻微差异；要无损抠图用 `image.matting`，要可编辑图层才用它。

目视核对 16 层语义正确：前后发分左右、眼白/瞳孔/睫毛/眉毛/耳左右分开、裙/上衣/袜/鞋齐全。

## 交接包的 `eval_layers.py` 有两个 bug

交接实验包里那两个脚本不要直接用，会在非方形输入上给出**恒为 1 dB 级**的 PSNR：

1. **`--dir` 要传 PSD 所在的父目录**，不是 `<stem>/` 子目录。传子目录直接找不到 PSD。
2. **参考图不能是原图**。原图是 832×1248，PSD 画布是 1024×1024 方形；再叠加
   `psd.composite()` 把透明区摊成**黑底**、原图是白底，两个错误叠起来 → 无论拆得多好都是 1 dB 级。

正确做法（已固化进 `scripts/engines/see_through_split.py`）：
合成时自己按 `layer.left/top` 逐层 `alpha_composite` 到透明画布。
算 PSNR 时两边都压到白底，参考图用上游写出的 `src_img.png`（同尺寸），且只在 `alpha > 8` 的人物区域上算。

## 平台侧改动

### 新增第五种产物 kind `file`

`psd / zip` 是平台第一次出现的二进制交付物，不内联预览。三处必须同步：

| 位置 | 改动 |
|---|---|
| `platform_core/contracts.py` | `Artifact.kind` 的 Literal 加 `file` |
| `platform_core/media.py` | `KIND_BY_SUFFIX` 加 `.psd / .zip → file` |
| `platform_core/assets.py` | `MEDIA_TYPES` 加对应 MIME |
| `apps/web/src/App.tsx` | `ArtifactMedia` 给下载链接（默认分支是 `<img>`，不处理会显示破图） |

每个 job 四个产物：

| 文件 | kind | 说明 |
|---|---|---|
| `output.png` | image | 合成预览（`--out`，`cli.output_ext: png`） |
| `layers.psd` | file | 主交付物，`image/vnd.adobe.photoshop` |
| `layers.zip` | file | PSD + 每层 PNG + `report.json`，给不吃 PSD 的工具 |
| `report.txt` | text | 质检摘要（层数 / 覆盖率 / PSNR / 可疑项） |

### `report.txt` 存在的理由

平台没有「看日志」的入口，`data/logs/*.log` 只有运维会去翻。
所以把判定结论（PASS / WARN + 可疑项）写成文本产物，调用方（人或多模态 Agent）能直接读到。
阈值：层数 < 15、单层覆盖率 > 55%（疑似粘连）、PSNR < 18 dB 都记 WARN。

## 部署（换机器）

```powershell
# 仓库 + venv + torch cu128 + requirements（约 10 分钟）
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-seethrough.ps1 `
  -RepoDir D:\AI\see-through -EnvDir D:\AI\envs\see-through -HfHome D:\AI\models\hf-seethrough

# 环境已就绪、只重新对齐路径
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-seethrough.ps1 -SkipEnv ...
```

脚本会往 `.env` 提示三行，必须都配上（占位符替换不到会**在启动前**报错，不会静默跑空路径）：

```ini
PLATFORM_SEETHROUGH_PYTHON=D:/AI/envs/see-through/Scripts/python.exe
PLATFORM_SEETHROUGH_HOME=D:/AI/see-through
PLATFORM_SEETHROUGH_HF_HOME=D:/AI/models/hf-seethrough
```

`doctor` 查不到这套权重（它不在 `PLATFORM_MODELS_DIR`，走 HF 缓存），首次任务时自动下载。

## 坑

1. **`.ps1` 不能写中文**。PowerShell 5.1 把无 BOM 脚本按 GBK 读，中文会炸成解析错误。
   引导脚本一律 ASCII，输出只用 `[OK]` / `[!!]`。
2. **`git fetch --depth 1 origin <sha>` 可能被拒**。git 全局代理失效时直接 128；
   本地已有该 commit 时应当跳过而不是失败（脚本已改成：先比 `HEAD`，fetch 失败再 `cat-file -e` 兜底）。
3. **C 盘别沾**。venv、仓库、HF 缓存全放 D 盘：不设 `HF_HOME` 的话 9.5 GB 会钻进
   `C:\Users\<你>\.cache\huggingface`。
4. **`HF_HUB_DISABLE_XET=1`**。hf-mirror 不代理 Xet 存储，不关会只拿到几百字节的 LFS 指针。
5. **进度只有粗略百分比**。解析的是上游 tqdm 的 `NN%`，第二组（head）开始会从 0% 重新涨，属正常。
6. **不要在拆层跑的时候提交 ComfyUI 任务**。显存只有 12 GB，两个一起必定 OOM。
7. **`nf4` 档没装**（需要 bitsandbytes）。manifest 的下拉只给 `offload` / `full`，
   `full` 只适合更大显存的卡。

## 没做的

- **`heuristic_partseg.py` 的 `seg_wdepth` / `seg_wlr` 没接**：裙摆与腿、左右肢体的前后关系仍会错，
  要更细的分层得再跑上游后处理，那是另一条链。
- **没有按原图比例回裁**：`report.json` 里给了每层 bbox，调用方按需裁。
- **没有自动选抠图/拆层**：两者解决不同问题（像素级无损 vs 可编辑图层），必须由调用方显式指定。
