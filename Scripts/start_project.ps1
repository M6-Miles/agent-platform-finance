param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8003,
    [string]$HostAddress = "127.0.0.1",
    [switch]$NoStopExisting
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到项目虚拟环境。请先在项目目录执行：python -m venv .venv"
}

Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

if (-not $NoStopExisting) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        $commandLine = [string]$existing.CommandLine
        if ($commandLine -notmatch "uvicorn" -or $commandLine -notmatch "agent_platform\.api\.main") {
            throw "端口 $Port 已被非本项目进程占用（PID $($listener.OwningProcess)），为避免误关程序已停止启动。"
        }
        Write-Host "正在关闭端口 $Port 上的旧项目进程（PID $($listener.OwningProcess)）..."
        Stop-Process -Id $listener.OwningProcess -Force
        Wait-Process -Id $listener.OwningProcess -Timeout 10 -ErrorAction SilentlyContinue
    }
}

Write-Host "正在启动 Harness Agent 投研平台并执行自检..."
$arguments = @(
    "-m", "uvicorn", "agent_platform.api.main:app",
    "--host", $HostAddress, "--port", [string]$Port
)
$process = Start-Process -FilePath $Python -ArgumentList $arguments -PassThru -NoNewWindow
$baseUrl = "http://${HostAddress}:$Port"

try {
    $healthy = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        if ($process.HasExited) { throw "后端启动失败，退出码 $($process.ExitCode)" }
        try {
            $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 2
            if ($health.status -eq "ok") { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $healthy) { throw "后端在 15 秒内未通过健康检查" }
    Write-Host "后端自检通过。访问地址：$baseUrl"
    try {
        $providers = Invoke-RestMethod -Uri "$baseUrl/health/providers" -TimeoutSec 3
        foreach ($name in @("market_quote", "open_meteo", "deepseek")) {
            $state = $providers.providers.$name.status
            Write-Host "  $name : $state"
        }
    } catch {
        Write-Warning "Provider 自检仍在后台进行，可在页面顶部查看最终状态。"
    }
    Write-Host "停止服务：在当前窗口按 Ctrl+C"
    while (-not $process.HasExited) { Start-Sleep -Seconds 1 }
    exit $process.ExitCode
} finally {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
