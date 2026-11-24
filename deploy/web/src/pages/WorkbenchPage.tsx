import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Eye,
  Pause,
  Play,
  Radio,
  Search,
  ShieldAlert,
  Sparkles,
  Trash2,
} from "lucide-react";
import { api } from "../api/client";
import { ClientBadge } from "../components/ClientBadge";
import { TelemetryScopeDeck } from "../components/TelemetryScopeDeck";
import { copyToClipboard } from "../platform/adapter";
import type {
  AclRule,
  LogInvocation,
  LogStatsResponse,
  TokenRecord,
  VaultInfo,
} from "../types";

interface WorkbenchPageProps {
  vaults: VaultInfo[];
  activeVaultId: string;
  onSelectVault: (id: string) => void;
  tokens: TokenRecord[];
  acl: Record<string, AclRule>;
  stats: LogStatsResponse | null;
  logs: LogInvocation[];
  sseConnected: boolean;
  onRefreshStats: () => void;
  onClearLogs: () => Promise<void>;
  externalQuery?: string;
  onConsumeExternalQuery?: () => void;
  onToast: (kind: "success" | "error" | "info", msg: string) => void;
}

interface UnifiedHit {
  key: string;
  layer: 1 | 2 | 3 | 4;
  kindLabel: string;
  title: string;
  source: string;
  excerpt: string;
  fullContent: string;
  score: number;
  tokens: number;
  freshness?: string;
}

function humanizeBlockReason(code?: string): string {
  if (code === "wiki_provenance_blocked") return "引用了无权查看的文档";
  return "所在文件夹未授权";
}

