# 启动平台自带的 ComfyUI 引擎
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start-engine.ps1
$ErrorActionPreference = 'Stop'
$comfy = Join-Path (Split-Path $PSScriptRoot -Parent) 'engines\comfy'

if (-not (Test-Path "$comfy\.venv\Scripts\python.exe")) {
    throw '引擎环境不存在，先跑 scripts\install-comfy-env.ps1'
}

Set-Location $comfy
# 不用 --lowvram：MOSS-Image 在 NORMAL_VRAM 下 1.5s/it，--lowvram 会退化到 6.2s/it
& "$comfy\.venv\Scripts\python.exe" main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch
