// QuantLab 桌面壳（Tauri 2）
//
// 职责边界：
// - 窗口 / 托盘 / 更新入口 / 系统文件对话框 / Python 后端进程管理，都在这一层；
// - 页面仍然由本机 Python 后端提供（同源，前端不需要改 API 地址）；
// - 现有 React 前端与 Python 后端（Agent、数据、因子、回测、组合）保持不动。
//
// 旧版 pywebview 启动器（packaging/desktop + quant_agent.local.launcher desktop）保留但已弃用。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    fs::{self, OpenOptions},
    io::Write,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread::sleep,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogResult};

const APP_TITLE: &str = "QuantLab · 本地研究";
const APP_ID: &str = "main";
const BODY_LIMIT_SECONDS: u64 = 90;
/// 桌面壳版本：启动后端时写进 `QUANTLAB_DESKTOP_VERSION`，健康端点原样返回。
/// 复用旧进程前必须和它比对，不一致就说明这个后端是旧版壳起的。
const DESKTOP_VERSION: &str = env!("CARGO_PKG_VERSION");
const DESKTOP_VERSION_ENV: &str = "QUANTLAB_DESKTOP_VERSION";
const BUILD_ID_ENV: &str = "QUANTLAB_BUILD_ID";
const RELEASES_URL: &str = "https://github.com/NagisaShip/quantlab/releases";

// ---------------------------------------------------------------------------
// 启动参数与本地配置
// ---------------------------------------------------------------------------

#[derive(Default)]
struct Args {
    project: Option<PathBuf>,
    python: Option<String>,
    src: Option<PathBuf>,
}

fn parse_args() -> Args {
    let mut out = Args::default();
    let mut it = env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--project" => out.project = it.next().map(PathBuf::from),
            "--python" => out.python = it.next(),
            "--src" => out.src = it.next().map(PathBuf::from),
            other => {
                if let Some(value) = other.strip_prefix("--project=") {
                    out.project = Some(PathBuf::from(value));
                } else if let Some(value) = other.strip_prefix("--python=") {
                    out.python = Some(value.to_string());
                } else if let Some(value) = other.strip_prefix("--src=") {
                    out.src = Some(PathBuf::from(value));
                }
            }
        }
    }
    out
}

#[derive(Default, Serialize, Deserialize)]
struct DesktopConfig {
    project: Option<String>,
    python: Option<String>,
    src: Option<String>,
}

fn config_path(app: &AppHandle) -> PathBuf {
    // Windows: %APPDATA%\com.quantlab.desktop\desktop.json
    app.path()
        .app_config_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("desktop.json")
}

fn read_config(app: &AppHandle) -> DesktopConfig {
    read_json_file(&config_path(app)).unwrap_or_default()
}

fn read_json_file<T: for<'de> Deserialize<'de>>(path: &Path) -> Option<T> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

/// 项目内的兜底副本：`app_config_dir` 不可写时不静默丢掉设置，改写进项目 `.quantlab/`。
fn project_fallback_path(project: &Path, name: &str) -> PathBuf {
    project.join(".quantlab").join(name)
}

fn write_text_file(path: &Path, text: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, text)
}

/// 写桌面配置；写入失败回退到项目 `.quantlab/desktop.json`，并在启动日志留下可见告警。
fn write_config(app: &AppHandle, cfg: &DesktopConfig, project: &Path) {
    let text = match serde_json::to_string_pretty(cfg) {
        Ok(text) => text,
        Err(error) => {
            append_log(project, &format!("desktop.json serialize failed: {error}"));
            return;
        }
    };
    let path = config_path(app);
    match write_text_file(&path, &text) {
        Ok(()) => {}
        Err(error) => {
            let fallback = project_fallback_path(project, "desktop.json");
            match write_text_file(&fallback, &text) {
                Ok(()) => append_log(
                    project,
                    &format!(
                        "desktop.json write failed at {} ({error}); fallback saved to {}",
                        path.display(),
                        fallback.display()
                    ),
                ),
                Err(fallback_error) => append_log(
                    project,
                    &format!(
                        "desktop.json write failed at {} ({error}); fallback {} also failed ({fallback_error})",
                        path.display(),
                        fallback.display()
                    ),
                ),
            }
        }
    }
}

/// 主配置缺失时，用项目里的兜底副本补上项目/解释器/源码路径，避免设置静默丢失。
fn merge_fallback_config(cfg: &mut DesktopConfig, project: &Path) {
    let Some(fallback) = read_json_file::<DesktopConfig>(&project_fallback_path(project, "desktop.json")) else {
        return;
    };
    if cfg.project.is_none() {
        cfg.project = fallback.project;
    }
    if cfg.python.is_none() {
        cfg.python = fallback.python;
    }
    if cfg.src.is_none() {
        cfg.src = fallback.src;
    }
}

// ---------------------------------------------------------------------------
// 后端进程
// ---------------------------------------------------------------------------

/// 托盘菜单里“取消退出”的句柄：只有处于“等待任务完成后退出”时才可用。
struct TrayHandles {
    cancel_quit: MenuItem<tauri::Wry>,
}

/// `/api/healthz` 的版本握手结果：判断旧后端能否复用。
#[derive(Default, Clone)]
struct Handshake {
    version: String,
    build: String,
    desktop_version: String,
}

impl Handshake {
    fn describe(&self) -> String {
        let desktop = if self.desktop_version.is_empty() {
            "unknown"
        } else {
            self.desktop_version.as_str()
        };
        let build = if self.build.is_empty() { "-" } else { self.build.as_str() };
        format!("backend {} desktop {desktop} build {build}", self.version)
    }

    /// 只有同一版桌面壳启动的后端才可复用；没有握手的旧进程同样视为陈旧。
    fn compatible(&self) -> bool {
        !self.desktop_version.is_empty() && self.desktop_version == DESKTOP_VERSION
    }
}

