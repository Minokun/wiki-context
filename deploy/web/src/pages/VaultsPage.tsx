import React, { useEffect, useMemo, useState } from "react";
import {
  Archive,
  Check,
  ChevronDown,
  ChevronRight,
  Edit3,
  Eye,
  FolderInput,
  FolderOpen,
  FolderTree,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { BrandGlyph } from "../components/BrandIcons";
import { FolderPickerModal } from "../components/FolderPickerModal";
import {
  isTauriRuntime,
  pickNativeDirectory,
  subscribeNativeFileDrop,
} from "../platform/adapter";
import type {
  AsyncJobRecord,
  LlmCompilerStatus,
  LogStatsResponse,
  MemoryItem,
  VaultInfo,
} from "../types";

interface VaultsPageProps {
  vaults: VaultInfo[];
  activeVaultId: string;
  onSelectVault: (vaultId: string) => void;
  llmCompiler: LlmCompilerStatus | null;
  stats: LogStatsResponse | null;
  compileJobs: Record<string, AsyncJobRecord>;
  onTrackCompileJob: (vaultId: string, job: AsyncJobRecord) => void;
  openOnboardSignal?: number;
  onOpenAgentConfig: () => void;
  onRefresh: () => void;
  onToast: (kind: "success" | "error" | "info", msg: string) => void;
  onOpenPlayground: (vaultId: string, query?: string) => void;
}

type VaultSubTab = "structure" | "memory" | "synonyms";

function formatCompilePhase(phase?: string): string {
  switch (phase) {
    case "planning":
      return "正在扫描新增文档";
    case "compiling_new":
      return "AI 正在整理新文档";
    case "refreshing_stale":
      return "正在更新变更内容";
    case "consolidating_notes":
      return "正在合并补充说明";
    case "rebuilding_index":
      return "正在更新搜索索引";
    default:
      return "正在自动整理";
  }
}

const MEMORY_KIND_LABELS: Record<string, string> = {
  errata: "纠错",
  decision: "决策",
  insight: "经验",
  glossary: "术语",
};

export const VaultsPage: React.FC<VaultsPageProps> = ({
  vaults,
  activeVaultId,
  onSelectVault,
  llmCompiler,
  stats,
  compileJobs,
  onTrackCompileJob,
  openOnboardSignal = 0,
  onOpenAgentConfig,
  onRefresh,
  onToast,
  onOpenPlayground,
}) => {
  const activeVault = vaults.find((v) => v.id === activeVaultId) || vaults[0] || null;
  const [subTab, setSubTab] = useState<VaultSubTab>("structure");

  // Picker mode: "onboard" (new vault) vs "import" (import folder into active vault)
  const [pickerMode, setPickerMode] = useState<"onboard" | "import">("onboard");
  const [pickerOpen, setPickerOpen] = useState(false);

  // Onboarding state
  const [selectedPath, setSelectedPath] = useState("");
  const [inspected, setInspected] = useState<{
    kind: string;
    suggested_name: string;
    suggested_id: string;
    detected_layout: string;
    file_count: number;
    warnings?: string[];
  } | null>(null);
  const [vaultIdInput, setVaultIdInput] = useState("");
  const [vaultNameInput, setVaultNameInput] = useState("");
  const [layoutInput, setLayoutInput] = useState("zh");
  const [onboardAutoCompile, setOnboardAutoCompile] = useState(false);
  const [onboarding, setOnboarding] = useState(false);

  // Sync, Plan Preview, Lint & Rename state
  const [syncingJobs, setSyncingJobs] = useState<Record<string, string>>({});
  const [compilePlan, setCompilePlan] = useState<{
    vaultId: string;
    uncompiled_sources: string[];
    stale_pages: string[];
  } | null>(null);
  const [queueOpen, setQueueOpen] = useState(false);
  const [lintReport, setLintReport] = useState<{
    vaultId: string;
    ok: boolean;
    error_count: number;
    warning_count: number;
    errors: { code: string; file: string; detail: string }[];
  } | null>(null);
  const [renameTarget, setRenameTarget] = useState<{ oldId: string; newId: string } | null>(null);

  // Collapsible directory preview in VaultsPage
  const [expandedDirs, setExpandedDirs] = useState<Record<string, boolean>>({});

  // Collapsible technical metadata in right maintenance drawer
  const [showMeta, setShowMeta] = useState(false);

  // Synonyms state
  const [synGroups, setSynGroups] = useState<{ canonical: string; aliases: string }[]>([]);
  const [synLoading, setSynLoading] = useState(false);

  // Layer-4 Memory state
  const [memItems, setMemItems] = useState<MemoryItem[]>([]);
  const [memStatusFilter, setMemStatusFilter] = useState("active");
  const [memKindFilter, setMemKindFilter] = useState("all");
  const [memQ, setMemQ] = useState("");
  const [newKind, setNewKind] = useState<"errata" | "insight" | "decision" | "glossary">("errata");
  const [newText, setNewText] = useState("");
  const [newSources, setNewSources] = useState("");
  const [newTags, setNewTags] = useState("");

  const activePendingCount = activeVault
    ? (activeVault.uncompiled_count || 0) + (activeVault.stale_count || 0)
    : 0;

  // Load pending compilation file list for active local vault when pending > 0
  useEffect(() => {
    if (!activeVault || activeVault.kind !== "local" || activePendingCount === 0) {
      setCompilePlan(null);
      return;
    }
    let cancelled = false;
    void api
      .getCompilePlan(activeVault.id)
      .then((res) => {
        if (!cancelled) {
          setCompilePlan({
            vaultId: activeVault.id,
            uncompiled_sources: res.uncompiled_sources || [],
            stale_pages: res.stale_pages || [],
          });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [activeVault?.id, activeVault?.generation_id, activePendingCount]);

  const handleStartCompile = async (vid: string) => {
    if (!llmCompiler?.ready) {
      onOpenAgentConfig();
      onToast("info", "请先填写大模型 API Key");
      return;
    }
    try {
      const job = await api.compileVaultAsync(vid);
      if (job.job_id && job.status === "running") {
        onTrackCompileJob(vid, job);
        onToast("info", "已开始用 AI 整理新增文档，完成后会自动更新索引");
      } else {
        onToast("info", "当前没有需要整理的新增或变更文档");
        onRefresh();
      }
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const inspectSelectedPath = async (p: string) => {
    setSelectedPath(p);
    setSubTab("structure");
    try {
      const info = await api.fsInspect(p);
      setInspected(info);
      setVaultIdInput(info.suggested_id);
      setVaultNameInput(info.suggested_name);
      setLayoutInput(info.detected_layout || "zh");
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleImportExternalFolder = async (folderPath: string) => {
    if (!activeVault) return;
    try {
      const res = await api.importFolder({
        vault_id: activeVault.id,
        external_folder: folderPath,
      });
      if (res.compile_job?.job_id && res.compile_job.status === "running") {
        onTrackCompileJob(activeVault.id, res.compile_job);
        onToast("success", `已导入 ${res.copied_count} 个文件，正在后台整理摘要并更新索引`);
      } else {
        onToast("success", `已导入 ${res.copied_count} 个文件并更新搜索索引`);
      }
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (openOnboardSignal > 0) {
      void chooseFolder("onboard");
    }
  }, [openOnboardSignal]);

  useEffect(() => {
    let unlisten: (() => void) | null = null;
    void subscribeNativeFileDrop((paths) => {
      if (paths[0]) {
        void inspectSelectedPath(paths[0]);
        onToast("info", `已识别拖入目录: ${paths[0]}`);
      }
    }).then((fn) => {
      unlisten = fn;
    });
    return () => {
      unlisten?.();
    };
  }, []);

  const chooseFolder = async (mode: "onboard" | "import" = "onboard") => {
    setPickerMode(mode);
    if (isTauriRuntime()) {
      const p = await pickNativeDirectory(
        mode === "import" ? "选择要导入到当前知识库的文件夹" : "选择要添加的本地文件夹",
      );
      if (p) {
        if (mode === "import") {
          await handleImportExternalFolder(p);
        } else {
          await inspectSelectedPath(p);
        }
      }
    } else {
      setPickerOpen(true);
    }
  };

  const handleOnboard = async () => {
    if (!selectedPath) return;
    setOnboarding(true);
    try {
      const res = await api.onboardVault({
        path: selectedPath,
        vault_id: vaultIdInput.trim(),
        name: vaultNameInput.trim(),
        layout: layoutInput,
        auto_compile: onboardAutoCompile,
      });

      if (res.compile_job?.job_id && res.compile_job.status === "running") {
        onTrackCompileJob(res.vault_id, res.compile_job);
        onToast("success", `知识库已添加并建立基础索引，正在后台自动整理内容摘要`);
      } else if (onboardAutoCompile && !res.llm_ready && (res.uncompiled_count || 0) > 0) {
        onToast(
          "info",
          `知识库已添加（含 ${res.uncompiled_count} 篇待整理文档，配置顶部整理模型后即可自动整理）`,
        );
      } else {
        onToast("success", "知识库已添加并建立搜索索引");
      }

      setSelectedPath("");
      setInspected(null);
      onRefresh();
      onSelectVault(res.vault_id);
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setOnboarding(false);
    }
  };

  const handleAsyncSync = async (vid: string, force = false) => {
    try {
      const job = await api.syncVaultAsync(vid, force);
      if (!job.job_id) {
        onRefresh();
        return;
      }
      setSyncingJobs((prev) => ({ ...prev, [vid]: job.job_id }));
      const poll = async () => {
        try {
          const st = await api.getJob(job.job_id);
          if (st.status === "running") {
            setTimeout(() => void poll(), 400);
            return;
          }
          setSyncingJobs((prev) => {
            const nxt = { ...prev };
            delete nxt[vid];
            return nxt;
          });
          if (st.status === "done") {
            const cjob = (st.result as { compile_job?: AsyncJobRecord } | undefined)?.compile_job;
            if (cjob?.job_id && cjob.status === "running") {
              onTrackCompileJob(vid, cjob);
              onToast("info", "已扫描到文档更新，正在后台整理并刷新索引");
            } else {
              onToast("success", "文档与搜索索引更新完成");
            }
            onRefresh();
          } else {
            onToast("error", st.error || "更新失败");
          }
        } catch (err) {
          setSyncingJobs((prev) => {
            const nxt = { ...prev };
            delete nxt[vid];
            return nxt;
          });
          onToast("error", err instanceof Error ? err.message : String(err));
        }
      };
      setTimeout(() => void poll(), 350);
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleToggleWatch = async (v: VaultInfo) => {
    try {
      const res = await api.watchVault(v.id, !v.watching);
      onToast(
        "info",
        res.watching
          ? `已开启「${v.name}」自动监听，文件夹有变动时会自动更新索引`
          : `已关闭「${v.name}」自动监听`,
      );
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleLint = async (vid: string) => {
    try {
      const rep = await api.lintVault(vid);
      setLintReport({
        vaultId: vid,
        ok: rep.ok,
        error_count: rep.error_count,
        warning_count: rep.warning_count,
        errors: rep.errors || [],
      });
      if (rep.ok) {
        onToast("success", `文档完整性检查通过（0 个问题，${rep.warning_count} 条提醒）`);
      } else {
        onToast("error", `检查发现 ${rep.error_count} 处引用异常`);
      }
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleRename = async () => {
    if (!renameTarget || !renameTarget.newId.trim()) return;
    try {
      const res = await api.renameVault(renameTarget.oldId, renameTarget.newId.trim(), true);
      onToast("success", `已更新标识为 '${res.new_id}'，并同步更新了已连接的 AI 工具`);
      setRenameTarget(null);
      onRefresh();
      onSelectVault(res.new_id);
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  // Load synonyms when switching to synonyms tab
  const loadSynonyms = async (vid: string) => {
    setSynLoading(true);
    try {
      const res = await api.getSynonyms(vid);
      setSynGroups(
        (res.groups || []).map((g) => ({
          canonical: g.canonical.join(", "),
          aliases: g.aliases.join(", "),
        })),
      );
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setSynLoading(false);
    }
  };

  const saveSynonyms = async () => {
    if (!activeVault) return;
    try {
      const payload = synGroups
        .map((g) => ({
          canonical: g.canonical
            .split(/[,，]/)
            .map((s) => s.trim())
            .filter(Boolean),
          aliases: g.aliases
            .split(/[,，]/)
            .map((s) => s.trim())
            .filter(Boolean),
        }))
        .filter((g) => g.canonical.length > 0 && g.aliases.length > 0);
      const res = await api.saveSynonyms(activeVault.id, payload);
      onToast("success", `已保存 ${res.count} 组同义词，搜索时将立即生效`);
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  // Load Layer-4 Memory when switching to memory tab
  const loadMemory = async () => {
    if (!activeVault || !activeVault.capabilities.can_edit_memory) {
      setMemItems([]);
      return;
    }
    try {
      const res = await api.listMemory(activeVault.id, memStatusFilter, memKindFilter, memQ);
      setMemItems(
        Array.isArray(res.items) ? res.items : Array.isArray(res.notes) ? res.notes : [],
      );
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (!activeVault) return;
    if (subTab === "synonyms" && activeVault.capabilities.can_edit_synonyms) {
      void loadSynonyms(activeVault.id);
    } else if (subTab === "memory") {
      void loadMemory();
    }
  }, [activeVault?.id, subTab, memStatusFilter, memKindFilter]);

  const handleCreateMemory = async () => {
    if (!activeVault || !newText.trim() || !activeVault.capabilities.can_edit_memory) return;
    try {
      await api.createMemory({
        vault_id: activeVault.id,
        kind: newKind,
        text: newText.trim(),
        related_sources: newSources
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
        tags: newTags
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
      });
      setNewText("");
      setNewSources("");
      setNewTags("");
      onToast("success", "已保存补充说明");
      await loadMemory();
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  // Vault Call Distribution metrics
  const vaultCallMap = useMemo(() => {
    const m: Record<string, number> = {};
    for (const row of stats?.by_vault || []) {
      m[row.vault_id] = row.count;
    }
    return m;
  }, [stats]);

  const totalVaultCalls = Math.max(1, stats?.total_calls || 0);
  const activeVaultCalls = activeVault ? vaultCallMap[activeVault.id] || 0 : 0;

  const activeCompileJob = activeVault
    ? compileJobs[activeVault.id] || activeVault.compile_job || null
    : null;

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden lg:flex-row">
      {/* Center Stage */}
      <main className="flex min-w-0 flex-1 flex-col overflow-y-auto bg-[var(--bg-surface)]">
        {/* Stage Header */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-default)] px-5 py-3.5">
          <div className="flex items-center gap-2.5">
            <BrandGlyph
              brand={activeVault?.kind === "remote" ? "tailscale" : "obsidian"}
              className="h-5 w-5"
            />
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-[15px] font-bold text-[var(--ink-primary)]">
                  {activeVault ? activeVault.name : "我的知识库"}
                </h1>
                {activeVault && (
                  <span
                    className={`rounded-md px-2 py-0.5 text-xs font-semibold ${
                      activeVault.kind === "remote"
                        ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                        : activeVault.ready
                          ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                          : "bg-amber-50 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                    }`}
                  >
                    {activeVault.kind === "remote"
                      ? "共享知识库"
                      : activeVault.ready
                        ? "就绪"
                        : "正在建立索引"}
                  </span>
                )}
              </div>
              <p className="font-mono text-xs text-[var(--ink-muted)]">
                {activeVault?.kind === "remote"
                  ? activeVault.gateway_url || activeVault.mcp_url
                  : activeVault?.path || "点击「添加文件夹」即可接入本地 Markdown、PDF、Word、PPT 等文档"}
              </p>
            </div>
          </div>
        </div>

        <div className="space-y-4 p-5">
          {/* Vault Content Overview */}
          {activeVault && (
            <section className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-4 py-2.5 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="h-2 w-2 rounded-full bg-[var(--chart-http)]" />
                  <span className="font-semibold text-[var(--ink-primary)]">资料概览</span>
                  {activeVault.kind === "local" && activePendingCount > 0 && !activeCompileJob && (
                    <span className="rounded-md bg-amber-100 px-2 py-0.5 font-semibold text-amber-900 dark:bg-amber-950/60 dark:text-amber-300">
                      {activePendingCount} 篇待整理
                    </span>
                  )}
                </div>
                <span className="text-[var(--ink-secondary)]">
                  近 24 小时被 AI 查询 {activeVaultCalls} 次
                </span>
              </div>

              {/* Live AI Compiler Progress Banner */}
              {activeCompileJob && activeCompileJob.status === "running" && (
                <div className="border-b border-[var(--border-subtle)] bg-[var(--accent-soft)]/70 px-4 py-2.5 text-xs">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="flex items-center gap-2 font-semibold text-[var(--accent)]">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      {formatCompilePhase(activeCompileJob.progress?.phase)}
                      {activeCompileJob.progress?.current
                        ? ` (${activeCompileJob.progress.current})`
                        : ""}
                    </span>
                    <span className="font-mono font-semibold text-[var(--ink-secondary)]">
                      {activeCompileJob.progress?.done ?? 0} /{" "}
                      {Math.max(1, activeCompileJob.progress?.total ?? 1)}
                      {(activeCompileJob.progress?.pending_total ?? activePendingCount) >
                        (activeCompileJob.progress?.total ?? 0) && (
                        <span className="ml-2 font-sans font-normal text-[var(--ink-muted)]">
                          本批上限{" "}
                          {activeCompileJob.progress?.batch_limit ||
                            llmCompiler?.max_items_per_run ||
                            15}{" "}
                          篇 · 共 {activeCompileJob.progress?.pending_total ?? activePendingCount}{" "}
                          篇待整理
                        </span>
                      )}
                    </span>
                  </div>
                  <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-[var(--bg-muted)]">
                    <div
                      className="h-full rounded-full bg-[var(--accent)] transition-all duration-300"
                      style={{
                        width: `${Math.min(
                          100,
                          Math.max(
                            8,
                            Math.round(
                              ((activeCompileJob.progress?.done ?? 0) /
                                Math.max(1, activeCompileJob.progress?.total ?? 1)) *
                                100,
                            ),
                          ),
                        )}%`,
                      }}
                    />
                  </div>
                </div>
              )}

              <div className="grid grid-cols-2 divide-x divide-y divide-[var(--border-subtle)] sm:grid-cols-4 sm:divide-y-0">
                <div className="p-3.5">
                  <div className="text-xs text-[var(--ink-muted)]">原始文档</div>
                  <div className="mt-1 font-disp text-2xl font-extrabold text-[var(--ink-primary)]">
                    {activeVault.docs}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-[var(--ink-muted)]">
                    {activeVault.sources_dir || "原文档"} 文件夹
                  </div>
                </div>

                <div className="p-3.5">
                  <div className="text-xs text-[var(--ink-muted)]">AI 整理摘要</div>
                  <div className="mt-1 font-disp text-2xl font-extrabold text-[var(--ink-primary)]">
                    {activeVault.wiki_docs}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-[var(--ink-muted)]">
                    {activeVault.uncompiled_count
                      ? `${activeVault.uncompiled_count} 篇待整理`
                      : "已全部同步"}
                  </div>
                </div>

                <div className="p-3.5">
                  <div className="text-xs text-[var(--ink-muted)]">人工补充</div>
                  <div className="mt-1 font-disp text-2xl font-extrabold text-[var(--ink-primary)]">
                    {activeVault.active_memory_notes}
                  </div>
                  <div className="mt-0.5 text-xs text-[var(--ink-muted)]">纠错与经验备注</div>
                </div>

                <div className="p-3.5">
                  <div className="text-xs text-[var(--ink-muted)]">可检索段落</div>
                  <div className="mt-1 font-disp text-2xl font-extrabold text-[var(--chart-http)]">
                    {activeVault.sections}
                  </div>
                  <div className="mt-0.5 text-xs text-[var(--ink-muted)]">已建索引，支持快速搜索</div>
                </div>
              </div>

              {/* Cross-Vault Call Share Bar (only when multiple vaults have queries) */}
              {vaults.length > 1 && (stats?.total_calls ?? 0) > 0 && (
                <div className="border-t border-[var(--border-subtle)] bg-[var(--chart-plot-bg)] px-4 py-2.5">
                  <div className="mb-1.5 flex items-center justify-between text-xs text-[var(--ink-muted)]">
                    <span>各知识库近 24 小时查询占比</span>
                    <span className="font-mono">总计 {stats?.total_calls ?? 0} 次</span>
                  </div>
                  <div className="flex h-2 w-full overflow-hidden rounded-full bg-[var(--bg-muted)]">
                    {vaults.map((v, idx) => {
                      const cnt = vaultCallMap[v.id] || 0;
                      const pct = (cnt / totalVaultCalls) * 100;
                      const palette = ["#1A56DB", "#0891B2", "#D97706", "#10B981"];
                      return (
                        <div
                          key={v.id}
                          style={{
                            width: `${Math.max(pct, cnt > 0 ? 4 : 0)}%`,
                            backgroundColor: palette[idx % palette.length],
                          }}
                          title={`${v.name}: ${cnt} 次`}
                        />
                      );
                    })}
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-4 text-xs text-[var(--ink-secondary)]">
                    {vaults.map((v, idx) => {
                      const palette = ["#1A56DB", "#0891B2", "#D97706", "#10B981"];
                      return (
                        <span key={v.id} className="inline-flex items-center gap-1.5">
                          <span
                            className="h-2 w-2 rounded-full"
                            style={{ backgroundColor: palette[idx % palette.length] }}
                          />
                          <span className="font-semibold text-[var(--ink-primary)]">{v.name}</span>
                          <span className="font-mono text-[var(--ink-muted)]">
                            {vaultCallMap[v.id] || 0}
                          </span>
                        </span>
                      );
                    })}
                  </div>
                </div>
              )}
            </section>
          )}

          {/* Vault-Scoped Sub-Navigation Bar */}
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] pb-3">
            <div className="flex flex-wrap items-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-muted)] p-0.5">
              {(
                [
                  { id: "structure", label: "文件夹与目录" },
                  { id: "memory", label: `补充与纠错 (${activeVault?.active_memory_notes ?? 0})` },
                  { id: "synonyms", label: "同义词表" },
                ] as { id: VaultSubTab; label: string }[]
              ).map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setSubTab(t.id)}
                  className={`inline-flex items-center gap-1.5 rounded-[6px] px-3 py-1 text-xs font-semibold ${
                    subTab === t.id
                      ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs"
                      : "bg-transparent text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                  }`}
                >
                  <span>{t.label}</span>
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={() => void chooseFolder("onboard")}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)]"
            >
              <FolderOpen className="h-3.5 w-3.5" /> 添加文件夹
            </button>
          </div>

          {/* Pending Onboard Inspector Banner */}
          {selectedPath && inspected && (
            <div className="rounded-xl border border-[var(--accent)]/45 bg-[var(--accent-soft)]/55 p-4">
              <div className="flex items-center justify-between">
                <div className="text-[14px] font-bold text-[var(--ink-primary)]">
                  添加本地文件夹（检测到 {inspected.file_count} 个文档）
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedPath("");
                    setInspected(null);
                  }}
                  className="rounded p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-surface)]"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <div className="mt-1 truncate font-mono text-xs text-[var(--ink-secondary)]">
                {selectedPath}
              </div>

              <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--ink-secondary)]">
                    显示名称
                  </label>
                  <input
                    value={vaultNameInput}
                    onChange={(e) => setVaultNameInput(e.target.value)}
                    className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--ink-secondary)]">
                    标识名 (英文 ID)
                  </label>
                  <input
                    value={vaultIdInput}
                    onChange={(e) => setVaultIdInput(e.target.value)}
                    className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-[var(--ink-secondary)]">
                    内部目录命名
                  </label>
                  <select
                    value={layoutInput}
                    onChange={(e) => setLayoutInput(e.target.value)}
                    className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs text-[var(--ink-primary)]"
                  >
                    <option value="zh">中文目录（推荐）</option>
                    <option value="en">英文目录</option>
                  </select>
                </div>
              </div>

              {/* Global Agent Auto-Compile Link */}
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] px-3.5 py-2.5 text-xs">
                <label className="flex cursor-pointer items-center gap-2 font-semibold text-[var(--ink-primary)]">
                  <input
                    type="checkbox"
                    checked={onboardAutoCompile}
                    onChange={(e) => setOnboardAutoCompile(e.target.checked)}
                  />
                  <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
                  添加后自动用大模型整理文档并更新索引
                </label>

                {llmCompiler?.ready ? (
                  <button
                    type="button"
                    onClick={onOpenAgentConfig}
                    className="rounded-md bg-emerald-50 px-2 py-0.5 font-mono text-emerald-700 hover:underline dark:bg-emerald-950/60 dark:text-emerald-300"
                  >
                    当前模型: {llmCompiler.model}
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={onOpenAgentConfig}
                    className="rounded-md bg-amber-100 px-2.5 py-0.5 font-semibold text-amber-900 hover:underline dark:bg-amber-950/60 dark:text-amber-300"
                  >
                    配置整理模型
                  </button>
                )}
              </div>

              <div className="mt-3 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setSelectedPath("");
                    setInspected(null);
                  }}
                  className="rounded-lg px-3 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-surface)]"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={onboarding}
                  onClick={() => void handleOnboard()}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
                >
                  {onboarding ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Check className="h-3.5 w-3.5" />
                  )}
                  {onboardAutoCompile && llmCompiler?.ready ? "确认添加并整理" : "确认添加"}
                </button>
              </div>
            </div>
          )}

          {/* SUB-TAB 1: STRUCTURE & VAULT ROSTER */}
          {subTab === "structure" && (
            <div className="space-y-5">
              {/* All Registered Vaults Roster */}
              <div>
                <div className="mb-2.5 flex items-center justify-between">
                  <h2 className="text-[13px] font-bold text-[var(--ink-primary)]">全部知识库</h2>
                  <span className="text-xs text-[var(--ink-muted)]">
                    共 {vaults.length} 个（支持直接拖入文件夹添加）
                  </span>
                </div>
                <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
                  {vaults.map((v) => {
                    const isSyncing = Boolean(syncingJobs[v.id]);
                    const vCompileJob = compileJobs[v.id] || v.compile_job;
                    const isCompiling = vCompileJob?.status === "running";
                    const pendingCnt = (v.uncompiled_count || 0) + (v.stale_count || 0);
                    const selected = v.id === activeVault?.id;
                    return (
                      <div
                        key={v.id}
                        onClick={() => onSelectVault(v.id)}
                        className={`cursor-pointer rounded-xl border p-4 ${
                          selected
                            ? "border-[var(--accent)] bg-[var(--accent-soft)]/45"
                            : "border-[var(--border-default)] bg-[var(--bg-subtle)] hover:border-[var(--border-strong)] hover:bg-[var(--bg-surface)]"
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="truncate text-[14px] font-bold text-[var(--ink-primary)]">
                                {v.name}
                              </span>
                              {isCompiling ? (
                                <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded bg-[var(--accent-soft)] px-1.5 py-0.5 text-xs font-semibold text-[var(--accent)]">
                                  <Loader2 className="h-3 w-3 animate-spin" />
                                  整理中
                                </span>
                              ) : (
                                v.kind === "local" &&
                                pendingCnt > 0 && (
                                  <span className="shrink-0 whitespace-nowrap rounded bg-amber-100 px-1.5 py-0.5 font-mono text-xs font-semibold text-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
                                    待整理 {pendingCnt}
                                  </span>
                                )
                              )}
                            </div>
                            <div className="mt-1 truncate font-mono text-xs text-[var(--ink-muted)]">
                              {v.kind === "remote" ? v.gateway_url : v.path}
                            </div>
                          </div>
                          <span
                            className={`shrink-0 rounded-md px-2 py-0.5 text-xs font-medium ${
                              v.kind === "remote"
                                ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                                : v.ready
                                  ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                                  : "bg-amber-50 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                            }`}
                          >
                            {v.kind === "remote" ? "共享库" : `${v.docs} 篇文档 · ${v.wiki_docs} 篇整理`}
                          </span>
                        </div>

                        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-subtle)] pt-2.5 text-xs">
                          <div
                            className="flex flex-wrap items-center gap-1.5"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {v.capabilities.can_sync && (
                              <button
                                type="button"
                                disabled={isSyncing}
                                onClick={() => void handleAsyncSync(v.id, false)}
                                className="inline-flex items-center gap-1 rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2.5 py-1 font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)] disabled:opacity-50"
                              >
                                <RefreshCw
                                  className={`h-3 w-3 ${isSyncing ? "animate-spin text-[var(--accent)]" : ""}`}
                                />
                                {isSyncing ? "更新中" : "更新索引"}
                              </button>
                            )}
                            {v.capabilities.can_watch && (
                              <button
                                type="button"
                                onClick={() => void handleToggleWatch(v)}
                                className={`inline-flex items-center gap-1 rounded-md border px-2.5 py-1 font-medium ${
                                  v.watching
                                    ? "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300"
                                    : "border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                                }`}
                              >
                                <Eye className="h-3 w-3" />
                                {v.watching ? "自动监听中" : "开启自动监听"}
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => onOpenPlayground(v.id)}
                              className="rounded-md bg-[var(--accent-soft)] px-2.5 py-1 font-semibold text-[var(--accent)] hover:opacity-90"
                            >
                              去检索测试
                            </button>
                          </div>

                          <div
                            className="flex items-center gap-1"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <button
                              type="button"
                              onClick={() => setRenameTarget({ oldId: v.id, newId: v.id })}
                              className="rounded p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-surface)] hover:text-[var(--ink-primary)]"
                              title="重命名"
                            >
                              <Edit3 className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                void api.removeVault(v.id).then(() => {
                                  onToast(
                                    "info",
                                    `已移除「${v.name}」（本地原文件不会被删除）`,
                                  );
                                  onRefresh();
                                });
                              }}
                              className="rounded p-1 text-[var(--ink-muted)] hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-950/50 dark:hover:text-rose-400"
                              title="移除知识库"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Active Vault Collapsible 2-Level Directory Tree */}
              {activeVault && activeVault.kind === "local" && (
                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <h2 className="flex items-center gap-1.5 text-[13px] font-bold text-[var(--ink-primary)]">
                      <FolderTree className="h-4 w-4 text-[var(--accent)]" />
                      {activeVault.name} 文件夹目录
                    </h2>
                    <span className="text-xs text-[var(--ink-muted)]">
                      点击文件夹可展开子目录或测试搜索
                    </span>
                  </div>
                  {(activeVault.folder_tree || []).length === 0 ? (
                    <div className="rounded-xl border border-dashed border-[var(--border-strong)] p-6 text-center text-xs text-[var(--ink-muted)]">
                      当前知识库下还没有子文件夹
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
                      {(activeVault.folder_tree || []).map((node) => {
                        const dKey = `${activeVault.id}:${node.prefix}`;
                        const hasSub = node.children.length > 0;
                        const isOpen = Boolean(expandedDirs[dKey]);
                        return (
                          <div
                            key={node.prefix}
                            className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-3 text-xs"
                          >
                            <div className="flex items-center justify-between gap-2">
                              {hasSub ? (
                                <button
                                  type="button"
                                  onClick={() =>
                                    setExpandedDirs((prev) => ({ ...prev, [dKey]: !prev[dKey] }))
                                  }
                                  className="flex min-w-0 items-center gap-1.5 text-left font-mono font-semibold text-[var(--ink-primary)]"
                                >
                                  {isOpen ? (
                                    <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
                                  ) : (
                                    <ChevronRight className="h-3.5 w-3.5 shrink-0 text-[var(--ink-muted)]" />
                                  )}
                                  <span className="truncate">{node.name}/</span>
                                  <span className="rounded bg-[var(--bg-muted)] px-1.5 py-0.5 text-xs text-[var(--ink-muted)]">
                                    {node.children.length}
                                  </span>
                                </button>
                              ) : (
                                <span className="truncate font-mono font-semibold text-[var(--ink-primary)]">
                                  {node.name}/
                                </span>
                              )}

                              <button
                                type="button"
                                onClick={() => onOpenPlayground(activeVault.id, node.name)}
                                className="shrink-0 font-semibold text-[var(--accent)] hover:underline"
                              >
                                试搜
                              </button>
                            </div>
                            {hasSub && isOpen && (
                              <div className="mt-2 flex flex-wrap gap-1.5 border-t border-[var(--border-subtle)] pt-2">
                                {node.children.map((ch) => (
                                  <button
                                    key={ch.prefix}
                                    type="button"
                                    onClick={() => onOpenPlayground(activeVault.id, ch.name)}
                                    className="rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-0.5 font-mono text-xs text-[var(--ink-secondary)] hover:border-[var(--accent)] hover:text-[var(--accent)]"
                                  >
                                    {ch.name}/
                                  </button>
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
          )}

          {/* SUB-TAB 2: ERRATA & NOTES */}
          {subTab === "memory" && (
            <div className="space-y-4">
              {!activeVault?.capabilities.can_edit_memory ? (
                <div className="flex items-center gap-2 rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-5 text-xs text-[var(--ink-secondary)]">
                  <Lock className="h-4 w-4 text-[var(--ink-muted)]" />
                  <span>共享知识库为只读模式，请在共享该知识库的电脑上添加或修改补充说明。</span>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-5 xl:grid-cols-12">
                  {/* Write Memory Card */}
                  <div className="space-y-3 rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-4 text-xs xl:col-span-5">
                    <div className="font-bold text-[var(--ink-primary)]">新增补充说明 / 纠错</div>
                    <select
                      value={newKind}
                      onChange={(e) =>
                        setNewKind(
                          e.target.value as "errata" | "insight" | "decision" | "glossary",
                        )
                      }
                      className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs text-[var(--ink-primary)]"
                    >
                      <option value="errata">纠错提醒（搜索时优先展示）</option>
                      <option value="decision">重要结论 / 决策</option>
                      <option value="insight">经验与备忘</option>
                      <option value="glossary">专有名词解释</option>
                    </select>
                    <textarea
                      rows={4}
                      value={newText}
                      onChange={(e) => setNewText(e.target.value)}
                      placeholder="输入对文档的补充说明、最新变化或需要纠正的内容..."
                      className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] p-2.5 text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
                    />
                    <input
                      value={newSources}
                      onChange={(e) => setNewSources(e.target.value)}
                      placeholder="关联文档路径（可选，多个用逗号隔开）"
                      className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 font-mono text-xs text-[var(--ink-primary)]"
                    />
                    <input
                      value={newTags}
                      onChange={(e) => setNewTags(e.target.value)}
                      placeholder="标签（可选，逗号分隔）"
                      className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs text-[var(--ink-primary)]"
                    />
                    <button
                      type="button"
                      disabled={!newText.trim()}
                      onClick={() => void handleCreateMemory()}
                      className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-[var(--accent)] py-2 text-xs font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-40"
                    >
                      <Plus className="h-3.5 w-3.5" /> 保存补充
                    </button>
                  </div>

                  {/* Memory List */}
                  <div className="space-y-3 xl:col-span-7">
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <select
                        value={memStatusFilter}
                        onChange={(e) => setMemStatusFilter(e.target.value)}
                        className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                      >
                        <option value="active">生效中</option>
                        <option value="archived">已归档</option>
                        <option value="consolidated">已合并到整理</option>
                        <option value="all">全部状态</option>
                      </select>
                      <select
                        value={memKindFilter}
                        onChange={(e) => setMemKindFilter(e.target.value)}
                        className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                      >
                        <option value="all">全部类型</option>
                        <option value="errata">纠错提醒</option>
                        <option value="decision">结论决策</option>
                        <option value="insight">经验备忘</option>
                        <option value="glossary">名词解释</option>
                      </select>
                      <input
                        value={memQ}
                        onChange={(e) => setMemQ(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && void loadMemory()}
                        placeholder="按关键词筛选..."
                        className="min-w-[140px] flex-1 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                      />
                    </div>

                    {memItems.length === 0 ? (
                      <div className="rounded-xl border border-dashed border-[var(--border-strong)] p-8 text-center text-xs text-[var(--ink-muted)]">
                        当前筛选条件下暂无补充记录
                      </div>
                    ) : (
                      <div className="space-y-2.5">
                        {memItems.map((m) => (
                          <div
                            key={m.note_id}
                            title={m.note_id}
                            className={`rounded-xl border p-3.5 text-xs ${
                              m.kind === "errata"
                                ? "border-amber-300 bg-amber-50/50 dark:border-amber-700 dark:bg-amber-950/30"
                                : "border-[var(--border-default)] bg-[var(--bg-surface)]"
                            }`}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex items-center gap-2">
                                <span
                                  className={`rounded px-2 py-0.5 text-xs font-semibold ${
                                    m.kind === "errata"
                                      ? "bg-amber-100 text-amber-900 dark:bg-amber-900/60 dark:text-amber-200"
                                      : "bg-[var(--accent-soft)] text-[var(--accent)]"
                                  }`}
                                >
                                  {MEMORY_KIND_LABELS[m.kind] || m.kind}
                                </span>
                                <span className="text-xs text-[var(--ink-muted)]">
                                  {m.created_at}
                                </span>
                              </div>
                              <div className="flex items-center gap-1">
                                <button
                                  type="button"
                                  onClick={() =>
                                    void api
                                      .updateMemory({
                                        vault_id: activeVault.id,
                                        note_id: m.note_id,
                                        status: m.status === "archived" ? "active" : "archived",
                                      })
                                      .then(() => loadMemory())
                                  }
                                  className="rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-0.5 text-xs text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
                                >
                                  <Archive className="mr-1 inline h-3 w-3" />
                                  {m.status === "archived" ? "恢复" : "归档"}
                                </button>
                                <button
                                  type="button"
                                  onClick={() =>
                                    void api.deleteMemory(activeVault.id, m.note_id).then(() => {
                                      void loadMemory();
                                      onRefresh();
                                    })
                                  }
                                  className="rounded p-1 text-[var(--ink-muted)] hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-950/50 dark:hover:text-rose-400"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </button>
                              </div>
                            </div>
                            <p className="mt-2 text-[13px] leading-relaxed text-[var(--ink-primary)]">
                              {m.text}
                            </p>
                            {(m.related_sources || []).length > 0 && (
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {(m.related_sources || []).map((s) => (
                                  <span
                                    key={s}
                                    className="rounded border border-[var(--border-default)] bg-[var(--bg-subtle)] px-2 py-0.5 font-mono text-xs text-[var(--ink-secondary)]"
                                  >
                                    {s}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* SUB-TAB 3: SYNONYMS DICTIONARY */}
          {subTab === "synonyms" && (
            <div className="space-y-4">
              {!activeVault?.capabilities.can_edit_synonyms ? (
                <div className="flex items-center gap-2 rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-5 text-xs text-[var(--ink-secondary)]">
                  <Lock className="h-4 w-4 text-[var(--ink-muted)]" />
                  <span>共享知识库不支持直接修改同义词表。</span>
                </div>
              ) : (
                <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <div className="text-[13px] font-bold text-[var(--ink-primary)]">
                        同义词表（{activeVault.name}）
                      </div>
                      <p className="text-xs text-[var(--ink-muted)]">
                        把常见简称、俗称或英文缩写对应到文档里的正式说法，保存后立即生效。
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() =>
                          setSynGroups([...synGroups, { canonical: "", aliases: "" }])
                        }
                        className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
                      >
                        <Plus className="h-3.5 w-3.5" /> 添加一组
                      </button>
                      <button
                        type="button"
                        onClick={() => void saveSynonyms()}
                        className="inline-flex items-center gap-1 rounded-lg bg-[var(--accent)] px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)]"
                      >
                        <Check className="h-3.5 w-3.5" /> 保存
                      </button>
                    </div>
                  </div>

                  {synLoading ? (
                    <div className="py-10 text-center text-xs text-[var(--ink-muted)]">
                      正在加载同义词表...
                    </div>
                  ) : synGroups.length === 0 ? (
                    <div className="mt-4 rounded-xl border border-dashed border-[var(--border-strong)] bg-[var(--bg-surface)] p-6 text-center text-xs text-[var(--ink-muted)]">
                      暂无同义词，点击右上「添加一组」可把常用简称或缩写关联到正式名词。
                    </div>
                  ) : (
                    <div className="mt-4 space-y-2.5">
                      {synGroups.map((g, idx) => (
                        <div
                          key={idx}
                          className="grid grid-cols-1 gap-2 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] p-3 sm:grid-cols-11"
                        >
                          <div className="sm:col-span-5">
                            <label className="mb-1 block text-xs font-medium text-[var(--ink-muted)]">
                              文档中的正式说法（逗号分隔）
                            </label>
                            <input
                              value={g.canonical}
                              onChange={(e) => {
                                const nxt = [...synGroups];
                                nxt[idx] = { ...nxt[idx], canonical: e.target.value };
                                setSynGroups(nxt);
                              }}
                              placeholder="如 受电弓滑板"
                              className="w-full rounded-md border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                            />
                          </div>
                          <div className="sm:col-span-5">
                            <label className="mb-1 block text-xs font-medium text-[var(--ink-muted)]">
                              常见简称 / 别名 / 英文缩写
                            </label>
                            <input
                              value={g.aliases}
                              onChange={(e) => {
                                const nxt = [...synGroups];
                                nxt[idx] = { ...nxt[idx], aliases: e.target.value };
                                setSynGroups(nxt);
                              }}
                              placeholder="如 滑板条, Panto Slider"
                              className="w-full rounded-md border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                            />
                          </div>
                          <div className="flex items-end justify-end sm:col-span-1">
                            <button
                              type="button"
                              onClick={() =>
                                setSynGroups(synGroups.filter((_, i) => i !== idx))
                              }
                              className="rounded p-1.5 text-[var(--ink-muted)] hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-950/50 dark:hover:text-rose-400"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </main>

      {/* Right Inspector (356px) — Maintenance & Actions First, Tech Metadata Collapsed */}
      <aside className="flex w-full shrink-0 flex-col border-t border-[var(--border-default)] bg-[var(--bg-sidebar)] lg:w-[356px] lg:border-t-0 lg:border-l">
        <div className="border-b border-[var(--border-default)] px-4 py-3">
          <div className="text-xs font-semibold text-[var(--ink-muted)]">整理与维护</div>
          <div className="mt-1 truncate text-[14px] font-bold text-[var(--ink-primary)]">
            {activeVault ? activeVault.name : "未选择知识库"}
          </div>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
          {activeVault ? (
            <>
              {/* Primary Compile Action + Pending File Queue Preview */}
              {activeVault.capabilities.can_sync && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-[var(--ink-secondary)]">文档整理与索引</span>
                    <button
                      type="button"
                      onClick={onOpenAgentConfig}
                      className="font-mono text-xs text-[var(--accent)] hover:underline"
                    >
                      {llmCompiler?.ready ? `模型：${llmCompiler.model}` : "配置整理模型"}
                    </button>
                  </div>

                  <button
                    type="button"
                    disabled={activeCompileJob?.status === "running"}
                    onClick={() => void handleStartCompile(activeVault.id)}
                    className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-[var(--accent)] py-2 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
                  >
                    {activeCompileJob?.status === "running" ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Sparkles className="h-3.5 w-3.5" />
                    )}
                    {activeCompileJob?.status === "running"
                      ? "正在整理文档..."
                      : `AI 整理新文档并更新索引${
                          activePendingCount > 0 ? ` (${activePendingCount})` : ""
                        }`}
                  </button>

                  {activeCompileJob?.status !== "running" &&
                    activePendingCount > (llmCompiler?.max_items_per_run || 15) && (
                      <p className="text-[11px] leading-relaxed text-[var(--ink-muted)]">
                        每次最多整理 {llmCompiler?.max_items_per_run || 15} 篇，可在顶部「整理模型设置」调大单次篇数
                      </p>
                    )}

                  {/* Collapsible Pending Queue Detail */}
                  {activePendingCount > 0 &&
                    compilePlan &&
                    compilePlan.vaultId === activeVault.id && (
                      <div className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)]">
                        <button
                          type="button"
                          onClick={() => setQueueOpen((o) => !o)}
                          className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
                        >
                          <span>
                            待整理文件（新增 {compilePlan.uncompiled_sources.length} · 需更新{" "}
                            {compilePlan.stale_pages.length}）
                          </span>
                          {queueOpen ? (
                            <ChevronDown className="h-3.5 w-3.5 text-[var(--accent)]" />
                          ) : (
                            <ChevronRight className="h-3.5 w-3.5 text-[var(--ink-muted)]" />
                          )}
                        </button>
                        {queueOpen && (
                          <div className="max-h-40 space-y-1 overflow-y-auto border-t border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-2.5 font-mono text-xs">
                            {compilePlan.uncompiled_sources.slice(0, 15).map((s) => (
                              <div
                                key={`u-${s}`}
                                className="truncate text-amber-700 dark:text-amber-300"
                              >
                                [新增] {s}
                              </div>
                            ))}
                            {compilePlan.stale_pages.slice(0, 10).map((p) => (
                              <div key={`s-${p}`} className="truncate text-[var(--accent)]">
                                [需更新] {p}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                  <button
                    type="button"
                    onClick={() => void chooseFolder("import")}
                    className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] py-2 font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-subtle)]"
                  >
                    <FolderInput className="h-3.5 w-3.5 text-[var(--accent)]" />
                    导入外部文件夹到此库
                  </button>

                  <div className="grid grid-cols-2 gap-2">
                    <button
                      type="button"
                      onClick={() => void handleAsyncSync(activeVault.id, true)}
                      className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] py-2 font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
                    >
                      <RefreshCw className="h-3.5 w-3.5 text-[var(--accent)]" /> 重建全部索引
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleLint(activeVault.id)}
                      className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] py-2 font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
                    >
                      <ShieldAlert className="h-3.5 w-3.5 text-amber-500" /> 检查文档完整性
                    </button>
                  </div>

                  {lintReport && lintReport.vaultId === activeVault.id && (
                    <div
                      className={`rounded-xl border p-3 ${
                        lintReport.ok
                          ? "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
                          : "border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-300"
                      }`}
                    >
                      <div className="font-semibold">
                        {lintReport.ok
                          ? `检查通过（0 个问题，${lintReport.warning_count} 条提醒）`
                          : `发现 ${lintReport.error_count} 处异常，${lintReport.warning_count} 条提醒`}
                      </div>
                      {lintReport.errors.slice(0, 3).map((er, i) => (
                        <div key={i} className="mt-1 truncate font-mono text-xs">
                          {er.file}: {er.detail}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Sample Sources Quick Jump */}
              {(activeVault.sample_sources || []).length > 0 && (
                <div>
                  <div className="mb-1.5 font-bold text-[var(--ink-secondary)]">部分已收录文档</div>
                  <div className="space-y-1 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-2.5">
                    {(activeVault.sample_sources || []).slice(0, 6).map((src) => (
                      <button
                        key={src}
                        type="button"
                        onClick={() => {
                          const base = (src.split("/").pop() || src).replace(
                            /\.[a-zA-Z0-9]+$/,
                            "",
                          );
                          onOpenPlayground(activeVault.id, base);
                        }}
                        className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left font-mono text-xs text-[var(--ink-secondary)] hover:bg-[var(--accent-soft)] hover:text-[var(--accent)]"
                      >
                        <span className="truncate">{src}</span>
                        <Sparkles className="h-3 w-3 shrink-0 text-[var(--accent)]" />
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Collapsible Technical Details (Collapsed by Default) */}
              <div className="overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)]">
                <button
                  type="button"
                  onClick={() => setShowMeta((v) => !v)}
                  className="flex w-full items-center justify-between px-3.5 py-2.5 text-left font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
                >
                  <span>查看详细信息</span>
                  {showMeta ? (
                    <ChevronDown className="h-3.5 w-3.5 text-[var(--accent)]" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5 text-[var(--ink-muted)]" />
                  )}
                </button>
                {showMeta && (
                  <div className="divide-y divide-[var(--border-subtle)] border-t border-[var(--border-subtle)] px-3.5">
                    <div className="flex items-center justify-between py-2">
                      <span className="text-[var(--ink-muted)]">来源类型</span>
                      <span className="font-semibold text-[var(--ink-primary)]">
                        {activeVault.kind === "remote" ? "其他电脑共享" : "本机文件夹"}
                      </span>
                    </div>
                    <div className="flex items-center justify-between py-2">
                      <span className="text-[var(--ink-muted)]">标识 ID</span>
                      <span className="font-mono font-semibold text-[var(--ink-primary)]">
                        {activeVault.id}
                      </span>
                    </div>
                    <div className="flex items-center justify-between py-2">
                      <span className="text-[var(--ink-muted)]">目录格式</span>
                      <span className="font-semibold text-[var(--ink-primary)]">
                        {activeVault.layout === "en" ? "英文目录" : "中文目录"}
                      </span>
                    </div>
                    <div className="flex items-center justify-between py-2">
                      <span className="text-[var(--ink-muted)]">索引版本</span>
                      <span className="max-w-[170px] truncate font-mono text-[var(--ink-primary)]">
                        {activeVault.generation_id || "—"}
                      </span>
                    </div>
                    <div className="flex items-center justify-between py-2">
                      <span className="text-[var(--ink-muted)]">最近更新时间</span>
                      <span className="font-mono text-[var(--ink-primary)]">
                        {activeVault.generated_at
                          ? activeVault.generated_at.slice(0, 19).replace("T", " ")
                          : "—"}
                      </span>
                    </div>
                  </div>
                )}
              </div>
            </>
          ) : null}
        </div>
      </aside>

      <FolderPickerModal
        open={pickerOpen}
        title={pickerMode === "import" ? "选择要导入的文件夹" : "选择要添加的本地文件夹"}
        onClose={() => setPickerOpen(false)}
        onSelect={(p) => {
          if (pickerMode === "import") {
            void handleImportExternalFolder(p);
          } else {
            void inspectSelectedPath(p);
          }
        }}
      />

      {/* Rename Vault ID Modal */}
      {renameTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] p-5 text-[var(--ink-primary)] shadow-2xl">
            <h3 className="text-[14px] font-bold text-[var(--ink-primary)]">
              修改知识库标识 ({renameTarget.oldId})
            </h3>
            <p className="mt-1 text-xs text-[var(--ink-secondary)]">
              修改后会自动同步更新授权配置与已连接的 AI 工具。
            </p>
            <input
              value={renameTarget.newId}
              onChange={(e) => setRenameTarget({ ...renameTarget, newId: e.target.value })}
              className="mt-3 w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-2 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setRenameTarget(null)}
                className="rounded-lg px-3 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void handleRename()}
                className="rounded-lg bg-[var(--accent)] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)]"
              >
                确认修改并同步
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
