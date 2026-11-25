import { getBootstrapConfig } from "../platform/adapter";
import type {
  AsyncJobRecord,
  DoctorResponse,
  LlmCompilerStatus,
  LogInvocation,
  LogStatsResponse,
  MemoryItem,
  NetworkDiscoveryResponse,
  OverviewResponse,
  TokenRecord,
} from "../types";

async function authHeaders(extra?: Record<string, string>): Promise<Record<string, string>> {
  const boot = await getBootstrapConfig();
  const h: Record<string, string> = { ...(extra || {}) };
  if (boot.adminToken) {
    h["X-WCTX-Admin-Token"] = boot.adminToken;
  } else if (boot.csrfToken) {
    h["X-WCTX-CSRF"] = boot.csrfToken;
  }
  return h;
}

async function requestJson<T>(path: string, body?: unknown): Promise<T> {
  const boot = await getBootstrapConfig();
  const url = `${boot.baseUrl}${path}`;
  const isPost = body !== undefined;
  const headers = await authHeaders(isPost ? { "Content-Type": "application/json" } : undefined);
  const resp = await fetch(url, {
    method: isPost ? "POST" : "GET",
    headers,
    body: isPost ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  let parsed: unknown = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    parsed = { error: text || `HTTP ${resp.status}` };
  }
  if (!resp.ok) {
    const errMsg = (parsed as { error?: string })?.error || `HTTP ${resp.status}`;
    throw new Error(errMsg);
  }
  return parsed as T;
}

export const api = {
  overview: () => requestJson<OverviewResponse>("/api/overview"),
  doctor: () => requestJson<DoctorResponse>("/api/doctor"),

  // Filesystem browser
  fsList: (path = "") =>
    requestJson<{
      current: string;
      parent: string;
      file_count_here: number;
      directories: { name: string; path: string; is_vault: boolean }[];
      shortcuts: { label: string; path: string }[];
    }>(`/api/fs/list?path=${encodeURIComponent(path)}`),
  fsInspect: (path: string) =>
    requestJson<{
      kind: string;
      path: string;
      suggested_name: string;
      suggested_id: string;
      detected_layout: string;
      recommended_storage_mode?: "managed" | "in_place";
      storage_mode?: "managed" | "in_place";
      sources_path?: string;
      file_count: number;
      loose_total?: number;
      warnings?: string[];
    }>("/api/fs/inspect", { path }),
  fsMkdir: (parent: string, name: string) =>
    requestJson<{ created: string }>("/api/fs/mkdir", { parent, name }),

  // Vaults & Sync / Compile Jobs
  onboardVault: (payload: {
    path: string;
    vault_id?: string;
    name?: string;
    layout?: string;
    storage_mode?: "auto" | "managed" | "in_place";
    auto_compile?: boolean;
  }) =>
    requestJson<{
      vault_id: string;
      storage_mode?: "managed" | "in_place";
      workspace_path?: string;
      sources_path?: string;
      uncompiled_count?: number;
      llm_ready?: boolean;
      compile_job?: AsyncJobRecord | null;
    }>("/api/vaults/onboard", payload),
  importFolder: (payload: { vault_id: string; external_folder: string; target_subfolder?: string }) =>
    requestJson<{
      vault_id: string;
      mode?: "mount" | "copy";
      mounted?: boolean;
      mount_name?: string;
      copied_count: number;
      llm_ready?: boolean;
      compile_job?: AsyncJobRecord | null;
    }>("/api/vaults/import-folder", payload),
  mountFolder: (payload: { vault_id: string; external_folder: string; mount_name?: string }) =>
    requestJson<{
      vault_id: string;
      mounted: boolean;
      mount_name: string;
      external_folder: string;
      target_subfolder: string;
      llm_ready?: boolean;
      compile_job?: AsyncJobRecord | null;
    }>("/api/vaults/mount-folder", payload),
  unmountFolder: (payload: { vault_id: string; mount_name: string; purge_mirrors?: boolean }) =>
    requestJson<{
      vault_id: string;
      unmounted: boolean;
      mount_name: string;
      purged_mirrors: number;
    }>("/api/vaults/unmount-folder", payload),
  relinkVault: (payload: { vault_id: string; new_source_dir: string; mount_name?: string }) =>
    requestJson<{
      vault_id: string;
      mount_name: string;
      old_path: string;
      new_path: string;
    }>("/api/vaults/relink", payload),
  syncVaultAsync: (vault_id: string, force = false) =>
    requestJson<AsyncJobRecord>("/api/vaults/sync", {
      vault_id,
      force,
      async: true,
    }),
  compileVaultAsync: (vault_id: string, max_items?: number) =>
    requestJson<AsyncJobRecord>("/api/vaults/compile", {
      vault_id,
      ...(max_items !== undefined ? { max_items } : {}),
    }),
  getCompilePlan: (vault_id: string) =>
    requestJson<{
      vault_id: string;
      uncompiled_sources: string[];
      stale_pages: string[];
      active_notes: string[];
      uncompiled_count: number;
      stale_count: number;
      cluster_count?: number;
    }>(`/api/vaults/compile-plan?vault_id=${encodeURIComponent(vault_id)}`),
  getJob: (job_id: string) =>
    requestJson<AsyncJobRecord>(`/api/jobs/${encodeURIComponent(job_id)}`),
  cancelJob: (job_id: string) =>
    requestJson<AsyncJobRecord>(`/api/jobs/${encodeURIComponent(job_id)}/cancel`, {}),

  // Built-in AI Compiler Agent & LLM Credentials
  getLlmConfig: () => requestJson<LlmCompilerStatus>("/api/llm/config"),
  saveLlmConfig: (payload: {
    provider?: string;
    protocol?: string;
    base_url?: string;
    model?: string;
    api_key?: string;
    clear_api_key?: boolean;
    auto_compile_on_sync?: boolean;
    max_items_per_run?: number;
    workers?: number;
    max_tokens?: number;
    disable_thinking?: boolean;
    skip_csv?: boolean;
    project_rollup?: boolean;
  }) => requestJson<LlmCompilerStatus>("/api/llm/config", payload),
  testLlmConfig: (payload?: {
    provider?: string;
    protocol?: string;
    base_url?: string;
    model?: string;
    api_key?: string;
    disable_thinking?: boolean;
  }) =>
    requestJson<{ ok: boolean; latency_ms: number; reply: string; model: string }>(
      "/api/llm/test",
      payload || {},
    ),
  watchVault: (vault_id: string, enabled: boolean) =>
    requestJson<{ vault_id: string; watching: boolean }>("/api/vaults/watch", { vault_id, enabled }),
  lintVault: (vault_id: string) =>
    requestJson<{ ok: boolean; error_count: number; warning_count: number; errors: { code: string; file: string; detail: string }[]; warnings: { code: string; file: string; detail: string }[] }>(
      "/api/vaults/lint",
      { vault_id },
    ),
  renameVault: (old_id: string, new_id: string, sync_clients = true) =>
    requestJson<{ old_id: string; new_id: string }>("/api/vaults/rename", { old_id, new_id, sync_clients }),
  removeVault: (vault_id: string, cleanup_workspace = false) =>
    requestJson<{ removed: boolean; workspace_cleaned?: boolean; archived_to?: string }>(
      "/api/vaults/remove",
      { vault_id, cleanup_workspace },
    ),

  // Synonyms
  getSynonyms: (vault_id: string) =>
    requestJson<{ vault_id: string; path: string; groups: { canonical: string[]; aliases: string[] }[] }>(
      `/api/vaults/synonyms?vault_id=${encodeURIComponent(vault_id)}`,
    ),
  saveSynonyms: (vault_id: string, groups: { canonical: string[]; aliases: string[] }[]) =>
    requestJson<{ vault_id: string; count: number; groups: { canonical: string[]; aliases: string[] }[] }>(
      "/api/vaults/synonyms",
      { vault_id, groups },
    ),
  saveDomainProfile: (payload: {
    vault_id: string;
    action?: "save" | "generate" | "unlock";
    domain?: string;
    entity_hints?: string[];
    query_tips?: string;
  }) =>
    requestJson<{
      vault_id: string;
      action: string;
      domain_profile: {
        domain: string;
        entity_hints: string[];
        query_tips: string;
        source?: string;
        locked?: boolean;
        updated_at?: string;
      };
    }>("/api/vaults/domain-profile", payload),

  // Playground
  playgroundInspect: (payload: {
    vault_id: string;
    question: string;
    mode: "wiki_context" | "wiki_search" | "wiki_route";
    principal_spec?: { type: "admin" | "group" | "token"; groups?: string[]; token_id?: string };
    expanded_terms?: string[];
    max_tokens?: number;
  }) => requestJson<Record<string, unknown>>("/api/playground/inspect", payload),

  // Logs & Telemetry
  logsList: (params: {
    limit?: number;
    before_id?: number;
    client?: string;
    vault_id?: string;
    only_missing?: boolean;
    only_errors?: boolean;
    min_elapsed_ms?: number;
    q?: string;
  }) => {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.before_id) qs.set("before_id", String(params.before_id));
    if (params.client) qs.set("client", params.client);
    if (params.vault_id) qs.set("vault_id", params.vault_id);
    if (params.only_missing) qs.set("only_missing", "1");
    if (params.only_errors) qs.set("only_errors", "1");
    if (params.min_elapsed_ms) qs.set("min_elapsed_ms", String(params.min_elapsed_ms));
    if (params.q) qs.set("q", params.q);
    return requestJson<{ items: LogInvocation[]; latest_id: number; next_before_id: number | null }>(
      `/api/logs/list?${qs.toString()}`,
    );
  },
  logsStats: (hours = 24) => requestJson<LogStatsResponse>(`/api/logs/stats?hours=${hours}`),
  logsClear: () => requestJson<{ cleared: number }>("/api/logs/clear", {}),

  // Network & OTP Pairing
  networkDiscover: (refresh = false) =>
    requestJson<NetworkDiscoveryResponse>(`/api/network/discover?refresh=${refresh ? "1" : "0"}`),
  createPairingCode: (payload: { allowed_vaults: string[]; groups: string[]; ttl_sec?: number }) =>
    requestJson<{
      code: string;
      otp_id: string;
      expires_at: number;
      ttl_sec: number;
      urls: string[];
      instance_id: string;
      fp: string;
    }>("/api/network/pairing-code", payload),
  connectWithPairingCode: (payload: { code: string; client_label?: string; sync_clients?: boolean }) =>
    requestJson<{
      paired_host: string;
      latency_ms: number;
      connected_vaults: { id: string; remote_vault_id: string; name: string }[];
    }>("/api/network/connect-code", payload),
  lanGatewayStart: (port = 8090) => requestJson<unknown>("/api/lan-gateway/start", { host: "0.0.0.0", port }),
  lanGatewayStop: () => requestJson<unknown>("/api/lan-gateway/stop", {}),
  probeRemote: (gateway_url: string, token: string) =>
    requestJson<{
      gateway_url: string;
      uid: string;
      groups: string[];
      vaults: { remote_vault_id: string; name: string; docs: number; sections: number }[];
    }>("/api/remotes/probe", { gateway_url, token }),
  connectRemote: (payload: {
    gateway_url: string;
    token: string;
    remote_vault_ids?: string[];
    sync_clients?: boolean;
  }) => requestJson<unknown>("/api/remotes/connect", payload),
  updateRemote: (payload: {
    vault_id: string;
    gateway_url?: string;
    token?: string;
    sync_clients?: boolean;
  }) =>
    requestJson<{
      vault_id: string;
      gateway_url: string;
      uid: string;
      groups: string[];
      docs: number;
      sections: number;
      token_masked: string;
      client_sync: { client_id: string; client_name: string }[];
    }>("/api/remotes/update", payload),
  testRemote: (vault_id: string) =>
    requestJson<{
      ok: boolean;
      vault_id: string;
      gateway_url: string;
      uid?: string;
      groups?: string[];
      docs?: number;
      sections?: number;
      latency_ms: number;
      error: string;
    }>("/api/remotes/test", { vault_id }),

  // Tokens & ACL
  createToken: (payload: { uid: string; label?: string; groups: string[]; allowed_vaults: string[] }) =>
    requestJson<{ token: string; record: TokenRecord }>("/api/tokens/create", payload),
  updateToken: (payload: {
    id: string;
    label?: string;
    groups?: string[];
    allowed_vaults?: string[];
    disabled?: boolean;
  }) => requestJson<{ updated: boolean }>("/api/tokens/update", payload),
  revealToken: (id: string) => requestJson<{ id: string; token: string }>("/api/tokens/reveal", { id }),
  rotateToken: (id: string) => requestJson<{ token: string; record: TokenRecord }>("/api/tokens/rotate", { id }),
  deleteToken: (id: string) => requestJson<{ deleted: boolean }>("/api/tokens/delete", { id }),
  saveAcl: (acl: Record<string, { allow: string[]; deny: string[] }>) =>
    requestJson<{ acl: Record<string, { allow: string[]; deny: string[] }> }>("/api/acl/save", { acl }),
  simulateAcl: (token_id: string, vault_id: string) =>
    requestJson<{
      vault_allowed: boolean;
      reason: string;
      allowed_sources_count?: number;
      denied_sources_count?: number;
      allowed_sources: string[];
      denied_sources: string[];
      allowed_wiki: string[];
      denied_wiki: string[];
    }>("/api/acl/simulate", { token_id, vault_id }),

  // Clients
  scanClients: () =>
    requestJson<{
      clients: OverviewResponse["clients"];
      installed_count: number;
      synced_count: number;
      total_supported: number;
    }>("/api/clients/scan"),
  previewClient: (payload: {
    client_id: string;
    mode: string;
    vault_ids: string[];
    gateway_url?: string;
    token?: string;
  }) => requestJson<{ client_id: string; config_path: string; snippet: string }>("/api/clients/preview", payload),
  syncClients: (payload: {
    client_ids: string[];
    mode: string;
    vault_ids: string[];
    gateway_url?: string;
    token?: string;
    remove_unlisted_wctx?: boolean;
  }) => requestJson<{ results: { client_id: string; client_name: string; path: string; written: string[] }[] }>(
    "/api/clients/sync",
    payload,
  ),
  disconnectClients: (client_ids: string[]) =>
    requestJson<{ results: { client_id: string; client_name: string; path: string; removed: string[] }[] }>(
      "/api/clients/disconnect",
      { client_ids },
    ),

  // Memory
  listMemory: (vault_id: string, status = "all", kind = "all", q = "") =>
    requestJson<{ vault_id?: string; count?: number; items?: MemoryItem[]; notes?: MemoryItem[] }>(
      `/api/memory/list?vault_id=${encodeURIComponent(vault_id)}&status=${encodeURIComponent(status)}&kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}`,
    ),
  createMemory: (payload: {
    vault_id: string;
    text: string;
    kind: string;
    related_sources: string[];
    tags: string[];
  }) => requestJson<MemoryItem>("/api/memory/create", payload),
  updateMemory: (payload: {
    vault_id: string;
    note_id: string;
    text?: string;
    kind?: string;
    status?: string;
    related_sources?: string[];
    tags?: string[];
  }) => requestJson<MemoryItem>("/api/memory/update", payload),
  deleteMemory: (vault_id: string, note_id: string) =>
    requestJson<{ deleted: boolean }>("/api/memory/delete", { vault_id, note_id }),
  importMemory: (vault_id: string, items: unknown[]) =>
    requestJson<{ imported_count: number }>("/api/memory/import", { vault_id, items }),
};