struct Backend {
    child: Mutex<Option<Child>>,
    base_url: String,
    project: PathBuf,
    python: String,
    sidecar: bool,
    src: Option<PathBuf>,
    first_run: bool,
    handshake: Handshake,
    quitting: AtomicBool,
    pending_quit: AtomicBool,
    tray: Mutex<Option<TrayHandles>>,
}

/// 会阻止“直接退出”的后台任务；与后端 /api/local/tasks、shutdown 409 同源。
#[derive(Deserialize, Default, Clone)]
struct TaskInfo {
    #[serde(default)]
    kind: String,
    #[serde(default)]
    id: String,
    #[serde(default)]
    title: Option<String>,
    #[serde(default)]
    state: String,
}

#[derive(Deserialize, Default)]
struct TasksPayload {
    #[serde(default)]
    tasks: Vec<TaskInfo>,
}

enum ShutdownOutcome {
    Stopped,
    Busy(Vec<TaskInfo>),
    Error(String),
}

/// 窗口大小/位置；保存在 app_config_dir/window.json，重启时校验后再恢复。
#[derive(Serialize, Deserialize, Default, Clone)]
struct WindowState {
    width: f64,
    height: f64,
    x: Option<f64>,
    y: Option<f64>,
    maximized: bool,
}

fn canonical(path: &Path) -> Option<PathBuf> {
    let resolved = path.canonicalize().ok().or_else(|| {
        // 目录还不存在时，用父目录 + 文件名拼出稳定路径
        let parent = path.parent()?;
        let name = path.file_name()?;
        canonical(parent).map(|p| p.join(name))
    })?;
    Some(strip_unc_prefix(resolved))
}

/// Windows 的 canonicalize 会加 `\\?\` 前缀；去掉它，避免传给 Python 后路径比较不一致。
fn strip_unc_prefix(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{rest}"))
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        PathBuf::from(rest.to_string())
    } else {
        path
    }
}

fn http_get_json(url: &str) -> Result<serde_json::Value, String> {
    let response = ureq::get(url)
        .timeout(Duration::from_millis(2_000))
        .call()
        .map_err(|error| error.to_string())?;
    let text = response.into_string().map_err(|error| error.to_string())?;
    serde_json::from_str(&text).map_err(|error| error.to_string())
}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|error| format!("获取空闲端口失败：{error}"))?;
    listener
        .local_addr()
        .map(|addr| addr.port())
        .map_err(|error| format!("读取空闲端口失败：{error}"))
}

/// 读取 `/api/healthz` 的版本/构建标识；旧后端没有这些字段时返回 None。
fn backend_handshake(base: &str) -> Option<Handshake> {
    let value = http_get_json(&format!("{base}/api/healthz")).ok()?;
    if value.get("status").and_then(|item| item.as_str()) != Some("ok") {
        return None;
    }
    Some(Handshake {
        version: value
            .get("version")
            .and_then(|item| item.as_str())
            .unwrap_or("unknown")
            .to_string(),
        build: value
            .get("build")
            .and_then(|item| item.as_str())
            .unwrap_or_default()
            .to_string(),
        desktop_version: value
            .get("desktop_version")
            .and_then(|item| item.as_str())
            .unwrap_or_default()
            .to_string(),
    })
}

/// `.quantlab/instance.json` 指向的后端状态。
enum InstanceState {
    /// 没有实例文件，或实例文件指向的进程已经退出。
    Absent,
    /// 同一版桌面壳启动的存活后端，可以复用。
    Alive {
        base: String,
        first_run: bool,
        handshake: Handshake,
    },
    /// 端口还活着但握手对不上：旧版壳启动的进程，或没有版本信息的旧后端。
    Stale { base: String, reason: String },
}

/// 读取实例文件并做两层校验：项目根目录一致 + 版本握手一致。
/// 项目根不一致说明实例文件属于别的项目：不碰它，也不复用。
fn inspect_instance(project: &Path) -> InstanceState {
    let instance = project.join(".quantlab/instance.json");
    let Some(value) = read_json_file::<serde_json::Value>(&instance) else {
        return InstanceState::Absent;
    };
    let Some(port) = value.get("port").and_then(|item| item.as_u64()) else {
        return InstanceState::Absent;
    };
    let base = format!("http://127.0.0.1:{port}");
    let Ok(info) = http_get_json(&format!("{base}/api/local/info")) else {
        return InstanceState::Absent;
    };
    let same_project = info
        .get("root")
        .and_then(|item| item.as_str())
        .and_then(|root| canonical(Path::new(root)))
        == canonical(project);
    if !same_project {
        return InstanceState::Absent;
    }
    let first_run = info.get("first_run").and_then(|item| item.as_bool()).unwrap_or(false);
    match backend_handshake(&base) {
        Some(handshake) if handshake.compatible() => InstanceState::Alive {
            base,
            first_run,
            handshake,
        },
        Some(handshake) => InstanceState::Stale {
            base,
            reason: format!(
                "shell version mismatch: process reports {} (expected desktop {DESKTOP_VERSION})",
                handshake.describe()
            ),
        },
        None => InstanceState::Stale {
            base,
            reason: "no version handshake on /api/healthz".to_string(),
        },
    }
}

/// 等旧后端进程真正退出：它有实例锁，没退干净之前新进程会抢锁失败。
fn wait_instance_gone(base: &str, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if http_get_json(&format!("{base}/api/local/info")).is_err() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        sleep(Duration::from_millis(200));
    }
}

