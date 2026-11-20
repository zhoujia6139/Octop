$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
}
uv tool install "octop[cloud]" @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Initialize: octop-cloud-init"
Write-Host "Start cloud management: octop-cloud"
Write-Host "For local Agent execution use Octop Desktop."
