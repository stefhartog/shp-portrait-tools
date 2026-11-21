<#
SHP Portrait Tools installer for Windows (current user, no administrator rights).

    irm https://github.com/stefhartog/shp-portrait-tools/releases/latest/download/install.ps1 | iex

With options (a specific release, a Desktop shortcut, no launch at the end):

    & ([scriptblock]::Create((irm https://github.com/stefhartog/shp-portrait-tools/releases/latest/download/install.ps1))) -Version v0.2.0 -Desktop -NoLaunch

Run it again to update to the newest release. It:
  1. checks for an NVIDIA GPU with at least 8 GB of memory and a CUDA 13 driver (580 or newer), and WebView2;
  2. installs uv for the current user if it is missing;
  3. downloads the release's source into the install folder (default %LOCALAPPDATA%\Programs\shp-portrait-tools);
  4. installs Python 3.13 and the locked dependencies with uv;
  5. downloads and verifies the models (see MODELS.md; they are not redistributed by this project);
  6. writes launcher.json if there is none, adds a Start Menu shortcut (never replacing an existing one) and starts Tools.
Settings, presets and models live in %LOCALAPPDATA% / %APPDATA%\shp-portrait-tools and survive updates.
#>
[CmdletBinding()]
param(
    [string]$Version = $(if ($env:SHP_TOOLS_VERSION) { $env:SHP_TOOLS_VERSION } else { "latest" }),
    [string]$InstallDir = $(if ($env:SHP_TOOLS_INSTALL_DIR) { $env:SHP_TOOLS_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "Programs\shp-portrait-tools" }),
    [switch]$Desktop,
    [switch]$NoLaunch,
    [switch]$SkipGpuCheck,
    # Development: install from a local checkout (its tracked files via git) instead of a GitHub release.
    [string]$SourceDir,
    [string]$SourceRef = "HEAD"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = "stefhartog/shp-portrait-tools"
$Marker = ".shp-portrait-tools-install"
$MinMemoryMiB = 7800   # 8 GB cards report slightly under 8192 MiB
$MinDriver = 580       # CUDA 13 user-space libraries

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; throw $text }

function Test-Gpu {
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (-not $smi) {
        foreach ($candidate in "$env:SystemRoot\System32\nvidia-smi.exe", "$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe") {
            if (Test-Path $candidate) { $smi = $candidate; break }
        }
    }
    if (-not $smi) { Fail "No NVIDIA driver found (nvidia-smi is missing). Install the current NVIDIA driver from https://www.nvidia.com/drivers and run this again." }
    $lines = & $smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $lines) { Fail "nvidia-smi could not read the GPU. Reinstall or update the NVIDIA driver." }
    $gpus = @($lines | ForEach-Object {
        $name, $memory, $driver = $_.Split(",") | ForEach-Object { $_.Trim() }
        [pscustomobject]@{ Name = $name; MemoryMiB = [int]$memory; Driver = $driver }
    })
    $best = $gpus | Sort-Object MemoryMiB -Descending | Select-Object -First 1
    $major = [int]($best.Driver.Split(".")[0])
    Write-Host "    GPU: $($best.Name), $([math]::Round($best.MemoryMiB / 1024, 1)) GB, driver $($best.Driver)"
    if ($best.MemoryMiB -lt $MinMemoryMiB) { Fail "SHP Portrait Tools needs an NVIDIA GPU with at least 8 GB of memory; $($best.Name) has $([math]::Round($best.MemoryMiB / 1024, 1)) GB." }
    if ($major -lt $MinDriver) { Fail "NVIDIA driver $($best.Driver) is too old; version $MinDriver or newer is needed (CUDA 13). Update it from https://www.nvidia.com/drivers." }
}

function Test-WebView2 {
    $id = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    foreach ($key in "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$id", "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id", "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id") {
        $pv = (Get-ItemProperty -Path $key -Name pv -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne "0.0.0.0") { Write-Host "    WebView2: $pv"; return }
    }
    Fail "The Microsoft Edge WebView2 Runtime is missing. Install the Evergreen runtime from https://developer.microsoft.com/microsoft-edge/webview2/ and run this again."
}

function Get-Uv {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if ($uv) { return $uv }
    $local = Join-Path $HOME ".local\bin\uv.exe"
    if (-not (Test-Path $local)) {
        Step "Installing uv for the current user"
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }
    if (-not (Test-Path $local)) { Fail "uv was not installed to $local." }
    $env:Path = "$(Split-Path $local);$env:Path"
    return $local
}

function Resolve-Tag {
    if ($SourceDir) { return "local-" + (& git -C $SourceDir rev-parse --short HEAD) }
    if ($Version -ne "latest") { return $Version }
    $release = Invoke-RestMethod -Headers @{ "User-Agent" = "shp-portrait-tools-installer" } "https://api.github.com/repos/$Repo/releases/latest"
    return $release.tag_name
}

function Stop-IfRunning {
    $running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase) }
    if ($running) { Fail "SHP Portrait Tools is running from $InstallDir. Save your work, choose Quit Tools in its tray menu, then run this again." }
}