/// 清理陈旧后端：先请它优雅退出；还有任务时不杀进程（避免丢运行证据），直接报错让用户处理。
fn retire_stale_instance(base: &str, project: &Path, reason: &str) -> Result<(), String> {
    append_log(project, &format!("stale backend {base}: {reason}; retiring it"));
    match shutdown_backend(base) {
        ShutdownOutcome::Stopped => {}
        ShutdownOutcome::Busy(tasks) => {
            append_log(
                project,
                &format!(
                    "stale backend {base} still has {} task(s); refusing to kill it",
                    tasks.len()
                ),
            );
            return Err(format!(
                "检测到旧版本的本地后端（{reason}）仍在执行 {} 个任务。\n\
                 为避免丢失运行证据，请先在旧窗口里完成或取消任务并退出，再重新启动 QuantLab。",
                tasks.len()
            ));
        }
        ShutdownOutcome::Error(error) => {
            append_log(project, &format!("stale backend {base} shutdown failed: {error}"));
        }
    }
    if !wait_instance_gone(base, Duration::from_secs(15)) {
        append_log(project, &format!("stale backend {base} did not exit within 15s"));
        return Err(format!(
            "旧版本的本地后端（{base}）没有在 15 秒内退出。\n请稍后重试，或在任务管理器里结束对应 Python 进程。"
        ));
    }
    let instance = project.join(".quantlab/instance.json");
    match fs::remove_file(&instance) {
        Ok(()) => append_log(project, &format!("removed stale instance {}", instance.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => append_log(project, &format!("remove stale instance failed: {error}")),
    }
    Ok(())
}

fn bundled_sidecar() -> Option<PathBuf> {
    // 1) 显式覆盖（开发/测试）；2) 安装包资源目录；3) 便携版 exe 同级的 backend/。
    if let Ok(value) = env::var("QUANTLAB_SIDECAR") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Some(path);
        }
    }
    let name = if cfg!(windows) {
        "quantlab-backend.exe"
    } else {
        "quantlab-backend"
    };
    let exe = env::current_exe().ok()?;
    let dir = exe.parent()?.to_path_buf();
    for candidate in [
        dir.join("backend").join(name),
        dir.join("backend").join("quantlab-backend").join(name),
        dir.join("resources").join("backend").join(name),
        dir.join("resources").join("backend").join("quantlab-backend").join(name),
        dir.join("..").join("resources").join("backend").join(name),
    ] {
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

fn spawn_backend(
    project: &Path,
    python: &str,
    src: Option<&Path>,
    sidecar: Option<&Path>,
) -> Result<(Child, u16, PathBuf), String> {
    let home = project.join(".quantlab");
    fs::create_dir_all(&home).map_err(|error| format!("创建项目目录失败：{error}"))?;
    let log_path = home.join("tauri-launcher.log");
    let log_a = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| format!("打开启动日志失败：{error}"))?;
    let log_b = log_a.try_clone().map_err(|error| format!("打开启动日志失败：{error}"))?;

    let port = free_port()?;
    let mut command = if let Some(binary) = sidecar {
        let mut command = Command::new(binary);
        command.arg("--serve");
        command
    } else {
        let mut command = Command::new(python);
        command.arg("-m").arg("quant_agent.local.launcher").arg("--serve");
        if let Some(src) = src {
            command.env("PYTHONPATH", src);
        }
        command
    };
    command
        .arg("--project")
        .arg(project)
        .arg("--port")
        .arg(port.to_string())
        // 本地项目的密钥/库放在项目 .quantlab 下，避免 AppData 权限/多项目串用。
        .env("QUANTLAB_CREDENTIALS", home.join("credentials"))
        .env("QUANTLAB_LIBRARY", home.join("library"))
        // 版本握手：/api/healthz 会把这两个值原样带回来，供下次启动判断能否复用。
        .env(DESKTOP_VERSION_ENV, DESKTOP_VERSION)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_a))
        .stderr(Stdio::from(log_b));
    if let Ok(build) = env::var(BUILD_ID_ENV) {
        if !build.trim().is_empty() {
            command.env(BUILD_ID_ENV, build);
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let child = command
        .spawn()
        .map_err(|error| format!("启动本地后端失败（{}）：{error}", sidecar.map(|p| p.display().to_string()).unwrap_or_else(|| python.to_string())))?;
    Ok((child, port, log_path))
}

/// 等后端可用：项目根目录一致 + 版本握手一致，返回握手结果。
/// 只看到端口活着还不够——旧版本的进程也能应门，必须确认是自己这一版起的。
fn wait_healthy(child: &mut Child, base: &str, project: &Path, log_path: &Path) -> Result<Handshake, String> {
    let deadline = Instant::now() + Duration::from_secs(BODY_LIMIT_SECONDS);
    let expected = canonical(project);
    let mut reason = "waiting for /api/local/info".to_string();
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "后端进程提前退出（{status}）。\n日志：{}",
                log_path.display()
            ));
        }
        if let Ok(info) = http_get_json(&format!("{base}/api/local/info")) {
            let root = info.get("root").and_then(|value| value.as_str()).unwrap_or("");
            if canonical(Path::new(root)) == expected {
                match backend_handshake(base) {
                    Some(handshake) if handshake.compatible() => return Ok(handshake),
                    Some(handshake) => {
                        reason = format!(
                            "version handshake mismatch: {} (expected desktop {DESKTOP_VERSION})",
                            handshake.describe()
                        )
                    }
                    None => reason = "port answered but /api/healthz has no version handshake".to_string(),
                }
            }
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            return Err(format!(
                "后端启动超时（{} 秒）：{reason}。\n日志：{}",
                BODY_LIMIT_SECONDS,
                log_path.display()
            ));
        }
        sleep(Duration::from_millis(250));
    }
}

/// 首次打开新项目进「本地项目」设置页；已有项目直接进研究工作区。
/// 工作流页会从项目偏好恢复上次打开的研究；显式 `#/home` 仍是普通入口。
/// `first_run` 由后端在初始化前记录（`/api/local/info.first_run`），不再由壳层猜
/// `.quantlab/project.json` 是否存在：文件在执行期间才被创建，猜错会让启动先落到设置页。
fn landing_suffix(first_run: bool) -> &'static str {
    if first_run {
        "/#/local"
    } else {
        "/#/workflows"
    }
}

