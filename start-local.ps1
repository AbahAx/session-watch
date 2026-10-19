param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Error '请先按 README 创建 .venv 并安装 Python 依赖。'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
    Write-Error '请先在 frontend 目录执行 pnpm install 和 pnpm build（需要 Node.js 22.12+）。'
}
Write-Host "Session Watch: http://127.0.0.1:$Port"
& $pythonPath -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port $Port --no-access-log --no-proxy-headers
exit $LASTEXITCODE

