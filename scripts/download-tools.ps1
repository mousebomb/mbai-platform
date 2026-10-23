# 下载 Real-ESRGAN ncnn-vulkan 工具包到 tools\realesrgan\（exe + 模型一起，约 30 MB）。
#
# 幂等：exe 和 models\ 下的模型都在就跳过；下载失败给出手动方案。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\download-tools.ps1
#
# 注意：本脚本必须是 UTF-8 with BOM（PowerShell 5.1 否则按 GBK 解析中文）。
param(
    [string]$Url = 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip',
    [string]$DestDir = ''
)

$ErrorActionPreference = 'Stop'
# GitHub 走 TLS 1.2（老 PS 默认协议可能被拒）
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path $PSScriptRoot -Parent
if (-not $DestDir) { $DestDir = Join-Path $root 'tools\realesrgan' }
$exe = Join-Path $DestDir 'realesrgan-ncnn-vulkan.exe'
$models = Join-Path $DestDir 'models'

# ---------- 幂等：已装好就跳过 ----------
if ((Test-Path $exe) -and (Test-Path $models) -and (Get-ChildItem $models -Filter '*.bin' -ErrorAction SilentlyContinue)) {
    Write-Host '[OK] tools\realesrgan 已存在（exe + 模型齐全），跳过下载'
    exit 0
}

$zip = Join-Path $env:TEMP 'realesrgan-ncnn-vulkan-20220424-windows.zip'
$extractDir = Join-Path $env:TEMP 'realesrgan-extract'

Write-Host '[1/3] 下载 Real-ESRGAN ncnn-vulkan 工具包（约 30 MB）...'
try {
    Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
}
catch {
    Write-Host '[!!] 下载失败：多为网络/代理问题（GitHub 直连不稳）'
    Write-Host '     代理提示：git 全局代理配置不作用于 Invoke-WebRequest，可先设置'
    Write-Host '       $env:HTTPS_PROXY = "http://127.0.0.1:7890"（换成你实际代理端口）'
    Write-Host '     手动方案：浏览器打开下面地址下载 zip，解压后把 exe 和 models 放到 tools\realesrgan\，再跑一次本脚本'
    Write-Host "     $Url"
    Write-Host "     原始错误：$($_.Exception.Message)"
    exit 1
}

Write-Host '[2/3] 解压...'
if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $extractDir -Force

$found = Get-ChildItem -Path $extractDir -Recurse -Filter 'realesrgan-ncnn-vulkan.exe' | Select-Object -First 1
if (-not $found) {
    Write-Host '[!!] 压缩包里没有 realesrgan-ncnn-vulkan.exe，包可能损坏，删除重试'
    exit 1
}
# 压缩包通常带顶层目录：把 exe 所在目录整个平铺进目标目录
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Copy-Item -Path (Join-Path $found.Directory.FullName '*') -Destination $DestDir -Recurse -Force

Write-Host '[3/3] 校验...'
if (-not (Test-Path $exe)) {
    Write-Host '[!!] 校验失败：realesrgan-ncnn-vulkan.exe 不存在'
    exit 1
}
$modelFiles = Get-ChildItem $models -Filter '*.bin' -ErrorAction SilentlyContinue
if (-not $modelFiles) {
    Write-Host '[!!] 校验失败：models\ 下没有模型文件（*.bin），包不完整'
    exit 1
}
Write-Host "[OK] 完成：$exe"
Write-Host "[OK] 模型 $($modelFiles.Count) 个："
$modelFiles | ForEach-Object { Write-Host "     $($_.Name)  ($([math]::Round($_.Length / 1MB, 1)) MB)" }

Remove-Item $zip -Force -ErrorAction SilentlyContinue
Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host '[OK] 临时文件已清理'
exit 0