fn boot_backend(app: &AppHandle, args: &Args) -> Result<Backend, String> {
    let mut cfg = read_config(app);

    let project = match args
        .project
        .clone()
        .or_else(|| env::var("QUANTLAB_PROJECT").ok().map(PathBuf::from))
        .or_else(|| cfg.project.clone().map(PathBuf::from))
    {
        Some(project) => project,
        None => {
            let picked = app
                .dialog()
                .file()
                .set_title("选择 QuantLab 项目文件夹（数据保存在该目录）")
                .blocking_pick_folder()
                .ok_or_else(|| "未选择项目文件夹，已取消启动".to_string())?;
            PathBuf::from(file_path_to_string(picked))
        }
    };
    fs::create_dir_all(&project).map_err(|error| format!("创建项目目录失败：{error}"))?;
    let project = canonical(&project).unwrap_or(project);

    // 主配置若曾写失败，项目里的兜底副本还能补回解释器/源码路径。
    merge_fallback_config(&mut cfg, &project);

    let python = args
        .python
        .clone()
        .or_else(|| env::var("QUANTLAB_PYTHON").ok())
        .or_else(|| cfg.python.clone())
        .unwrap_or_else(|| "python".to_string());
    let src = args
        .src
        .clone()
        .or_else(|| env::var("QUANTLAB_SRC").ok().map(PathBuf::from))
        .or_else(|| cfg.src.clone().map(PathBuf::from))
        .or_else(guess_src);
    // 打包版优先使用随安装包分发的 sidecar；显式 --python / QUANTLAB_PYTHON 时仍尊重用户解释器。
    let sidecar = if args.python.is_none() && env::var("QUANTLAB_PYTHON").is_err() {
        bundled_sidecar()
    } else {
        None
    };
    let python_label = sidecar
        .as_ref()
        .map(|path| path.display().to_string())
        .unwrap_or_else(|| python.clone());

    cfg.project = Some(project.to_string_lossy().into_owned());
    cfg.python = Some(python.clone());
    cfg.src = src.as_ref().map(|path| path.to_string_lossy().into_owned());
    write_config(app, &cfg, &project);

    // 已有实例：先做项目根 + 版本握手校验；对不上就清理陈旧后端再新起。
    match inspect_instance(&project) {
        InstanceState::Alive {
            base,
            first_run,
            handshake,
        } => {
            append_log(
                &project,
                &format!("reuse backend {base} ({}); first_run={first_run}", handshake.describe()),
            );
            return Ok(Backend {
                child: Mutex::new(None),
                base_url: base,
                project,
                python,
                sidecar: sidecar.is_some(),
                src,
                first_run,
                handshake,
                quitting: AtomicBool::new(false),
                pending_quit: AtomicBool::new(false),
                tray: Mutex::new(None),
            });
        }
        InstanceState::Stale { base, reason } => {
            retire_stale_instance(&base, &project, &reason)?;
        }
        InstanceState::Absent => {}
    }

    let (mut child, port, log_path) = spawn_backend(&project, &python, src.as_deref(), sidecar.as_deref())?;
    let base = format!("http://127.0.0.1:{port}");
    let handshake = wait_healthy(&mut child, &base, &project, &log_path)?;
    // 首次打开新项目以 后端记录的初始化状态 为准；拿不到时退回文件判断。
    let first_run = http_get_json(&format!("{base}/api/local/info"))
        .ok()
        .and_then(|info| info.get("first_run").and_then(|item| item.as_bool()))
        .unwrap_or_else(|| !project.join(".quantlab/project.json").exists());
    append_log(
        &project,
        &format!(
            "start backend {base} backend={python_label} src={} first_run={first_run} ({})",
            src.as_ref().map(|path| path.display().to_string()).unwrap_or_else(|| "-".into()),
            handshake.describe()
        ),
    );
    Ok(Backend {
        child: Mutex::new(Some(child)),
        base_url: base,
        project,
        python,
        sidecar: sidecar.is_some(),
        src,
        first_run,
        handshake,
        quitting: AtomicBool::new(false),
        pending_quit: AtomicBool::new(false),
        tray: Mutex::new(None),
    })
}

/// 开发模式下的启发式：exe 位于 target/{debug,release} 时向上找含 src/quant_agent 的仓库根。
fn guess_src() -> Option<PathBuf> {
    if let Ok(value) = env::var("PYTHONPATH") {
        for part in value.split(';') {
            if Path::new(part).join("quant_agent/__init__.py").exists() {
                return Some(PathBuf::from(part));
            }
        }
    }
    let exe = env::current_exe().ok()?;
    let mut dir = exe.parent()?.to_path_buf();
    for _ in 0..8 {
        if dir.join("src/quant_agent/__init__.py").exists() {
            return Some(dir.join("src"));
        }
        dir = dir.parent()?.to_path_buf();
    }
    None
}

fn append_log(project: &Path, message: &str) {
    let path = project.join(".quantlab/tauri.log");
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(file, "[{stamp}] {message}");
    }
}

// ---------------------------------------------------------------------------
// 桌面动作
// ---------------------------------------------------------------------------

fn file_path_to_string(path: tauri_plugin_dialog::FilePath) -> String {
    path.to_string()
}

fn read_close_action(base: &str) -> String {
    let Ok(info) = http_get_json(&format!("{base}/api/local/info")) else {
        return "ask".into();
    };
    info.get("preferences")
        .and_then(|prefs| prefs.get("close_action"))
        .and_then(|value| value.as_str())
        .filter(|value| *value == "ask" || *value == "background" || *value == "exit")
        .unwrap_or("ask")
        .to_string()
}

