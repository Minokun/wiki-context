use crate::sidecar::{http_request, wctx_config_dir, SidecarState};
use notify::{Event, RecommendedWatcher, RecursiveMode, Watcher};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

fn extract_port(base_url: &str) -> Option<u16> {
    base_url.rsplit(':').next()?.parse::<u16>().ok()
}

fn read_watched_vault_dirs() -> HashMap<String, Vec<PathBuf>> {
    let mut map: HashMap<String, Vec<PathBuf>> = HashMap::new();
    let Some(cfg_dir) = wctx_config_dir() else {
        return map;
    };
    let cfile = cfg_dir.join("console.json");
    let Ok(raw) = fs::read_to_string(cfile) else {
        return map;
    };
    let Ok(doc) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return map;
    };

    let watched: HashSet<String> = doc
        .get("watched_vaults")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();

    if watched.is_empty() {
        return map;
    }

    if let Some(vaults) = doc.get("vaults").and_then(|v| v.as_array()) {
        for v in vaults {
            let vid = v.get("id").and_then(|x| x.as_str()).unwrap_or("");
            if !watched.contains(vid) || v.get("kind").and_then(|x| x.as_str()) == Some("remote") {
                continue;
            }
            if let Some(p_str) = v.get("path").and_then(|x| x.as_str()) {
                let root = Path::new(p_str);
                let mut dirs = Vec::new();
                for sub in ["sources", "史料", "wiki", "知识库"] {
                    let cand = root.join(sub);
                    if cand.is_dir() {
                        dirs.push(cand);
                    }
                }
                if !dirs.is_empty() {
                    map.insert(vid.to_string(), dirs);
                }
            }
        }
    }
    map
}

pub fn start_native_watcher(sidecar: Arc<SidecarState>) {
    std::thread::spawn(move || {
        let dirty: Arc<Mutex<HashMap<String, Instant>>> = Arc::new(Mutex::new(HashMap::new()));
        let dir_to_vault: Arc<Mutex<Vec<(PathBuf, String)>>> = Arc::new(Mutex::new(Vec::new()));
        let mut active_paths: HashSet<PathBuf> = HashSet::new();

        let dirty_cb = Arc::clone(&dirty);
        let map_cb = Arc::clone(&dir_to_vault);

        let mut watcher: Option<RecommendedWatcher> = notify::recommended_watcher(
            move |res: Result<Event, notify::Error>| {
                let Ok(ev) = res else { return };
                if ev.kind.is_access() {
                    return;
                }
                let Ok(map_guard) = map_cb.lock() else { return };
                let mut matched_vids: HashSet<String> = HashSet::new();
                for p in &ev.paths {
                    for (dir, vid) in map_guard.iter() {
                        if p.starts_with(dir) {
                            matched_vids.insert(vid.clone());
                        }
                    }
                }
                drop(map_guard);
                if !matched_vids.is_empty() {
                    if let Ok(mut dguard) = dirty_cb.lock() {
                        let now = Instant::now();
                        for vid in matched_vids {
                            dguard.insert(vid, now);
                        }
                    }
                }
            },
        )
        .ok();

        let mut last_scan = Instant::now();
        loop {
            if sidecar.is_shutting_down() {
                break;
            }

            if last_scan.elapsed() >= Duration::from_secs(5) {
                last_scan = Instant::now();
                let desired = read_watched_vault_dirs();
                let mut next_pairs = Vec::new();
                let mut next_set = HashSet::new();
                for (vid, dirs) in desired {
                    for d in dirs {
                        next_set.insert(d.clone());
                        next_pairs.push((d, vid.clone()));
                    }
                }

                if let Some(ref mut w) = watcher {
                    for old in active_paths.difference(&next_set) {
                        let _ = w.unwatch(old);
                    }
                    for added in next_set.difference(&active_paths) {
                        let _ = w.watch(added, RecursiveMode::Recursive);
                    }
                }
                active_paths = next_set;
                if let Ok(mut mg) = dir_to_vault.lock() {
                    *mg = next_pairs;
                }
            }

            // Debounce 2s before triggering async sync
            let mut due: Vec<String> = Vec::new();
            if let Ok(mut dg) = dirty.lock() {
                let now = Instant::now();
                dg.retain(|vid, ts| {
                    if now.duration_since(*ts) >= Duration::from_secs(2) {
                        due.push(vid.clone());
                        false
                    } else {
                        true
                    }
                });
            }

            if !due.is_empty() {
                if let Ok(boot) = sidecar.get_bootstrap() {
                    if let Some(port) = extract_port(&boot.base_url) {
                        for vid in due {
                            let body = format!(r#"{{"vault_id":"{}","async":true,"trigger":"watcher"}}"#, vid);
                            let _ = http_request(
                                port,
                                "POST",
                                "/api/vaults/sync",
                                Some(&boot.admin_token),
                                Some(&body),
                            );
                        }
                    }
                }
            }

            std::thread::sleep(Duration::from_millis(500));
        }
    });
}
