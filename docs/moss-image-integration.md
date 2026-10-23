# MOSS-Image 5B 接入记录

> 平台第一个落地模块，纵切验证：能力注册 → 队列 → ComfyUI → 产物。

## 结论

能跑。12GB 显存下属于**降级可用**（degraded），不是舒适区。

| 项 | 值 |
|---|---|
| 参数规模 | 5B DiT（单流 Muon），文本编码器 Qwen3.5-9B |
| 权重许可 | Apache-2.0（可商用） |
| ComfyUI 支持 | 原生，2026-10-16 合入 master（commit `e087d8f6` 之后） |
| 本平台固定 commit | `d07dff45` |
| 落盘占用 | 11.9 GB（base 5.04 + sft 5.04 + 编码器 6.84 + VAE 0.23，十进制 GB） |
| 商用 | ✅ |

## 为什么必须用 master/commit 而不是 release

MOSS-Image 的原生节点在 `comfy_extras/nodes_moss_image.py`，v0.41.3 的 release 里没有。
所以引擎固定的是 **commit SHA** 而不是版本号——可复现性等价，只是升级时多看一行 release notes。

## 文件落位（D:\AI\models）

```
diffusion_models/moss_image_base_int8_convrot.safetensors   5.04 GB
diffusion_models/moss_image_sft_int8_convrot.safetensors    5.04 GB
text_encoders/qwen3.5_9b_w4a8_gptq.safetensors              6.84 GB
vae/flux2-vae_small.safetensors                             0.23 GB
```

bf16 版本不可用：DiT 10.16 GB + 编码器 19.31 GB，12GB 显存完全放不下，也不值得下载（68 GB 整仓）。

## 显存账

量化后 DiT 5.04 + 编码器 6.84 = 11.88 GB，**不能同时驻留**。ComfyUI 是懒加载 + 可卸载：

- 编码阶段：加载编码器 6.84 GB → 出 embedding → 卸载
- 采样阶段：加载 DiT 5.04 GB + 激活

因此必须 `--lowvram`。代价是每个任务重新加载编码器（十几秒）。

## 节点图（API 格式）

官方模板 `image_moss_image_base_t2i.json` 的实际链路（已核对 ComfyUI 源码）：

```
UNETLoader ──┐
             SkipLayerGuidanceDiTSimple(single_layers=22, end_percent=0.5)
CLIPLoader(moss_image) ── CLIPTextEncode(正/负)
EmptyFlux2LatentImage(128ch, /16) ── KSampler ── VAEDecode(flux2 vae) ── SaveImage
```

关键点：

- 潜空间是 **Flux2** 格式（128 通道，16 倍下采样），必须用 `EmptyFlux2LatentImage`，用 SD3 的 latent 节点会直接报错
- `shift=5.0` 写在模型 `sampling_settings` 里，**不需要**再加 ModelSampling 节点
- SLG 用 `SkipLayerGuidanceDiTSimple`（只改 uncond 分支），官方模板取 `single_layers=22, start=0, end=0.5`
- 官方模板参数（kijai 的提速版）：25 步 / CFG 2.5 / `er_sde` / `simple`
- OpenMOSS 官方参数：≥50 步 / CFG 3.0 / `euler`

平台默认跟官方模板走（25 步），需要更高质量时把 `steps=50, cfg=3.0, sampler=euler` 传进去。

## 版式控制（Layout JSON）

MOSS 的差异化能力：prompt 可以是普通字符串，也可以是 JSON 布局：

```json
{"summary":{"caption":"..."},
 "items":[{"label":"text","text":"MOSS","bbox":[-416,-384,-288,384]}],
 "bbox_coord_space":"abs_centralized","bbox_format":"yxyx"}
```

平台的处理方式：`prompt` 参数类型是 `prompt`（字符串或对象），传对象时自动序列化成 JSON 字符串交给 `CLIPTextEncode`。
**不需要**在平台侧复刻 `MOSSImageLayoutPrompt` 节点——那个节点只是把前端画框转成同样的 JSON。

## 已知限制

- 不支持参考图生图（官方模板的 `ref_image` 只喂给提示词增强 LLM，模型本身不吃图）；图生图/编辑要走 edit LoRA 或别的模型
- 原生支持不带插件里的 `text_device` 选项，编码器只能靠 `--lowvram` 卸载；如果实测太慢，下一步是给 CLIPLoader 传 `device=cpu`
- 未接 ComfyUI websocket，任务的 `progress` 目前只有 0/1 两态（`// TODO` 在 `platform_core/worker.py`）
