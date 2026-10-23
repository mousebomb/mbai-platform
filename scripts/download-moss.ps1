# 下载 MOSS-Image 权重到统一模型池（默认 D:\AI\models）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\download-moss.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\download-moss.ps1 -ModelsDir 'E:\models'
#
# 只取 12GB 显存能跑的组合：DiT int8(5.41G) + 文本编码器 w4a8(7.34G) + small VAE(0.25G)
# 注意：hf download 一次传多个 --include 模式会漏文件，所以这里逐个下
param(
    [string]$ModelsDir = 'D:\AI\models',
    [string]$Repo = 'Comfy-Org/MOSS-Image'
)

$ErrorActionPreference = 'Stop'
# 国内直连 huggingface 基本不可用，走镜像
$env:HF_ENDPOINT = 'https://hf-mirror.com'

New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

# 逻辑名 → 相对路径，必须与 models/*.yaml 的 files 一致
$files = @(
    'diffusion_models/moss_image_base_int8_convrot.safetensors',
    'diffusion_models/moss_image_sft_int8_convrot.safetensors',
    'text_encoders/qwen3.5_9b_w4a8_gptq.safetensors',
    'vae/flux2-vae_small.safetensors'
)

foreach ($f in $files) {
    Write-Host "[dl] $f"
    uvx --from huggingface_hub hf download $Repo --include $f --local-dir $ModelsDir --max-workers 4
    if ($LASTEXITCODE -ne 0) { throw "download failed: $f ($LASTEXITCODE)" }
}

# ---------- 校验：文件在不在、大小像不像完整的 ----------
Write-Host ''
Write-Host '[verify]'
# 下限写得很松，只用来抓「下到一半断掉」这种明显残缺
$expect = @{
    'diffusion_models/moss_image_base_int8_convrot.safetensors' = 5.0GB
    'diffusion_models/moss_image_sft_int8_convrot.safetensors'  = 5.0GB
    'text_encoders/qwen3.5_9b_w4a8_gptq.safetensors'            = 7.0GB
    'vae/flux2-vae_small.safetensors'                           = 0.2GB
}

$bad = 0
foreach ($f in $files) {
    $p = Join-Path $ModelsDir ($f -replace '/', '\')
    if (-not (Test-Path $p)) { Write-Host "  [!!] missing $f"; $bad++; continue }
    $size = (Get-Item $p).Length
    $okSize = $size -ge $expect[$f]
    if (-not $okSize) { $bad++ }
    $mark = if ($okSize) { '[OK]' } else { '[!!]' }
    Write-Host ('  {0} {1,6:N2} GB  {2}' -f $mark, ($size / 1GB), $f)
}

if ($bad -gt 0) { throw "$bad 个文件缺失或大小异常" }
Write-Host 'DONE: 现在可以跑 uv run platform doctor'
