use rand::RngCore;
use serde::Serialize;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::Manager;

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BootstrapConfig {
    pub base_url: String,
    pub admin_token: String,
    pub version: String,
    pub platform: String,
    pub attached: bool,
}

struct RunningChild {
    child: Child,
    _stdin: Option<ChildStdin>,
}

pub struct SidecarState {
    config: Mutex<Option<BootstrapConfig>>,
    child: Mutex<Option<RunningChild>>,
    shutting_down: AtomicBool,
    session_admin_token: String,
}

impl SidecarState {
    pub fn new() -> Self {
        let mut bytes = [0u8; 24];
        rand::thread_rng().fill_bytes(&mut bytes);
        let tok: String = bytes.iter().map(|b| format!("{:02x}", b)).collect();
        Self {
            config: Mutex::new(None),
            child: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            session_admin_token: format!("adm_{tok}"),
        }
    }

    pub fn get_bootstrap(&self) -> Result<BootstrapConfig, String> {
        let guard = self.config.lock().map_err(|e| e.to_string())?;
        guard
            .clone()
            .ok_or_else(|| "Sidecar is still initializing".to_string())
    }

    pub fn start_or_attach(&self, app: &tauri::AppHandle) -> Result<BootstrapConfig, String> {
        // 1. Check existing daemon.json first (CLI vs Desktop single-instance attach)
        if let Some(attached) = try_attach_existing_daemon() {
            if let Ok(mut g) = self.config.lock() {
                *g = Some(attached.clone());
            }
            return Ok(attached);
        }

        // 2. Locate bundled --onedir engine or dev fallback
        let (cmd, base_args, shim_target) = resolve_engine_binary(app);
        if let Some(ref exe_path) = shim_target {
            let _ = ensure_stable_shim(exe_path);
        }

        let cfg = self.spawn_once(&cmd, &base_args)?;
        if let Ok(mut g) = self.config.lock() {
            *g = Some(cfg.clone());
        }
        Ok(cfg)
    }

    fn spawn_once(&self, cmd: &str, base_args: &[String]) -> Result<BootstrapConfig, String> {
        let mut command = Command::new(cmd);
        command
            .args(base_args)
            .args(["ui", "--no-browser", "--port", "0", "--sidecar", "--watch-stdin-eof"])
            .env("WCTX_ADMIN_TOKEN", &self.session_admin_token)
            .env("WCTX_DESKTOP_WATCHER", "1")
            .env("PYTHONUTF8", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());

        #[cfg(target_os = "windows")]
        {
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = command
            .spawn()
            .map_err(|e| format!("Failed to spawn wiki-context engine ({cmd}): {e}"))?;

        let stdin = child.stdin.take();
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Failed to capture engine stdout".to_string())?;

        let mut reader = BufReader::new(stdout);
        let deadline = Instant::now() + Duration::from_secs(15);
        let mut ready_port: Option<u16> = None;
        let mut ready_pid: Option<u32> = None;

        while Instant::now() < deadline {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => {
                    let trimmed = line.trim();
                    if let Some(rest) = trimmed.strip_prefix("WCTX_READY ") {
                        let parts: Vec<&str> = rest.split_whitespace().collect();
                        if let Some(p_str) = parts.first() {
                            if let Ok(p) = p_str.parse::<u16>() {
                                ready_port = Some(p);
                                ready_pid = parts.get(1).and_then(|s| s.parse::<u32>().ok());
                                break;
                            }
                        }
                    }
                }
                Err(e) => return Err(format!("Error reading sidecar handshake: {e}")),
            }
        }

        let port = ready_port.ok_or_else(|| "Timed out waiting for WCTX_READY handshake".to_string())?;
        let spawned_pid = child.id();

        // Drain stdout in background so child never blocks on pipe buffer
        std::thread::spawn(move || {
            let mut sink = String::new();
            while reader.read_line(&mut sink).unwrap_or(0) > 0 {
                sink.clear();
            }
        });

        // If console.py attached to an already-running daemon on another PID, fetch its live csrfToken
        if let Some(rpid) = ready_pid {
            if rpid != spawned_pid {
                if let Some(attached_cfg) = fetch_daemon_bootstrap(port) {
                    return Ok(attached_cfg);
                }
            }
        }

        if let Ok(mut cg) = self.child.lock() {
            *cg = Some(RunningChild {
                child,
                _stdin: stdin,
            });
        }

        Ok(BootstrapConfig {
            base_url: format!("http://127.0.0.1:{port}"),
            admin_token: self.session_admin_token.clone(),
            version: env!("CARGO_PKG_VERSION").to_string(),
            platform: std::env::consts::OS.to_string(),
            attached: false,
        })
    }

