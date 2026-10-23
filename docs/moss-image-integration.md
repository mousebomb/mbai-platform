# MOSS-Image 5B 接入记录

> 平台第一个落地模块，纵切验证：能力注册 → 队列 → ComfyUI → 产物。

## 结论

能跑，且**不慢**（1024²/25 步 46 秒）。前提是两件事必须做对：torch 必须是 cu130，引擎不要加 `--lowvram`。

| 项 | 值 |
|---|---|
| 参数规模 | 5B DiT（单流 Muon），文本编码器 Qwen3.5-9B |
| 权重许可 | Apache-2.0（可商用） |
| ComfyUI 支持 | 原生，2026-10-16 合入 master（commit `e087d8f6` 之后） |
| 本平台固定 commit | `d07dff45` |
| 落盘占用 | 18.5 GB（base 5.41 + sft 5.41 + 编码器 7.34 + VAE 0.25，GiB） |
| 商用 | ✅ |

## 两个决定性能的开关（实测，差 3.5 倍）

| 配置 | s/it | 25 步端到端 | 说明 |
|---|---|---|---|
| torch **cu128** + `--lowvram` | 6.2 | 162.5 s | 初始配置，能跑但很慢 |
| torch **cu130** + 默认显存 | **1.5** | **46.2 s** | 正确配置 |

原因有两条，缺一不可：

1. **torch 必须 cu130**。`comfy_kitchen` 的原生 int8 / w4a8 算子要求 CUDA 13；cu128 下它会把 backend 标成 `disabled: True`，量化矩阵乘退回 eager 逐层实现。启动日志里搜 `Found comfy_kitchen backend cuda` 看 `disabled` 是否为 `False`。
2. **不要 `--lowvram`**。模型 staged 5.16 GB + 文本编码器 7.0 GB，单看总量超过 12 GB，但 ComfyUI 的 DynamicVRAM 会按时序换入换出；强制 lowvram 反而退化成逐层搬运。

## 文件落位（D:\AI\models）

```
diffusion_models/moss_image_base_int8_convrot.safetensors   5.41 GiB
diffusion_models/moss_image_sft_int8_convrot.safetensors    5.41 GiB
text_encoders/qwen3.5_9b_w4a8_gptq.safetensors              7.34 GiB
vae/flux2-vae_small.safetensors                             0.25 GiB
```

bf16 版本不可用：DiT 10.16 GB + 编码器 19.31 GB，12 GB 显存放不下，也不值得下（整仓 68 GB）。

## 实测

| 分辨率 | 步数 | 参数 | 端到端 |
|---|---|---|---|
| 1024×1024（1M px） | 25 | SFT / cfg 2.5 / er_sde | 46.2 s |
| 1024×1024 | 50 | Base / cfg 3.0 / euler | 78.8 s |
| 1088×1472（1.6M px） | 25 | SFT / cfg 2.5 / er_sde | 66.5 s |

采样速度稳定在 1.5 s/it（1M px）。文本编码器每次任务重新加载，约 5~7 s 固定开销。

## 节点图（API 格式）

官方模板 `image_moss_image_base_t2i.json` 的实际链路（已对照 ComfyUI 源码和 `/object_info` 核对）：

```
UNETLoader ──> SkipLayerGuidanceDiTSimple(single_layers=22, end_percent=0.5) ──┐
CLIPLoader(type=moss_image) ──> CLIPTextEncode(正/负) ────────────────────────┤
EmptyFlux2LatentImage(128ch, /16) ───────────────────────────────────────────┴─> KSampler
   ──> VAEDecode(flux2 vae) ──> SaveImage
```

关键点：

- 潜空间是 **Flux2** 格式（128 通道，16 倍下采样），必须用 `EmptyFlux2LatentImage`；用 SD3 的 latent 节点会直接报错
- `shift=5.0` 写在模型 `sampling_settings` 里，**不需要**再加 ModelSampling 节点
- SLG 用 `SkipLayerGuidanceDiTSimple`（只改 uncond 分支），官方模板的细参在子图里：`single_layers=22, start=0, end=0.5`
- 官方模板参数（kijai 提速版）：25 步 / CFG 2.5 / `er_sde` / `simple`
- OpenMOSS 官方参数：≥50 步 / CFG 3.0 / `euler`

平台默认跟官方模板走（25 步），要更高质量时传 `steps=50, cfg=3.0, sampler_name=euler`。

## 版式控制（Layout JSON）

MOSS 的差异化能力：prompt 可以是普通字符串，也可以是 JSON 布局：

```json
{"summary":{"caption":"..."},
 "items":[{"label":"text","text":"MOSS","bbox":[-416,-384,-288,384]}],
 "bbox_coord_space":"abs_centralized","bbox_format":"yxyx"}
```

平台的处理：`prompt` 参数类型是 `prompt`（字符串或对象），传对象时自动序列化成 JSON 字符串交给 `CLIPTextEncode`。
**不需要**在平台侧复刻 `MOSSImageLayoutPrompt` 节点——那个节点只是把前端画框转成同样的 JSON。

实测印证了它的必要性：不指定布局时，主标题（PEAKS / NOCTURNE）稳定正确，**副标题和小字会乱码**
（"FILM NOIIT E FETTIVA"）。要做海报，小字必须走 Layout JSON 显式给 bbox + 原文。

## 已知限制

- 不支持参考图生图（官方模板的 `ref_image` 只喂给提示词增强 LLM，模型本身不吃图）
- 小字不指定布局就会乱码，属于模型行为，不是接入问题
- 未接 ComfyUI websocket，任务的 `progress` 目前只有 0/1 两态（`// TODO` 在 `platform_core/worker.py`）
- 文本编码器每次任务重载约 5~7 s；如果后面任务密度高，可以再做常驻进程池