/// POST /api/local/shutdown：200 = 已安排停止；409 = 还有任务在跑；其它 = 错误。
fn shutdown_backend(base: &str) -> ShutdownOutcome {
    let response = ureq::post(&format!("{base}/api/local/shutdown"))
        .set("X-Requested-With", "quantlab-desktop")
        .timeout(Duration::from_secs(8))
        .send_string("{}");
    match response {
        Ok(_) => ShutdownOutcome::Stopped,
        Err(ureq::Error::Status(409, response)) => {
            let text = response.into_string().unwrap_or_default();
            let payload: TasksPayload = serde_json::from_str(&text).unwrap_or_default();
            ShutdownOutcome::Busy(payload.tasks)
        }
        Err(ureq::Error::Status(code, response)) => {
            let detail = response.into_string().unwrap_or_default();
            ShutdownOutcome::Error(format!("HTTP {code}: {detail}"))
        }
        Err(error) => ShutdownOutcome::Error(error.to_string()),
    }
}

fn window_state_path(app: &AppHandle) -> PathBuf {
    // 测试 / 便携模式：把窗口状态文件放到指定位置；默认仍在 app_config_dir。
    if let Ok(path) = env::var("QUANTLAB_WINDOW_STATE") {
        if !path.trim().is_empty() {
            return PathBuf::from(path);
        }
    }
    config_path(app).with_file_name("window.json")
}

fn load_window_state(app: &AppHandle) -> Option<WindowState> {
    let path = window_state_path(app);
    if let Some(state) = read_json_file::<WindowState>(&path) {
        return Some(state);
    }
    // 主位置写不进去时，上次保存会回退到项目内副本；这里读回来并记录来源。
    let project = app.try_state::<Backend>()?.project.clone();
    let fallback = project_fallback_path(&project, "window.json");
    if fallback == path {
        return None;
    }
    let state = read_json_file::<WindowState>(&fallback)?;
    append_log(&project, &format!("window state restored from fallback {}", fallback.display()));
    Some(state)
}

/// 尺寸夹到可用范围；位置必须落在某个显示器上，否则丢弃位置只恢复大小。
fn clamp_window_size(width: f64, height: f64) -> (f64, f64) {
    (width.clamp(900.0, 8000.0), height.clamp(640.0, 6000.0))
}

fn usable_window_state(app: &AppHandle, saved: &WindowState) -> Option<WindowState> {
    if !saved.width.is_finite() || !saved.height.is_finite() {
        return None;
    }
    let mut out = saved.clone();
    let (width, height) = clamp_window_size(saved.width, saved.height);
    out.width = width;
    out.height = height;
    let usable = match (saved.x, saved.y) {
        (Some(x), Some(y)) if x.is_finite() && y.is_finite() => {
            app.monitor_from_point(x, y).ok().flatten().is_some()
        }
        _ => false,
    };
    if !usable {
        out.x = None;
        out.y = None;
    }
    Some(out)
}

fn save_window_state(app: &AppHandle, win: &tauri::WebviewWindow) {
    let (Ok(size), Ok(scale)) = (win.inner_size(), win.scale_factor()) else {
        return;
    };
    let size = size.to_logical::<f64>(scale);
    let position = win.outer_position().ok().map(|p| p.to_logical::<f64>(scale));
    let state = WindowState {
        width: size.width,
        height: size.height,
        x: position.map(|p| p.x),
        y: position.map(|p| p.y),
        maximized: win.is_maximized().unwrap_or(false),
    };
    let path = window_state_path(app);
    let Ok(text) = serde_json::to_string_pretty(&state) else {
        return;
    };
    if write_text_file(&path, &text).is_ok() {
        return;
    }
    // 主位置写失败：回退到项目 `.quantlab/window.json`，并在启动日志留下告警，不静默丢窗口状态。
    let Some(project) = app.try_state::<Backend>().map(|state| state.project.clone()) else {
        return;
    };
    let fallback = project_fallback_path(&project, "window.json");
    match write_text_file(&fallback, &text) {
        Ok(()) => append_log(
            &project,
            &format!(
                "window.json write failed at {}; fallback saved to {}",
                path.display(),
                fallback.display()
            ),
        ),
        Err(error) => append_log(
            &project,
            &format!(
                "window.json write failed at {}; fallback {} also failed ({error})",
                path.display(),
                fallback.display()
            ),
        ),
    }
}

fn set_pending_quit(app: &AppHandle, pending: bool) {
    if let Some(state) = app.try_state::<Backend>() {
        state.pending_quit.store(pending, Ordering::SeqCst);
        if let Ok(guard) = state.tray.lock() {
            if let Some(handles) = guard.as_ref() {
                let _ = handles.cancel_quit.set_enabled(pending);
            }
        }
    }
}

fn finish_quit(app: &AppHandle) {
    if let Some(state) = app.try_state::<Backend>() {
        state.pending_quit.store(false, Ordering::SeqCst);
        if let Ok(guard) = state.tray.lock() {
            if let Some(handles) = guard.as_ref() {
                let _ = handles.cancel_quit.set_enabled(false);
            }
        }
        if let Some(win) = window(app) {
            save_window_state(app, &win);
        }
        if let Ok(mut guard) = state.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        append_log(&state.project, "quit");
    }
    app.exit(0);
}

/// 有任务时退出：隐藏 / 完成后退出 / 取消。
fn task_state_label(state: &str) -> &'static str {
    match state {
        "QUEUED" | "queued" => "排队中",
        "RUNNING" | "running" => "进行中",
        "PAUSED" => "已暂停",
        "RECOVERING" => "恢复中",
        _ => "进行中",
    }
}

