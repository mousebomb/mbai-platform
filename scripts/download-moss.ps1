# 下载 MOSS-Image 权重到统一模型池 D:\AI\models
# 只取 12GB 显存可用的量化组合：DiT int8(5.41G) + 文本编码器 w4a8(7.34G) + small VAE(0.25G)
$ErrorActionPreference = 'Stop'
$env:HF_ENDPOINT = 'https://hf-mirror.com'

$target = 'D:\AI\models'
New-Item -ItemType Directory -Force -Path $target | Out-Null

uvx --from huggingface_hub hf download Comfy-Org/MOSS-Image `
  --include "diffusion_models/moss_image_base_int8_convrot.safetensors" `
            "diffusion_models/moss_image_sft_int8_convrot.safetensors" `
            "text_encoders/qwen3.5_9b_w4a8_gptq.safetensors" `
            "vae/flux2-vae_small.safetensors" `
  --local-dir $target `
  --max-workers 4

if ($LASTEXITCODE -ne 0) { throw "下载失败 ($LASTEXITCODE)" }
Write-Host 'DONE'
Get-ChildItem $target -Recurse -File -Filter '*.safetensors' |
  ForEach-Object { '{0,8:N2} GB  {1}' -f ($_.Length / 1GB), $_.FullName.Replace($target, '') }