export const WorkbenchPage: React.FC<WorkbenchPageProps> = ({
  vaults,
  activeVaultId,
  onSelectVault,
  tokens,
  acl,
  stats,
  logs,
  sseConnected,
  onRefreshStats,
  onClearLogs,
  externalQuery,
  onConsumeExternalQuery,
  onToast,
}) => {
  const activeVault = vaults.find((v) => v.id === activeVaultId) || vaults[0];
  const isRemote = activeVault?.kind === "remote";

  const [question, setQuestion] = useState("");
  const [queryMode, setQueryMode] = useState<"wiki_context" | "wiki_search" | "wiki_route">(
    "wiki_context",
  );
  const [perspective, setPerspective] = useState<string>("admin");
  const [stageMode, setStageMode] = useState<"evidence" | "stream">("evidence");
  const [running, setRunning] = useState(false);
  const [resp, setResp] = useState<Record<string, unknown> | null>(null);
  const [selectedHitIdx, setSelectedHitIdx] = useState(0);
  const [copiedJson, setCopiedJson] = useState(false);
  const [copiedContent, setCopiedContent] = useState(false);
  const [showDebug, setShowDebug] = useState(false);

  // Stream filters
  const [paused, setPaused] = useState(false);
  const [filterClient, setFilterClient] = useState("");
  const [onlyMissing, setOnlyMissing] = useState(false);
  const [logSearch, setLogSearch] = useState("");

  const inputRef = useRef<HTMLInputElement | null>(null);

  // Cmd/Ctrl+K focuses Spotlight X-Ray bar
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Derive realistic human-readable sample queries from actual vault sample_sources
  const sampleQueries = useMemo(() => {
    const samples = activeVault?.sample_sources || [];
    const cleanList = samples
      .map((s) => {
        const base = s.split("/").pop() || s;
        return base.replace(/\.[a-zA-Z0-9]+$/, "").replace(/[-_]+/g, " ").trim();
      })
      .filter((s) => s.length >= 2 && !/[A-Za-z0-9]{14,}/.test(s));
    const cjkPreferred = [
      ...cleanList.filter((s) => /[\u4e00-\u9fa5]/.test(s)),
      ...cleanList.filter((s) => !/[\u4e00-\u9fa5]/.test(s)),
    ];
    if (cjkPreferred.length > 0) return Array.from(new Set(cjkPreferred)).slice(0, 2);
    const folderNames = (activeVault?.folder_tree || []).map((n) => n.name).filter(Boolean);
    if (folderNames.length > 0) return folderNames.slice(0, 2);
    if (activeVault?.name) return [`${activeVault.name} 核心规范与架构`, "目录结构与关键规定"];
    return ["核心业务流程与规范"];
  }, [activeVault]);

  const executeInspect = async (overrideQ?: string, overridePersp?: string) => {
    const vid = activeVault?.id;
    const qText = (overrideQ !== undefined ? overrideQ : question).trim();
    if (!vid || !qText) return;
    setRunning(true);
    try {
      const pUse = overridePersp !== undefined ? overridePersp : perspective;
      let principal_spec: {
        type: "admin" | "group" | "token";
        groups?: string[];
        token_id?: string;
      } = { type: "admin" };
      if (!isRemote) {
        if (pUse.startsWith("group:")) {
          principal_spec = { type: "group", groups: [pUse.slice(6)] };
        } else if (pUse.startsWith("token:")) {
          principal_spec = { type: "token", token_id: pUse.slice(6) };
        }
      }
      const data = await api.playgroundInspect({
        vault_id: vid,
        question: qText,
        mode: isRemote ? "wiki_context" : queryMode,
        principal_spec,
      });
      setResp(data);
      setSelectedHitIdx(0);
      setStageMode("evidence");
      onRefreshStats();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  // Auto-run external gap query when clicked from Left Rail
  useEffect(() => {
    if (externalQuery && externalQuery.trim()) {
      setQuestion(externalQuery);
      void executeInspect(externalQuery);
      onConsumeExternalQuery?.();
    }
  }, [externalQuery]);

  // Auto-seed initial query when vault becomes ready so stage is immediately alive
  const autoSeededVaultRef = useRef<string>("");
  useEffect(() => {
    if (
      activeVault?.ready &&
      autoSeededVaultRef.current !== activeVault.id &&
      sampleQueries[0]
    ) {
      autoSeededVaultRef.current = activeVault.id;
      setQuestion(sampleQueries[0]);
      void executeInspect(sampleQueries[0]);
    }
  }, [activeVault?.id, activeVault?.ready, sampleQueries]);

  const roleNames = Object.keys(acl || {}).filter((r) => r !== "admin" && r !== "all");

  const resultObj = (resp?.result || {}) as Record<string, unknown>;
  const elapsedMs = Number(resp?.elapsed_ms || resultObj?.elapsed_ms || 0);
  const planDebug = (resultObj?.plan_debug || {}) as {
    route_mode?: string;
    token_budget?: number;
    auto_synonyms?: string[];
  };
  const routeLabel = String(planDebug.route_mode || resultObj?.route || queryMode);
  const shadowBlocked = ((resp?.shadow_blocked || []) as {
    layer: string;
    source: string;
    reason_code: string;
    matched_rule: string;
    blocked_refs?: string[];
  }[]);
  const missingConcepts = ((resultObj?.missing_concepts || []) as string[]);

  const unifiedHits: UnifiedHit[] = useMemo(() => {
    const out: UnifiedHit[] = [];
    const mems = (resultObj?.memory_notes || []) as {
      note_id?: string;
      kind?: string;
      text?: string;
      related_sources?: string[];
    }[];
    mems.forEach((m, i) => {
      const txt = String(m.text || "");
      out.push({
        key: `mem-${m.note_id || i}`,
        layer: 4,
        kindLabel: m.kind === "errata" ? "纠错提醒" : "人工补充",
        title: `${m.kind === "errata" ? "纠错" : "补充"}：${txt.slice(0, 34)}`,
        source: (m.related_sources || [])[0] || `补充/${m.note_id || "note"}.md`,
        excerpt: txt,
        fullContent: txt,
        score: 96,
        tokens: Math.max(24, Math.round(txt.length * 0.8)),
      });
    });

    const wikis = (resultObj?.wiki_hits || []) as {
      source?: string;
      heading?: string;
      content?: string;
      score?: number;
      freshness?: string;
    }[];
    wikis.forEach((w, i) => {
      const content = String(w.content || "");
      out.push({
        key: `wiki-${w.source}-${i}`,
        layer: 3,
        kindLabel: "AI 整理",
        title: String(w.heading || w.source || "整理摘要"),
        source: String(w.source || ""),
        excerpt: content.slice(0, 190),
        fullContent: content,
        score: Math.min(95, Math.max(35, Math.round(Math.abs(Number(w.score || 78))))),
        tokens: Math.max(48, Math.round(content.length * 0.65)),
        freshness: w.freshness,
      });
    });

    const prims = (resultObj?.primary_hits || resultObj?.hits || []) as {
      source?: string;
      heading?: string;
      content?: string;
      snippet?: string;
      score?: number;
    }[];
    prims.forEach((p, i) => {
      const content = String(p.content || p.snippet || "");
      const src = String(p.source || "");
      const isMirror = src.startsWith("镜像/") || src.startsWith("mirrors/");
      out.push({
        key: `prim-${src}-${i}`,
        layer: isMirror ? 2 : 1,
        kindLabel: "原始文档",
        title: String(p.heading || src.split("/").pop() || "文档片段"),
        source: src,
        excerpt: content.slice(0, 190),
        fullContent: content,
        score: Math.min(92, Math.max(25, Math.round(Math.abs(Number(p.score || 68))))),
        tokens: Math.max(40, Math.round(content.length * 0.65)),
      });
    });

    return out;
  }, [resultObj]);

  const activeHit = unifiedHits[selectedHitIdx] || unifiedHits[0] || null;

  const latestFrame = useMemo(() => {
    const top = logs[0];
    if (!top) return null;
    return {
      tool: top.tool,
      vaultId: top.vault_id,
      client: top.client,
      elapsedMs: top.elapsed_ms,
    };
  }, [logs]);

  const visibleLogs = useMemo(() => {
    return logs.filter((l) => {
      if (filterClient && !l.client.toLowerCase().includes(filterClient.toLowerCase())) {
        return false;
      }
      if (onlyMissing && (l.missing_concepts || []).length === 0) return false;
      if (
        logSearch.trim() &&
        !l.query.toLowerCase().includes(logSearch.trim().toLowerCase())
      ) {
        return false;
      }
      return true;
    });
  }, [logs, filterClient, onlyMissing, logSearch]);

  const vaultNameMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const v of vaults) {
      m[v.id] = v.name || v.id;
    }
    return m;
  }, [vaults]);

  const handleCopyContent = async () => {
    if (!activeHit?.fullContent) return;
    const ok = await copyToClipboard(activeHit.fullContent);
    if (ok) {
      setCopiedContent(true);
      setTimeout(() => setCopiedContent(false), 1800);
      onToast("info", "已复制该条内容");
    }
  };

  const handleCopyJson = async () => {
    const ok = await copyToClipboard(JSON.stringify(resp || {}, null, 2));
    if (ok) {
      setCopiedJson(true);
      setTimeout(() => setCopiedJson(false), 1800);
      onToast("info", "已复制调试数据 JSON");
    }
  };

  const perspectiveLabel = useMemo(() => {
    if (perspective === "admin") return "我自己（全部可见）";
    if (perspective.startsWith("group:")) return `角色：${perspective.slice(6)}`;
    if (perspective.startsWith("token:")) {
      const tid = perspective.slice(6);
      const tok = tokens.find((t) => t.id === tid);
      return `成员：${tok?.label || tok?.uid || tid}`;
    }
    return perspective;
  }, [perspective, tokens]);

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      {/* Center Workbench Stage */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-[var(--bg-surface)]">
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4 sm:px-5">
          {/* Compact 24h Usage Overview (Collapsed by default) */}
          <TelemetryScopeDeck
            stats={stats}
            sseConnected={sseConnected}
            vaultCount={vaults.length}
            latestFrame={latestFrame}
          />

          {/* Search Test Bar */}
          <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-3.5 shadow-2xs">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void executeInspect();
              }}
              className="flex flex-wrap items-center gap-2"
            >
              {vaults.length > 1 && (
                <select
                  value={activeVault?.id || ""}
                  onChange={(e) => onSelectVault(e.target.value)}
                  className="h-9 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 text-xs font-semibold text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none md:hidden"
                >
                  {vaults.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.name}
                    </option>
                  ))}
                </select>
              )}

              <div className="relative flex min-w-[180px] flex-1 items-center">
                <Search className="pointer-events-none absolute left-3 h-4 w-4 text-[var(--ink-muted)]" />
                <input
                  ref={inputRef}
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder={
                    activeVault
                      ? `在「${activeVault.name}」中试试提问或搜索关键词...`
                      : "请先在左侧选择或添加一个知识库..."
                  }
                  className={`h-9 w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] pl-9 text-[14px] font-medium text-[var(--ink-primary)] placeholder:text-[var(--ink-muted)] focus:border-[var(--accent)] focus:outline-none ${
                    question ? "pr-3" : "pr-11"
                  }`}
                />
                {!question && (
                  <span className="pointer-events-none absolute right-2.5 rounded border border-[var(--border-default)] bg-[var(--bg-subtle)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--ink-muted)]">
                    ⌘K
                  </span>
                )}
              </div>

              <select
                value={isRemote ? "wiki_context" : queryMode}
                disabled={isRemote}
                onChange={(e) =>
                  setQueryMode(
                    e.target.value as "wiki_context" | "wiki_search" | "wiki_route",
                  )
                }
                className="h-9 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 text-xs font-medium text-[var(--ink-secondary)] focus:border-[var(--accent)] focus:outline-none disabled:opacity-60"
              >
                <option value="wiki_context">智能检索</option>
                <option value="wiki_search">原文搜索</option>
                <option value="wiki_route">目录定位</option>
              </select>

              <button
                type="submit"
                disabled={running || !activeVault || !question.trim()}
                className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 text-xs font-semibold text-white transition hover:bg-[var(--accent-hover)] disabled:opacity-50"
              >
                <Play className="h-3.5 w-3.5" />
                {running ? "搜索中..." : "搜索"}
              </button>
            </form>

            {/* Viewer Role Switcher + View Mode Switcher */}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-subtle)] pt-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-[var(--ink-muted)]">查看身份</span>
                <div className="flex flex-wrap items-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-muted)] p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => {
                      setPerspective("admin");
                      if (question.trim()) void executeInspect(question, "admin");
                    }}
                    className={`rounded-[6px] px-2.5 py-1 font-semibold ${
                      perspective === "admin"
                        ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs"
                        : "bg-transparent text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                    }`}
                  >
                    我自己（全部可见）
                  </button>
                  {!isRemote &&
                    roleNames.slice(0, 3).map((r) => {
                      const val = `group:${r}`;
                      return (
                        <button
                          key={val}
                          type="button"
                          onClick={() => {
                            setPerspective(val);
                            if (question.trim()) void executeInspect(question, val);
                          }}
                          className={`rounded-[6px] px-2.5 py-1 font-semibold ${
                            perspective === val
                              ? "bg-[var(--bg-surface)] text-[var(--accent)] shadow-2xs"
                              : "bg-transparent text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                          }`}
                        >
                          角色：{r}
                        </button>
                      );
                    })}
                  {!isRemote &&
                    tokens.slice(0, 2).map((t) => {
                      const val = `token:${t.id}`;
                      return (
                        <button
                          key={val}
                          type="button"
                          onClick={() => {
                            setPerspective(val);
                            if (question.trim()) void executeInspect(question, val);
                          }}
                          className={`rounded-[6px] px-2.5 py-1 font-semibold ${
                            perspective === val
                              ? "bg-[var(--bg-surface)] text-[var(--accent)] shadow-2xs"
                              : "bg-transparent text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                          }`}
                        >
                          成员：{t.label || t.uid}
                        </button>
                      );
                    })}
                </div>
              </div>

              <div className="flex items-center gap-1.5">
                {sampleQueries.slice(0, 2).map((sq) => (
                  <button
                    key={sq}
                    type="button"
                    onClick={() => {
                      setQuestion(sq);
                      void executeInspect(sq);
                    }}
                    className="hidden max-w-[150px] truncate rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-1 text-xs text-[var(--ink-secondary)] hover:border-[var(--accent)] hover:text-[var(--accent)] xl:inline-block"
                    title={`试试搜索: ${sq}`}
                  >
                    {sq}
                  </button>
                ))}

                <div className="flex items-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-muted)] p-0.5 text-xs">
                  <button
                    type="button"
                    onClick={() => setStageMode("evidence")}
                    className={`rounded-[6px] px-2.5 py-1 font-semibold ${
                      stageMode === "evidence"
                        ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs"
                        : "text-[var(--ink-secondary)]"
                    }`}
                  >
                    搜索结果 ({unifiedHits.length})
                  </button>
                  <button
                    type="button"
                    onClick={() => setStageMode("stream")}
                    className={`inline-flex items-center gap-1 rounded-[6px] px-2.5 py-1 font-semibold ${
                      stageMode === "stream"
                        ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs"
                        : "text-[var(--ink-secondary)]"
                    }`}
                  >
                    <Radio
                      className={`h-3 w-3 ${
                        sseConnected ? "text-emerald-500" : "text-amber-500"
                      }`}
                    />
                    AI 调用记录 ({logs.length})
                  </button>
                </div>
              </div>
            </div>
          </div>

          {/* Stage View A: Search Results */}
          {stageMode === "evidence" ? (
            <div className="space-y-3">
              {/* Summary Strip */}
              <div className="flex flex-wrap items-center justify-between gap-2 px-1">
                <div className="flex items-center gap-2 text-xs text-[var(--ink-secondary)]">
                  <span className="font-bold text-[var(--ink-primary)]">搜索结果</span>
                  <span>
                    找到 {unifiedHits.length} 条相关内容
                    {shadowBlocked.length > 0 && (
                      <span className="ml-1.5 font-semibold text-rose-600 dark:text-rose-400">
                        · 当前身份隐藏了 {shadowBlocked.length} 条无权查看的内容
                      </span>
                    )}
                  </span>
                </div>
                {resp && (
                  <span className="rounded-md bg-[var(--bg-muted)] px-2 py-0.5 font-mono text-xs text-[var(--ink-secondary)]">
                    {elapsedMs.toFixed(0)} ms
                  </span>
                )}
              </div>

              {/* Hidden Items Notice when viewing under restricted role/token */}
              {shadowBlocked.length > 0 && (
                <div className="rounded-xl border border-rose-200 bg-rose-50/70 p-3.5 dark:border-rose-800 dark:bg-rose-950/35">
                  <div className="flex items-center gap-1.5 text-xs font-bold text-rose-800 dark:text-rose-300">
                    <ShieldAlert className="h-4 w-4 text-rose-600 dark:text-rose-400" />
                    当前查看身份无权访问以下 {shadowBlocked.length} 项内容，已自动隐藏
                  </div>
                  <div className="mt-2 space-y-1.5">
                    {shadowBlocked.map((b, i) => (
                      <div
                        key={i}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-rose-200/80 bg-[var(--bg-surface)] px-3 py-2 text-xs dark:border-rose-800/60"
                      >
                        <span className="font-mono font-medium text-rose-900 line-through dark:text-rose-200">
                          {b.source}
                        </span>
                        <span className="text-xs text-rose-700 dark:text-rose-300">
                          {humanizeBlockReason(b.reason_code)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Evidence Rows */}
              {unifiedHits.length === 0 ? (
                <div className="rounded-xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-subtle)] px-6 py-12 text-center">
                  <div className="text-[14px] font-semibold text-[var(--ink-primary)]">
                    {activeVault?.hydrating
                      ? "正在请求目录授权并加载知识库..."
                      : resp
                        ? "没有找到相关内容"
                        : "输入问题或点击示例词即可测试搜索效果"}
                  </div>
                  <p className="mt-1 text-xs text-[var(--ink-muted)]">
                    {activeVault?.online_error ||
                      (missingConcepts.length > 0
                        ? `未找到以下词的相关文档: ${missingConcepts.join(", ")}（可在「知识库 → 同义词表」添加对应关系）`
                        : "可以切换不同身份，查看团队成员各自能搜到哪些内容。")}
                  </p>
                </div>
              ) : (
                <div className="divide-y divide-[var(--border-subtle)] overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)]">
                  {unifiedHits.map((h, idx) => {
                    const selected = idx === selectedHitIdx;
                    const badgeCls =
                      h.layer === 4
                        ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                        : h.layer === 3
                          ? "bg-indigo-100 text-indigo-800 dark:bg-indigo-950/60 dark:text-indigo-300"
                          : "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300";
                    return (
                      <button
                        key={h.key}
                        type="button"
                        onClick={() => setSelectedHitIdx(idx)}
                        className={`flex w-full items-start justify-between gap-4 px-4 py-3.5 text-left transition ${
                          selected
                            ? "bg-[var(--accent-soft)] shadow-[inset_3px_0_0_var(--accent)]"
                            : "hover:bg-[var(--bg-subtle)]"
                        }`}
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className={`shrink-0 rounded-md px-2 py-0.5 text-xs font-semibold ${badgeCls}`}
                            >
                              {h.kindLabel}
                            </span>
                            <span className="truncate text-[14px] font-bold text-[var(--ink-primary)]">
                              {h.title}
                            </span>
                          </div>
                          <p className="mt-1.5 line-clamp-2 text-[13px] leading-relaxed text-[var(--ink-secondary)]">
                            {h.excerpt}
                          </p>
                          <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-[var(--ink-muted)]">
                            <span className="truncate font-mono text-[var(--ink-secondary)]">
                              {h.source}
                            </span>
                            <span className="inline-flex items-center gap-1.5">
                              <span className="inline-block h-1.5 w-12 overflow-hidden rounded-full bg-[var(--bg-muted)]">
                                <span
                                  className="block h-full bg-[var(--accent)]"
                                  style={{ width: `${h.score}%` }}
                                />
                              </span>
                              相关度 {h.score}%
                            </span>
                          </div>
                        </div>

                        {perspective !== "admin" && (
                          <div className="shrink-0 text-right">
                            <span className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-600 dark:text-emerald-400">
                              <Eye className="h-3.5 w-3.5" />
                              可见
                            </span>
                          </div>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          ) : (
            /* Stage View B: AI Query History */
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    value={filterClient}
                    onChange={(e) => setFilterClient(e.target.value)}
                    className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-primary)]"
                  >
                    <option value="">全部工具</option>
                    <option value="dsh">DSH</option>
                    <option value="claude">Claude Code</option>
                    <option value="cursor">Cursor</option>
                    <option value="codex">Codex CLI</option>
                    <option value="workbuddy">WorkBuddy</option>
                    <option value="agy">Antigravity</option>
                    <option value="playground">检索测试</option>
                  </select>

                  <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-secondary)]">
                    <input
                      type="checkbox"
                      checked={onlyMissing}
                      onChange={(e) => setOnlyMissing(e.target.checked)}
                      className="rounded"
                    />
                    仅看未查到的提问
                  </label>

                  <input
                    value={logSearch}
                    onChange={(e) => setLogSearch(e.target.value)}
                    placeholder="搜索提问内容..."
                    className="w-44 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                  />
                </div>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setPaused((p) => !p)}
                    className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
                  >
                    {paused ? (
                      <Play className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
                    ) : (
                      <Pause className="h-3.5 w-3.5" />
                    )}
                    {paused ? "恢复刷新" : "暂停"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void onClearLogs()}
                    className="inline-flex items-center gap-1 rounded-lg border border-rose-300 bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50 dark:border-rose-800 dark:text-rose-300 dark:hover:bg-rose-950/40"
                  >
                    <Trash2 className="h-3.5 w-3.5" /> 清空
                  </button>
                </div>
              </div>

              <div className="overflow-x-auto rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)]">
                <table className="w-full text-left text-xs">
                  <thead className="border-b border-[var(--border-default)] bg-[var(--bg-subtle)] text-[var(--ink-muted)]">
                    <tr>
                      <th className="px-3.5 py-2.5 font-semibold">时间</th>
                      <th className="px-3.5 py-2.5 font-semibold">AI 工具</th>
                      <th className="px-3.5 py-2.5 font-semibold">知识库</th>
                      <th className="px-3.5 py-2.5 font-semibold">提问内容</th>
                      <th className="px-3.5 py-2.5 text-right font-semibold">耗时</th>
                      <th className="px-3.5 py-2.5 text-right font-semibold">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {visibleLogs.length === 0 ? (
                      <tr>
                        <td colSpan={6} className="py-12 text-center text-xs text-[var(--ink-muted)]">
                          暂时还没有 AI 查询记录，在 Cursor、Claude 等工具里提问后会显示在这里。
                        </td>
                      </tr>
                    ) : (
                      visibleLogs.slice(0, 80).map((row) => (
                        <tr key={row.id} className="hover:bg-[var(--bg-subtle)]">
                          <td className="whitespace-nowrap px-3.5 py-2.5 font-mono text-[var(--ink-muted)]">
                            {(row.ts.split("T")[1] || row.ts).replace("Z", "")}
                          </td>
                          <td className="px-3.5 py-2.5">
                            <ClientBadge client={row.client} />
                          </td>
                          <td className="whitespace-nowrap px-3.5 py-2.5 text-[var(--ink-secondary)]">
                            {vaultNameMap[row.vault_id] || row.vault_id || "-"}
                          </td>
                          <td className="max-w-[260px] truncate px-3.5 py-2.5 text-[var(--ink-primary)]">
                            {row.query || "-"}
                            {(row.missing_concepts || []).length > 0 && (
                              <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
                                未命中
                              </span>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-3.5 py-2.5 text-right font-mono font-semibold text-[var(--accent)]">
                            {row.elapsed_ms.toFixed(1)} ms
                          </td>
                          <td className="whitespace-nowrap px-3.5 py-2.5 text-right">
                            {row.query && (
                              <button
                                type="button"
                                onClick={() => {
                                  if (row.vault_id) onSelectVault(row.vault_id);
                                  setQuestion(row.query);
                                  void executeInspect(row.query);
                                }}
                                className="font-semibold text-[var(--accent)] hover:underline"
                              >
                                再试一次
                              </button>
                            )}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Right Content Preview + Collapsible Technical Debug Drawer (352px) */}
      <aside className="hidden w-[352px] shrink-0 flex-col border-l border-[var(--border-default)] bg-[var(--bg-sidebar)] lg:flex">
        <div className="border-b border-[var(--border-default)] px-4 py-3">
          <div className="text-xs font-semibold text-[var(--ink-muted)]">内容预览</div>
          <div className="mt-1 truncate text-[14px] font-bold text-[var(--ink-primary)]">
            {activeHit?.title || "选择一条结果查看详情"}
          </div>
          {activeHit && (
            <div className="mt-1.5 flex items-center gap-2 text-xs">
              <span
                className={`shrink-0 rounded-md px-2 py-0.5 font-semibold ${
                  activeHit.layer === 4
                    ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                    : activeHit.layer === 3
                      ? "bg-indigo-100 text-indigo-800 dark:bg-indigo-950/60 dark:text-indigo-300"
                      : "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
                }`}
              >
                {activeHit.kindLabel}
              </span>
              <span className="truncate font-mono text-[var(--ink-muted)]" title={activeHit.source}>
                {activeHit.source}
              </span>
            </div>
          )}
        </div>

        <div className="flex-1 space-y-3.5 overflow-y-auto px-4 py-3 text-xs">
          {/* Primary: Full Content Preview */}
          {activeHit ? (
            <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-3.5">
              <div className="mb-2 flex items-center justify-between">
                <span className="font-semibold text-[var(--ink-secondary)]">正文片段</span>
                <button
                  type="button"
                  onClick={() => void handleCopyContent()}
                  className="inline-flex items-center gap-1 font-semibold text-[var(--accent)] hover:underline"
                >
                  {copiedContent ? (
                    <Check className="h-3.5 w-3.5 text-emerald-500" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                  {copiedContent ? "已复制" : "复制内容"}
                </button>
              </div>
              <div className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-[var(--ink-primary)]">
                {activeHit.fullContent || activeHit.excerpt || "（暂无正文内容）"}
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-surface)] p-6 text-center text-[var(--ink-muted)]">
              在左侧发起搜索或点击任意一条结果，即可在此阅读完整内容。
            </div>
          )}

          {/* Friendly Permission Hidden Summary (only when restricted viewer hides items) */}
          {shadowBlocked.length > 0 && (
            <div className="space-y-1.5 rounded-xl border border-rose-200 bg-rose-50/70 p-3 dark:border-rose-800 dark:bg-rose-950/35">
              <div className="font-bold text-rose-800 dark:text-rose-300">
                当前身份无权查看（已隐藏 {shadowBlocked.length} 项）
              </div>
              {shadowBlocked.slice(0, 6).map((b, i) => (
                <div key={i} className="rounded-lg bg-[var(--bg-surface)] p-2">
                  <div className="truncate font-mono font-semibold text-rose-800 dark:text-rose-300">
                    {b.source}
                  </div>
                  <div className="mt-0.5 text-rose-700 dark:text-rose-400">
                    {humanizeBlockReason(b.reason_code)}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Collapsible Technical & Debug Details (Collapsed by default) */}
          <div className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)]">
            <button
              type="button"
              onClick={() => setShowDebug((v) => !v)}
              className="flex w-full items-center justify-between px-3.5 py-2.5 text-left font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
            >
              <span>技术与调试详情</span>
              {showDebug ? (
                <ChevronDown className="h-3.5 w-3.5 text-[var(--accent)]" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5 text-[var(--ink-muted)]" />
              )}
            </button>

            {showDebug && (
              <div className="space-y-3 border-t border-[var(--border-subtle)] p-3">
                <dl className="grid grid-cols-2 gap-y-1.5 text-xs">
                  <dt className="text-[var(--ink-muted)]">目标知识库</dt>
                  <dd className="text-right font-mono font-semibold text-[var(--ink-primary)]">
                    {activeVault?.id || "-"}
                  </dd>
                  <dt className="text-[var(--ink-muted)]">查看身份</dt>
                  <dd className="truncate text-right font-semibold text-[var(--ink-primary)]">
                    {perspectiveLabel}
                  </dd>
                  <dt className="text-[var(--ink-muted)]">检索路由</dt>
                  <dd className="text-right font-mono font-semibold text-[var(--ink-primary)]">
                    {routeLabel}
                  </dd>
                  <dt className="text-[var(--ink-muted)]">总耗时</dt>
                  <dd className="text-right font-mono font-semibold text-[var(--ink-primary)]">
                    {elapsedMs.toFixed(1)} ms
                  </dd>
                  <dt className="text-[var(--ink-muted)]">Token 预算</dt>
                  <dd className="text-right font-mono font-semibold text-[var(--ink-primary)]">
                    {planDebug.token_budget ?? 4000}
                  </dd>
                  <dt className="text-[var(--ink-muted)]">召回切片</dt>
                  <dd className="text-right font-mono font-semibold text-[var(--ink-primary)]">
                    {unifiedHits.length} 段
                  </dd>
                  {activeHit && (
                    <>
                      <dt className="text-[var(--ink-muted)]">当前评分 / 长度</dt>
                      <dd className="text-right font-mono font-semibold text-[var(--ink-primary)]">
                        BM25 {activeHit.score} · {activeHit.tokens} tokens
                      </dd>
                    </>
                  )}
                </dl>

                {/* Synonym Expansion */}
                <div className="border-t border-[var(--border-subtle)] pt-2.5">
                  <div className="mb-1.5 flex items-center justify-between font-semibold text-[var(--ink-secondary)]">
                    <span>同义词扩展</span>
                    <span className="font-normal text-[var(--ink-muted)]">1 跳收敛</span>
                  </div>
                  {(planDebug.auto_synonyms || []).length > 0 ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
                      {(planDebug.auto_synonyms || []).map((s) => (
                        <span
                          key={s}
                          className="rounded-md bg-emerald-50 px-2 py-0.5 font-mono font-semibold text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <div className="text-[var(--ink-muted)]">本次查询未触发同义词扩展</div>
                  )}
                </div>

                {/* 4-Layer Provenance Chain */}
                <div className="border-t border-[var(--border-subtle)] pt-2.5">
                  <div className="mb-1.5 flex items-center justify-between font-semibold text-[var(--ink-secondary)]">
                    <span>四层来源链</span>
                    <span className="font-mono font-normal text-[var(--ink-muted)]">
                      {activeHit ? `L${activeHit.layer} 命中` : "待命"}
                    </span>
                  </div>
                  <div className="divide-y divide-[var(--border-subtle)] overflow-hidden rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)]">
                    {[
                      {
                        lvl: 1,
                        name: "原始文档",
                        desc:
                          activeHit?.layer === 1
                            ? activeHit.source
                            : activeVault?.storage_mode === "managed"
                              ? `${activeVault.sources_path || activeVault.sources_dir || "原文档"}（只读映射）`
                              : `${activeVault?.sources_dir || "原文档"}/ 原地解析`,
                      },
                      {
                        lvl: 2,
                        name: "结构切片",
                        desc:
                          activeHit?.layer === 2
                            ? activeHit.source
                            : `FTS5 BM25 索引 (${activeVault?.sections ?? 0} 段)`,
                      },
                      {
                        lvl: 3,
                        name: "AI 整理摘要",
                        desc:
                          activeHit?.layer === 3
                            ? activeHit.source
                            : shadowBlocked.some((x) => x.reason_code === "wiki_provenance_blocked")
                              ? "含无权访问的原文档，已连带隐藏"
                              : "带来源校验的 AI 整理文档",
                      },
                      {
                        lvl: 4,
                        name: "人工补充与纠错",
                        desc:
                          activeHit?.layer === 4
                            ? activeHit.title
                            : `活跃记录 ${activeVault?.active_memory_notes ?? 0} 条`,
                      },
                    ].map((step) => {
                      const on = activeHit?.layer === step.lvl;
                      return (
                        <div key={step.lvl} className="flex items-start gap-2 px-2.5 py-1.5">
                          <span
                            className={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded font-disp text-[11px] font-bold ${
                              on
                                ? "bg-[var(--accent)] text-white"
                                : "bg-[var(--bg-muted)] text-[var(--ink-secondary)]"
                            }`}
                          >
                            {step.lvl}
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="font-semibold text-[var(--ink-primary)]">
                              {step.name}
                            </div>
                            <div className="truncate font-mono text-[11px] text-[var(--ink-muted)]">
                              {step.desc}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* ACL Rule Details */}
                {shadowBlocked.length > 0 && (
                  <div className="border-t border-[var(--border-subtle)] pt-2.5">
                    <div className="mb-1.5 font-semibold text-[var(--ink-secondary)]">
                      权限过滤原始规则
                    </div>
                    <div className="space-y-1">
                      {shadowBlocked.slice(0, 6).map((b, i) => (
                        <div
                          key={i}
                          className="rounded border border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-2 font-mono text-[11px]"
                        >
                          <div className="truncate font-semibold text-[var(--ink-primary)]">
                            {b.source}
                          </div>
                          <div className="text-[var(--ink-muted)]">
                            {b.reason_code} ({b.matched_rule})
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div className="flex justify-end border-t border-[var(--border-subtle)] pt-2">
                  <button
                    type="button"
                    disabled={!resp}
                    onClick={() => void handleCopyJson()}
                    className="inline-flex items-center gap-1 font-semibold text-[var(--accent)] hover:underline disabled:opacity-40"
                  >
                    {copiedJson ? (
                      <Check className="h-3.5 w-3.5" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                    {copiedJson ? "已复制" : "复制原始 JSON"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
};