    pub fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut rc) = guard.take() {
                // Dropping stdin closes the pipe -> Python --watch-stdin-eof exits cleanly in <300ms
                drop(rc._stdin.take());
                let deadline = Instant::now() + Duration::from_millis(900);
                while Instant::now() < deadline {
                    if let Ok(Some(_)) = rc.child.try_wait() {
                        return;
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                let _ = rc.child.kill();
                let _ = rc.child.wait();
            }
        }
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }
}

pub fn user_home_dir() -> Option<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        std::env::var_os("USERPROFILE").map(PathBuf::from)
    }
    #[cfg(not(target_os = "windows"))]
    {
        std::env::var_os("HOME").map(PathBuf::from)
    }
}

pub fn wctx_config_dir() -> Option<PathBuf> {
    if let Ok(over) = std::env::var("WCTX_HOME") {
        if !over.trim().is_empty() {
            return Some(PathBuf::from(over.trim()));
        }
    }
    user_home_dir().map(|h| h.join(".wiki-context"))
}

fn fetch_daemon_bootstrap(port: u16) -> Option<BootstrapConfig> {
    if port == 0 {
        return None;
    }
    let body = http_request(port, "GET", "/api/bootstrap", None, None).ok()?;
    let boot: serde_json::Value = serde_json::from_str(&body).ok()?;
    let csrf = boot.get("csrfToken")?.as_str()?.to_string();
    let ver = boot
        .get("version")
        .and_then(|x| x.as_str())
        .unwrap_or("0.4.0")
        .to_string();

    Some(BootstrapConfig {
        base_url: format!("http://127.0.0.1:{port}"),
        admin_token: csrf,
        version: ver,
        platform: std::env::consts::OS.to_string(),
        attached: true,
    })
}

fn try_attach_existing_daemon() -> Option<BootstrapConfig> {
    let cfg_dir = wctx_config_dir()?;
    let dfile = cfg_dir.join("daemon.json");
    let raw = fs::read_to_string(&dfile).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let port = v.get("port")?.as_u64()? as u16;
    fetch_daemon_bootstrap(port)
}

fn normalize_local_path(p: PathBuf) -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        let s = p.to_string_lossy();
        if let Some(unc) = s.strip_prefix(r"\\?\UNC\") {
            return PathBuf::from(format!(r"\\{unc}"));
        }
        if let Some(stripped) = s.strip_prefix(r"\\?\") {
            return PathBuf::from(stripped);
        }
    }
    p
}

fn resolve_engine_binary(app: &tauri::AppHandle) -> (String, Vec<String>, Option<PathBuf>) {
    let exe_name = if cfg!(target_os = "windows") {
        "wctx.exe"
    } else {
        "wctx"
    };

    if let Ok(res_dir) = app.path().resource_dir() {
        for cand in [
            res_dir.join("resources").join("engine").join(exe_name),
            res_dir.join("engine").join(exe_name),
        ] {
            if cand.is_file() {
                let clean = normalize_local_path(cand);
                return (clean.to_string_lossy().to_string(), vec![], Some(clean));
            }
        }
    }

    // Dev fallback: look for `wctx` in PATH or standard Homebrew paths, then python3 -m wiki_context
    for cand in ["/opt/homebrew/bin/wctx", "/usr/local/bin/wctx"] {
        let p = Path::new(cand);
        if p.is_file() {
            return (cand.to_string(), vec![], None);
        }
    }
    if cfg!(target_os = "windows") {
        return ("python".to_string(), vec!["-m".into(), "wiki_context".into()], None);
    }
    ("python3".to_string(), vec!["-m".into(), "wiki_context".into()], None)
}

