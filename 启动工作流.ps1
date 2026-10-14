$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8767/healthz' -TimeoutSec 2
    if ($taskHealth.status -eq 'ok') {
        Start-Process 'http://127.0.0.1:8767/'
        exit 0
    }
} catch {}
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '无法创建运行环境，请安装 Python 3.11 或更新版本。' }
    & $taskPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络。' }
}
& $taskPython -m visit_workflow serve
if ($LASTEXITCODE -ne 0) { Read-Host '服务启动失败，按回车关闭' }
