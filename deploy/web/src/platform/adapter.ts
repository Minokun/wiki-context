import type { BootstrapConfig } from "../types";

declare global {
  interface Window {
    __WCTX_BOOTSTRAP__?: {
      csrfToken?: string;
      version?: string;
      mode?: "desktop" | "web";
      gatewayBaseUrl?: string;
      instanceId?: string;
      fp?: string;
    };
    __TAURI_INTERNALS__?: unknown;
  }
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && Boolean(window.__TAURI_INTERNALS__);
}

let cachedBootstrap: BootstrapConfig | null = null;

export async function getBootstrapConfig(): Promise<BootstrapConfig> {
  if (cachedBootstrap) return cachedBootstrap;

  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    let cfg: { baseUrl: string; adminToken: string; version?: string } | null = null;
    let lastErr: unknown = null;
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      try {
        cfg = await invoke<{
          baseUrl: string;
          adminToken: string;
          version?: string;
        }>("get_bootstrap_config");
        break;
      } catch (err) {
        lastErr = err;
        await new Promise((r) => setTimeout(r, 120));
      }
    }
    if (!cfg) {
      throw new Error(lastErr instanceof Error ? lastErr.message : String(lastErr || "Sidecar initialization timed out"));
    }
    const tok = (cfg.adminToken || "").trim();
    const isAdminSecret = tok.startsWith("adm_");
    cachedBootstrap = {
      mode: "desktop",
      baseUrl: cfg.baseUrl.replace(/\/+$/, ""),
      adminToken: isAdminSecret ? tok : undefined,
      csrfToken: isAdminSecret ? undefined : tok,
      version: cfg.version || "0.5.2",
    };
    return cachedBootstrap;
  }

  const injected = typeof window !== "undefined" ? window.__WCTX_BOOTSTRAP__ : undefined;
  if (injected?.csrfToken) {
    cachedBootstrap = {
      mode: "web",
      baseUrl: "",
      csrfToken: injected.csrfToken,
      version: injected.version || "0.5.2",
      instanceId: injected.instanceId,
      fp: injected.fp,
    };
    return cachedBootstrap;
  }

  const resp = await fetch("/api/bootstrap", { credentials: "same-origin" });
  if (!resp.ok) {
    throw new Error(`Bootstrap failed (${resp.status})`);
  }
  const data = (await resp.json()) as {
    csrfToken: string;
    version: string;
    instanceId?: string;
    fp?: string;
  };
  cachedBootstrap = {
    mode: "web",
    baseUrl: "",
    csrfToken: data.csrfToken,
    version: data.version || "0.5.2",
    instanceId: data.instanceId,
    fp: data.fp,
  };
  return cachedBootstrap;
}

export async function pickNativeDirectory(title = "选择知识库文件夹"): Promise<string | null> {
  if (!isTauriRuntime()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: true,
    multiple: false,
    title,
  });
  if (typeof selected === "string" && selected.trim()) {
    return selected.trim();
  }
  return null;
}

export async function subscribeNativeFileDrop(
  onDropPaths: (paths: string[]) => void,
): Promise<() => void> {
  if (!isTauriRuntime()) return () => {};
  try {
    const { getCurrentWebview } = await import("@tauri-apps/api/webview");
    const unlisten = await getCurrentWebview().onDragDropEvent((event) => {
      if (event.payload.type === "drop" && Array.isArray(event.payload.paths) && event.payload.paths.length > 0) {
        onDropPaths(event.payload.paths);
      }
    });
    return unlisten;
  } catch {
    return () => {};
  }
}

export async function openNativeDirectory(path: string): Promise<boolean> {
  if (!isTauriRuntime() || !path.trim()) return false;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_directory", { path: path.trim() });
    return true;
  } catch {
    return false;
  }
}

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export interface AppUpdateInfo {
  version: string;
  currentVersion: string;
  date?: string;
  body?: string;
}

export interface UpdateProgress {
  phase: "downloading" | "installing";
  downloaded: number;
  total?: number;
  percent?: number;
}

export interface AppUpdateHandle extends AppUpdateInfo {
  install: (onProgress?: (progress: UpdateProgress) => void) => Promise<void>;
  close: () => Promise<void>;
}

export async function checkForAppUpdate(): Promise<AppUpdateHandle | null> {
  if (!isTauriRuntime()) return null;
  const { check } = await import("@tauri-apps/plugin-updater");
  const update = await check({ timeout: 15000 });
  if (!update) return null;

  return {
    version: update.version,
    currentVersion: update.currentVersion,
    date: update.date,
    body: update.body,
    close: async () => {
      try {
        await update.close();
      } catch {
        // ignore double-close
      }
    },
    install: async (onProgress) => {
      let downloaded = 0;
      let total: number | undefined;
      onProgress?.({ phase: "downloading", downloaded: 0, total, percent: 0 });
      await update.download((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength;
          onProgress?.({
            phase: "downloading",
            downloaded: 0,
            total,
            percent: total ? 0 : undefined,
          });
        } else if (event.event === "Progress") {
          downloaded += event.data.chunkLength;
          const pct = total && total > 0 ? Math.min(99, Math.round((downloaded / total) * 100)) : undefined;
          onProgress?.({
            phase: "downloading",
            downloaded,
            total,
            percent: pct,
          });
        } else if (event.event === "Finished") {
          onProgress?.({
            phase: "downloading",
            downloaded,
            total,
            percent: 100,
          });
        }
      });

      onProgress?.({ phase: "installing", downloaded, total, percent: 100 });
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("prepare_for_update");
      await update.install();
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    },
  };
}
