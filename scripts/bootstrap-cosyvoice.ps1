# 一次性引导：拉 CosyVoice 仓库（固定 commit）→ 建 conda 环境 → 装依赖 → 下模型 → 写 .env
#
# 用法（默认参数对应「仓库内自包含」布局，不对应开发机上的 E:\AI\CosyVoice）：
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-cosyvoice.ps1
#
# 只要环境不要 4.8 GB 模型：
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-cosyvoice.ps1 -SkipModels
#
# 环境已经好了，只想重新对齐路径 / 写 .env：
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-cosyvoice.ps1 -SkipEnv -SkipModels
#
# 为什么必须 conda（这条删不得）：
#   CosyVoice 的文本正则化走 WeTextProcessing → 依赖 pynini → 依赖 openfst。
#   pynini 在 PyPI 上**只有源码包、没有 Windows wheel**，pip 装它要在 Windows 上编译 openfst，
#   实际不可行。conda-forge 有预编译的 pynini + openfst，所以环境必须是 conda 建的，
#   python 3.10（和开发机实测一致）。
#
# 其余依赖（含 torch cu121）全部 pip，版本锁在 scripts\cosyvoice-requirements.lock.txt。

param(
    [string]$EnvName = 'cosyvoice',
    [string]$CosyVoiceSha = '86e26f54',
    [string]$CosyVoiceRepo = 'https://github.com/FunAudioLLM/CosyVoice.git',
    [string]$ModelId = 'iic/CosyVoice2-0.5B',
    [string]$ModelDirName = 'CosyVoice2-0.5B',
    # 模型必须钉 revision，不能跟 master：
    # 2025-04-07 那次 "add flow cache model" 之后，仓库里 cosyvoice.yaml 改名为 cosyvoice2.yaml、
    # 权重也换成 cache 版，需要更新的代码（CausalConditionalDecoder）才认，而本平台钉的
    # commit 86e26f54 是 2025-01 的，只认旧版权重。这一串 sha 对应旧版（2024-12-13）。
    [string]$ModelRevision = '9d5c22a8e62c4cb6f2c5ba3d276c19cb367c2f2a',
    # 模型根目录：默认放仓库内 engines\cosyvoice\pretrained_models（已被 .gitignore 覆盖）。
    # 想放别的盘就传 -ModelsRoot 'E:\AI\CosyVoice\CosyVoice\pretrained_models'
    [string]$ModelsRoot = '',
    # 网络代理：git / conda / pip 共用。不传就用系统默认（git 全局配置、环境变量）。
    # 本机实测：git 全局配置指向 7890（死的），真正能用的是 7897，conda 直连镜像也会被 RST，
    # 所以这台机器上要显式传 -Proxy http://127.0.0.1:7897
    [string]$Proxy = '',
    [switch]$SkipEnv,
    [switch]$SkipModels
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$cvd = Join-Path $root 'engines\cosyvoice'
if (-not $ModelsRoot) { $ModelsRoot = Join-Path $cvd 'pretrained_models' }
$modelPath = Join-Path $ModelsRoot $ModelDirName
$lock = Join-Path $PSScriptRoot 'cosyvoice-requirements.lock.txt'

# 代理同时喂给 git / conda / pip：conda 与 pip 都读标准环境变量，只影响本脚本的子进程
if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
}

# Windows 控制台是 GBK：一律英文输出 + [OK]/[!!]，不用 ✓/✗
function Step([string]$n, [string]$msg) { Write-Host "[$n] $msg" }
function Fail([string]$msg) { Write-Host "[!!] $msg" -ForegroundColor Red; throw $msg }

# ---------- 1/7 定位 conda ----------
Step '1/7' 'locate conda'
# 只用真正的可执行文件：conda 在部分 shell 里被 conda init 包成函数/别名，
# Get-Command conda 会返回 "Conda" 这种名字，后面 & $conda 就直接找不到了
$conda = (Get-Command conda.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $conda -or -not (Test-Path $conda)) {
    $candidates = @(
        (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'anaconda3\Scripts\conda.exe'),
        'C:\ProgramData\Miniconda3\Scripts\conda.exe',
        'C:\ProgramData\Anaconda3\Scripts\conda.exe'
    )
    $conda = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $conda) {
    Fail @'
conda not found. CosyVoice needs conda because pynini has no Windows wheel.
  Install Miniconda: https://docs.conda.io/en/latest/miniconda.html
  Then re-run this script (no need to restart the shell if you use the full path).
'@
}
Write-Host "      conda: $conda"

