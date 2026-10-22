# 构建 QuantLab 桌面端（Tauri release 可执行文件）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1

param(
    [switch]$DebugBuild
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$workspace = Split-Path -Parent $repo
$cargoHome = Join-Path $workspace ".tmp\cargo"
$rustupHome = Join-Path $workspace ".tmp\rustup"

if ((Test-Path (Join-Path $cargoHome "bin\cargo.exe")) -and -not $env:CARGO_HOME) {
    $env:CARGO_HOME = $cargoHome
    $env:RUSTUP_HOME = $rustupHome
    $env:PATH = "$cargoHome\bin;$env:PATH"
}

# 前端产物是窗口的页面来源之一（页面实际由后端提供，但构建会嵌入一份作为回退）
$webDist = Join-Path $repo "web\dist\index.html"
if (-not (Test-Path $webDist)) {
    Write-Host "web/dist 不存在，先构建前端…"
    Push-Location (Join-Path $repo "web")
    & npm.cmd run build
    Pop-Location
}

Push-Location (Join-Path $here "src-tauri")
try {
    if ($DebugBuild) {
        & cargo build
    } else {
        & cargo build --release
    }
    if ($LASTEXITCODE -ne 0) { throw "cargo build 失败（exit $LASTEXITCODE）" }
} finally {
    Pop-Location
}

$target = if ($DebugBuild) { "debug" } else { "release" }
Write-Host "完成：desktop\src-tauri\target\$target\quantlab-desktop.exe"
