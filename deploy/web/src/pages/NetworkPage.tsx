import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  Copy,
  Globe,
  KeyRound,
  Link2,
  RefreshCw,
  ScanSearch,
  Server,
  Unlink,
  Wifi,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { BrandGlyph } from "../components/BrandIcons";
import { copyToClipboard } from "../platform/adapter";
import type {
  AclRule,
  ClientInfo,
  LanGatewayStatus,
  LogStatsResponse,
  NetworkDiscoveryResponse,
  VaultInfo,
} from "../types";

interface NetworkPageProps {
  lan: LanGatewayStatus | null;
  vaults: VaultInfo[];
  clients: ClientInfo[];
  acl: Record<string, AclRule>;
  stats: LogStatsResponse | null;
  focusPairSignal?: number;
  onRefresh: () => void;
  onToast: (kind: "success" | "error" | "info", msg: string) => void;
}

export const NetworkPage: React.FC<NetworkPageProps> = ({
  lan,
  vaults,
  clients,
  acl,
  stats,
  focusPairSignal = 0,
  onRefresh,
  onToast,
}) => {
  const [discovery, setDiscovery] = useState<NetworkDiscoveryResponse | null>(null);
  const [scanning, setScanning] = useState(false);

  // OTP Pairing generator (Provider side)
  const localVaults = vaults.filter((v) => v.kind === "local");
  const [pairVaults, setPairVaults] = useState<string[]>([]);
  const [pairGroups, setPairGroups] = useState<string[]>(["reader"]);
  const [generatedCode, setGeneratedCode] = useState<{
    code: string;
    expires_at: number;
    urls: string[];
  } | null>(null);
  const [remainSec, setRemainSec] = useState<number>(0);
  const [copiedCode, setCopiedCode] = useState(false);

  // OTP Pairing consumer (Client side)
  const [inputCode, setInputCode] = useState("");
  const [pairingBusy, setPairingBusy] = useState(false);

  // Manual fallback
  const [showManual, setShowManual] = useState(false);
  const [manualUrl, setManualUrl] = useState("");
  const [manualToken, setManualToken] = useState("");

  // AI Clients Auto-Discovery & Sync state
  const [selectedClients, setSelectedClients] = useState<string[]>([]);
  const [selectedSyncVaults, setSelectedSyncVaults] = useState<string[]>([]);
  const [syncMode, setSyncMode] = useState<"auto" | "stdio" | "http" | "bridge">("auto");
  const [showUnconnectedInstalled, setShowUnconnectedInstalled] = useState(true);
  const [showNotInstalled, setShowNotInstalled] = useState(false);
  const [scanningAgents, setScanningAgents] = useState(false);
  const [busyClientId, setBusyClientId] = useState<string | null>(null);
  const [previewSnippet, setPreviewSnippet] = useState<{
    cid: string;
    name: string;
    configPath: string;
    text: string;
  } | null>(null);
  const [copiedSnippet, setCopiedSnippet] = useState(false);
  const [syncingClients, setSyncingClients] = useState(false);
  const [showToolDetail, setShowToolDetail] = useState(false);
  const didInitSelection = useRef(false);

  const connectedClients = useMemo(
    () =>
      clients
        .filter((c) => Object.keys(c.configured_servers || {}).length > 0)
        .sort((a, b) => {
          const aRank = a.sync_status === "synced" ? 0 : 1;
          const bRank = b.sync_status === "synced" ? 0 : 1;
          return aRank - bRank;
        }),
    [clients],
  );
  const installedUnconnected = useMemo(
    () =>
      clients.filter(
        (c) => c.installed && Object.keys(c.configured_servers || {}).length === 0,
      ),
    [clients],
  );
  const notInstalledClients = useMemo(
    () =>
      clients.filter(
        (c) => !c.installed && Object.keys(c.configured_servers || {}).length === 0,
      ),
    [clients],
  );

  useEffect(() => {
    if (localVaults.length > 0 && pairVaults.length === 0) {
      setPairVaults(localVaults.map((v) => v.id));
    }
    if (vaults.length > 0 && selectedSyncVaults.length === 0) {
      setSelectedSyncVaults(vaults.map((v) => v.id));
    }
  }, [vaults]);

  useEffect(() => {
    if (clients.length > 0 && !didInitSelection.current) {
      didInitSelection.current = true;
      setSelectedClients(
        connectedClients.length > 0
          ? connectedClients.map((c) => c.id)
          : installedUnconnected.map((c) => c.id),
      );
    }
  }, [clients, connectedClients, installedUnconnected]);

  // Countdown tick for generated OTP pairing code
  useEffect(() => {
    if (!generatedCode) {
      setRemainSec(0);
      return;
    }
    const update = () => {
      const left = Math.max(0, Math.floor(generatedCode.expires_at - Date.now() / 1000));
      setRemainSec(left);
    };
    update();
    const t = setInterval(update, 1000);
    return () => clearInterval(t);
  }, [generatedCode]);

  const scanNetwork = async (force = false) => {
    setScanning(true);
    try {
      const res = await api.networkDiscover(force);
      setDiscovery(res);
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  };

  useEffect(() => {
    void scanNetwork(false);
  }, []);

  useEffect(() => {
    if (focusPairSignal > 0 && localVaults.length > 0 && !generatedCode) {
      void handleGenerateCode();
    }
  }, [focusPairSignal]);

  const toggleGateway = async () => {
    try {
      if (lan?.running) {
        await api.lanGatewayStop();
        onToast("info", "已关闭局域网共享");
      } else {
        await api.lanGatewayStart(lan?.port || 8090);
        onToast("success", "局域网共享已开启（未授权设备无法访问）");
      }
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleGenerateCode = async () => {
    try {
      const res = await api.createPairingCode({
        allowed_vaults: pairVaults.length > 0 ? pairVaults : ["*"],
        groups: pairGroups.length > 0 ? pairGroups : ["reader"],
        ttl_sec: 300,
      });
      setGeneratedCode({ code: res.code, expires_at: res.expires_at, urls: res.urls });
      onToast("success", "已生成 5 分钟有效的一次性配对码，用完即失效");
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleConnectCode = async () => {
    if (!inputCode.trim()) return;
    setPairingBusy(true);
    try {
      const res = await api.connectWithPairingCode({
        code: inputCode.trim(),
        sync_clients: true,
      });
      onToast(
        "success",
        `已连接 ${res.paired_host}（${res.latency_ms}ms），接入 ${res.connected_vaults.length} 个共享知识库并已同步到 AI 工具`,
      );
      setInputCode("");
      onRefresh();
      void scanNetwork(false);
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setPairingBusy(false);
    }
  };

  const handleManualConnect = async () => {
    if (!manualUrl.trim() || !manualToken.trim()) return;
    try {
      await api.connectRemote({
        gateway_url: manualUrl.trim(),
        token: manualToken.trim(),
        sync_clients: true,
      });
      onToast("success", "已连接共享电脑并同步到本机 AI 工具");
      setManualToken("");
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const shortClientName = (c: ClientInfo): string =>
    c.id === "dsh" ? "DSH" : c.name.replace(/\s*\(.*\)$/, "");

  const handlePreviewClient = async (c: ClientInfo) => {
    try {
      const res = await api.previewClient({
        client_id: c.id,
        mode: syncMode,
        vault_ids: selectedSyncVaults,
      });
      setCopiedSnippet(false);
      setPreviewSnippet({
        cid: c.id,
        name: shortClientName(c),
        configPath: res.config_path || c.config_path,
        text: res.snippet,
      });
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleScanAgents = async () => {
    setScanningAgents(true);
    try {
      const res = await api.scanClients();
      const connectedNow = res.clients.filter(
        (c) => Object.keys(c.configured_servers || {}).length > 0,
      ).length;
      const unconnNow = res.clients.filter(
        (c) => c.installed && Object.keys(c.configured_servers || {}).length === 0,
      ).length;
      onToast(
        "success",
        `扫描完成：已连接 ${connectedNow} 款，本机另检测到 ${unconnNow} 款已安装工具`,
      );
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setScanningAgents(false);
    }
  };

  const handleSyncClients = async (targetIds: string[]) => {
    if (targetIds.length === 0) return;
    if (targetIds.length === 1) {
      setBusyClientId(targetIds[0]);
    } else {
      setSyncingClients(true);
    }
    try {
      const res = await api.syncClients({
        client_ids: targetIds,
        mode: syncMode,
        vault_ids: selectedSyncVaults,
        remove_unlisted_wctx: true,
      });
      onToast("success", `已成功连接 ${res.results.length} 个 AI 工具，重启对应工具即可生效`);
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setSyncingClients(false);
      setBusyClientId(null);
    }
  };

  const handleDisconnectClient = async (c: ClientInfo) => {
    setBusyClientId(c.id);
    try {
      await api.disconnectClients([c.id]);
      setSelectedClients((prev) => prev.filter((x) => x !== c.id));
      onToast("info", `已移除「${shortClientName(c)}」中的知识库连接（原有其他配置已保留）`);
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setBusyClientId(null);
    }
  };

  const renderCompactClientRow = (c: ClientInfo, actionLabel: string) => {
    const checked = selectedClients.includes(c.id);
    const isBusy = busyClientId === c.id;
    return (
      <div
        key={c.id}
        title={c.config_path}
        className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs transition ${
          checked
            ? "border-[var(--accent)]/45 bg-[var(--accent-soft)]/50"
            : "border-[var(--border-default)] bg-[var(--bg-subtle)]"
        }`}
      >
        <label className="flex min-w-0 cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={checked}
            onChange={() =>
              setSelectedClients((prev) =>
                checked ? prev.filter((x) => x !== c.id) : [...prev, c.id],
              )
            }
          />
          <BrandGlyph brand={c.id} className="h-3.5 w-3.5" />
          <span className="truncate font-semibold text-[var(--ink-primary)]">
            {shortClientName(c)}
          </span>
          {(c.other_mcp_count || 0) > 0 && (
            <span className="shrink-0 text-[11px] text-[var(--ink-muted)]">
              (保留原有 {c.other_mcp_count} 个)
            </span>
          )}
        </label>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => void handlePreviewClient(c)}
            className="text-[11px] font-medium text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
          >
            查看
          </button>
          <button
            type="button"
            disabled={isBusy}
            onClick={() => void handleSyncClients([c.id])}
            className="rounded-md border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-0.5 text-[11px] font-semibold text-[var(--accent)] hover:bg-[var(--accent-soft)] disabled:opacity-40"
          >
            {isBusy ? "处理中..." : actionLabel}
          </button>
        </div>
      </div>
    );
  };

  const roleChoices = [
    "reader",
    "all",
    "admin",
    ...Object.keys(acl || {}).filter((k) => !["reader", "all", "admin"].includes(k)),
  ];

  const maxClientCalls = Math.max(1, ...(stats?.by_client || []).map((x) => x.count));
  const fmtCountdown = `${String(Math.floor(remainSec / 60)).padStart(2, "0")}:${String(
    remainSec % 60,
  ).padStart(2, "0")}`;

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden lg:flex-row">
      {/* Center Stage */}
      <main className="flex min-w-0 flex-1 flex-col overflow-y-auto bg-[var(--bg-surface)]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-default)] px-5 py-3.5">
          <div>
            <h1 className="text-[15px] font-bold text-[var(--ink-primary)]">
              设备连接与 AI 工具
            </h1>
            <p className="text-xs text-[var(--ink-muted)]">
              一键连接本机已安装的 AI 工具，或通过一次性配对码把知识库共享给其他电脑
            </p>
          </div>
          <button
            type="button"
            onClick={() => void toggleGateway()}
            className={`inline-flex items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-xs font-semibold transition ${
              lan?.running
                ? "border border-rose-300 bg-rose-50 text-rose-800 hover:bg-rose-100 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-300"
                : "bg-emerald-600 text-white hover:bg-emerald-700"
            }`}
          >
            <Wifi className="h-3.5 w-3.5" />
            {lan?.running ? `关闭局域网共享 (:${lan.port})` : "开启局域网共享"}
          </button>
        </div>

        <div className="space-y-6 p-5">
          {/* LAN Gateway Active Banner / Port Conflict */}
          {lan?.port_conflict && (
            <div className="flex items-center gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-2.5 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <span>{lan.port_conflict}</span>
            </div>
          )}

          {lan?.running && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50/70 px-4 py-2.5 text-xs dark:border-emerald-800 dark:bg-emerald-950/35">
              <span className="h-2 w-2 rounded-full bg-emerald-500" />
              <span className="font-semibold text-emerald-950 dark:text-emerald-200">
                局域网共享已开启：
              </span>
              {(lan.lan_urls || []).map((u) => (
                <span
                  key={u}
                  className="rounded-md border border-emerald-200 bg-[var(--bg-surface)] px-2 py-0.5 font-mono text-emerald-900 dark:border-emerald-800 dark:text-emerald-300"
                >
                  {u}
                </span>
              ))}
              {discovery?.tailscale.self_ips.map((ip) => (
                <span
                  key={ip}
                  className="rounded-md border border-[var(--accent)]/30 bg-[var(--accent-soft)] px-2 py-0.5 font-mono text-[var(--accent)]"
                >
                  Tailscale http://{ip}:{lan.port}
                </span>
              ))}
            </div>
          )}

          {/* Section 1 (Primary): Local AI Tools (Connected + Installed) */}
          <section className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-[14px] font-bold text-[var(--ink-primary)]">本机 AI 工具</h2>
                <p className="text-xs text-[var(--ink-muted)]">
                  已连接 {connectedClients.length} 款 · 本机另检测到 {installedUnconnected.length}{" "}
                  款未连接（自动保留原有配置）
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={scanningAgents}
                  onClick={() => void handleScanAgents()}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-subtle)] disabled:opacity-50"
                >
                  <ScanSearch
                    className={`h-3.5 w-3.5 text-[var(--accent)] ${scanningAgents ? "animate-pulse" : ""}`}
                  />
                  {scanningAgents ? "扫描中..." : "重新扫描"}
                </button>
                <select
                  value={syncMode}
                  onChange={(e) =>
                    setSyncMode(e.target.value as "auto" | "stdio" | "http" | "bridge")
                  }
                  className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-primary)]"
                >
                  <option value="auto">自动选择（推荐）</option>
                  <option value="stdio">仅本地直连</option>
                  <option value="http">仅局域网地址</option>
                  <option value="bridge">兼容桥接模式</option>
                </select>
                <button
                  type="button"
                  disabled={syncingClients || selectedClients.length === 0}
                  onClick={() => void handleSyncClients(selectedClients)}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-40"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${syncingClients ? "animate-spin" : ""}`} />
                  一键同步所选 ({selectedClients.length})
                </button>
              </div>
            </div>

            {/* Vault Selection Pills & Quick Select */}
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium text-[var(--ink-secondary)]">选择要接入的知识库：</span>
                {vaults.map((v) => {
                  const on = selectedSyncVaults.includes(v.id);
                  return (
                    <button
                      key={v.id}
                      type="button"
                      onClick={() =>
                        setSelectedSyncVaults((prev) =>
                          on ? prev.filter((x) => x !== v.id) : [...prev, v.id],
                        )
                      }
                      className={`rounded-md border px-2.5 py-0.5 font-semibold ${
                        on
                          ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                          : "border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-secondary)]"
                      }`}
                    >
                      {v.name}
                    </button>
                  );
                })}
              </div>
              <div className="flex items-center gap-2 text-[11px]">
                {connectedClients.length > 0 && (
                  <>
                    <button
                      type="button"
                      onClick={() => setSelectedClients(connectedClients.map((c) => c.id))}
                      className="font-medium text-[var(--accent)] hover:underline"
                    >
                      全选已连接
                    </button>
                    <span className="text-[var(--border-strong)]">|</span>
                  </>
                )}
                <button
                  type="button"
                  onClick={() =>
                    setSelectedClients(
                      clients.filter((c) => c.installed).map((c) => c.id),
                    )
                  }
                  className="font-medium text-[var(--ink-secondary)] hover:text-[var(--ink-primary)] hover:underline"
                >
                  全选本机已安装
                </button>
                <span className="text-[var(--border-strong)]">|</span>
                <button
                  type="button"
                  onClick={() => setSelectedClients([])}
                  className="font-medium text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
                >
                  清空
                </button>
              </div>
            </div>

            {/* Group 1: Connected Clients */}
            {connectedClients.length > 0 && (
              <div className="space-y-2">
                <div className="text-xs font-semibold text-[var(--ink-secondary)]">
                  已连接 ({connectedClients.length})
                </div>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                  {connectedClients.map((c) => {
                    const checked = selectedClients.includes(c.id);
                    const hasIssue = c.command_ok === false && (c.command_issues || []).length > 0;
                    const isBusy = busyClientId === c.id;
                    return (
                      <div
                        key={c.id}
                        className={`flex flex-col justify-between rounded-xl border p-3.5 text-xs transition ${
                          checked
                            ? "border-[var(--accent)]/45 bg-[var(--accent-soft)]/50"
                            : "border-[var(--border-default)] bg-[var(--bg-subtle)]"
                        }`}
                      >
                        <div>
                          <div className="flex items-start justify-between gap-2">
                            <label className="flex min-w-0 cursor-pointer items-center gap-2">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() =>
                                  setSelectedClients((prev) =>
                                    checked ? prev.filter((x) => x !== c.id) : [...prev, c.id],
                                  )
                                }
                              />
                              <BrandGlyph brand={c.id} className="h-4 w-4" />
                              <span className="truncate text-[13px] font-bold text-[var(--ink-primary)]">
                                {shortClientName(c)}
                              </span>
                            </label>
                            <span
                              className={`shrink-0 whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-semibold ${
                                c.sync_status === "synced"
                                  ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                                  : "bg-amber-50 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                              }`}
                            >
                              {c.sync_status === "synced"
                                ? `已连接 ${c.matched_count}/${c.total_vaults}`
                                : `待更新 ${c.matched_count}/${c.total_vaults}`}
                            </span>
                          </div>

                          <div
                            className="mt-1.5 truncate font-mono text-[11px] text-[var(--ink-muted)]"
                            title={c.config_path}
                          >
                            {c.config_path}
                          </div>

                          {(c.other_mcp_count || 0) > 0 && (
                            <div className="mt-1 text-[11px] text-[var(--ink-secondary)]">
                              已保留原有 {c.other_mcp_count} 个工具
                            </div>
                          )}

                          {hasIssue && (
                            <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-2 text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                              <div className="font-semibold">检测到程序路径变更：</div>
                              <div className="truncate font-mono">{c.command_issues?.join("; ")}</div>
                            </div>
                          )}
                        </div>

                        <div className="mt-3 flex items-center justify-between gap-2 border-t border-[var(--border-subtle)] pt-2">
                          <div className="flex items-center gap-2.5">
                            <button
                              type="button"
                              onClick={() => void handlePreviewClient(c)}
                              className="inline-flex items-center gap-1 font-medium text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                            >
                              <Code2 className="h-3.5 w-3.5" /> 查看配置
                            </button>
                            <button
                              type="button"
                              disabled={isBusy}
                              onClick={() => void handleDisconnectClient(c)}
                              className="inline-flex items-center gap-1 font-medium text-rose-600 hover:text-rose-700 disabled:opacity-40 dark:text-rose-400"
                              title="仅移除 wiki-context 配置，保留其他工具"
                            >
                              <Unlink className="h-3 w-3" /> 断开
                            </button>
                          </div>
                          <button
                            type="button"
                            disabled={isBusy}
                            onClick={() => void handleSyncClients([c.id])}
                            className="inline-flex items-center gap-1 rounded-md border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1 font-semibold text-[var(--accent)] hover:bg-[var(--accent-soft)] disabled:opacity-40"
                          >
                            <Check className="h-3 w-3" />
                            {isBusy ? "处理中..." : hasIssue ? "一键修复" : "更新连接"}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Group 2: Installed on This Machine (Unconnected, Compact & Collapsible) */}
            {installedUnconnected.length > 0 && (
              <div className="space-y-2">
                <button
                  type="button"
                  onClick={() => setShowUnconnectedInstalled((v) => !v)}
                  className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                >
                  {showUnconnectedInstalled ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                  )}
                  <span>本机已安装（未连接） ({installedUnconnected.length})</span>
                  {!showUnconnectedInstalled && (
                    <span className="truncate font-normal text-[var(--ink-muted)]">
                      · {installedUnconnected.slice(0, 4).map(shortClientName).join("、")}
                      {installedUnconnected.length > 4 ? " 等" : ""}
                    </span>
                  )}
                </button>
                {showUnconnectedInstalled && (
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {installedUnconnected.map((c) => renderCompactClientRow(c, "连接"))}
                  </div>
                )}
              </div>
            )}

            {connectedClients.length === 0 && installedUnconnected.length === 0 && (
              <div className="rounded-xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-subtle)] px-4 py-5 text-center text-xs text-[var(--ink-secondary)]">
                暂未在本机检测到已安装的 AI 工具，可点击右上角「重新扫描」或展开下方兼容列表。
              </div>
            )}

            {/* Group 3: Other Supported Tools Not Installed (Collapsed by Default) */}
            {notInstalledClients.length > 0 && (
              <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)]/60">
                <button
                  type="button"
                  onClick={() => setShowNotInstalled((v) => !v)}
                  className="flex w-full items-center justify-between gap-2 px-3.5 py-2 text-left text-xs text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    {showNotInstalled ? (
                      <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5 shrink-0" />
                    )}
                    <span className="font-medium text-[var(--ink-secondary)]">
                      其他未安装的兼容工具 ({notInstalledClients.length})
                    </span>
                    {!showNotInstalled && (
                      <span className="truncate text-[11px] text-[var(--ink-muted)]">
                        {notInstalledClients.map(shortClientName).join("、")}
                      </span>
                    )}
                  </span>
                  <span className="shrink-0 text-[11px]">
                    {showNotInstalled ? "收起" : "展开"}
                  </span>
                </button>
                {showNotInstalled && (
                  <div className="grid grid-cols-1 gap-2 border-t border-[var(--border-subtle)] p-3 md:grid-cols-2 xl:grid-cols-3">
                    {notInstalledClients.map((c) => renderCompactClientRow(c, "写入配置"))}
                  </div>
                )}
              </div>
            )}

            {/* Config Snippet Preview Drawer */}
            {previewSnippet && (
              <div className="overflow-hidden rounded-xl border border-[var(--border-strong)] bg-[var(--bg-subtle)] text-[var(--ink-primary)]">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-default)] px-4 py-2 text-xs">
                  <div className="min-w-0">
                    <span className="font-semibold text-[var(--accent)]">
                      配置预览 · {previewSnippet.name}
                    </span>
                    <span className="ml-2 truncate font-mono text-[11px] text-[var(--ink-muted)]">
                      ({previewSnippet.configPath})
                    </span>
                  </div>
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => {
                        void copyToClipboard(previewSnippet.text).then((ok) => {
                          if (ok) {
                            setCopiedSnippet(true);
                            onToast("info", "配置内容已复制");
                            setTimeout(() => setCopiedSnippet(false), 1800);
                          }
                        });
                      }}
                      className="inline-flex items-center gap-1 font-semibold text-[var(--accent)] hover:underline"
                    >
                      {copiedSnippet ? (
                        <Check className="h-3.5 w-3.5 text-emerald-500" />
                      ) : (
                        <Copy className="h-3.5 w-3.5" />
                      )}
                      {copiedSnippet ? "已复制" : "复制配置"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setPreviewSnippet(null)}
                      className="text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <pre className="max-h-60 overflow-auto p-3.5 font-mono text-xs text-[var(--ink-secondary)]">
                  {previewSnippet.text}
                </pre>
              </div>
            )}
          </section>

          {/* Section 2 (Secondary): Multi-Device Sharing & Pairing */}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            {/* Provider Card */}
            <section className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-4 text-xs">
              <div className="flex items-center gap-2 text-[13px] font-bold text-[var(--ink-primary)]">
                <KeyRound className="h-4 w-4 text-[var(--accent)]" />
                共享给另一台电脑
              </div>
              <p className="mt-1 text-[var(--ink-muted)]">
                生成 5 分钟有效的一次性配对码，用完即失效，无需手动填写 IP 地址。
              </p>

              <div className="mt-3 space-y-2.5">
                <div>
                  <div className="mb-1 font-medium text-[var(--ink-secondary)]">
                    选择共享的知识库
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {localVaults.map((v) => {
                      const on = pairVaults.includes(v.id);
                      return (
                        <button
                          key={v.id}
                          type="button"
                          onClick={() =>
                            setPairVaults((prev) =>
                              on ? prev.filter((x) => x !== v.id) : [...prev, v.id],
                            )
                          }
                          className={`rounded-md border px-2.5 py-1 font-semibold ${
                            on
                              ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                              : "border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-secondary)]"
                          }`}
                        >
                          {v.name}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div>
                  <div className="mb-1 font-medium text-[var(--ink-secondary)]">对方权限角色</div>
                  <div className="flex flex-wrap gap-1.5">
                    {roleChoices.map((g) => {
                      const on = pairGroups.includes(g);
                      return (
                        <button
                          key={g}
                          type="button"
                          onClick={() =>
                            setPairGroups((prev) =>
                              on ? prev.filter((x) => x !== g) : [...prev, g],
                            )
                          }
                          className={`rounded-md border px-2.5 py-1 font-mono font-semibold ${
                            on
                              ? "border-emerald-400 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300"
                              : "border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-secondary)]"
                          }`}
                        >
                          {g}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={() => void handleGenerateCode()}
                  className="w-full rounded-lg bg-[var(--accent)] py-2 font-semibold text-white hover:bg-[var(--accent-hover)]"
                >
                  生成一次性配对码
                </button>

                {/* OTP Ticket Readout */}
                {generatedCode && (
                  <div className="overflow-hidden rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-3 text-[var(--ink-primary)]">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-mono font-semibold text-[var(--chart-amber)]">
                        {remainSec > 0 ? `${fmtCountdown} 后过期` : "已过期"}
                      </span>
                      <button
                        type="button"
                        onClick={() => {
                          void copyToClipboard(generatedCode.code).then((ok) => {
                            if (ok) {
                              setCopiedCode(true);
                              setTimeout(() => setCopiedCode(false), 1800);
                            }
                          });
                        }}
                        className="inline-flex items-center gap-1 font-semibold text-[var(--accent)] hover:underline"
                      >
                        {copiedCode ? (
                          <Check className="h-3.5 w-3.5 text-emerald-500" />
                        ) : (
                          <Copy className="h-3.5 w-3.5" />
                        )}
                        {copiedCode ? "已复制" : "复制配对码"}
                      </button>
                    </div>
                    <div className="mt-1.5 break-all rounded-md border border-[var(--border-default)] bg-[var(--bg-subtle)] p-2 font-mono text-xs text-emerald-700 dark:text-emerald-300">
                      {generatedCode.code}
                    </div>
                  </div>
                )}
              </div>
            </section>

            {/* Consumer Card */}
            <section className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-4 text-xs">
              <div className="flex items-center gap-2 text-[13px] font-bold text-[var(--ink-primary)]">
                <Link2 className="h-4 w-4 text-[var(--accent)]" />
                连接其他电脑的知识库
              </div>
              <p className="mt-1 text-[var(--ink-muted)]">
                粘贴对方发来的配对码，自动完成连接并同步到本机 AI 工具。
              </p>

              <textarea
                rows={4}
                value={inputCode}
                onChange={(e) => setInputCode(e.target.value)}
                placeholder="在此粘贴对方发来的配对码..."
                className="mt-3 w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-2.5 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
              />

              <button
                type="button"
                disabled={pairingBusy || !inputCode.trim()}
                onClick={() => void handleConnectCode()}
                className="mt-2.5 w-full rounded-lg bg-[var(--accent)] py-2 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-40"
              >
                {pairingBusy ? "正在连接..." : "立即连接"}
              </button>

              <button
                type="button"
                onClick={() => setShowManual((s) => !s)}
                className="mt-2.5 font-medium text-[var(--ink-secondary)] underline hover:text-[var(--ink-primary)]"
              >
                {showManual ? "收起手动填写" : "手动填写地址和密钥"}
              </button>

              {showManual && (
                <div className="mt-2.5 space-y-2 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] p-3">
                  <input
                    value={manualUrl}
                    onChange={(e) => setManualUrl(e.target.value)}
                    placeholder="http://192.168.1.25:8090"
                    className="w-full rounded-md border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2.5 py-1.5 font-mono text-xs text-[var(--ink-primary)]"
                  />
                  <input
                    type="password"
                    value={manualToken}
                    onChange={(e) => setManualToken(e.target.value)}
                    placeholder="wctx_..."
                    className="w-full rounded-md border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2.5 py-1.5 font-mono text-xs text-[var(--ink-primary)]"
                  />
                  <button
                    type="button"
                    onClick={() => void handleManualConnect()}
                    className="rounded-md bg-[var(--ink-primary)] px-3 py-1.5 font-semibold text-[var(--bg-surface)] hover:opacity-90"
                  >
                    连接
                  </button>
                </div>
              )}
            </section>
          </div>
        </div>
      </main>

      {/* Right Inspector: Usage Stats + Nearby Devices */}
      <aside className="flex w-full shrink-0 flex-col border-t border-[var(--border-default)] bg-[var(--bg-sidebar)] lg:w-[356px] lg:border-t-0 lg:border-l">
        <div className="border-b border-[var(--border-default)] px-4 py-3">
          <div className="text-xs font-semibold text-[var(--ink-muted)]">使用统计与附近设备</div>
          <div className="mt-1 text-[14px] font-bold text-[var(--ink-primary)]">
            查询活跃度与局域网发现
          </div>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
          {/* Unified 24h Client Call Telemetry */}
          <div className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-primary)]">
            <div className="flex items-center justify-between border-b border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-3.5 py-2.5">
              <span className="font-semibold text-[var(--ink-primary)]">
                近 24 小时各工具查询次数
              </span>
              <span className="font-mono font-semibold text-[var(--accent)]">
                {stats?.total_calls ?? 0} 次
              </span>
            </div>

            <div className="divide-y divide-[var(--border-subtle)]">
              {(stats?.by_client || []).length === 0 ? (
                <div className="px-3.5 py-5 text-center text-[var(--ink-muted)]">
                  近 24 小时暂无工具查询记录
                </div>
              ) : (
                (stats?.by_client || []).slice(0, 6).map((row) => {
                  const pct = Math.round((row.count / maxClientCalls) * 100);
                  return (
                    <div key={row.client} className="px-3.5 py-2.5">
                      <div className="flex items-center justify-between">
                        <span className="flex items-center gap-2 font-medium text-[var(--ink-primary)]">
                          <BrandGlyph brand={row.client} className="h-3.5 w-3.5" />
                          <span>{row.client}</span>
                        </span>
                        <span className="font-disp font-bold text-[var(--accent)]">
                          {row.count}
                        </span>
                      </div>
                      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-[var(--bg-muted)]">
                        <div
                          style={{ width: `${Math.max(4, pct)}%` }}
                          className="h-full rounded-full bg-[var(--chart-stdio)]"
                        />
                      </div>
                    </div>
                  );
                })
              )}
            </div>

            {(stats?.by_tool || []).length > 0 && (
              <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-3.5 py-2">
                <button
                  type="button"
                  onClick={() => setShowToolDetail((v) => !v)}
                  className="flex w-full items-center justify-between text-left font-medium text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
                >
                  <span>查看接口明细</span>
                  {showToolDetail ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                  )}
                </button>
                {showToolDetail && (
                  <div className="mt-2 flex flex-wrap gap-1.5 font-mono">
                    {(stats?.by_tool || []).slice(0, 6).map((t) => (
                      <span
                        key={t.tool}
                        className="rounded border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-0.5 text-[var(--ink-secondary)]"
                      >
                        {t.tool} <strong className="text-[var(--accent)]">{t.count}</strong>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Nearby Devices */}
          <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-3.5">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-1.5 font-bold text-[var(--ink-primary)]">
                <Globe className="h-3.5 w-3.5 text-[var(--accent)]" /> 附近发现的电脑
              </span>
              <button
                type="button"
                disabled={scanning}
                onClick={() => void scanNetwork(true)}
                className="inline-flex items-center gap-1 rounded-md border border-[var(--border-default)] bg-[var(--bg-subtle)] px-2 py-1 font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
              >
                <RefreshCw className={`h-3 w-3 ${scanning ? "animate-spin text-[var(--accent)]" : ""}`} />
                刷新
              </button>
            </div>

            <p className="mt-1 text-[var(--ink-muted)]">
              {discovery?.tailscale.available
                ? `Tailscale 已连接 (${discovery.tailscale.self_ips.join(", ")})`
                : "正在搜索同一局域网内的设备"}
            </p>

            <div className="mt-3 space-y-2">
              {(discovery?.lan_peers || []).map((p) => (
                <div
                  key={p.instance_id}
                  className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-2.5 dark:border-emerald-800 dark:bg-emerald-950/35"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-emerald-950 dark:text-emerald-200">
                      {p.host}
                    </span>
                    <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-900 dark:bg-emerald-900/60 dark:text-emerald-300">
                      局域网
                    </span>
                  </div>
                  <div className="mt-0.5 font-mono text-emerald-800 dark:text-emerald-400">
                    {p.url}
                  </div>
                </div>
              ))}

              {(discovery?.tailscale.peers || []).slice(0, 6).map((tp) => (
                <div
                  key={tp.hostname}
                  className="rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)] p-2.5"
                >
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 font-semibold text-[var(--ink-primary)]">
                      <Server className="h-3.5 w-3.5 text-[var(--accent)]" /> {tp.hostname}
                    </span>
                    <span
                      className={`rounded px-1.5 py-0.5 font-medium ${
                        tp.online
                          ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                          : "bg-[var(--bg-muted)] text-[var(--ink-muted)]"
                      }`}
                    >
                      {tp.online ? "在线" : "离线"}
                    </span>
                  </div>
                  <div className="mt-0.5 font-mono text-[var(--ink-secondary)]">
                    {tp.tailscale_ips[0]}
                  </div>
                </div>
              ))}

              {(discovery?.lan_peers || []).length === 0 &&
                (discovery?.tailscale?.peers || []).length === 0 && (
                  <div className="rounded-lg border border-dashed border-[var(--border-strong)] p-4 text-center text-[var(--ink-muted)]">
                    暂未发现同一局域网内的其他设备，可直接使用左侧配对码连接。
                  </div>
                )}
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
};