fn show_busy_quit_dialog(app: &AppHandle, tasks: Vec<TaskInfo>) {
    let count = tasks.len();
    let mut lines = String::new();
    for task in tasks.iter().take(5) {
        let title = task
            .title
            .clone()
            .filter(|value| !value.trim().is_empty())
            .unwrap_or_else(|| match task.kind.as_str() {
                "conversation" => "Agent 对话".into(),
                "job" => "管理任务".into(),
                _ => task.id.clone(),
            });
        lines.push_str(&format!("· {title}（{}）\n", task_state_label(&task.state)));
    }
    if count > 5 {
        lines.push_str(&format!("…另有 {} 个任务\n", count - 5));
    }
    let app2 = app.clone();
    app.dialog()
        .message(format!(
            "还有 {count} 个任务正在运行：\n\n{lines}\n可以隐藏到托盘继续研究，或等任务全部完成后自动退出。"
        ))
        .title("退出 QuantLab")
        .buttons(MessageDialogButtons::YesNoCancelCustom(
            "隐藏到托盘".into(),
            "完成后退出".into(),
            "取消".into(),
        ))
        .show_with_result(move |result| {
            // YesNoCancelCustom 在 Windows 上返回 Custom(按钮文案)，不是 Yes/No。
            let choice = match result {
                MessageDialogResult::Custom(label) => label,
                MessageDialogResult::Yes => "隐藏到托盘".into(),
                MessageDialogResult::No => "完成后退出".into(),
                MessageDialogResult::Cancel => "取消".into(),
                _ => String::new(),
            };
            match choice.as_str() {
                "隐藏到托盘" => hide_window(&app2),
                "完成后退出" => begin_pending_quit(app2.clone()),
                _ => {}
            }
        });
}

fn begin_pending_quit(app: AppHandle) {
    let state = app.state::<Backend>();
    if state.pending_quit.swap(true, Ordering::SeqCst) {
        return;
    }
    let project = state.project.clone();
    if let Ok(guard) = state.tray.lock() {
        if let Some(handles) = guard.as_ref() {
            let _ = handles.cancel_quit.set_enabled(true);
        }
    }
    append_log(&project, "pending quit: waiting for tasks");
    // 选择“完成后退出”后先隐藏窗口：任务仍在后台跑，避免用户误以为没退出。
    hide_window(&app);
    std::thread::spawn(move || loop {
        sleep(Duration::from_secs(5));
        let Some(state) = app.try_state::<Backend>() else {
            return;
        };
        if !state.pending_quit.load(Ordering::SeqCst) {
            return;
        }
        let base = state.base_url.clone();
        match http_get_json(&format!("{base}/api/local/tasks")) {
            Ok(value) => {
                let active = value
                    .get("active_count")
                    .and_then(|count| count.as_u64())
                    .unwrap_or(0);
                if active == 0 {
                    match shutdown_backend(&base) {
                        ShutdownOutcome::Stopped => {
                            finish_quit(&app);
                            return;
                        }
                        ShutdownOutcome::Busy(_) => continue,
                        ShutdownOutcome::Error(error) => {
                            append_log(&project, &format!("pending quit shutdown failed: {error}"))
                        }
                    }
                }
            }
            Err(error) => append_log(&project, &format!("pending quit poll failed: {error}")),
        }
    });
}

fn cancel_pending_quit(app: &AppHandle) {
    set_pending_quit(app, false);
    if let Some(state) = app.try_state::<Backend>() {
        append_log(&state.project, "cancel pending quit");
    }
    show_window(app);
}

fn request_quit(app: AppHandle) {
    let state = app.state::<Backend>();
    if state.pending_quit.load(Ordering::SeqCst) {
        show_window(&app);
        return;
    }
    if state.quitting.swap(true, Ordering::SeqCst) {
        return;
    }
    if let Some(win) = window(&app) {
        save_window_state(&app, &win);
    }
    match shutdown_backend(&state.base_url) {
        ShutdownOutcome::Stopped => finish_quit(&app),
        ShutdownOutcome::Busy(tasks) => {
            state.quitting.store(false, Ordering::SeqCst);
            show_busy_quit_dialog(&app, tasks);
        }
        ShutdownOutcome::Error(error) => {
            state.quitting.store(false, Ordering::SeqCst);
            append_log(&state.project, &format!("shutdown failed: {error}"));
            app.dialog()
                .message(format!("暂时不能退出：\n\n{error}\n\n可以隐藏到托盘继续研究。"))
                .title("退出 QuantLab")
                .buttons(MessageDialogButtons::Ok)
                .show(|_| {});
        }
    }
}

fn window(app: &AppHandle) -> Option<tauri::WebviewWindow> {
    app.get_webview_window(APP_ID)
}