/**
 * Subscribe to `/api/logs/stream` using `fetch()` ReadableStream so custom
 * `X-WCTX-Admin-Token` / `X-WCTX-CSRF` headers are sent without URL query leaks.
 */
export function subscribeLogStream(handlers: {
  onInit: (items: LogInvocation[]) => void;
  onLog: (item: LogInvocation) => void;
  onStatus?: (connected: boolean) => void;
}): () => void {
  let aborted = false;
  let controller: AbortController | null = null;
  let lastId = 0;
  let backoffMs = 800;

  async function pump() {
    while (!aborted) {
      controller = new AbortController();
      try {
        const boot = await getBootstrapConfig();
        const headers = await authHeaders(lastId > 0 ? { "Last-Event-ID": String(lastId) } : undefined);
        const resp = await fetch(`${boot.baseUrl}/api/logs/stream?after_id=${lastId}`, {
          headers,
          signal: controller.signal,
        });
        if (!resp.ok || !resp.body) {
          throw new Error(`SSE HTTP ${resp.status}`);
        }
        handlers.onStatus?.(true);
        backoffMs = 800;

        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let curEvent = "message";
        let curData: string[] = [];
        let curId = "";

        const dispatch = () => {
          if (curData.length === 0) {
            curEvent = "message";
            curId = "";
            return;
          }
          const raw = curData.join("\n");
          if (curId && /^\d+$/.test(curId)) {
            lastId = Math.max(lastId, Number(curId));
          }
          try {
            const parsed = JSON.parse(raw);
            if (curEvent === "init" && Array.isArray(parsed?.items)) {
              for (const it of parsed.items as LogInvocation[]) {
                lastId = Math.max(lastId, it.id);
              }
              handlers.onInit(parsed.items as LogInvocation[]);
            } else if (curEvent === "log" && parsed && typeof parsed === "object") {
              const row = parsed as LogInvocation;
              lastId = Math.max(lastId, row.id);
              handlers.onLog(row);
            }
          } catch {
            // ignore keepalive/malformed chunk
          }
          curEvent = "message";
          curData = [];
          curId = "";
        };

        while (!aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split(/\r?\n/);
          buffer = lines.pop() || "";
          for (const ln of lines) {
            if (ln === "") {
              dispatch();
            } else if (ln.startsWith(":")) {
              // keepalive comment
            } else if (ln.startsWith("event:")) {
              curEvent = ln.slice(6).trim();
            } else if (ln.startsWith("id:")) {
              curId = ln.slice(3).trim();
            } else if (ln.startsWith("data:")) {
              curData.push(ln.slice(5).trimStart());
            }
          }
        }
      } catch {
        handlers.onStatus?.(false);
      }
      if (aborted) break;
      await new Promise((r) => setTimeout(r, backoffMs));
      backoffMs = Math.min(8000, backoffMs * 1.6);
    }
  }

  void pump();
  return () => {
    aborted = true;
    controller?.abort();
  };
}
