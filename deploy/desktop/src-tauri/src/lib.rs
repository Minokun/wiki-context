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
        .manage(sidecar)
        .invoke_handler(tauri::generate_handler![get_bootstrap_config])
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
