"""FramePack I2V 一次性生成脚本：一张图 + 一句提示词 → 一段 mp4。

这是对 FramePack 官方 demo_gradio.py 的「去 UI 化」搬运：
模型加载、低显存动态换页、分节采样、拼接写 mp4 全部照抄，
只是把 gradio 的 AsyncStream 换成了 stdout 进度输出（平台 CLI 适配器按 % 解析）。

不自研任何推理逻辑——采样走 diffusers_helper.pipelines.k_diffusion_hunyuan.sample_hunyuan。

用法（必须用 FramePack 自带的便携 Python，环境是 cu126 + torch 2.6）：
    python framepack_i2v.py --repo <webui> --hf-home <webui>/hf_download \
        --image in.png --prompt "..." --seconds 2 --steps 25 --out out.mp4
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _snapshot(hf_home: Path, repo: str) -> Path:
    """把 HF 仓库名解析成本地缓存里的 snapshot 目录。

    直接用目录而不是 repo id：sharded 模型走 repo id 时 diffusers 会调 hub API
    （`model_info`）确认分片清单，离线环境直接抛 OfflineModeIsEnabled。
    指向本地目录就完全不走网络。缓存里只有一个 revision 时取它即可。
    """
    base = hf_home / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots"
    snaps = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if not snaps:
        raise SystemExit(f"[!!] 找不到 {repo} 的本地缓存：{base}")
    return snaps[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description="FramePack I2V 无头生成")
    ap.add_argument("--repo", required=True, help="FramePack webui 目录（含 diffusers_helper/）")
    ap.add_argument("--hf-home", required=True, help="HF 缓存根目录（模型都在这下面）")
    ap.add_argument("--image", required=True, help="首帧图片")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--n-prompt", default="")
    ap.add_argument("--seconds", type=float, default=2.0, help="目标时长（秒，30fps 换算）")
    ap.add_argument("--steps", type=int, default=25)
    # cfg=1 时负向提示词会被忽略（省一次文本编码），这是官方 demo 的约定
    ap.add_argument("--cfg", type=float, default=1.0)
    ap.add_argument("--gs", type=float, default=10.0, help="distilled guidance scale")
    ap.add_argument("--rs", type=float, default=0.0, help="guidance rescale")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--latent-window", type=int, default=9)
    ap.add_argument("--gpu-preserve", type=float, default=6.0, help="给系统留的显存（GB）")
    ap.add_argument("--no-teacache", action="store_true")
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "diffusers_helper").is_dir():
        print(f"[!!] {repo} 不像 FramePack webui（缺 diffusers_helper/）", file=sys.stderr)
        return 2

    # 必须在 import 之前设：transformers/diffusers 读 HF_HOME 定位缓存
    hf_home = Path(args.hf_home).resolve()
    os.environ["HF_HOME"] = str(hf_home)
    # 模型已经在缓存里，禁止联网校验，否则没网时 from_pretrained 会卡住/报错
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    sys.path.insert(0, str(repo))

    import numpy as np
    import torch
    from PIL import Image
    from diffusers import AutoencoderKLHunyuanVideo
    from diffusers_helper.bucket_tools import find_nearest_bucket
    from diffusers_helper.clip_vision import hf_clip_vision_encode
    from diffusers_helper.hunyuan import encode_prompt_conds, vae_decode, vae_decode_fake, vae_encode
    from diffusers_helper.memory import (
        DynamicSwapInstaller,
        fake_diffusers_current_device,
        get_cuda_free_memory_gb,
        gpu,
        load_model_as_complete,
        move_model_to_device_with_memory_preservation,
        offload_model_from_device_for_memory_preservation,
        unload_complete_models,
    )
    from diffusers_helper.models.hunyuan_video_packed import HunyuanVideoTransformer3DModelPacked
    from diffusers_helper.pipelines.k_diffusion_hunyuan import sample_hunyuan
    from diffusers_helper.utils import (
        crop_or_pad_yield_mask,
        resize_and_center_crop,
        save_bcthw_as_mp4,
        soft_append_bcthw,
    )
    from transformers import (
        CLIPTextModel,
        CLIPTokenizer,
        LlamaModel,
        LlamaTokenizerFast,
        SiglipImageProcessor,
        SiglipVisionModel,
    )

    free_mem_gb = get_cuda_free_memory_gb(gpu)
    high_vram = free_mem_gb > 60
    print(f"[i] 空闲显存 {free_mem_gb:.1f} GB，high_vram={high_vram}", flush=True)

    # ---- 模型：从本地缓存目录直读，不走 hub API ----
    hy = _snapshot(hf_home, "hunyuanvideo-community/HunyuanVideo")
    redux = _snapshot(hf_home, "lllyasviel/flux_redux_bfl")
    pack = _snapshot(hf_home, "lllyasviel/FramePackI2V_HY")

    text_encoder = LlamaModel.from_pretrained(
        str(hy), subfolder="text_encoder", torch_dtype=torch.float16
    ).cpu()
    text_encoder_2 = CLIPTextModel.from_pretrained(
        str(hy), subfolder="text_encoder_2", torch_dtype=torch.float16
    ).cpu()
    tokenizer = LlamaTokenizerFast.from_pretrained(str(hy), subfolder="tokenizer")
    tokenizer_2 = CLIPTokenizer.from_pretrained(str(hy), subfolder="tokenizer_2")
    vae = AutoencoderKLHunyuanVideo.from_pretrained(
        str(hy), subfolder="vae", torch_dtype=torch.float16
    ).cpu()
    feature_extractor = SiglipImageProcessor.from_pretrained(str(redux), subfolder="feature_extractor")
    image_encoder = SiglipVisionModel.from_pretrained(
        str(redux), subfolder="image_encoder", torch_dtype=torch.float16
    ).cpu()
    transformer = HunyuanVideoTransformer3DModelPacked.from_pretrained(
        str(pack), torch_dtype=torch.bfloat16
    ).cpu()

    for m in (vae, text_encoder, text_encoder_2, image_encoder, transformer):
        m.eval()
        m.requires_grad_(False)

    if not high_vram:
        vae.enable_slicing()
        vae.enable_tiling()
        # 低显存关键：权重放 CPU，按层换页，比 sequential offload 快 3 倍
        DynamicSwapInstaller.install_model(transformer, device=gpu)
        DynamicSwapInstaller.install_model(text_encoder, device=gpu)
    else:
        for m in (text_encoder, text_encoder_2, image_encoder, vae, transformer):
            m.to(gpu)

    transformer.high_quality_fp32_output_for_inference = True
    transformer.to(dtype=torch.bfloat16)
    vae.to(dtype=torch.float16)
    image_encoder.to(dtype=torch.float16)
    text_encoder.to(dtype=torch.float16)
    text_encoder_2.to(dtype=torch.float16)
    print("[i] 模型加载完成，开始生成", flush=True)

    total_latent_sections = (args.seconds * 30) / (args.latent_window * 4)
    total_latent_sections = int(max(round(total_latent_sections), 1))
    # 官方约定的分节顺序：>4 节时先 3 再 2，最后 1、0
    latent_paddings = reversed(range(total_latent_sections))
    if total_latent_sections > 4:
        latent_paddings = [3] + [2] * (total_latent_sections - 3) + [1, 0]
    latent_paddings = list(latent_paddings)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with torch.no_grad():
        if not high_vram:
            unload_complete_models(text_encoder, text_encoder_2, image_encoder, vae, transformer)

        # 文本编码：cfg=1 时负向条件直接置零
        if not high_vram:
            fake_diffusers_current_device(text_encoder, gpu)
            load_model_as_complete(text_encoder_2, target_device=gpu)
        llama_vec, clip_l_pooler = encode_prompt_conds(
            args.prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2
        )
        if args.cfg == 1:
            llama_vec_n, clip_l_pooler_n = torch.zeros_like(llama_vec), torch.zeros_like(clip_l_pooler)
        else:
            llama_vec_n, clip_l_pooler_n = encode_prompt_conds(
                args.n_prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2
            )
        llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
        llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)

        # 首帧：按 640 分辨率就近取桶，再等比缩放居中裁剪
        src = Image.open(args.image).convert("RGB")
        input_image = np.asarray(src)
        height, width = find_nearest_bucket(input_image.shape[0], input_image.shape[1], resolution=640)
        input_image_np = resize_and_center_crop(input_image, target_width=width, target_height=height)
        input_image_pt = torch.from_numpy(input_image_np).float() / 127.5 - 1
        input_image_pt = input_image_pt.permute(2, 0, 1)[None, :, None]
        print(f"[i] 首帧 {width}x{height}（原始 {src.width}x{src.height}）", flush=True)

        if not high_vram:
            load_model_as_complete(vae, target_device=gpu)
        start_latent = vae_encode(input_image_pt, vae)

        if not high_vram:
            load_model_as_complete(image_encoder, target_device=gpu)
        image_encoder_output = hf_clip_vision_encode(input_image_np, feature_extractor, image_encoder)
        image_encoder_last_hidden_state = image_encoder_output.last_hidden_state

        llama_vec = llama_vec.to(transformer.dtype)
        llama_vec_n = llama_vec_n.to(transformer.dtype)
        clip_l_pooler = clip_l_pooler.to(transformer.dtype)
        clip_l_pooler_n = clip_l_pooler_n.to(transformer.dtype)
        image_encoder_last_hidden_state = image_encoder_last_hidden_state.to(transformer.dtype)

        rnd = torch.Generator("cpu").manual_seed(args.seed)
        num_frames = args.latent_window * 4 - 3
        history_latents = torch.zeros(
            size=(1, 16, 1 + 2 + 16, height // 8, width // 8), dtype=torch.float32
        ).cpu()
        history_pixels = None
        total_generated_latent_frames = 0

        for section, latent_padding in enumerate(latent_paddings):
            is_last_section = latent_padding == 0
            latent_padding_size = latent_padding * args.latent_window

            indices = torch.arange(
                0, sum([1, latent_padding_size, args.latent_window, 1, 2, 16])
            ).unsqueeze(0)
            (
                clean_latent_indices_pre,
                blank_indices,
                latent_indices,
                clean_latent_indices_post,
                clean_latent_2x_indices,
                clean_latent_4x_indices,
            ) = indices.split([1, latent_padding_size, args.latent_window, 1, 2, 16], dim=1)
            clean_latent_indices = torch.cat([clean_latent_indices_pre, clean_latent_indices_post], dim=1)

            clean_latents_pre = start_latent.to(history_latents)
            clean_latents_post, clean_latents_2x, clean_latents_4x = history_latents[
                :, :, : 1 + 2 + 16, :, :
            ].split([1, 2, 16], dim=2)
            clean_latents = torch.cat([clean_latents_pre, clean_latents_post], dim=2)

            if not high_vram:
                unload_complete_models()
                move_model_to_device_with_memory_preservation(
                    transformer, target_device=gpu, preserved_memory_gb=args.gpu_preserve
                )

            transformer.initialize_teacache(
                enable_teacache=not args.no_teacache, num_steps=args.steps
            )

            def callback(d, _section=section):
                # 平台按日志里最后一个百分数取进度，所以这里打总进度
                step_pct = (d["i"] + 1) / args.steps
                overall = (_section + step_pct) / len(latent_paddings) * 100
                print(f"[step] {_section + 1}/{len(latent_paddings)} {d['i'] + 1}/{args.steps} {overall:.1f}%",
                      flush=True)

            generated_latents = sample_hunyuan(
                transformer=transformer,
                sampler="unipc",
                width=width,
                height=height,
                frames=num_frames,
                real_guidance_scale=args.cfg,
                distilled_guidance_scale=args.gs,
                guidance_rescale=args.rs,
                num_inference_steps=args.steps,
                generator=rnd,
                prompt_embeds=llama_vec,
                prompt_embeds_mask=llama_attention_mask,
                prompt_poolers=clip_l_pooler,
                negative_prompt_embeds=llama_vec_n,
                negative_prompt_embeds_mask=llama_attention_mask_n,
                negative_prompt_poolers=clip_l_pooler_n,
                device=gpu,
                dtype=torch.bfloat16,
                image_embeddings=image_encoder_last_hidden_state,
                latent_indices=latent_indices,
                clean_latents=clean_latents,
                clean_latent_indices=clean_latent_indices,
                clean_latents_2x=clean_latents_2x,
                clean_latent_2x_indices=clean_latent_2x_indices,
                clean_latents_4x=clean_latents_4x,
                clean_latent_4x_indices=clean_latent_4x_indices,
                callback=callback,
            )

            if is_last_section:
                generated_latents = torch.cat(
                    [start_latent.to(generated_latents), generated_latents], dim=2
                )

            total_generated_latent_frames += int(generated_latents.shape[2])
            history_latents = torch.cat([generated_latents.to(history_latents), history_latents], dim=2)

            if not high_vram:
                offload_model_from_device_for_memory_preservation(
                    transformer, target_device=gpu, preserved_memory_gb=8
                )
                load_model_as_complete(vae, target_device=gpu)

            real_history_latents = history_latents[:, :, :total_generated_latent_frames, :, :]
            if history_pixels is None:
                history_pixels = vae_decode(real_history_latents, vae).cpu()
            else:
                section_latent_frames = (
                    (args.latent_window * 2 + 1) if is_last_section else (args.latent_window * 2)
                )
                overlapped_frames = args.latent_window * 4 - 3
                current_pixels = vae_decode(real_history_latents[:, :, :section_latent_frames], vae).cpu()
                history_pixels = soft_append_bcthw(current_pixels, history_pixels, overlapped_frames)

            if not high_vram:
                unload_complete_models()

            # 每节都整段覆盖写，最后一节落的就是完整视频
            save_bcthw_as_mp4(history_pixels, str(out_path), fps=30, crf=args.crf)
            print(
                f"[i] 第 {section + 1}/{len(latent_paddings)} 节完成，"
                f"当前 {history_pixels.shape[2]} 帧",
                flush=True,
            )

            if is_last_section:
                break

    if history_pixels is None:
        print("[!!] 没有任何输出帧", file=sys.stderr)
        return 3
    print(f"[OK] {out_path}  {history_pixels.shape[2]} 帧 @30fps", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
