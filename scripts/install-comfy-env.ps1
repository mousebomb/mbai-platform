# 安装 ComfyUI 运行环境（平台自带引擎）
# 顺序很关键：先装 CUDA 版 torch，再装 requirements（requirements 里的 torch 不带索引会拉 CPU 版）
$ErrorActionPreference = 'Stop'
$comfy = 'D:\AI\platform\engines\comfy'
$py = "$comfy\.venv\Scripts\python.exe"
$uv = (Get-Command uv).Source

Set-Location $comfy

Write-Host '[1/2] 安装 torch cu128'
& $uv pip install --python $py torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw "torch 安装失败 ($LASTEXITCODE)" }

# 已装好 CUDA torch，这里把 requirements 的 torch 三件套排除，避免被 PyPI 的 CPU 版覆盖
Write-Host '[2/2] 安装 ComfyUI 依赖'
$req = Get-Content "$comfy\requirements.txt" | Where-Object { $_ -notmatch '^(torch|torchvision|torchaudio)\s*$' }
$tmp = Join-Path $env:TEMP 'comfy-req-no-torch.txt'
$req | Set-Content $tmp -Encoding utf8
# 用官方 PyPI：清华镜像缺少 comfyui-workflow-templates 的子包 media-assets-02
& $uv pip install --python $py -r $tmp --index-url https://pypi.org/simple
if ($LASTEXITCODE -ne 0) { throw "requirements 安装失败 ($LASTEXITCODE)" }

Write-Host '[verify]'
& $py -c "import torch,sys;print('python',sys.version.split()[0]);print('torch',torch.__version__,'cuda',torch.version.cuda,'avail',torch.cuda.is_available())"
Write-Host 'DONE'
