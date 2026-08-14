param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8003,
    [string]$HostAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到项目虚拟环境。请先在项目目录执行：python -m venv .venv"
}

Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

Write-Host "正在启动 Harness Agent 投研平台..."
Write-Host "访问地址：http://${HostAddress}:$Port"
Write-Host "停止服务：在当前窗口按 Ctrl+C"

& $Python -m uvicorn agent_platform.api.main:app --host $HostAddress --port $Port
exit $LASTEXITCODE
