# One-click installer for hermes-update-check on Windows (PowerShell 5.1+ / 7+).
#
#   irm https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.ps1))) -Dir <path> -NoHook
#
# It clones the repository, creates an isolated virtual environment, installs the
# package, links the `hermes-update-check` command and runs a first check.
# It never uses sudo/admin, never modifies HERMES_HOME and never runs `hermes update`.

[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:USERPROFILE 'projects\hermes-update-check'),
    [string]$Repo = 'https://github.com/xinian5216/hermes-update-check.git',
    [string]$Branch = 'main',
    [string]$BinDir = (Join-Path $env:USERPROFILE '.local\bin'),
    [switch]$NoHook
)

$ErrorActionPreference = 'Stop'

function Write-Step($message) { Write-Host "==> $message" }
function Write-Warn($message) { Write-Warning $message }

# ---------------------------------------------------------------- 1. python ---
# Probe by executing the candidate: the WindowsApps python3.exe stubs exist on
# PATH but fail, so `Get-Command` alone is not enough.
$python = $null
foreach ($candidate in @('python', 'py', 'python3')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        $ok = & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            Write-Step "python:   $candidate ($version)"
            break
        }
    } catch { continue }
}
if (-not $python) { throw 'python >= 3.9 not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").' }

# --------------------------------------------------------------- 2. checkout ---
$git = Get-Command git -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $Dir '.git')) {
    Write-Step "updating: $Dir"
    git -C $Dir fetch --quiet origin $Branch
    git -C $Dir checkout --quiet $Branch 2>$null
    git -C $Dir pull --ff-only --quiet
} elseif ($git) {
    Write-Step "cloning:  $Repo -> $Dir"
    New-Item -ItemType Directory -Force -Path (Split-Path $Dir) | Out-Null
    git clone --quiet --branch $Branch --depth 1 $Repo $Dir
} else {
    Write-Step 'git not found; downloading the source archive instead'
    $slug = ($Repo -replace '^https://github\.com/', '') -replace '\.git$', ''
    $url = "https://github.com/$slug/archive/refs/heads/$Branch.zip"
    $zip = Join-Path $env:TEMP 'hermes-update-check.zip'
    $extract = Join-Path $env:TEMP ('hermes-update-check-' + [guid]::NewGuid().ToString('N'))
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $extract -Force
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    Copy-Item -Path (Join-Path $extract '*\*') -Destination $Dir -Recurse -Force
    Remove-Item -Recurse -Force $zip, $extract
}
if (-not (Test-Path (Join-Path $Dir 'pyproject.toml'))) { throw "$Dir does not look like hermes-update-check" }

# ------------------------------------------------------------------ 3. venv ---
$venv = Join-Path $Dir '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Step "creating virtual environment in $venv"
    if (Get-Command uv -ErrorAction SilentlyContinue) { uv venv $venv --quiet }
    else { & $python -m venv $venv }
}

Write-Step 'installing package (this pulls PyYAML and rich)'
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --quiet --python $venvPython -e $Dir
} else {
    & $venvPython -m pip install --quiet --upgrade pip | Out-Null
    & $venvPython -m pip install --quiet -e $Dir
}

# -------------------------------------------------------------- 4. launcher ---
$entry = Join-Path $venv 'Scripts\hermes-update-check.exe'
if (-not (Test-Path $entry)) { throw "the command entry point was not created ($entry)" }
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Set-Content -Path (Join-Path $BinDir 'hermes-update-check.cmd') -Encoding ASCII -Value @"
@echo off
"$entry" %*
"@
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$BinDir*") {
    Write-Warn "$BinDir is not on your PATH. Add it with:  setx PATH `"$userPath;$BinDir`""
}

# ---------------------------------------------------------------- 5. verify ---
Write-Step 'verifying installation'
& $venvPython -m hermes_update_check version | Out-Null

# ------------------------------------------------------------------ 6. hook ---
if (-not $NoHook -and (Test-Path (Join-Path $Dir '.git'))) {
    git -C $Dir config core.hooksPath .githooks
    Write-Step 'privacy:  pre-commit secret scan enabled (.githooks)'
}

# ------------------------------------------------------------- 7. next steps ---
Write-Host @"

Installed hermes-update-check in $Dir

Next:
  1. see what it thinks about the current release:
       & "$entry" check
       & "$entry" report            # full factor-by-factor detail
  2. optional Telegram/webhook alerts: put the token in the environment (the
     Hermes .env file is a good place) and enable the channel in the config file.
  3. optional daily watch (nothing is ever updated automatically):
       schtasks /create /tn hermes-update-watch /sc daily /st 09:00 ^
         /tr "\"$entry\" watch"

Exit codes: 0 = can update / already current, 10 = wait, 11 = not enough data,
12 = health check failed, 13 = cancelled, 14 = preflight failed.
This tool only ever *advises*; `hermes-update-check update` asks for confirmation.
"@
