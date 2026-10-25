# 启动 QuantLab 桌面端（Tauri 开发版）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File desktop\run.ps1 -Project "D:\Research\ProjectA"
# 不传 -Project 时首次会弹文件夹选择器，之后记住上次的项目。

param(
    [string]$Project = "",
    [string]$Python = "",
    [string]$Exe = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$workspace = Split-Path -Parent $repo

# 仓库内的便携 Rust 工具链（若存在则优先使用，不依赖系统 PATH）
$cargoHome = Join-Path $workspace ".tmp\cargo"
$rustupHome = Join-Path $workspace ".tmp\rustup"
if ((Test-Path (Join-Path $cargoHome "bin\cargo.exe")) -and -not $env:CARGO_HOME) {
    $env:CARGO_HOME = $cargoHome
    $env:RUSTUP_HOME = $rustupHome
    $env:PATH = "$cargoHome\bin;$env:PATH"
}

if (-not $Python) { $Python = "python" }
$env:QUANTLAB_PYTHON = $Python
$env:QUANTLAB_SRC = (Join-Path $repo "src")
# Some Windows profiles deny WebView2's default LocalAppData directory. Keep the
# cache in a writable project-local path unless the caller explicitly overrides it.
if (-not $env:QUANTLAB_WEBVIEW_DATA_DIR) {
    $dataProject = if ($Project) { $Project } else { $repo }
    $env:QUANTLAB_WEBVIEW_DATA_DIR = Join-Path $dataProject ".quantlab\webview"
}

if (-not $Exe) {
    $Exe = Join-Path $here "src-tauri\target\debug\quantlab-desktop.exe"
    if (-not (Test-Path $Exe)) {
        $Exe = Join-Path $here "src-tauri\target\release\quantlab-desktop.exe"
    }
}
if (-not (Test-Path $Exe)) {
    throw "找不到桌面端可执行文件。请先运行 desktop\build.ps1 构建。"
}

$arguments = @()
if ($Project) { $arguments += @("--project", $Project) }
Write-Host "QuantLab desktop: $Exe"
Write-Host "python=$Python  src=$env:QUANTLAB_SRC"
& $Exe @arguments
exit $LASTEXITCODE
