use crate::sidecar::{http_request, SidecarState};
use std::sync::Arc;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;
use tauri_plugin_autostart::ManagerExt;

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

pub fn setup_tray(app: &tauri::AppHandle, sidecar: Arc<SidecarState>) -> Result<(), tauri::Error> {
    let show_i = MenuItem::with_id(app, "show", "打开 wiki-context 控制中心", true, None::<&str>)?;
    let sync_i = MenuItem::with_id(app, "sync_all", "立即同步全部本地知识库", true, None::<&str>)?;
    let auto_i = MenuItem::with_id(app, "autostart", "切换开机自启动", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "彻底退出 (关闭网关与索引守护)", true, None::<&str>)?;

    let menu = Menu::with_items(app, &[&show_i, &sync_i, &auto_i, &quit_i])?;
    let icon = app.default_window_icon().cloned();

    let mut builder = TrayIconBuilder::new()
        .menu(&menu)
        .tooltip("wiki-context 知识中枢")
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "sync_all" => {
                let sc = Arc::clone(&sidecar);
                std::thread::spawn(move || {
                    if let Ok(boot) = sc.get_bootstrap() {
                        if let Some(port) = boot.base_url.rsplit(':').next().and_then(|s| s.parse::<u16>().ok()) {
                            if let Ok(ov_raw) = http_request(port, "GET", "/api/overview", Some(&boot.admin_token), None) {
                                if let Ok(ov) = serde_json::from_str::<serde_json::Value>(&ov_raw) {
                                    if let Some(vaults) = ov.get("vaults").and_then(|v| v.as_array()) {
                                        for v in vaults {
                                            if v.get("kind").and_then(|k| k.as_str()) == Some("local") {
                                                if let Some(vid) = v.get("id").and_then(|x| x.as_str()) {
                                                    let body = format!(r#"{{"vault_id":"{}","async":true}}"#, vid);
                                                    let _ = http_request(port, "POST", "/api/vaults/sync", Some(&boot.admin_token), Some(&body));
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                });
            }
            "autostart" => {
                let mgr = app.autolaunch();
                if mgr.is_enabled().unwrap_or(false) {
                    let _ = mgr.disable();
                } else {
                    let _ = mgr.enable();
                }
            }
            "quit" => {
                sidecar.shutdown();
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });

    if let Some(ic) = icon {
        builder = builder.icon(ic);
    }

    builder.build(app)?;
    Ok(())
}