fn ensure_stable_shim(engine_exe: &Path) -> std::io::Result<()> {
    #[cfg(target_os = "windows")]
    {
        let local_app = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| user_home_dir().map(|h| h.join("AppData").join("Local")))
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "LOCALAPPDATA"))?;
        let bin_dir = local_app.join("Programs").join("WikiContext").join("bin");
        fs::create_dir_all(&bin_dir)?;
        let shim = bin_dir.join("wctx.cmd");
        let content = format!("@echo off\r\n\"{}\" %*\r\n", engine_exe.display());
        fs::write(&shim, content)?;
        Ok(())
    }
    #[cfg(not(target_os = "windows"))]
    {
        use std::os::unix::fs::PermissionsExt;
        let home = user_home_dir()
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "HOME"))?;
        let bin_dir = home.join(".local").join("bin");
        fs::create_dir_all(&bin_dir)?;
        let shim = bin_dir.join("wctx");
        let escaped = engine_exe.display().to_string().replace('\'', "'\\''");
        let content = format!("#!/bin/sh\nexec '{}' \"$@\"\n", escaped);
        fs::write(&shim, content)?;
        fs::set_permissions(&shim, fs::Permissions::from_mode(0o755))?;
        Ok(())
    }
}

pub fn http_request(
    port: u16,
    method: &str,
    path: &str,
    token: Option<&str>,
    json_body: Option<&str>,
) -> Result<String, String> {
    let addr: SocketAddr = format!("127.0.0.1:{port}").parse().map_err(|e| format!("{e}"))?;
    let mut stream =
        TcpStream::connect_timeout(&addr, Duration::from_secs(2)).map_err(|e| format!("{e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));

    let body_bytes = json_body.unwrap_or("").as_bytes();
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n"
    );
    if let Some(tok) = token {
        if tok.starts_with("adm_") {
            req.push_str(&format!("X-WCTX-Admin-Token: {tok}\r\n"));
        } else {
            req.push_str(&format!("X-WCTX-CSRF: {tok}\r\n"));
        }
    }
    if json_body.is_some() {
        req.push_str("Content-Type: application/json\r\n");
        req.push_str(&format!("Content-Length: {}\r\n", body_bytes.len()));
    }
    req.push_str("\r\n");

    stream.write_all(req.as_bytes()).map_err(|e| format!("{e}"))?;
    if !body_bytes.is_empty() {
        stream.write_all(body_bytes).map_err(|e| format!("{e}"))?;
    }
    stream.flush().map_err(|e| format!("{e}"))?;

    let mut raw = String::new();
    stream.read_to_string(&mut raw).map_err(|e| format!("{e}"))?;
    if let Some((_hdr, body)) = raw.split_once("\r\n\r\n") {
        Ok(body.to_string())
    } else {
        Ok(raw)
    }
}

pub fn spawn_watchdog(state: Arc<SidecarState>, app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut restarts = 0u32;
        loop {
            std::thread::sleep(Duration::from_secs(2));
            if state.is_shutting_down() {
                break;
            }
            let exited = {
                if let Ok(mut guard) = state.child.lock() {
                    if let Some(ref mut rc) = *guard {
                        matches!(rc.child.try_wait(), Ok(Some(_)))
                    } else {
                        false
                    }
                } else {
                    false
                }
            };
            if exited && !state.is_shutting_down() && restarts < 3 {
                restarts += 1;
                std::thread::sleep(Duration::from_millis(400 * u64::from(restarts)));
                let _ = state.start_or_attach(&app);
            }
        }
    });
}