function Install-Source($tag) {
    $temp = Join-Path ([IO.Path]::GetTempPath()) ("shp-portrait-tools-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $temp | Out-Null
    try {
        $zip = Join-Path $temp "source.zip"
        if ($SourceDir) {
            Step "Packing the local checkout $SourceDir"
            & git -C $SourceDir archive --format=zip --prefix=local/ -o $zip $SourceRef
            if ($LASTEXITCODE -ne 0) { Fail "git archive failed in $SourceDir." }
        } else {
            Step "Downloading SHP Portrait Tools $tag"
            Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/tags/$tag.zip" -OutFile $zip
        }
        Expand-Archive -Path $zip -DestinationPath (Join-Path $temp "x")
        $source = @(Get-ChildItem (Join-Path $temp "x") -Directory)
        if ($source.Count -ne 1 -or -not (Test-Path (Join-Path $source[0].FullName "pyproject.toml"))) { Fail "Unexpected archive layout for $tag." }
        if (Test-Path $InstallDir) {
            if (-not (Test-Path (Join-Path $InstallDir $Marker))) {
                if (@(Get-ChildItem -Force $InstallDir).Count -gt 0) { Fail "$InstallDir exists and was not made by this installer. Choose another folder with -InstallDir." }
            }
            Stop-IfRunning
            # Replace the previous release's files; keep the environment so uv only applies the differences.
            Get-ChildItem -Force $InstallDir | Where-Object { $_.Name -ne ".venv" } | Remove-Item -Recurse -Force
        } else {
            New-Item -ItemType Directory -Path $InstallDir | Out-Null
        }
        Get-ChildItem -Force $source[0].FullName | Copy-Item -Destination $InstallDir -Recurse -Force
        Set-Content -Path (Join-Path $InstallDir $Marker) -Value $tag -Encoding ascii
    } finally {
        Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
    }
}

function Invoke-Uv {
    & $script:Uv --directory $InstallDir @args
    if ($LASTEXITCODE -ne 0) { Fail "uv $($args -join ' ') failed (exit $LASTEXITCODE)." }
}

if (-not [Environment]::Is64BitOperatingSystem) { Fail "SHP Portrait Tools needs 64-bit Windows." }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

Step "Checking the system"
if ($SkipGpuCheck) { Write-Host "    GPU check skipped (-SkipGpuCheck): Skin Tools analysis needs an NVIDIA GPU." -ForegroundColor Yellow } else { Test-Gpu }
Test-WebView2
$script:Uv = Get-Uv
$tag = Resolve-Tag
Install-Source $tag

Step "Installing Python 3.13 and dependencies (first run downloads a few GB)"
& $script:Uv python install 3.13 --no-bin   # managed Python only; no python3.13 shim on PATH
if ($LASTEXITCODE -ne 0) { Fail "uv python install 3.13 failed." }
Invoke-Uv sync --locked --extra gpu

$configPath = Join-Path $env:APPDATA "shp-portrait-tools\launcher.json"
Step "Downloading and verifying models"
$modelArgs = @()
if (Test-Path $configPath) {
    $models = (Get-Content -Raw $configPath | ConvertFrom-Json).models
    if ($models) { $modelArgs = @("--dest", $models) }
}
Invoke-Uv run --no-sync python -m shp_portrait_tools.fetch_models @modelArgs

if (-not (Test-Path $configPath)) {
    Step "Writing $configPath"
    Invoke-Uv run --no-sync shp-portrait-tools --init-config
}

Step "Adding the Start Menu shortcut"
$shortcutArgs = @("scripts/install_windows_shortcut.py", "--repo", $InstallDir, "--config", $configPath)
if ($Desktop) { $shortcutArgs += "--desktop" }
& $script:Uv --directory $InstallDir run --no-sync python @shortcutArgs
if ($LASTEXITCODE -ne 0) { Write-Host "    An existing shortcut was kept. If it points to another copy, remove it and run this again." -ForegroundColor Yellow }

Write-Host ""
Write-Host "SHP Portrait Tools $tag is installed in $InstallDir" -ForegroundColor Green
$pythonw = Join-Path $InstallDir ".venv\Scripts\pythonw.exe"
if (-not $NoLaunch) {
    Step "Starting SHP Portrait Tools"
    Start-Process -FilePath $pythonw -ArgumentList @("-m", "shp_portrait_tools.launcher", "--config", "`"$configPath`"") -WorkingDirectory $InstallDir
}
Write-Host "Photoshop users: install the connector from plugins\photoshop\releases and pair it in Settings & Info."
