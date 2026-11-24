import React, { useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Code2,
  Copy,
  Folder,
  FolderTree,
  Key,
  Plus,
  RotateCw,
  Send,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { copyToClipboard } from "../platform/adapter";
import type { AclRule, LanGatewayStatus, TokenRecord, VaultInfo } from "../types";

type McpClientFormat = "cursor" | "claude" | "vscode" | "workbuddy" | "codex" | "raw";

interface ShareConfigState {
  token: string;
  isPlaceholder: boolean;
  uid: string;
  label: string;
  allowedVaults: string[];
}

const CLIENT_FORMAT_OPTIONS: { id: McpClientFormat; label: string; targetHint: string }[] = [
  {
    id: "cursor",
    label: "Cursor / 通用",
    targetHint: "~/.cursor/mcp.json（亦兼容 Windsurf / Trae / Kiro / Cline / Kimi / LM Studio / Antigravity）",
  },
  { id: "claude", label: "Claude Code", targetHint: "~/.claude.json（或直接在终端执行上方命令）" },
  { id: "vscode", label: "VS Code", targetHint: "项目 .vscode/mcp.json 或 VS Code 用户 mcp.json" },
  { id: "workbuddy", label: "WorkBuddy", targetHint: "~/.workbuddy/mcp.json" },
  { id: "codex", label: "Codex CLI", targetHint: "~/.codex/config.toml" },
  { id: "raw", label: "URL + 密钥", targetHint: "在支持 Streamable HTTP 的工具中分别填入地址与请求头" },
];

const SHARE_BASE_URL_STORAGE_KEY = "wctx-share-base-url";
const DEFAULT_FALLBACK_URL = "http://服务器IP:8090";

function pickDefaultShareBaseUrl(lan: LanGatewayStatus | null, gatewayBaseUrl?: string): string {
  const lanNonLoopback = (lan?.lan_urls || []).find(
    (u) => u && !u.includes("127.0.0.1") && !u.includes("localhost"),
  );
  const raw =
    lanNonLoopback ||
    lan?.recommended_url ||
    (gatewayBaseUrl && !gatewayBaseUrl.includes("127.0.0.1") ? gatewayBaseUrl : "") ||
    DEFAULT_FALLBACK_URL;
  return raw.replace(/\/+$/, "");
}

function buildTeammateMcpSnippet(
  format: McpClientFormat,
  baseUrl: string,
  token: string,
  targetVaults: { id: string; name: string }[],
): { snippet: string; cliCmd: string } {
  const effBase = (baseUrl.trim() || DEFAULT_FALLBACK_URL).replace(/\/+$/, "");
  const effToken = token || "wctx_请替换为你的密钥";
  const vaults = targetVaults.length > 0 ? targetVaults : [{ id: "wiki", name: "知识库" }];

  const cliCmd = vaults
    .map(
      (v) =>
        `claude mcp add --transport http ${v.id} ${effBase}/mcp/${v.id} --header "Authorization: Bearer ${effToken}"`,
    )
    .join("\n");

  if (format === "vscode") {
    const servers: Record<string, Record<string, unknown>> = {};
    for (const v of vaults) {
      servers[v.id] = {
        type: "http",
        url: `${effBase}/mcp/${v.id}`,
        headers: { Authorization: `Bearer ${effToken}` },
      };
    }
    return {
      snippet: JSON.stringify({ servers }, null, 2),
      cliCmd: "",
    };
  }

  if (format === "cursor" || format === "claude" || format === "workbuddy") {
    const mcpServers: Record<string, Record<string, unknown>> = {};
    for (const v of vaults) {
      const entry: Record<string, unknown> = {};
      if (format === "claude") entry.type = "http";
      if (format === "workbuddy") entry.type = "streamable-http";
      entry.url = `${effBase}/mcp/${v.id}`;
      entry.headers = { Authorization: `Bearer ${effToken}` };
      mcpServers[v.id] = entry;
    }
    return {
      snippet: JSON.stringify({ mcpServers }, null, 2),
      cliCmd: format === "claude" ? cliCmd : "",
    };
  }

  if (format === "codex") {
    const blocks = vaults.map((v) => {
      const hdr = /^[A-Za-z0-9_-]+$/.test(v.id)
        ? `[mcp_servers.${v.id}]`
        : `[mcp_servers."${v.id}"]`;
      return `${hdr}\nurl = "${effBase}/mcp/${v.id}"\nhttp_headers = { Authorization = "Bearer ${effToken}" }`;
    });
    return { snippet: blocks.join("\n\n"), cliCmd: "" };
  }

  const lines = vaults.map((v) => `知识库「${v.name}」地址：${effBase}/mcp/${v.id}`);
  lines.push(`协议类型：Streamable HTTP`);
  lines.push(`请求头 (Header)：Authorization: Bearer ${effToken}`);
  return { snippet: lines.join("\n"), cliCmd: "" };
}

function buildTeammateMessage(
  format: McpClientFormat,
  snippet: string,
  cliCmd: string,
  targetHint: string,
): string {
  const lines = [
    "这是团队知识库的 MCP 连接配置（电脑上无需安装任何软件，直接粘入 AI 工具即可）：",
    `1. 填写/粘贴位置：${targetHint}`,
  ];
  if (format === "claude" && cliCmd) {
    lines.push("2. 终端一键命令：\n" + cliCmd);
    lines.push("3. 或将下面内容合并进配置文件：\n" + snippet);
  } else {
    lines.push("2. 配置内容：\n" + snippet);
  }
  lines.push("保存后重启或刷新 AI 工具的 MCP 列表即可使用。");
  return lines.join("\n");
}

interface AccessPageProps {
  vaults: VaultInfo[];
  tokens: TokenRecord[];
  acl: Record<string, AclRule>;
  lan: LanGatewayStatus | null;
  gatewayBaseUrl?: string;
  onRefresh: () => void;
  onToast: (kind: "success" | "error" | "info", msg: string) => void;
}

export const AccessPage: React.FC<AccessPageProps> = ({
  vaults,
  tokens,
  acl,
  lan,
  gatewayBaseUrl,
  onRefresh,
  onToast,
}) => {
  // Token issuer form
  const [uid, setUid] = useState("");
  const [label, setLabel] = useState("");
  const [selectedGroups, setSelectedGroups] = useState<string[]>(["reader"]);
  const [selectedVaults, setSelectedVaults] = useState<string[]>(["*"]);
  const [shareConfig, setShareConfig] = useState<ShareConfigState | null>(null);
  const [shareFormat, setShareFormat] = useState<McpClientFormat>("cursor");
  const [shareBaseUrl, setShareBaseUrl] = useState<string>(() => {
    try {
      const saved = localStorage.getItem(SHARE_BASE_URL_STORAGE_KEY);
      if (saved && saved.trim()) return saved.trim().replace(/\/+$/, "");
    } catch {
      // ignore storage errors
    }
    return pickDefaultShareBaseUrl(lan, gatewayBaseUrl);
  });
  const [copiedKind, setCopiedKind] = useState<"" | "token" | "snippet" | "message" | "cli">("");

  // Visual Role ACL editor state
  const [localAcl, setLocalAcl] = useState<Record<string, AclRule>>(acl || {});
  const [activeRole, setActiveRole] = useState<string>("reader");
  const [newRoleName, setNewRoleName] = useState("");

  // Collapsible Folder Tree state — collapsed by default per user requirement
  const [expandedVaults, setExpandedVaults] = useState<Record<string, boolean>>({});
  const [expandedFolders, setExpandedFolders] = useState<Record<string, boolean>>({});

  // ACL Simulator
  const [simTokenId, setSimTokenId] = useState("");
  const [simVaultId, setSimVaultId] = useState("");
  const [simResult, setSimResult] = useState<{
    vault_allowed: boolean;
    reason: string;
    allowed_sources: string[];
    denied_sources: string[];
    denied_wiki: string[];
  } | null>(null);

  const localVaults = vaults.filter((v) => v.kind === "local");

  useEffect(() => {
    let hasSaved = false;
    try {
      hasSaved = Boolean(localStorage.getItem(SHARE_BASE_URL_STORAGE_KEY)?.trim());
    } catch {
      hasSaved = false;
    }
    if (!hasSaved && shareBaseUrl === DEFAULT_FALLBACK_URL) {
      const nextUrl = pickDefaultShareBaseUrl(lan, gatewayBaseUrl);
      if (nextUrl !== DEFAULT_FALLBACK_URL) {
        setShareBaseUrl(nextUrl);
      }
    }
  }, [lan, gatewayBaseUrl, shareBaseUrl]);

  const handleShareBaseUrlChange = (val: string) => {
    setShareBaseUrl(val);
    try {
      const trimmed = val.trim();
      if (trimmed) {
        localStorage.setItem(SHARE_BASE_URL_STORAGE_KEY, trimmed);
      } else {
        localStorage.removeItem(SHARE_BASE_URL_STORAGE_KEY);
      }
    } catch {
      // ignore storage errors
    }
  };

  const triggerCopied = (kind: "token" | "snippet" | "message" | "cli", msg: string) => {
    setCopiedKind(kind);
    onToast("info", msg);
    setTimeout(() => {
      setCopiedKind((cur) => (cur === kind ? "" : cur));
    }, 1800);
  };

  useEffect(() => {
    setLocalAcl(acl || {});
    const keys = Object.keys(acl || {});
    if (keys.length > 0 && !keys.includes(activeRole)) {
      setActiveRole(keys[0]);
    }
  }, [acl]);

  useEffect(() => {
    const tid = simTokenId || tokens[0]?.id || "";
    const vid = simVaultId || localVaults[0]?.id || "";
    if (!simTokenId && tid) setSimTokenId(tid);
    if (!simVaultId && vid) setSimVaultId(vid);
    if (tid && vid && !simResult) {
      void api
        .simulateAcl(tid, vid)
        .then((res) =>
          setSimResult({
            vault_allowed: res.vault_allowed,
            reason: res.reason,
            allowed_sources: res.allowed_sources || [],
            denied_sources: res.denied_sources || [],
            denied_wiki: res.denied_wiki || [],
          }),
        )
        .catch(() => {});
    }
  }, [tokens, localVaults.length]);

  const roleList = Object.keys(localAcl);
  const selectableGroups = Array.from(new Set(["reader", "admin", "all", ...roleList]));

  const handleCreateToken = async () => {
    if (!uid.trim()) return;
    try {
      const reqUid = uid.trim();
      const reqLabel = label.trim() || reqUid;
      const reqVaults = selectedVaults.length > 0 ? selectedVaults : ["*"];
      const res = await api.createToken({
        uid: reqUid,
        label: reqLabel,
        groups: selectedGroups.length > 0 ? selectedGroups : ["reader"],
        allowed_vaults: reqVaults,
      });
      setShareConfig({
        token: res.token,
        isPlaceholder: false,
        uid: res.record?.uid || reqUid,
        label: res.record?.label || reqLabel,
        allowedVaults: res.record?.allowed_vaults || reqVaults,
      });
      setUid("");
      setLabel("");
      onToast("success", "访问密钥已生成，可直接复制配置发给同事");
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleRotate = async (id: string) => {
    try {
      const existing = tokens.find((t) => t.id === id);
      const res = await api.rotateToken(id);
      setShareConfig({
        token: res.token,
        isPlaceholder: false,
        uid: res.record?.uid || existing?.uid || "",
        label: res.record?.label || existing?.label || existing?.uid || "",
        allowedVaults: res.record?.allowed_vaults || existing?.allowed_vaults || ["*"],
      });
      onToast("success", "密钥已重置，可直接复制新配置发给同事");
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const shareData = useMemo(() => {
    if (!shareConfig) return null;
    const allowAll = shareConfig.allowedVaults.includes("*");
    const matched = allowAll
      ? localVaults
      : localVaults.filter((v) => shareConfig.allowedVaults.includes(v.id));
    const targetVaults = (
      matched.length > 0
        ? matched
        : shareConfig.allowedVaults
            .filter((x) => x !== "*")
            .map((id) => ({ id, name: id }))
    ).map((v) => ({ id: v.id, name: v.name || v.id }));
    const { snippet, cliCmd } = buildTeammateMcpSnippet(
      shareFormat,
      shareBaseUrl,
      shareConfig.token,
      targetVaults,
    );
    const fmtMeta =
      CLIENT_FORMAT_OPTIONS.find((f) => f.id === shareFormat) || CLIENT_FORMAT_OPTIONS[0];
    const fullMessage = buildTeammateMessage(shareFormat, snippet, cliCmd, fmtMeta.targetHint);
    return { snippet, cliCmd, targetHint: fmtMeta.targetHint, fullMessage };
  }, [shareConfig, shareFormat, shareBaseUrl, localVaults]);

  const toggleRulePrefix = (kind: "allow" | "deny", prefix: string) => {
    setLocalAcl((prev) => {
      const cur = prev[activeRole] || { allow: [], deny: [] };
      const list = cur[kind] || [];
      const nextList = list.includes(prefix)
        ? list.filter((p) => p !== prefix)
        : [...list, prefix];
      return {
        ...prev,
        [activeRole]: {
          ...cur,
          [kind]: nextList,
        },
      };
    });
  };

  const handleSaveAcl = async () => {
    try {
      await api.saveAcl(localAcl);
      onToast("success", "权限设置已保存并立即生效");
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleSimulate = async () => {
    if (!simTokenId || !simVaultId) return;
    try {
      const res = await api.simulateAcl(simTokenId, simVaultId);
      setSimResult({
        vault_allowed: res.vault_allowed,
        reason: res.reason,
        allowed_sources: res.allowed_sources || [],
        denied_sources: res.denied_sources || [],
        denied_wiki: res.denied_wiki || [],
      });
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const curRule = localAcl[activeRole] || { allow: [], deny: [] };
  const simTotal = simResult
    ? Math.max(
        1,
        simResult.allowed_sources.length +
          simResult.denied_sources.length +
          simResult.denied_wiki.length,
      )
    : 1;

  const anyVaultExpanded = localVaults.some((v) => Boolean(expandedVaults[v.id]));

  const toggleAllTrees = () => {
    if (anyVaultExpanded) {
      setExpandedVaults({});
      setExpandedFolders({});
    } else {
      const nextV: Record<string, boolean> = {};
      for (const v of localVaults) {
        nextV[v.id] = true;
      }
      setExpandedVaults(nextV);
    }
  };

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden lg:flex-row">
      {/* Center Stage: Collapsible Folder Access Tree + Authorized Devices Table */}
      <main className="flex min-w-0 flex-1 flex-col overflow-y-auto bg-[var(--bg-surface)]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-default)] px-5 py-3.5">
          <div>
            <h1 className="text-[15px] font-bold text-[var(--ink-primary)]">
              共享权限与授权设备
            </h1>
            <p className="text-xs text-[var(--ink-muted)]">
              按角色设置可访问的文件夹；同时设置允许和禁止时，以「禁止」优先。引用了受限文档的 AI 整理内容也会自动隐藏。
            </p>
          </div>
          <button
            type="button"
            onClick={() => void handleSaveAcl()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)]"
          >
            <Check className="h-3.5 w-3.5" /> 保存权限设置
          </button>
        </div>

        <div className="space-y-6 p-5">
          {/* Teammate Shareable MCP Config Generator */}
          {shareConfig && shareData && (
            <div
              className={`space-y-3 rounded-xl border p-4 text-xs ${
                shareConfig.isPlaceholder
                  ? "border-[var(--accent)]/45 bg-[var(--accent-soft)]/35"
                  : "border-emerald-300 bg-emerald-50/75 dark:border-emerald-700 dark:bg-emerald-950/35"
              }`}
            >
              {/* Header + Raw Token Bar */}
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div
                    className={`font-bold ${
                      shareConfig.isPlaceholder
                        ? "text-[var(--ink-primary)]"
                        : "text-emerald-900 dark:text-emerald-200"
                    }`}
                  >
                    {shareConfig.isPlaceholder
                      ? `「${shareConfig.label}」的 MCP 配置模板（需填入密钥）`
                      : `已为「${shareConfig.label}」生成访问密钥（仅显示这一次）`}
                  </div>
                  {shareConfig.isPlaceholder ? (
                    <p className="mt-0.5 text-[var(--ink-muted)]">
                      出于安全考虑，系统不保留明文密钥；可让同事自行填入，或点击下方对应行的「重置密钥」生成含完整密钥的配置。
                    </p>
                  ) : (
                    <div className="mt-1.5 flex flex-wrap items-center gap-2">
                      <code className="break-all rounded-md border border-emerald-200 bg-[var(--bg-surface)] px-2 py-1 font-mono text-emerald-800 dark:border-emerald-800 dark:text-emerald-300">
                        {shareConfig.token}
                      </code>
                      <button
                        type="button"
                        onClick={() =>
                          void copyToClipboard(shareConfig.token).then(
                            (ok) => ok && triggerCopied("token", "已复制访问密钥"),
                          )
                        }
                        className="inline-flex items-center gap-1 rounded-md border border-emerald-300 bg-[var(--bg-surface)] px-2.5 py-1 font-semibold text-emerald-800 hover:bg-emerald-100 dark:border-emerald-700 dark:text-emerald-300 dark:hover:bg-emerald-900/50"
                      >
                        {copiedKind === "token" ? (
                          <Check className="h-3.5 w-3.5 text-emerald-600" />
                        ) : (
                          <Copy className="h-3.5 w-3.5" />
                        )}
                        {copiedKind === "token" ? "已复制" : "仅复制密钥"}
                      </button>
                    </div>
                  )}
                </div>

                <button
                  type="button"
                  onClick={() => setShareConfig(null)}
                  className="rounded-md p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-surface)] hover:text-[var(--ink-primary)]"
                  title="关闭"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              {/* Teammate Ready-to-Paste MCP Config */}
              <div className="space-y-3 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-3.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="font-bold text-[var(--ink-primary)]">
                      复制给同事的 MCP 配置（同事电脑无需安装任何软件）
                    </div>
                    <div className="text-[var(--ink-muted)]">
                      适用位置：{shareData.targetHint}
                    </div>
                  </div>
                </div>

                {/* Server Base URL Input + Detected LAN Pills */}
                <div className="space-y-1.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <label className="font-medium text-[var(--ink-secondary)]">
                      服务器访问地址（如有公司域名请直接修改，会自动记住）
                    </label>
                    {!lan?.running && (
                      <span className="text-[11px] text-amber-700 dark:text-amber-300">
                        提示：请确认服务器已开启共享或运行 wctx gateway
                      </span>
                    )}
                  </div>
                  <input
                    value={shareBaseUrl}
                    onChange={(e) => handleShareBaseUrlChange(e.target.value)}
                    placeholder="如 https://kb.company.com 或 http://192.168.1.25:8090"
                    className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-1.5 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
                  />
                  {(lan?.lan_urls || []).filter(
                    (u) => !u.includes("127.0.0.1") && !u.includes("localhost"),
                  ).length > 0 && (
                    <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                      <span className="text-[11px] text-[var(--ink-muted)]">本机检测地址：</span>
                      {(lan?.lan_urls || [])
                        .filter((u) => !u.includes("127.0.0.1") && !u.includes("localhost"))
                        .map((u) => {
                          const clean = u.replace(/\/+$/, "");
                          return (
                            <button
                              key={u}
                              type="button"
                              onClick={() => handleShareBaseUrlChange(clean)}
                              className={`rounded border px-2 py-0.5 font-mono text-[11px] ${
                                shareBaseUrl.replace(/\/+$/, "") === clean
                                  ? "border-[var(--accent)] bg-[var(--accent-soft)] font-semibold text-[var(--accent)]"
                                  : "border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)] hover:border-[var(--accent)]"
                              }`}
                            >
                              {clean}
                            </button>
                          );
                        })}
                    </div>
                  )}
                </div>

                {/* Client Format Tabs */}
                <div className="flex flex-wrap items-center gap-1 rounded-lg border border-[var(--border-default)] bg-[var(--bg-muted)] p-0.5">
                  {CLIENT_FORMAT_OPTIONS.map((opt) => {
                    const active = shareFormat === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        onClick={() => setShareFormat(opt.id)}
                        className={`rounded-[6px] px-2.5 py-1 text-xs font-semibold ${
                          active
                            ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs"
                            : "bg-transparent text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                        }`}
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>

                {/* Claude Code One-Line CLI Command */}
                {shareFormat === "claude" && shareData.cliCmd && (
                  <div className="rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)] p-2.5">
                    <div className="mb-1 flex items-center justify-between">
                      <span className="font-semibold text-[var(--ink-secondary)]">
                        终端一键添加命令（直接发给同事运行）
                      </span>
                      <button
                        type="button"
                        onClick={() =>
                          void copyToClipboard(shareData.cliCmd).then(
                            (ok) => ok && triggerCopied("cli", "已复制 Claude Code 添加命令"),
                          )
                        }
                        className="inline-flex items-center gap-1 font-semibold text-[var(--accent)] hover:underline"
                      >
                        {copiedKind === "cli" ? (
                          <Check className="h-3 w-3 text-emerald-500" />
                        ) : (
                          <Copy className="h-3 w-3" />
                        )}
                        {copiedKind === "cli" ? "已复制" : "复制命令"}
                      </button>
                    </div>
                    <pre className="overflow-x-auto font-mono text-[11px] text-[var(--ink-primary)]">
                      {shareData.cliCmd}
                    </pre>
                  </div>
                )}

                {/* Code Preview */}
                <pre className="max-h-56 overflow-auto rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)] p-3 font-mono text-xs text-[var(--ink-primary)]">
                  {shareData.snippet}
                </pre>

                {/* Primary Actions */}
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <button
                    type="button"
                    onClick={() =>
                      void copyToClipboard(shareData.snippet).then(
                        (ok) => ok && triggerCopied("snippet", "已复制配置片段"),
                      )
                    }
                    className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-1.5 font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-muted)]"
                  >
                    {copiedKind === "snippet" ? (
                      <Check className="h-3.5 w-3.5 text-emerald-500" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                    {copiedKind === "snippet" ? "已复制片段" : "仅复制配置片段"}
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      void copyToClipboard(shareData.fullMessage).then(
                        (ok) => ok && triggerCopied("message", "已复制完整配置与使用说明，直接发给同事即可"),
                      )
                    }
                    className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3.5 py-1.5 font-semibold text-white hover:bg-[var(--accent-hover)]"
                  >
                    {copiedKind === "message" ? (
                      <Check className="h-3.5 w-3.5" />
                    ) : (
                      <Send className="h-3.5 w-3.5" />
                    )}
                    {copiedKind === "message" ? "已复制完整说明" : "复制完整说明发给同事"}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Visual Role Selector & Collapsible 2-Level Folder Tree Matrix */}
          <section className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <FolderTree className="mr-0.5 h-4 w-4 text-[var(--accent)]" />
                <span className="mr-1 text-xs font-medium text-[var(--ink-secondary)]">
                  选择角色
                </span>
                {(roleList.length > 0 ? roleList : ["reader"]).map((r) => {
                  const active = activeRole === r;
                  return (
                    <button
                      key={r}
                      type="button"
                      onClick={() => {
                        if (!localAcl[r]) {
                          setLocalAcl((prev) => ({ ...prev, [r]: { allow: ["**"], deny: [] } }));
                        }
                        setActiveRole(r);
                      }}
                      className={`rounded-lg border px-3 py-1 font-mono text-xs font-semibold transition ${
                        active
                          ? "border-[var(--accent)] bg-[var(--accent)] text-white"
                          : "border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--ink-secondary)] hover:border-[var(--accent)]"
                      }`}
                    >
                      {r}
                    </button>
                  );
                })}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <input
                  value={newRoleName}
                  onChange={(e) => setNewRoleName(e.target.value)}
                  placeholder="新角色名（如 外部协作）"
                  className="w-40 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs text-[var(--ink-primary)]"
                />
                <button
                  type="button"
                  onClick={() => {
                    const clean = newRoleName.trim();
                    if (!clean || clean === "admin" || clean === "all") return;
                    setLocalAcl((prev) => ({
                      ...prev,
                      [clean]: prev[clean] || { allow: ["**"], deny: [] },
                    }));
                    setActiveRole(clean);
                    setNewRoleName("");
                  }}
                  className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
                >
                  <Plus className="h-3.5 w-3.5" /> 新建角色
                </button>
              </div>
            </div>

            {/* Active Role Summary Strip + Global Expand/Collapse */}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-[var(--ink-secondary)]">
                  当前角色权限 ({activeRole})：
                </span>
                <span className="font-mono text-emerald-700 dark:text-emerald-400">
                  允许 [{curRule.allow.join(", ") || "无"}]
                </span>
                <span className="text-[var(--ink-muted)]">·</span>
                <span className="font-mono text-rose-700 dark:text-rose-400">
                  禁止 [{curRule.deny.join(", ") || "无"}]
                </span>
              </div>
              {localVaults.length > 0 && (
                <button
                  type="button"
                  onClick={toggleAllTrees}
                  className="inline-flex items-center gap-1 rounded-md border border-[var(--border-default)] bg-[var(--bg-subtle)] px-2.5 py-1 text-xs font-semibold text-[var(--ink-secondary)] hover:border-[var(--accent)] hover:text-[var(--accent)]"
                >
                  {anyVaultExpanded ? (
                    <>
                      <ChevronDown className="h-3.5 w-3.5" /> 收起全部目录
                    </>
                  ) : (
                    <>
                      <ChevronRight className="h-3.5 w-3.5" /> 展开全部目录
                    </>
                  )}
                </button>
              )}
            </div>

            {/* Vault Collapsible Cards (Default Collapsed) */}
            <div className="mt-3 space-y-2.5">
              {localVaults.map((v) => {
                const isVaultOpen = Boolean(expandedVaults[v.id]);
                const treeNodes = v.folder_tree || [];
                const allPrefixes = treeNodes.flatMap((n) => [
                  n.prefix,
                  ...n.children.map((c) => c.prefix),
                ]);
                const allowCount = allPrefixes.filter((p) => curRule.allow.includes(p)).length;
                const denyCount = allPrefixes.filter((p) => curRule.deny.includes(p)).length;

                return (
                  <div
                    key={v.id}
                    className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] text-xs"
                  >
                    {/* Collapsible Vault Header Row */}
                    <div className="flex flex-wrap items-center justify-between gap-2 px-3.5 py-2.5">
                      <button
                        type="button"
                        onClick={() =>
                          setExpandedVaults((prev) => ({ ...prev, [v.id]: !prev[v.id] }))
                        }
                        aria-expanded={isVaultOpen}
                        className="flex min-w-0 flex-1 items-center gap-2 text-left"
                      >
                        {isVaultOpen ? (
                          <ChevronDown className="h-4 w-4 shrink-0 text-[var(--accent)]" />
                        ) : (
                          <ChevronRight className="h-4 w-4 shrink-0 text-[var(--ink-muted)]" />
                        )}
                        <span className="truncate font-bold text-[var(--ink-primary)]">{v.name}</span>
                        <span className="shrink-0 rounded-md bg-[var(--bg-muted)] px-2 py-0.5 font-mono text-xs text-[var(--ink-secondary)]">
                          {treeNodes.length} 个文件夹
                        </span>
                        {allowCount > 0 && (
                          <span className="shrink-0 rounded-md bg-emerald-100 px-2 py-0.5 font-mono text-xs font-semibold text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300">
                            允许 {allowCount}
                          </span>
                        )}
                        {denyCount > 0 && (
                          <span className="shrink-0 rounded-md bg-rose-100 px-2 py-0.5 font-mono text-xs font-semibold text-rose-800 dark:bg-rose-950/60 dark:text-rose-300">
                            禁止 {denyCount}
                          </span>
                        )}
                      </button>

                      <div className="flex shrink-0 items-center gap-2">
                        <button
                          type="button"
                          onClick={() => toggleRulePrefix("allow", "**")}
                          className={`rounded-md border px-2.5 py-1 text-xs font-semibold transition ${
                            curRule.allow.includes("**")
                              ? "border-emerald-400 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300"
                              : "border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)]"
                          }`}
                        >
                          允许全部文件夹
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedVaults((prev) => ({ ...prev, [v.id]: !prev[v.id] }))
                          }
                          className="rounded-md border border-[var(--border-default)] bg-[var(--bg-subtle)] px-2.5 py-1 text-xs font-medium text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                        >
                          {isVaultOpen ? "收起" : "设置文件夹"}
                        </button>
                      </div>
                    </div>

                    {/* Expanded Folder Tree Body */}
                    {isVaultOpen && (
                      <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-3">
                        {treeNodes.length === 0 ? (
                          <div className="py-3 text-center text-xs text-[var(--ink-muted)]">
                            该知识库下暂时没有子文件夹
                          </div>
                        ) : (
                          <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
                            {treeNodes.map((node) => {
                              const folderKey = `${v.id}:${node.prefix}`;
                              const hasChildren = node.children.length > 0;
                              const isFolderOpen = Boolean(expandedFolders[folderKey]);
                              const childRulesCount = node.children.filter(
                                (c) =>
                                  curRule.allow.includes(c.prefix) ||
                                  curRule.deny.includes(c.prefix),
                              ).length;

                              return (
                                <div
                                  key={node.prefix}
                                  className="rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] p-2.5"
                                >
                                  <div className="flex items-center justify-between gap-2">
                                    {hasChildren ? (
                                      <button
                                        type="button"
                                        onClick={() =>
                                          setExpandedFolders((prev) => ({
                                            ...prev,
                                            [folderKey]: !prev[folderKey],
                                          }))
                                        }
                                        aria-expanded={isFolderOpen}
                                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                                      >
                                        {isFolderOpen ? (
                                          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
                                        ) : (
                                          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-[var(--ink-muted)]" />
                                        )}
                                        <span className="truncate font-mono font-semibold text-[var(--ink-primary)]">
                                          {node.name}/
                                        </span>
                                        <span className="shrink-0 rounded bg-[var(--bg-muted)] px-1.5 py-0.5 font-mono text-xs text-[var(--ink-muted)]">
                                          {node.children.length}
                                        </span>
                                        {!isFolderOpen && childRulesCount > 0 && (
                                          <span className="shrink-0 rounded bg-[var(--accent-soft)] px-1.5 py-0.5 font-mono text-xs font-semibold text-[var(--accent)]">
                                            子项已设 {childRulesCount}
                                          </span>
                                        )}
                                      </button>
                                    ) : (
                                      <div className="flex min-w-0 flex-1 items-center gap-1.5">
                                        <Folder className="h-3.5 w-3.5 shrink-0 text-[var(--ink-muted)]" />
                                        <span className="truncate font-mono font-semibold text-[var(--ink-primary)]">
                                          {node.name}/
                                        </span>
                                      </div>
                                    )}

                                    <div className="flex shrink-0 gap-1">
                                      <button
                                        type="button"
                                        onClick={() => toggleRulePrefix("allow", node.prefix)}
                                        className={`rounded px-2 py-0.5 text-xs font-semibold transition ${
                                          curRule.allow.includes(node.prefix)
                                            ? "bg-emerald-600 text-white"
                                            : "border border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)] hover:border-emerald-400"
                                        }`}
                                      >
                                        允许
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => toggleRulePrefix("deny", node.prefix)}
                                        className={`rounded px-2 py-0.5 text-xs font-semibold transition ${
                                          curRule.deny.includes(node.prefix)
                                            ? "bg-rose-600 text-white"
                                            : "border border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)] hover:border-rose-400"
                                        }`}
                                      >
                                        禁止
                                      </button>
                                    </div>
                                  </div>

                                  {/* L2 Subfolders (Default Collapsed) */}
                                  {hasChildren && isFolderOpen && (
                                    <div className="mt-2 ml-2 space-y-1.5 border-l border-[var(--border-default)] pl-3">
                                      {node.children.map((ch) => (
                                        <div
                                          key={ch.prefix}
                                          className="flex items-center justify-between gap-2 text-xs"
                                        >
                                          <span className="truncate font-mono text-[var(--ink-secondary)]">
                                            {ch.name}/
                                          </span>
                                          <div className="flex shrink-0 gap-1">
                                            <button
                                              type="button"
                                              onClick={() => toggleRulePrefix("allow", ch.prefix)}
                                              className={`rounded px-2 py-0.5 text-xs font-semibold ${
                                                curRule.allow.includes(ch.prefix)
                                                  ? "bg-emerald-600 text-white"
                                                  : "border border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)]"
                                              }`}
                                            >
                                              允许
                                            </button>
                                            <button
                                              type="button"
                                              onClick={() => toggleRulePrefix("deny", ch.prefix)}
                                              className={`rounded px-2 py-0.5 text-xs font-semibold ${
                                                curRule.deny.includes(ch.prefix)
                                                  ? "bg-rose-600 text-white"
                                                  : "border border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)]"
                                              }`}
                                            >
                                              禁止
                                            </button>
                                          </div>
                                        </div>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>

          {/* Issued Device Tokens Roster */}
          <section>
            <div className="mb-2.5 flex items-center justify-between">
              <h2 className="text-[13px] font-bold text-[var(--ink-primary)]">
                已授权的设备 / 成员
              </h2>
              <span className="text-xs text-[var(--ink-muted)]">共 {tokens.length} 个</span>
            </div>

            {tokens.length === 0 ? (
              <div className="rounded-xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-subtle)] p-8 text-center text-xs text-[var(--ink-muted)]">
                还没有授权过其他设备。未授权时，局域网内任何设备都无法访问你的知识库，可在右侧生成访问密钥。
              </div>
            ) : (
              <div className="overflow-x-auto rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)]">
                <table className="w-full border-collapse text-left text-xs">
                  <thead className="border-b border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-muted)]">
                    <tr>
                      <th className="px-3.5 py-2.5 font-semibold">设备 / 成员</th>
                      <th className="px-3.5 py-2.5 font-semibold">所属角色</th>
                      <th className="px-3.5 py-2.5 font-semibold">可访问的知识库</th>
                      <th className="px-3.5 py-2.5 font-semibold">密钥尾号</th>
                      <th className="px-3.5 py-2.5 text-right font-semibold">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {tokens.map((t) => (
                      <tr key={t.id} className="hover:bg-[var(--bg-subtle)]">
                        <td className="px-3.5 py-2.5">
                          <div className="font-semibold text-[var(--ink-primary)]">
                            {t.label || t.uid}
                          </div>
                          <div className="font-mono text-[var(--ink-muted)]">{t.uid}</div>
                        </td>
                        <td className="px-3.5 py-2.5">
                          <div className="flex flex-wrap gap-1">
                            {(t.groups || []).map((g) => (
                              <span
                                key={g}
                                className="rounded bg-[var(--accent-soft)] px-2 py-0.5 font-mono font-semibold text-[var(--accent)]"
                              >
                                {g}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="px-3.5 py-2.5 font-mono text-[var(--ink-secondary)]">
                          {(t.allowed_vaults || []).join(", ")}
                        </td>
                        <td className="px-3.5 py-2.5 font-mono text-[var(--ink-muted)]">
                          {t.token_preview}
                        </td>
                        <td className="px-3.5 py-2.5 text-right">
                          <div className="inline-flex items-center gap-1.5">
                            <button
                              type="button"
                              onClick={() =>
                                setShareConfig({
                                  token: "",
                                  isPlaceholder: true,
                                  uid: t.uid,
                                  label: t.label || t.uid,
                                  allowedVaults: t.allowed_vaults || ["*"],
                                })
                              }
                              className="inline-flex items-center gap-1 rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-1 font-medium text-[var(--accent)] hover:border-[var(--accent)] hover:bg-[var(--accent-soft)]"
                            >
                              <Code2 className="h-3 w-3" /> MCP 配置
                            </button>
                            <button
                              type="button"
                              onClick={() => void handleRotate(t.id)}
                              className="inline-flex items-center gap-1 rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-1 font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
                            >
                              <RotateCw className="h-3 w-3" /> 重置密钥
                            </button>
                            <button
                              type="button"
                              onClick={() =>
                                void api.deleteToken(t.id).then(() => {
                                  onToast("info", `已撤销 '${t.label || t.uid}' 的访问权限`);
                                  onRefresh();
                                })
                              }
                              className="rounded-md p-1 text-[var(--ink-muted)] hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-950/50 dark:hover:text-rose-400"
                              title="撤销授权"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </main>

      {/* Right Inspector: Token Issuer + Permission Preview */}
      <aside className="flex w-full shrink-0 flex-col border-t border-[var(--border-default)] bg-[var(--bg-sidebar)] lg:w-[356px] lg:border-t-0 lg:border-l">
        <div className="border-b border-[var(--border-default)] px-4 py-3">
          <div className="text-xs font-semibold text-[var(--ink-muted)]">授权与预览</div>
          <div className="mt-1 text-[14px] font-bold text-[var(--ink-primary)]">添加设备授权</div>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto p-4 text-xs">
          {/* Issue New Device Token Card */}
          <div className="space-y-3 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-3.5">
            <div className="flex items-center gap-1.5 font-bold text-[var(--ink-primary)]">
              <Key className="h-3.5 w-3.5 text-[var(--accent)]" /> 新建访问密钥
            </div>
            <div>
              <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
                成员 / 设备代号
              </label>
              <input
                value={uid}
                onChange={(e) => setUid(e.target.value)}
                placeholder="如 xiaoming-mac"
                className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2.5 py-1.5 font-mono text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block font-medium text-[var(--ink-secondary)]">备注名称</label>
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="如 小明的笔记本"
                className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2.5 py-1.5 text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1.5 block font-medium text-[var(--ink-secondary)]">
                分配角色
              </label>
              <div className="flex flex-wrap gap-1.5">
                {selectableGroups.map((g) => {
                  const on = selectedGroups.includes(g);
                  return (
                    <button
                      key={g}
                      type="button"
                      onClick={() =>
                        setSelectedGroups((prev) =>
                          on ? prev.filter((x) => x !== g) : [...prev, g],
                        )
                      }
                      className={`rounded-md border px-2 py-0.5 font-mono font-semibold ${
                        on
                          ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                          : "border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)]"
                      }`}
                    >
                      {g}
                    </button>
                  );
                })}
              </div>
            </div>
            <div>
              <label className="mb-1.5 block font-medium text-[var(--ink-secondary)]">
                可访问的知识库
              </label>
              <div className="flex flex-wrap gap-1.5">
                {["*", ...localVaults.map((v) => v.id)].map((vid) => {
                  const on = selectedVaults.includes(vid);
                  const matchedVault = localVaults.find((v) => v.id === vid);
                  return (
                    <button
                      key={vid}
                      type="button"
                      onClick={() => {
                        if (vid === "*") {
                          setSelectedVaults(["*"]);
                        } else {
                          setSelectedVaults((prev) => {
                            const cleaned = prev.filter((x) => x !== "*");
                            return on ? cleaned.filter((x) => x !== vid) : [...cleaned, vid];
                          });
                        }
                      }}
                      className={`rounded-md border px-2 py-0.5 font-semibold ${
                        on
                          ? "border-emerald-400 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300"
                          : "border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)]"
                      }`}
                    >
                      {vid === "*" ? "全部知识库" : matchedVault?.name || vid}
                    </button>
                  );
                })}
              </div>
            </div>
            <button
              type="button"
              disabled={!uid.trim()}
              onClick={() => void handleCreateToken()}
              className="w-full rounded-lg bg-[var(--accent)] py-2 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-40"
            >
              生成访问密钥
            </button>
          </div>

          {/* Permission Effect Preview */}
          <div className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-primary)]">
            <div className="flex items-center justify-between border-b border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-3.5 py-2.5">
              <span className="flex items-center gap-1.5 font-semibold text-[var(--ink-primary)]">
                <ShieldCheck className="h-3.5 w-3.5 text-[var(--chart-http)]" /> 权限效果预览
              </span>
              <span className="text-xs text-[var(--ink-muted)]">实时验证</span>
            </div>

            <div className="space-y-2.5 p-3.5">
              <div className="grid grid-cols-2 gap-2">
                <select
                  value={simTokenId}
                  onChange={(e) => setSimTokenId(e.target.value)}
                  className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2 py-1.5 text-xs text-[var(--ink-primary)]"
                >
                  {tokens.length === 0 ? (
                    <option value="">暂无已授权设备</option>
                  ) : (
                    tokens.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.label || t.uid}
                      </option>
                    ))
                  )}
                </select>
                <select
                  value={simVaultId}
                  onChange={(e) => setSimVaultId(e.target.value)}
                  className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2 py-1.5 text-xs text-[var(--ink-primary)]"
                >
                  {localVaults.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.name}
                    </option>
                  ))}
                </select>
              </div>

              <button
                type="button"
                disabled={!simTokenId || !simVaultId}
                onClick={() => void handleSimulate()}
                className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] py-1.5 font-semibold text-[var(--accent)] hover:border-[var(--accent)] hover:bg-[var(--accent-soft)] disabled:opacity-40"
              >
                预览可见范围
              </button>

              {simResult && (
                <div className="space-y-2.5 border-t border-[var(--border-subtle)] pt-2.5">
                  <div className="grid grid-cols-3 divide-x divide-[var(--border-subtle)] overflow-hidden rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)]">
                    <div className="p-2">
                      <div className="text-[var(--ink-muted)]">可查看文档</div>
                      <div className="mt-0.5 font-disp text-lg font-bold text-[var(--chart-http)]">
                        {simResult.allowed_sources.length}
                      </div>
                    </div>
                    <div className="p-2">
                      <div className="text-[var(--ink-muted)]">已禁止文档</div>
                      <div className="mt-0.5 font-disp text-lg font-bold text-[var(--chart-block)]">
                        {simResult.denied_sources.length}
                      </div>
                    </div>
                    <div className="p-2">
                      <div className="text-[var(--ink-muted)]">连带隐藏整理</div>
                      <div className="mt-0.5 font-disp text-lg font-bold text-[var(--chart-amber)]">
                        {simResult.denied_wiki.length}
                      </div>
                    </div>
                  </div>

                  {/* Proportional Ratio Bar */}
                  <div className="flex h-2 w-full overflow-hidden rounded-full bg-[var(--bg-muted)]">
                    <div
                      style={{
                        width: `${(simResult.allowed_sources.length / simTotal) * 100}%`,
                      }}
                      className="bg-[var(--chart-http)]"
                    />
                    <div
                      style={{
                        width: `${(simResult.denied_sources.length / simTotal) * 100}%`,
                      }}
                      className="bg-[var(--chart-block)]"
                    />
                    <div
                      style={{
                        width: `${(simResult.denied_wiki.length / simTotal) * 100}%`,
                      }}
                      className="bg-[var(--chart-amber)]"
                    />
                  </div>

                  {simResult.denied_sources.length > 0 && (
                    <div className="max-h-36 space-y-1 overflow-y-auto rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)] p-2 font-mono text-xs text-rose-700 dark:text-rose-300">
                      {simResult.denied_sources.slice(0, 8).map((p) => (
                        <div key={p} className="truncate">
                          禁止 {p}
                        </div>
                      ))}
                      {simResult.denied_wiki.slice(0, 4).map((w) => (
                        <div key={w} className="truncate text-amber-700 dark:text-amber-300">
                          连带隐藏 {w}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
};
