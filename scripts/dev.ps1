# 一键启动（Windows PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\dev.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

Write-Host "==> 安装依赖（锁定版本）"
python -m pip install -q -r requirements-dev.lock
if ($LASTEXITCODE -ne 0) { python -m pip install -q -e ".[dev]" }
python -m pip install -q --no-deps -e .

if (-not (Test-Path "agent.config.toml")) {
    Write-Host "==> 未找到 agent.config.toml，复制示例配置（可只用环境变量填写模型密钥）"
    Copy-Item "agent.config.example.toml" "agent.config.toml"
}

if ((Test-Path "web") -and (-not (Test-Path "web\dist"))) {
    Write-Host "==> 构建前端"
    Push-Location web
    npm ci --no-audit --no-fund
    npm run build
    Pop-Location
}

$env:PYTHONPATH = "$root\src"
$env:QAGENT_WORKER = "external"

Write-Host "==> 数据库迁移"
python -m quant_agent.cli migrate

Write-Host "==> 启动 worker（日志：logs\worker.log）"
New-Item -ItemType Directory -Force -Path logs | Out-Null
$worker = Start-Process -FilePath python -ArgumentList "-m","quant_agent.cli","worker","--slots","2" `
    -RedirectStandardOutput "logs\worker.log" -RedirectStandardError "logs\worker.err.log" -PassThru -WindowStyle Hidden

try {
    Write-Host "==> 启动 API：http://127.0.0.1:8000（API 文档 /docs）。第一个注册的账号成为管理员"
    python -m quant_agent.cli serve --host 127.0.0.1 --port 8000
} finally {
    Write-Host "==> 停止 worker"
    Stop-Process -Id $worker.Id -ErrorAction SilentlyContinue
}