# git 全局代理指向没启动的本地端口是这台机器上的常见故障（AGENTS.md §8），
# 所以每次 git 调用都按「-Proxy → 全局配置 → 无代理」依次重试，不让它阻塞整套还原
function Invoke-Git {
    param([string[]]$GitArgs, [string]$What)

    $attempts = @()
    if ($Proxy) { $attempts += , @('-c', "http.proxy=$Proxy", '-c', "https.proxy=$Proxy") }
    $attempts += , @()
    $attempts += , @('-c', 'http.proxy=', '-c', 'https.proxy=')

    $n = 0
    foreach ($pre in $attempts) {
        $n++
        & git @pre @GitArgs
        if ($LASTEXITCODE -eq 0) { return }
        if ($n -lt $attempts.Count) { Write-Host '      git failed, trying next proxy setting' }
    }
    Fail "$What failed. Check network, or pass -Proxy, or use -CosyVoiceRepo for a mirror."
}

# pip 同理：这台机器上 pip 全局配置指向的国内镜像时通时不通，
# 失败一次就换官方源重试（配合 -Proxy 用）
function Invoke-Pip {
    param([string[]]$PipArgs, [string]$What, [switch]$RetryWithPypi)

    & $py -m pip install --quiet @PipArgs
    if ($LASTEXITCODE -eq 0) { return }
    if ($RetryWithPypi) {
        Write-Host '      pip failed, retrying with pypi.org index'
        & $py -m pip install --quiet @PipArgs --index-url https://pypi.org/simple
        if ($LASTEXITCODE -eq 0) { return }
    }
    Fail "$What failed"
}

# ---------- 2/7 仓库对齐到固定 commit ----------
Step '2/7' "cosyvoice repo -> $cvd"
if (Test-Path (Join-Path $cvd '.git')) {
    # 本地已有目标 commit 就不联网
    & git -C $cvd cat-file -e "$CosyVoiceSha^{commit}" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      commit $CosyVoiceSha already present, skip fetch"
    }
    else {
        Invoke-Git -GitArgs @('-C', $cvd, 'fetch', 'origin') -What 'git fetch'
    }
}
else {
    New-Item -ItemType Directory -Force -Path (Split-Path $cvd -Parent) | Out-Null
    Invoke-Git -GitArgs @('clone', $CosyVoiceRepo, $cvd) -What 'git clone'
}
Invoke-Git -GitArgs @('-C', $cvd, 'checkout', '--detach', $CosyVoiceSha) -What 'git checkout'

# Matcha-TTS 是子模块，缺了 flow 模型加载不了（不是可选依赖）
Invoke-Git -GitArgs @('-C', $cvd, 'submodule', 'update', '--init', '--recursive') -What 'git submodule update'
if (-not (Test-Path (Join-Path $cvd 'third_party\Matcha-TTS'))) { Fail 'third_party\Matcha-TTS missing' }
Write-Host "      HEAD -> $CosyVoiceSha"

# ---------- 3/7 conda 环境 ----------
Step '3/7' "conda env $EnvName (python 3.10)"
$envs = (& $conda env list --json | ConvertFrom-Json).envs
$prefix = $envs | Where-Object { (Split-Path $_ -Leaf) -eq $EnvName } | Select-Object -First 1
if ($prefix) {
    Write-Host "      exists: $prefix"
}
elseif ($SkipEnv) {
    Fail "conda env '$EnvName' not found and -SkipEnv was given"
}
else {
    # 显式走 conda-forge：避开 defaults 频道的 ToS，且保证和 pynini 同一个 solver 域
    & $conda create -y -n $EnvName -c conda-forge 'python=3.10'
    if ($LASTEXITCODE -ne 0) { Fail 'conda create failed' }
    $envs = (& $conda env list --json | ConvertFrom-Json).envs
    $prefix = $envs | Where-Object { (Split-Path $_ -Leaf) -eq $EnvName } | Select-Object -First 1
    if (-not $prefix) { Fail "conda env $EnvName still not found after create" }
}
$py = Join-Path $prefix 'python.exe'
if (-not (Test-Path $py)) { Fail "python.exe not found in $prefix" }

