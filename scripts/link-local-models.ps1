# 把「不在模型池里」的本地权重挂进模型池（符号链接，不复制）
#
# 为什么需要：ComfyUI 的加载器只认自己目录列表里的文件名，历史权重如果不在
# diffusion_models/ 之类的位置，引擎根本看不见。
#
# 为什么用符号链接而不是 junction：junction 只能指本地目录，而且这里要精确到单个文件；
# 符号链接可以跨卷、按文件建。链接本身不入库（模型池不在仓库里）。
#
# 清单放在 profiles/local-models.json（不入库），模板 profiles/local-models.example.json：
#   links —— 手工挑的单文件（逐条写理由）
#   bulk  —— 整目录镜像（LoRA 这种几十上百个、还会继续增长的）
#
# 用法：
#   Copy-Item profiles\local-models.example.json profiles\local-models.json  # 改成本机路径
#   powershell -ExecutionPolicy Bypass -File scripts\link-local-models.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\link-local-models.ps1 -WhatIf
param(
    [string]$ModelsDir = '',
    [string]$Config = '',
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent

# 从仓库根的 .env 取一个值：PowerShell 自己不会读 .env，这里补上，免得两处配路径
function Get-DotEnvValue {
    param([string]$Key)
    $envFile = Join-Path $Root '.env'
    if (-not (Test-Path $envFile)) { return '' }
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        if ($line -match "^\s*$Key\s*=\s*(.+?)\s*$") { return $Matches[1].Trim('"').Trim("'") }
    }
    return ''
}

if (-not $ModelsDir) { $ModelsDir = Get-DotEnvValue 'PLATFORM_MODELS_DIR' }
if (-not $ModelsDir) {
    Write-Host '[!!] 没有模型池路径：在 .env 里配 PLATFORM_MODELS_DIR，或传 -ModelsDir'
    exit 1
}
if (-not $Config) { $Config = Join-Path $Root 'profiles\local-models.json' }
if (-not (Test-Path $Config)) {
    Write-Host "[!!] 缺少清单 $Config"
    Write-Host '     复制 profiles\local-models.example.json 改成本机路径后再跑'
    exit 1
}

$cfg = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$links = @($cfg.links)
$bulk = @($cfg.bulk)

$created = 0
$skipped = 0
$failed = 0

function New-PoolLink {
    param([string]$Target, [string]$Dir, [string]$Why)
    $name = Split-Path $Target -Leaf
    $dirPath = Join-Path $ModelsDir $Dir
    $dst = Join-Path $dirPath $name

    # 一律用 -LiteralPath：权重文件名里常有 [xxx] 这种方括号，通配符会被展开成空
    if (-not (Test-Path -LiteralPath $Target)) {
        Write-Host "[!!] 源文件不存在，跳过: $Target"
        $script:skipped++
        return
    }
    if (Test-Path -LiteralPath $dst) {
        $script:skipped++
        return
    }
    if ($WhatIf) {
        Write-Host "[--] 将创建链接: $dst -> $Target"
        return
    }
    New-Item -ItemType Directory -Force -Path $dirPath | Out-Null
    $ok = $false
    try {
        # 用 -Path <目录> -Name <文件名>：New-Item 在 PS 5.1 没有 -LiteralPath，
        # 而 -Path 里带 [xxx] 会被当通配符，拆成目录 + 文件名就绕开了
        New-Item -ItemType SymbolicLink -Path $dirPath -Name $name -Target $Target | Out-Null
        $ok = $true
    }
    catch {
        # -Target 也会被当通配符解析（目标文件名带 [xxx] 时必失败），退回 cmd mklink：
        # 它不展开通配符，代价是输出走 stderr，靠退出码判断
        cmd /c mklink "$dst" "$Target" 2>&1 | Out-Null
        $ok = ($LASTEXITCODE -eq 0)
    }
    if (-not $ok) {
        Write-Host "[!!] 建链接失败（需要管理员或开发者模式）: $name"
        $script:failed++
        return
    }
    Write-Host "[OK] $Why`: $Dir\$name"
    $script:created++
}

foreach ($l in $links) {
    New-PoolLink -Target $l.Target -Dir $l.Dir -Why $l.Why
}

foreach ($b in $bulk) {
    if (-not (Test-Path $b.Src)) {
        Write-Host "[!!] 源目录不存在，跳过: $($b.Src)"
        continue
    }
    # 只镜像 safetensors：.pt/.ckpt 是 pickle，加载侧有安全与兼容问题
    Get-ChildItem $b.Src -File -Recurse -Filter *.safetensors | ForEach-Object {
        New-PoolLink -Target $_.FullName -Dir $b.Dir -Why 'LoRA'
    }
}

Write-Host "DONE: 新建 $created 个，跳过 $skipped 个，失败 $failed 个"
Write-Host '提示：这些链接只对本机有效，换机器要重跑本脚本（或自己下载同名文件）'
if ($WhatIf) { Write-Host '（预演模式，未落盘）' }
