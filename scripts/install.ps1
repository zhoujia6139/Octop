$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
}
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not (Test-Path (Join-Path $ProjectRoot "pyproject.toml"))) { throw "Use a complete Octop checkout." }
uv tool install --from $ProjectRoot "octop[cloud]" @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Initialize: octop-cloud-init"
Write-Host "Start cloud management: octop-cloud"
Write-Host "For local Agent execution use Octop Desktop."