if (-not $SkipEnv) {
    # ---------- 4/7 pynini + openfst（只能 conda） ----------
    # pynini 会带 openfst 作为依赖，不用单独点名
    Step '4/7' 'conda-forge: pynini (pulls openfst)'
    & $conda install -y -n $EnvName -c conda-forge 'pynini=2.1.5'
    if ($LASTEXITCODE -ne 0) { Fail 'conda install pynini failed' }

    # ---------- 5/7 torch cu121 + 锁定依赖 ----------
    Step '5/7' 'pip: torch 2.3.1+cu121, then lock file'

    # torch 必须从 PyTorch 官方 index 拿，PyPI 上的 Windows wheel 是 CPU 版
    Invoke-Pip -PipArgs @('torch==2.3.1', 'torchaudio==2.3.1', '--index-url', 'https://download.pytorch.org/whl/cu121') `
        -What 'pip install torch'
    # openai-whisper 还是 setup.py 时代，import pkg_resources 要 setuptools；
    # 而 setuptools 81 起删了 pkg_resources，所以这里必须钉 80.x
    Invoke-Pip -PipArgs @('setuptools==80.9.0', 'wheel') -What 'pip install setuptools' -RetryWithPypi
    # whisper 只有 sdist。pip 默认开 build isolation，会在一份临时环境里装**最新** setuptools，
    # 把上面那个 pin 完全无视掉（报错栈里路径是 Temp\pip-build-env-*），所以它必须单独 --no-build-isolation
    Invoke-Pip -PipArgs @('--no-build-isolation', 'openai-whisper==20231117') -What 'pip install openai-whisper' -RetryWithPypi
    # pynini 已由 conda 装好，pip 看到满足就不会去编译源码包
    Invoke-Pip -PipArgs @('-r', $lock) -What 'pip install -r lock' -RetryWithPypi

    # 自检：这三个 import 过了，才算"依赖齐了"（不加载模型，几秒钟）
    Step '5/7' 'self-test imports'
    $probe = Join-Path $env:TEMP 'cvd_selftest.py'
    $code = @"
import sys
sys.path.insert(0, r'$cvd')
sys.path.insert(0, r'$(Join-Path $cvd 'third_party\Matcha-TTS')')
import torch, pynini, onnxruntime
from cosyvoice.cli.cosyvoice import CosyVoice2
print('[OK] torch', torch.__version__, '| cuda', torch.cuda.is_available(), '| onnxruntime', onnxruntime.__version__)
"@
    Set-Content -Path $probe -Value $code -Encoding ASCII
    & $py $probe
    if ($LASTEXITCODE -ne 0) { Fail 'import self-test failed (see traceback above)' }
    Remove-Item $probe -Force
}
else {
    Step '4/7' 'skipped (-SkipEnv)'
    Step '5/7' 'skipped (-SkipEnv)'
}

# ---------- 6/7 模型 ----------
# 不用 modelscope 的 snapshot_download：它只认分支/tag，不认 commit sha，
# 而我们必须钉 sha（见 -ModelRevision 的注释）。这里直接用 modelscope 的文件下载 API，
# 按 sha 逐个文件拉，已存在且大小对的跳过（天然断点续传）。
Step '6/7' "model $ModelId @ $($ModelRevision.Substring(0,8)) -> $modelPath"

function Get-RemoteFiles {
    param([string]$Root = '')
    $uri = "$modelApi/repo/files?Revision=$ModelRevision&Root=$Root"
    $req = @{ Uri = $uri; TimeoutSec = 60; UseBasicParsing = $true }
    if ($Proxy) { $req.Proxy = $Proxy }
    $resp = Invoke-RestMethod @req
    $out = @()
    foreach ($f in $resp.Data.Files) {
        if ($f.Type -eq 'tree') {
            # 子目录递归（Qwen2-0.5B-CosyVoice-BlankEN 里是 LLM 的词表 + 底座）
            $child = if ($Root) { "$Root/$($f.Name)" } else { $f.Name }
            $out += @(Get-RemoteFiles -Root $child)
        }
        else {
            $path = if ($Root) { "$Root/$($f.Name)" } else { $f.Name }
            $out += [pscustomobject]@{ Path = $path; Size = [int64]$f.Size }
        }
    }
    return $out
}

if ($SkipModels) {
    Write-Host '      skipped (-SkipModels)'
}
elseif (Test-Path (Join-Path $modelPath 'llm.pt')) {
    Write-Host "      already present: $modelPath"
}
else {
    $modelApi = "https://www.modelscope.cn/api/v1/models/$ModelId"
    New-Item -ItemType Directory -Force -Path $modelPath | Out-Null

    $files = @(Get-RemoteFiles)
    $totalGB = [math]::Round(($files | Measure-Object Size -Sum).Sum / 1GB, 2)
    Write-Host "      $($files.Count) files, $totalGB GB total"

    $done = 0
    foreach ($f in $files) {
        $dest = Join-Path $modelPath ($f.Path -replace '/', '\')
        $dir = Split-Path $dest -Parent
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        # 大小对得上就跳过：bst 被打断后重跑不用从头下 3.7 GB
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -eq $f.Size)) {
            $done++
            continue
        }
        $uri = "$modelApi/repo?Revision=$ModelRevision&FilePath=$($f.Path)"
        $req = @{ Uri = $uri; OutFile = $dest; TimeoutSec = 3600; UseBasicParsing = $true }
        if ($Proxy) { $req.Proxy = $Proxy }
        try {
            Invoke-WebRequest @req
        }
        catch {
            Fail "download $($f.Path) failed: $($_.Exception.Message)"
        }
        $done++
        Write-Host ("      [{0}/{1}] {2}  {3:N1} MB" -f $done, $files.Count, $f.Path, ($f.Size / 1MB))
    }

    if (-not (Test-Path (Join-Path $modelPath 'cosyvoice.yaml'))) {
        Fail "cosyvoice.yaml missing after download（revision 选错了？旧版才有这个文件）"
    }
    # 目录名对不上：模型仓库里叫 Qwen2-0.5B-CosyVoice-BlankEN，
    # 而 cosyvoice/cli/cosyvoice.py:136 硬编码找 CosyVoice-BlankEN（LLM 底座 + 词表）
    $src = Join-Path $modelPath 'Qwen2-0.5B-CosyVoice-BlankEN'
    $dst = Join-Path $modelPath 'CosyVoice-BlankEN'
    if ((Test-Path $src) -and -not (Test-Path $dst)) {
        Move-Item $src $dst
        Write-Host '      compat: Qwen2-0.5B-CosyVoice-BlankEN -> CosyVoice-BlankEN'
    }
}

# ---------- 7/7 .env ----------
Step '7/7' 'write .env (PLATFORM_COSYVOICE_*)'
$voicesDir = Join-Path $root 'data\voices'
New-Item -ItemType Directory -Force -Path $voicesDir | Out-Null

# 幂等：先剔除已有的同类键，再按当前布局追加，不碰 .env 里其它内容
$envPath = Join-Path $root '.env'
$lines = @()
if (Test-Path $envPath) { $lines = @([IO.File]::ReadAllLines($envPath)) }
$keep = $lines | Where-Object { $_ -notmatch '^\s*PLATFORM_COSYVOICE_(PYTHON|HOME|MODEL|VOICES)\s*=' }
$new = @(
    '# CosyVoice 本机布局（由 scripts\bootstrap-cosyvoice.ps1 写入，换机器重跑即可）',
    "PLATFORM_COSYVOICE_PYTHON=$py",
    "PLATFORM_COSYVOICE_HOME=$cvd",
    "PLATFORM_COSYVOICE_MODEL=$modelPath",
    "PLATFORM_COSYVOICE_VOICES=$voicesDir"
)
# 显式 UTF-8 无 BOM：pydantic-settings 能读带 BOM 的 .env，但 BOM 会污染第一个键名
$out = (@($keep) + $new) -join "`n"
[IO.File]::WriteAllText($envPath, $out + "`n", (New-Object System.Text.UTF8Encoding($false)))
Write-Host "      -> $envPath"

Write-Host ''
Write-Host '[OK] done. next:'
Write-Host '  1) uv run platform doctor'
Write-Host "  2) voices are user data (not in git): put <name>.wav + <name>.txt into $voicesDir"
Write-Host '     (legacy format also works: point PLATFORM_COSYVOICE_CONFIG at your config.txt)'
Write-Host '  3) start the server and run audio.tts / audio.voice_design once from the web UI'

# 显式退出码：脚本里跑过 git/conda/pip，$LASTEXITCODE 会残留非 0，
# 不写这句的话「脚本退出码 0 = 成功」会被误判成失败
exit 0
