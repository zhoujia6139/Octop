@echo off
where uv >nul 2>nul
if errorlevel 1 (
  echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
  exit /b 1
)
uv tool install "octop[cloud]" %*
if errorlevel 1 exit /b %errorlevel%
echo Initialize: octop-cloud-init
echo Start cloud management: octop-cloud
echo For local Agent execution use Octop Desktop.
