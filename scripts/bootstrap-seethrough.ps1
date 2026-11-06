# See-through env bootstrap (Windows + NVIDIA). ASCII only: PS 5.1 reads BOM-less
# .ps1 as GBK and mangles CJK, so keep this file plain ASCII.
#
# What it does:
#   1. clone shitagaki-lab/see-through at a pinned commit
#   2. create a uv venv (python 3.12) OUTSIDE the repo
#   3. install torch 2.8.0+cu128 + the repo requirements
#   4. copy common\assets -> assets (the README uses `ln -sf`, Windows has no ln)
#   5. verify CUDA
#
# Weights (~9.5 GB) are NOT downloaded here: the upstream script pulls them from
# HF on first run into PLATFORM_SEETHROUGH_HF_HOME. Set that var before running.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap-seethrough.ps1
#   ... -RepoDir D:\engines\see-through -EnvDir D:\envs\see-through -HfHome D:\models\hf-seethrough
#   ... -SkipEnv          # repo already set up, only re-check assets/GPU
param(
    [string]$RepoDir = "D:\engines\see-through",
    [string]$EnvDir  = "D:\envs\see-through",
    [string]$HfHome  = "D:\models\hf-seethrough",
    [string]$Commit  = "df019de5129d6c4b406587a14c3501669441a783",
    [switch]$SkipEnv
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host "[!!] $msg" -ForegroundColor Red; exit 1 }

# ---- 0. preconditions ----
Write-Step "0/6 preconditions"
foreach ($bin in @("git", "uv", "nvidia-smi")) {
    if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) { Fail "$bin not found in PATH" }
}
Write-Host "    GPU: $((nvidia-smi --query-gpu=name,memory.total --format=csv,noheader))"

# The machine's global git config may point at a proxy that is not running
# (http.proxy=127.0.0.1:7890 is a common leftover). pip/uv need git for the
# git+https requirements, so point GIT_CONFIG_GLOBAL at a proxy-free temp file
# instead of editing the user's global config.
$noproxy = Join-Path $env:TEMP "gitconfig-noproxy"
Set-Content -Path $noproxy -Value "[core]`n`tautocrlf = false`n" -Encoding ascii
$env:GIT_CONFIG_GLOBAL = $noproxy

# HF mirror + no Xet: the mirror does not proxy Xet storage, and without this
# flag `hf_hub_download` happily saves a few-hundred-byte LFS pointer.
$env:HF_ENDPOINT = "https://hf-mirror.com"
$env:HF_HUB_DISABLE_XET = "1"
$env:HF_HOME = $HfHome

# ---- 1. clone ----
Write-Step "1/6 clone $RepoDir @ $($Commit.Substring(0,7))"
if (Test-Path (Join-Path $RepoDir ".git")) {
    Push-Location $RepoDir
    $head = (git rev-parse HEAD 2>$null)
    if ($head -eq $Commit) {
        Write-Host "    already at pinned commit, nothing to fetch"
    } else {
        Write-Host "    on $($head.Substring(0,7)), fetching pinned commit"
        # Fetching a bare SHA is not always allowed by the remote. If it fails but
        # the commit is already in the local object db (earlier clone), carry on.
        git fetch --depth 1 origin $Commit
        if ($LASTEXITCODE -ne 0) {
            git cat-file -e "$Commit^{commit}" 2>$null
            if ($LASTEXITCODE -ne 0) {
                Pop-Location
                Fail "cannot obtain commit $Commit (network / proxy); clone fresh into an empty dir"
            }
            Write-Host "    fetch refused, commit already local, continuing"
        }
        git checkout --force $Commit
        $code = $LASTEXITCODE
        Pop-Location
        if ($code -ne 0) { Fail "checkout $Commit failed" }
    }
} else {
    git clone https://github.com/shitagaki-lab/see-through.git $RepoDir
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed (proxy / network)" }
    Push-Location $RepoDir
    git checkout --force $Commit
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "checkout $Commit failed" }
}

if (-not $SkipEnv) {
    # ---- 2. venv ----
    Write-Step "2/6 create uv venv $EnvDir (python 3.12)"
    uv venv $EnvDir --python 3.12
    if ($LASTEXITCODE -ne 0) { Fail "uv venv failed" }
    $py = Join-Path $EnvDir "Scripts\python.exe"

    # ---- 3. torch ----
    Write-Step "3/6 install torch 2.8.0+cu128 (~2.5 GB)"
    uv pip install --python $py torch==2.8.0+cu128 torchvision==0.23.0+cu128 torchaudio==2.8.0+cu128 --index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { Fail "torch install failed" }

    # ---- 4. requirements ----
    Write-Step "4/6 install requirements (5-10 min)"
    Write-Host "    note: skip the detectron2 / mmdet / SAM2 tiers, CLI inference does not need them"
    Push-Location $RepoDir
    uv pip install --python $py -r requirements.txt
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "requirements install failed" }
} else {
    $py = Join-Path $EnvDir "Scripts\python.exe"
}

# ---- 5. assets + workspace ----
Write-Step "5/6 fix assets dir and workspace"
if (-not (Test-Path (Join-Path $RepoDir "assets"))) {
    Copy-Item -Recurse (Join-Path $RepoDir "common\assets") (Join-Path $RepoDir "assets")
    Write-Host "    copied common\assets -> assets"
} else {
    Write-Host "    assets already present"
}
New-Item -ItemType Directory -Force -Path (Join-Path $RepoDir "workspace\layerdiff_output") | Out-Null

# ---- 6. verify ----
Write-Step "6/6 verify CUDA"
@'
import torch
ok = torch.cuda.is_available()
print("torch", torch.__version__, "cuda", ok, torch.cuda.get_device_name(0) if ok else "N/A")
raise SystemExit(0 if ok else 1)
'@ | Out-File -FilePath (Join-Path $RepoDir "_check_gpu.py") -Encoding utf8
& $py (Join-Path $RepoDir "_check_gpu.py")
if ($LASTEXITCODE -ne 0) { Fail "CUDA unavailable: check the NVIDIA driver" }

Write-Host "`n[OK] env ready: $py" -ForegroundColor Green
Write-Host "Add these to the repo-root .env (PLATFORM_SEETHROUGH_*):" -ForegroundColor Yellow
Write-Host "  PLATFORM_SEETHROUGH_PYTHON=$($py -replace '\\','/')"
Write-Host "  PLATFORM_SEETHROUGH_HOME=$($RepoDir -replace '\\','/')"
Write-Host "  PLATFORM_SEETHROUGH_HF_HOME=$($HfHome -replace '\\','/')"
Write-Host "First task downloads ~9.5 GB of weights into HF_HOME." -ForegroundColor Yellow
