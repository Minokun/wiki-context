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
      version: cfg.version || "0.4.0",
    };
    return cachedBootstrap;
  }

  const injected = typeof window !== "undefined" ? window.__WCTX_BOOTSTRAP__ : undefined;
  if (injected?.csrfToken) {
    cachedBootstrap = {
      mode: "web",
      baseUrl: "",
      csrfToken: injected.csrfToken,
      version: injected.version || "0.4.0",
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
    version: data.version || "0.4.0",
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

export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