fn show_window(app: &AppHandle) {
    if let Some(win) = window(app) {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

fn hide_window(app: &AppHandle) {
    if let Some(win) = window(app) {
        let _ = win.hide();
    }
}

fn on_close_requested(app: AppHandle) {
    if let Some(win) = window(&app) {
        save_window_state(&app, &win);
    }
    if app.state::<Backend>().pending_quit.load(Ordering::SeqCst) {
        show_window(&app);
        let app2 = app.clone();
        app.dialog()
            .message("正在等待任务完成后退出。你可以取消退出，继续在窗口里工作。")
            .title("QuantLab 正在退出")
            .buttons(MessageDialogButtons::OkCancelCustom(
                "取消退出".into(),
                "继续等待".into(),
            ))
            .show(move |cancel| {
                if cancel {
                    cancel_pending_quit(&app2);
                }
            });
        return;
    }
    let action = {
        let state = app.state::<Backend>();
        read_close_action(&state.base_url)
    };
    match action.as_str() {
        "background" => hide_window(&app),
        "exit" => request_quit(app),
        _ => {
            let app2 = app.clone();
            app.dialog()
                .message("关闭窗口后 QuantLab 可以继续在托盘后台研究。要隐藏到托盘，还是退出？")
                .title("关闭 QuantLab")
                .buttons(MessageDialogButtons::YesNoCancelCustom(
                    "隐藏到托盘".into(),
                    "退出".into(),
                    "取消".into(),
                ))
                .show_with_result(move |result| {
                    let choice = match result {
                        MessageDialogResult::Custom(label) => label,
                        MessageDialogResult::Yes => "隐藏到托盘".into(),
                        MessageDialogResult::No => "退出".into(),
                        MessageDialogResult::Cancel => "取消".into(),
                        _ => String::new(),
                    };
                    match choice.as_str() {
                        "隐藏到托盘" => hide_window(&app2),
                        "退出" => request_quit(app2.clone()),
                        _ => {}
                    }
                });
        }
    }
}

fn check_updates(app: AppHandle) {
    #[cfg(windows)]
    let result = Command::new("explorer.exe").arg(RELEASES_URL).spawn();
    #[cfg(not(windows))]
    let result = Command::new("xdg-open").arg(RELEASES_URL).spawn();
    if let Err(error) = result {
        app.dialog().message(format!("打开发布页面失败：{error}\n{RELEASES_URL}"))
            .title("检查更新").buttons(MessageDialogButtons::Ok).show(|_| {});
    }
}

// ---------------------------------------------------------------------------
// 前端可调用的命令
// ---------------------------------------------------------------------------

#[tauri::command]
fn native_choose_folder(app: AppHandle) -> Result<Vec<String>, String> {
    let picked = app
        .dialog()
        .file()
        .set_title("选择文件夹")
        .blocking_pick_folder();
    Ok(picked.map(|path| vec![file_path_to_string(path)]).unwrap_or_default())
}

#[tauri::command]
fn native_choose_files(app: AppHandle) -> Result<Vec<String>, String> {
    let picked = app
        .dialog()
        .file()
        .set_title("选择文件")
        .blocking_pick_files()
        .unwrap_or_default();
    Ok(picked.into_iter().map(file_path_to_string).collect())
}

#[tauri::command]
fn native_desktop_info(app: AppHandle) -> Result<serde_json::Value, String> {
    let state = app.state::<Backend>();
    Ok(serde_json::json!({
        "kind": "tauri",
        "version": env!("CARGO_PKG_VERSION"),
        "baseUrl": state.base_url,
        "project": state.project.to_string_lossy(),
        "python": state.python,
        "src": state.src.as_ref().map(|path| path.to_string_lossy().into_owned()),
        // 后端握手：版本不一致时说明拿到的是旧进程，排障先看这里。
        "backendVersion": state.handshake.version.clone(),
        "backendBuild": state.handshake.build.clone(),
        "backendVersionMatch": state.handshake.compatible(),
        "backend": state.handshake.describe(),
    }))
}

#[tauri::command]
fn native_open_project(app: AppHandle, path: String) -> Result<bool, String> {
    let exe = env::current_exe().map_err(|error| format!("定位当前程序失败：{error}"))?;
    let state = app.state::<Backend>();
    let mut command = Command::new(exe);
    command.arg("--project").arg(&path);
    if !state.sidecar {
        command.arg("--python").arg(&state.python);
    }
    if let Some(src) = &state.src {
        command.arg("--src").arg(src);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
        .spawn()
        .map_err(|error| format!("打开新项目失败：{error}"))?;
    Ok(true)
}

/// 只允许打开真实存在的本机路径；供“成果专注阅读”打开文件夹/系统应用。
fn resolved_native_path(path: &str) -> Result<PathBuf, String> {
    if path.trim().is_empty() {
        return Err("路径为空".into());
    }
    let resolved = Path::new(path)
        .canonicalize()
        .map_err(|error| format!("路径不存在或无法访问：{error}"))?;
    Ok(strip_unc_prefix(resolved))
}

#[tauri::command]
fn native_reveal_path(path: String) -> Result<bool, String> {
    let resolved = resolved_native_path(&path)?;
    let mut command = Command::new("explorer.exe");
    command.arg(format!("/select,{}", resolved.display()));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
        .spawn()
        .map_err(|error| format!("打开文件夹失败：{error}"))?;
    Ok(true)
}

#[tauri::command]
fn native_open_path(path: String) -> Result<bool, String> {
    let resolved = resolved_native_path(&path)?;
    let mut command = Command::new("cmd.exe");
    command.args(["/C", "start", ""]).arg(&resolved);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    command
        .spawn()
        .map_err(|error| format!("用系统应用打开失败：{error}"))?;
    Ok(true)
}

// ---------------------------------------------------------------------------
// 窗口 / 托盘 / 入口
// ---------------------------------------------------------------------------

/// 按项目路径生成 WebView2 数据目录：不同项目的桌面实例可以并行运行。
fn default_webview_data_dir(app: &AppHandle, project: &Path) -> Option<PathBuf> {
    use std::hash::{Hash, Hasher};
    let base = app.path().app_local_data_dir().ok()?;
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    project.to_string_lossy().to_lowercase().hash(&mut hasher);
    Some(base.join("webview").join(format!("{:016x}", hasher.finish())))
}

fn create_window(app: &AppHandle, base_url: &str, first_run: bool, project: &Path) -> Result<(), String> {
    let url = format!("{base_url}{}", landing_suffix(first_run));
    append_log(
        project,
        &format!("create window {url} first_run={first_run} desktop={DESKTOP_VERSION}"),
    );
    let parsed = url.parse().map_err(|error| format!("后端地址无效（{url}）：{error}"))?;
    let handle = app.clone();
    let mut builder = WebviewWindowBuilder::new(app, APP_ID, WebviewUrl::External(parsed))
        .title(APP_TITLE)
        .inner_size(1380.0, 920.0)
        .min_inner_size(900.0, 640.0)
        .decorations(false);
    // 恢复上次窗口大小/位置；位置经过显示器校验，避免拔掉外接屏后窗口落在屏幕外。
    if let Some(saved) = load_window_state(app).and_then(|state| usable_window_state(app, &state)) {
        builder = builder.inner_size(saved.width, saved.height);
        if let (Some(x), Some(y)) = (saved.x, saved.y) {
            builder = builder.position(x, y);
        }
        if saved.maximized {
            builder = builder.maximized(true);
        }
    }
    // 本地排查用：设置 QUANTLAB_WEBVIEW_DEBUG_PORT 后可用 Edge DevTools / CDP 连上窗口
    if let Ok(port) = env::var("QUANTLAB_WEBVIEW_DEBUG_PORT") {
        builder = builder.additional_browser_args(&format!("--remote-debugging-port={port}"));
    }
    // 自定义 WebView2 数据目录（受限环境或需要把缓存放进项目目录时使用）。
    // 默认按项目分目录：多个桌面实例（不同项目）可以同时运行，缓存也能复用。
    let data_dir = match env::var("QUANTLAB_WEBVIEW_DATA_DIR") {
        Ok(dir) if !dir.trim().is_empty() => Some(PathBuf::from(dir)),
        _ => default_webview_data_dir(app, project),
    };
    if let Some(dir) = data_dir {
        builder = builder.data_directory(dir);
    }
    let win = builder
        .build()
        .map_err(|error| format!("创建桌面窗口失败：{error}"))?;
    win.on_window_event(move |event| match event {
        WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            on_close_requested(handle.clone());
        }
        WindowEvent::DragDrop(tauri::DragDropEvent::Drop { paths, .. }) => {
            let paths: Vec<String> = paths
                .iter()
                .map(|path| path.to_string_lossy().into_owned())
                .collect();
            if let Some(win) = handle.get_webview_window(APP_ID) {
                let _ = win.emit("quantlab://files-dropped", paths);
            }
        }
        _ => {}
    });
    Ok(())
}

fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItem::with_id(app, "show", "打开 QuantLab 窗口", true, None::<&str>)?;
    let update = MenuItem::with_id(app, "update", "检查更新", true, None::<&str>)?;
    let cancel_quit = MenuItem::with_id(app, "cancel-quit", "取消退出，继续运行", false, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出 QuantLab", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &show,
            &update,
            &PredefinedMenuItem::separator(app)?,
            &cancel_quit,
            &quit_item,
        ],
    )?;

    let mut builder = TrayIconBuilder::new()
        .tooltip(APP_TITLE)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_window(app),
            "update" => check_updates(app.clone()),
            "cancel-quit" => cancel_pending_quit(app),
            "quit" => request_quit(app.clone()),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    if let Some(state) = app.try_state::<Backend>() {
        if let Ok(mut guard) = state.tray.lock() {
            *guard = Some(TrayHandles {
                cancel_quit: cancel_quit.clone(),
            });
        }
    }
    Ok(())
}

