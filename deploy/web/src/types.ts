export type RuntimeMode = "desktop" | "web";

export interface BootstrapConfig {
  mode: RuntimeMode;
  baseUrl: string;
  version: string;
  csrfToken?: string;
  adminToken?: string;
  instanceId?: string;
  fp?: string;
}

export interface VaultCapabilities {
  kind: "local" | "remote";
  can_xray: boolean;
  can_shadow_diff: boolean;
  can_edit_synonyms: boolean;
  can_edit_memory: boolean;
  can_sync: boolean;
  can_watch: boolean;
  can_rename: boolean;
}

export interface FolderTreeNode {
  name: string;
  prefix: string;
  mounted?: boolean;
  mount_path?: string;
  available?: boolean;
  children: { name: string; prefix: string }[];
}

export interface SourceMountInfo {
  name: string;
  path: string;
  available: boolean;
}

export interface DomainProfile {
  domain: string;
  entity_hints: string[];
  query_tips: string;
  source?: "auto" | "manual" | string;
  locked?: boolean;
  fingerprint?: string;
  top_dirs_at_gen?: string[];
  doc_count_at_gen?: number;
  wiki_count_at_gen?: number;
  updated_at?: string;
}

export interface LlmProviderPreset {
  id: string;
  label: string;
  protocol: "openai" | "anthropic" | string;
  base_url: string;
  model: string;
}

export interface LlmCompilerStatus {
  provider: string;
  protocol: "openai" | "anthropic" | string;
  base_url: string;
  model: string;
  has_api_key: boolean;
  key_optional?: boolean;
  api_key_masked: string;
  key_source: "saved" | "env" | "local" | "none" | string;
  ready: boolean;
  auto_compile_on_sync: boolean;
  max_items_per_run: number;
  workers?: number;
  max_tokens?: number;
  disable_thinking?: boolean;
  skip_csv?: boolean;
  project_rollup?: boolean;
  presets: LlmProviderPreset[];
}

export interface CompileJobProgress {
  phase: string;
  done: number;
  total: number;
  pending_total?: number;
  batch_limit?: number;
  current: string;
  compiled_count: number;
  refreshed_count: number;
  failed_count?: number;
}

export interface AsyncJobRecord {
  job_id: string;
  kind?: "sync" | "compile" | string;
  vault_id: string;
  status: "running" | "done" | "failed" | "skipped" | "cancelled";
  progress?: CompileJobProgress;
  result?: Record<string, unknown>;
  error?: string;
}

export interface VaultInfo {
  id: string;
  kind: "local" | "remote";
  storage_mode?: "managed" | "in_place" | "remote";
  capabilities: VaultCapabilities;
  name: string;
  layout: string;
  path: string;
  workspace_path?: string;
  sources_path?: string;
  source_available?: boolean;
  source_mounts?: SourceMountInfo[];
  ready: boolean;
  exists: boolean;
  hydrating?: boolean;
  watching?: boolean;
  docs: number;
  pages?: number;
  sections: number;
  wiki_docs: number;
  wiki_sections?: number;
  uncompiled_count?: number;
  stale_count?: number;
  cluster_count?: number;
  compile_job?: AsyncJobRecord;
  active_memory_notes: number;
  generation_id: string;
  generated_at?: string;
  sources_dir?: string;
  wiki_dir?: string;
  notes_dir?: string;
  domain_profile?: DomainProfile;
  source_prefixes?: string[];
  folder_tree?: FolderTreeNode[];
  sample_sources?: string[];
  // Remote fields
  remote_vault_id?: string;
  gateway_url?: string;
  mcp_url?: string;
  token_masked?: string;
  has_token?: boolean;
  remote_uid?: string;
  remote_groups?: string[];
  online_error?: string;
  instance_id?: string;
}

export interface TokenRecord {
  id: string;
  label: string;
  uid: string;
  token_preview: string;
  groups: string[];
  allowed_vaults: string[];
  disabled: boolean;
  created_at: string;
  /** True when the console still holds this key's raw secret and can copy it in one click. */
  copyable?: boolean;
}

export interface AclRule {
  allow: string[];
  deny: string[];
}

