mod sidecar;
mod tray;
mod watcher;

use sidecar::{spawn_watchdog, BootstrapConfig, SidecarState};
use std::sync::Arc;
use tauri::WindowEvent;
use tauri_plugin_autostart::MacosLauncher;

#[tauri::command]
fn get_bootstrap_config(state: tauri::State<'_, Arc<SidecarState>>) -> Result<BootstrapConfig, String> {
    state.get_bootstrap()
}

#[tauri::command]
fn prepare_for_update(state: tauri::State<'_, Arc<SidecarState>>) {
    state.shutdown();
}

#[tauri::command]
fn open_directory(path: String) -> Result<(), String> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return Err("目录路径不能为空".to_string());
    }
    let dir = std::path::PathBuf::from(trimmed);
    if !dir.is_dir() {
        return Err(format!("目录不存在或不可访问: {}", dir.display()));
    }
    #[cfg(target_os = "macos")]
    let status = std::process::Command::new("open").arg(&dir).spawn();
    #[cfg(target_os = "windows")]
    let status = std::process::Command::new("explorer").arg(&dir).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let status = std::process::Command::new("xdg-open").arg(&dir).spawn();

    status
        .map(|_| ())
        .map_err(|e| format!("无法打开目录 {}: {e}", dir.display()))
}

pub fn run() {
    let sidecar = Arc::new(SidecarState::new());
    let sidecar_setup = Arc::clone(&sidecar);
    let sidecar_tray = Arc::clone(&sidecar);
    let sidecar_exit = Arc::clone(&sidecar);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--minimized"]),
        ))
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(sidecar)
        .invoke_handler(tauri::generate_handler![
            get_bootstrap_config,
            prepare_for_update,
            open_directory
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            tray::setup_tray(&handle, sidecar_tray)?;
            // Start sidecar on a background thread so Cocoa/WKWebView main loop renders immediately
            let bg_handle = handle.clone();
            let bg_sidecar = Arc::clone(&sidecar_setup);
            std::thread::spawn(move || {
                if let Err(err) = bg_sidecar.start_or_attach(&bg_handle) {
                    eprintln!("[wiki-context] sidecar start error: {err}");
                }
                spawn_watchdog(Arc::clone(&bg_sidecar), bg_handle);
                watcher::start_native_watcher(bg_sidecar);
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Hide to system tray instead of killing the background gateway & watcher
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building wiki-context desktop application");

    app.run(move |_app_handle, event| {
        if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
            sidecar_exit.shutdown();
        }
    });
}