fn main() {
    let args = parse_args();
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            native_choose_folder,
            native_choose_files,
            native_desktop_info,
            native_open_project,
            native_reveal_path,
            native_open_path
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            let backend = boot_backend(&handle, &args).map_err(|error| {
                append_log(
                    Path::new("."),
                    &format!("boot failed: {error}"),
                );
                handle
                    .dialog()
                    .message(format!("QuantLab 启动失败：\n\n{error}"))
                    .title("QuantLab")
                    .buttons(MessageDialogButtons::Ok)
                    .blocking_show();
                error
            })?;
            let base = backend.base_url.clone();
            let first_run = backend.first_run;
            let project = backend.project.clone();
            handle.manage(backend);
            if let Err(error) = create_window(&handle, &base, first_run, &project) {
                append_log(&project, &format!("window failed: {error}"));
                handle
                    .dialog()
                    .message(format!(
                        "创建桌面窗口失败：\n\n{error}\n\n日志：{}",
                        project.join(".quantlab/tauri.log").display()
                    ))
                    .title("QuantLab")
                    .buttons(MessageDialogButtons::Ok)
                    .blocking_show();
                return Err(error.into());
            }
            if let Err(error) = create_tray(&handle) {
                append_log(&project, &format!("tray failed: {error}"));
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building QuantLab desktop")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                // 退出统一走托盘菜单的 quit 流程；这里只保证子进程不会变成孤儿
                if let Some(win) = window(app) {
                    save_window_state(app, &win);
                }
                if let Some(state) = app.try_state::<Backend>() {
                    if let Ok(mut guard) = state.child.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{clamp_window_size, landing_suffix, Handshake, DESKTOP_VERSION};

    #[test]
    fn new_projects_start_in_local_setup_and_existing_projects_in_research() {
        assert_eq!(landing_suffix(true), "/#/local");
        assert_eq!(landing_suffix(false), "/#/workflows");
    }

    #[test]
    fn version_handshake_only_accepts_the_same_desktop_shell() {
        let matching = Handshake {
            version: "0.1.0-preview.1".into(),
            build: "0.1.0-preview.1+local".into(),
            desktop_version: DESKTOP_VERSION.into(),
        };
        assert!(matching.compatible());
        let stale = Handshake {
            desktop_version: "0.0.9".into(),
            ..Default::default()
        };
        assert!(!stale.compatible());
        // 旧后端没有 desktop_version：不能当作兼容，必须重启。
        assert!(!Handshake::default().compatible());
    }

    #[test]
    fn window_size_is_clamped_to_usable_bounds() {
        assert_eq!(clamp_window_size(1380.0, 920.0), (1380.0, 920.0));
        assert_eq!(clamp_window_size(10.0, 10.0), (900.0, 640.0));
        assert_eq!(clamp_window_size(99_999.0, 99_999.0), (8000.0, 6000.0));
    }
}
