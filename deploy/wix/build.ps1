# Phase 10 - builds RemoteBridgeHost.msi end to end.
#
# NOT RUN in the environment this was written in - this is a Windows-only
# script (PyInstaller producing a Windows .exe, then WiX's candle/light)
# and this project was built in a Linux sandbox with neither tool
# available and no network to install them. See ../README.md. Written
# and reviewed by hand against PyInstaller's and WiX v3's documented
# CLIs; treat it as reviewed, not proven, until it's actually run.
#
# Prerequisites (on the Windows machine this runs on):
#   pip install pyinstaller
#   WiX Toolset v3.11+ installed, with candle.exe/light.exe on PATH
#
# Usage, from the repo root:
#   .\deploy\wix\build.ps1
#   .\deploy\wix\build.ps1 -DeployConfig C:\path\to\acme-deploy_config.json
#
# Output: deploy\pyinstaller\dist\RemoteBridgeHost.msi

param(
    [string]$DeployConfig = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DistDir = Join-Path $RepoRoot "deploy\pyinstaller\dist"

Write-Host "==> Freezing host_p12.py with PyInstaller"
pyinstaller (Join-Path $RepoRoot "deploy\pyinstaller\host.spec") --distpath $DistDir --workpath (Join-Path $RepoRoot "deploy\pyinstaller\build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path (Join-Path $DistDir "RemoteBridgeHost.exe"))) {
    throw "Expected RemoteBridgeHost.exe was not produced - check the PyInstaller output above"
}

Write-Host "==> Preparing deploy_config.json"
$DistDeployConfig = Join-Path $DistDir "deploy_config.json"
if ($DeployConfig -ne "" -and (Test-Path $DeployConfig)) {
    Copy-Item $DeployConfig $DistDeployConfig -Force
    Write-Host "    Using org config: $DeployConfig"
} elseif (-not (Test-Path $DistDeployConfig)) {
    # WiX needs this file to exist even if IT hasn't customized it yet -
    # an empty one just means the installed host starts unconfigured,
    # identical to Phase 9. See ../deploy_config.template.json.
    Set-Content -Path $DistDeployConfig -Value "{}"
    Write-Host "    No org config given - writing an empty one (host starts unconfigured)"
}

Write-Host "==> Compiling WiX source (candle)"
candle.exe -dSourceDir="$DistDir" -out (Join-Path $DistDir "Product.wixobj") `
    (Join-Path $RepoRoot "deploy\wix\Product.wxs")
if ($LASTEXITCODE -ne 0) { throw "candle.exe failed" }

Write-Host "==> Linking the MSI (light)"
light.exe -out (Join-Path $DistDir "RemoteBridgeHost.msi") (Join-Path $DistDir "Product.wixobj")
if ($LASTEXITCODE -ne 0) { throw "light.exe failed" }

Write-Host "==> Done: $(Join-Path $DistDir 'RemoteBridgeHost.msi')"
Write-Host "    Publish its version/URL/SHA-256 in the admin console's Deployment page"
Write-Host "    so enrolled hosts with auto_update on can find it."