export interface ClientInfo {
  id: string;
  name: string;
  vendor: string;
  category?: "ide" | "cli" | "desktop" | string;
  format: string;
  description: string;
  installed: boolean;
  detected_via?: string[];
  config_path: string;
  config_exists: boolean;
  configured_servers: Record<string, {
    server_name: string;
    mode: string;
    command?: string;
    args?: string[];
    url?: string;
    vault_path?: string;
  }>;
  wctx_server_count?: number;
  other_mcp_count?: number;
  sync_status: "idle" | "synced" | "partial" | "unconfigured";
  matched_count: number;
  total_vaults: number;
  command_ok?: boolean;
  command_issues?: string[];
}

export interface LanGatewayStatus {
  running: boolean;
  mode: "embedded" | "external" | "stopped";
  bind_host: string;
  port: number;
  lan_ips: string[];
  lan_urls: string[];
  recommended_url: string;
  has_tokens: boolean;
  active_token_count: number;
  local_vault_count: number;
  port_conflict?: string;
  instance_id?: string;
  fp?: string;
}

export interface OverviewResponse {
  version: string;
  config_dir: string;
  vaults_dir?: string;
  tokens_file: string;
  gateway_base_url: string;
  lan_gateway: LanGatewayStatus;
  llm_compiler?: LlmCompilerStatus;
  wctx_command: string;
  vaults: VaultInfo[];
  tokens: TokenRecord[];
  acl: Record<string, AclRule>;
  clients: ClientInfo[];
}

export interface DoctorResponse {
  version: string;
  platform: string;
  python: { version: string; executable: string; ok: boolean };
  wctx_cli: { command: string; base_args: string[]; in_path: boolean; which: string };
  tools: {
    pdftotext: { available: boolean; path: string; install_hint: string };
    pdftoppm: { available: boolean; path: string; install_hint: string };
    tailscale?: { available: boolean; path: string };
    ocr_cmd_configured: boolean;
    asr_cmd_configured: boolean;
    vlm_key_configured: boolean;
  };
  ports: { ui_port: number; ui_listening: boolean; gateway_port: number; gateway_listening: boolean };
  checks: { id: string; label: string; ok: boolean; level: "ok" | "warn" | "error" | "info"; detail: string; fix: string }[];
}

export interface LogInvocation {
  id: number;
  ts: string;
  ts_epoch: number;
  client: string;
  mode: string;
  principal_uid: string;
  vault_id: string;
  tool: string;
  query: string;
  route: string;
  elapsed_ms: number;
  wiki_count: number;
  primary_count: number;
  memory_count: number;
  acl_filtered_count: number;
  missing_concepts: string[];
  hit_sources: string[];
  is_error: boolean;
  error_text: string;
}

export interface LogStatsResponse {
  hours: number;
  total_calls: number;
  stdio_calls?: number;
  http_calls?: number;
  acl_blocked_calls?: number;
  error_count: number;
  error_rate: number;
  zero_hit_count: number;
  p50_ms: number;
  p95_ms: number;
  by_client: { client: string; count: number }[];
  by_vault: { vault_id: string; count: number }[];
  by_tool: { tool: string; count: number }[];
  top_missing_concepts: { term: string; count: number; vaults: string[] }[];
  hourly: {
    hour_offset: number;
    count: number;
    stdio?: number;
    http?: number;
    acl_blocked?: number;
    p50_ms?: number;
  }[];
}

export interface MemoryItem {
  note_id: string;
  kind: "errata" | "insight" | "decision" | "glossary";
  status: "active" | "consolidated" | "archived";
  author: string;
  created_at: string;
  related_sources: string[];
  tags: string[];
  text: string;
  file: string;
}

export interface NetworkDiscoveryResponse {
  self_instance_id: string;
  self_fp: string;
  lan_ips: string[];
  tailscale: {
    available: boolean;
    tool_path: string;
    self_ips: string[];
    peers: { hostname: string; dns_name: string; tailscale_ips: string[]; os: string; online: boolean }[];
    note: string;
  };
  lan_peers: {
    instance_id: string;
    fp: string;
    host: string;
    ip: string;
    gateway_port: number;
    url: string;
    version: string;
    channel: string;
  }[];
  paired_nodes: {
    instance_id: string;
    host: string;
    fp: string;
    urls: string[];
    active_url: string;
    latency_ms: number;
    last_seen_at: string;
  }[];
}
