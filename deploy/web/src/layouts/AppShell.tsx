import React, { useEffect, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FolderPlus,
  Loader2,
  Monitor,
  Moon,
  Plus,
  RefreshCw,
  Sparkles,
  Sun,
} from "lucide-react";
import { BrandGlyph, WctxMark } from "../components/BrandIcons";
import { useTheme, type ThemeMode } from "../theme";
import type { AsyncJobRecord, LogStatsResponse, OverviewResponse, RuntimeMode } from "../types";

export type WorkspaceTab = "workbench" | "vaults" | "access" | "network";

interface AppShellProps {
  activeTab: WorkspaceTab;
  onTabChange: (tab: WorkspaceTab) => void;
  overview: OverviewResponse | null;
  stats: LogStatsResponse | null;
  mode: RuntimeMode;
  loading: boolean;
  activeVaultId: string;
  compileJobs?: Record<string, AsyncJobRecord>;
  onSelectVault: (vaultId: string) => void;
  onQuickGapQuery: (vaultId: string, term: string) => void;
  onQuickOnboard: () => void;
  onOpenAgentConfig: () => void;
  onQuickPair: () => void;
  onRefresh: () => void;
  availableUpdateVersion?: string;
  checkingUpdate?: boolean;
  onCheckUpdate?: () => void;
  onOpenUpdateModal?: () => void;
  children: React.ReactNode;
}

const SEGMENTS: { id: WorkspaceTab; label: string; shortLabel: string; keyHint: string }[] = [
  { id: "vaults", label: "知识库", shortLabel: "知识库", keyHint: "1" },
  { id: "network", label: "设备与客户端", shortLabel: "客户端", keyHint: "2" },
  { id: "access", label: "共享权限", shortLabel: "权限", keyHint: "3" },
  { id: "workbench", label: "检索测试", shortLabel: "检索", keyHint: "4" },
];

const THEME_OPTIONS: { id: ThemeMode; title: string; icon: React.FC<{ className?: string }> }[] = [
  { id: "light", title: "浅色外观", icon: Sun },
  { id: "dark", title: "深色外观", icon: Moon },
  { id: "system", title: "跟随电脑系统外观", icon: Monitor },
];

export const AppShell: React.FC<AppShellProps> = ({
  activeTab,
  onTabChange,
  overview,
  stats,
  mode,
  loading,
  activeVaultId,
  compileJobs,
  onSelectVault,
  onQuickGapQuery,
  onQuickOnboard,
  onOpenAgentConfig,
  onQuickPair,
  onRefresh,
  availableUpdateVersion,
  checkingUpdate,
  onCheckUpdate,
  onOpenUpdateModal,
  children,
}) => {
  const [showMoreInstalled, setShowMoreInstalled] = useState(false);
  const { themeMode, setThemeMode } = useTheme();
  const vaults = overview?.vaults || [];
  const clients = overview?.clients || [];
  const lan = overview?.lan_gateway;
  const llm = overview?.llm_compiler;
  const conflict = Boolean(lan?.port_conflict);
  const connectedClients = clients
    .filter((c) => Object.keys(c.configured_servers || {}).length > 0)
    .sort((a, b) => {
      const aRank = a.sync_status === "synced" ? 0 : 1;
      const bRank = b.sync_status === "synced" ? 0 : 1;
      return aRank - bRank;
    });
  const unconnectedInstalled = clients.filter(
    (c) => c.installed && Object.keys(c.configured_servers || {}).length === 0,
  );
  const missingTerms = (stats?.top_missing_concepts || []).slice(0, 5);
  const selectedVault = vaults.find((v) => v.id === activeVaultId) || vaults[0];

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (!(ev.metaKey || ev.ctrlKey)) return;
      if (ev.key === "1") {
        ev.preventDefault();
        onTabChange("vaults");
      } else if (ev.key === "2") {
        ev.preventDefault();
        onTabChange("network");
      } else if (ev.key === "3") {
        ev.preventDefault();
        onTabChange("access");
      } else if (ev.key === "4") {
        ev.preventDefault();
        onTabChange("workbench");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onTabChange]);

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-[var(--bg-app)] text-[var(--ink-primary)]">
      {/* Unified macOS Sequoia Native Toolbar */}
      <header className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-[var(--border-default)] bg-[var(--bg-toolbar)] px-3 backdrop-blur-md sm:px-4">
        {/* Brand & Version */}
        <div className="flex min-w-0 items-center gap-2">
          <WctxMark className="h-6 w-6" />
          <span className="hidden truncate text-[14px] font-bold tracking-tight text-[var(--ink-primary)] sm:inline">
            wiki-context
          </span>
          {mode === "desktop" && onCheckUpdate ? (
            <button
              type="button"
              disabled={checkingUpdate}
              onClick={onCheckUpdate}
              title={checkingUpdate ? "正在检查更新..." : "点击检查在线更新"}
              className="hidden rounded px-1 py-0.5 font-mono text-xs text-[var(--ink-muted)] hover:bg-[var(--bg-muted)] hover:text-[var(--ink-primary)] disabled:opacity-60 xl:inline"
            >
              {checkingUpdate ? "检查中..." : `v${overview?.version || "0.4.1"}`}
            </button>
          ) : (
            <span className="hidden font-mono text-xs text-[var(--ink-muted)] xl:inline">
              v{overview?.version || "0.4.1"}
            </span>
          )}
          {mode === "desktop" && availableUpdateVersion && onOpenUpdateModal && (
            <button
              type="button"
              onClick={onOpenUpdateModal}
              title={`点击更新到 v${availableUpdateVersion}`}
              className="inline-flex items-center gap-1 rounded-md border border-[var(--accent)]/40 bg-[var(--accent-soft)] px-2 py-0.5 text-[11px] font-semibold text-[var(--accent)] hover:bg-[var(--accent)]/15"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
              <span>新版本 v{availableUpdateVersion}</span>
            </button>
          )}
          <span className="hidden rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2 py-0.5 text-xs font-medium text-[var(--ink-secondary)] 2xl:inline">
            {mode === "desktop" ? "桌面版" : "Web 版"}
          </span>
        </div>

        {/* Native Segmented Control */}
        <nav
          aria-label="主工作区切换"
          className="flex items-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-muted)] p-0.5"
        >
          {SEGMENTS.map((seg) => {
            const active = activeTab === seg.id;
            return (
              <button
                key={seg.id}
                type="button"
                title={`${seg.label} (⌘${seg.keyHint})`}
                onClick={() => onTabChange(seg.id)}
                className={`flex items-center gap-1.5 rounded-[6px] px-2.5 py-1 text-xs font-semibold sm:px-3 ${
                  active
                    ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xs"
                    : "bg-transparent text-[var(--ink-secondary)] hover:text-[var(--ink-primary)]"
                }`}
              >
                <span className="sm:hidden">{seg.shortLabel}</span>
                <span className="hidden sm:inline">{seg.label}</span>
              </button>
            );
          })}
        </nav>

        {/* Right Global Controls: Built-in Agent, Theme Switcher, Gateway Status & Primary Action */}
        <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
          {/* Global Built-in AI Compiler Agent Config Button */}
          <button
            type="button"
            onClick={onOpenAgentConfig}
            title={
              llm?.ready
                ? `整理模型：${llm.model}（点击修改）`
                : "配置用于自动整理文档的大模型"
            }
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--accent)] hover:text-[var(--ink-primary)]"
          >
            <Sparkles className="h-3.5 w-3.5 text-[var(--accent)]" />
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${
                llm?.ready ? "bg-emerald-500" : "bg-amber-400"
              }`}
            />
            <span className="hidden max-w-[130px] truncate md:inline">
              {llm?.ready ? llm.model : "配置整理模型"}
            </span>
          </button>

          {/* Light / Dark / System Appearance Switcher */}
          <div
            role="group"
            aria-label="界面外观模式"
            className="flex items-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-muted)] p-0.5"
          >
            {THEME_OPTIONS.map((opt) => {
              const Icon = opt.icon;
              const active = themeMode === opt.id;
              return (
                <button
                  key={opt.id}
                  type="button"
                  title={opt.title}
                  aria-label={opt.title}
                  onClick={() => setThemeMode(opt.id)}
                  className={`inline-flex h-6 w-6 items-center justify-center rounded-[5px] ${
                    active
                      ? "bg-[var(--bg-surface)] text-[var(--accent)] shadow-2xs"
                      : "bg-transparent text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                </button>
              );
            })}
          </div>

          {conflict ? (
            <button
              type="button"
              onClick={() => onTabChange("network")}
              className="hidden items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-800 lg:inline-flex dark:border-amber-700 dark:bg-amber-950/50 dark:text-amber-300"
            >
              <AlertTriangle className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" />
              端口冲突
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onTabChange("network")}
              className="hidden items-center gap-1.5 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)] xl:inline-flex"
            >
              <span
                className={`h-2 w-2 rounded-full ${
                  lan?.running ? "bg-emerald-500" : "bg-slate-400"
                }`}
              />
              <span>{lan?.running ? `局域网共享中 :${lan.port}` : "局域网未开启"}</span>
            </button>
          )}

          <button
            type="button"
            onClick={onQuickPair}
            className="hidden items-center gap-1 rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-white shadow-2xs hover:bg-[var(--accent-hover)] sm:inline-flex"
          >
            <Plus className="h-3.5 w-3.5" />
            <span className="hidden lg:inline">生成配对码</span>
          </button>

          <button
            type="button"
            onClick={onRefresh}
            title="刷新状态"
            aria-label="刷新状态"
            className="inline-flex h-7 w-7 items-center justify-center rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)] hover:text-[var(--ink-primary)]"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin text-[var(--accent)]" : ""}`} />
          </button>
        </div>
      </header>

      {/* Body: Left Source Rail + Main Stage/Inspector */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* Left Source Rail (236px) */}
        <aside className="hidden w-[236px] shrink-0 flex-col border-r border-[var(--border-default)] bg-[var(--bg-sidebar)] md:flex">
          {/* Active Vault Summary Banner */}
          <div className="border-b border-[var(--border-default)] px-3.5 py-3">
            <div className="flex items-center gap-2">
              <span
                className={`h-2 w-2 rounded-full ${
                  selectedVault?.ready
                    ? "bg-emerald-500"
                    : selectedVault?.hydrating
                      ? "bg-amber-500"
                      : "bg-slate-400"
                }`}
              />
              <span className="truncate text-[13px] font-bold text-[var(--ink-primary)]">
                {selectedVault?.name || "尚未添加知识库"}
              </span>
            </div>
            <div className="mt-0.5 truncate text-xs text-[var(--ink-muted)]">
              {selectedVault
                ? selectedVault.kind === "remote"
                  ? "来自其他电脑的共享知识库"
                  : `${selectedVault.docs} 篇文档 · ${selectedVault.wiki_docs} 篇已整理`
                : "点击下方按钮添加本地文件夹"}
            </div>
          </div>

          <div className="flex-1 space-y-4 overflow-y-auto px-2.5 py-3">
            {/* Vaults Source Group */}
            <div>
              <div className="mb-1.5 flex items-center justify-between px-1.5 text-xs font-semibold text-[var(--ink-muted)]">
                <span>知识库</span>
                <span>{vaults.length} 个</span>
              </div>
              <div className="space-y-1">
                {vaults.map((v) => {
                  const selected = v.id === (selectedVault?.id || "");
                  const isRemote = v.kind === "remote";
                  const activeJob = compileJobs?.[v.id] || v.compile_job;
                  const isCompiling = activeJob?.status === "running";
                  const pendingCount = (v.uncompiled_count || 0) + (v.stale_count || 0);
                  const subtitle = isRemote
                    ? "共享知识库"
                    : pendingCount > 0
                      ? `待整理 ${pendingCount} 篇`
                      : `已整理 ${v.wiki_docs} 篇`;
                  return (
                    <button
                      key={v.id}
                      type="button"
                      title={v.path || v.gateway_url || v.id}
                      onClick={() => onSelectVault(v.id)}
                      className={`flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-2 text-left ${
                        selected
                          ? "border border-[var(--accent)]/45 bg-[var(--bg-surface)] shadow-2xs"
                          : "border border-transparent bg-transparent hover:bg-[var(--bg-surface)]/65"
                      }`}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <BrandGlyph
                          brand={isRemote ? "tailscale" : "obsidian"}
                          className="h-4 w-4"
                        />
                        <div className="min-w-0">
                          <div
                            className={`truncate text-[13px] font-semibold ${
                              selected ? "text-[var(--accent)]" : "text-[var(--ink-primary)]"
                            }`}
                          >
                            {v.name || v.id}
                          </div>
                          <div className="truncate text-xs text-[var(--ink-muted)]">
                            {subtitle}
                          </div>
                        </div>
                      </div>
                      {isCompiling ? (
                        <span className="inline-flex shrink-0 items-center gap-1 rounded bg-[var(--accent-soft)] px-1.5 py-0.5 font-mono text-xs font-medium text-[var(--accent)]">
                          <Loader2 className="h-3 w-3 animate-spin" />
                          {activeJob?.progress?.total
                            ? `整理 ${activeJob.progress.done}/${activeJob.progress.total}`
                            : "整理中"}
                        </span>
                      ) : isRemote ? (
                        <span className="shrink-0 rounded bg-[var(--accent-soft)] px-1.5 py-0.5 text-xs font-medium text-[var(--accent)]">
                          共享
                        </span>
                      ) : v.hydrating ? (
                        <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-950/60 dark:text-amber-300">
                          授权中
                        </span>
                      ) : (
                        <span className="shrink-0 font-mono text-xs text-[var(--ink-muted)]">
                          {v.docs} 篇
                        </span>
                      )}
                    </button>
                  );
                })}

                <button
                  type="button"
                  onClick={onQuickOnboard}
                  className="mt-1 flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-[var(--border-strong)] bg-[var(--bg-surface)]/60 px-2.5 py-1.5 text-xs font-semibold text-[var(--accent)] hover:border-[var(--accent)] hover:bg-[var(--bg-surface)]"
                >
                  <FolderPlus className="h-3.5 w-3.5" />
                  添加本地文件夹
                </button>
              </div>
            </div>

            {/* Connected + Installed AI Clients Group */}
            <div>
              <div className="mb-1.5 flex items-center justify-between px-1.5 text-xs font-semibold text-[var(--ink-muted)]">
                <span>AI 工具</span>
                <span className="font-mono">{connectedClients.length} 已连接</span>
              </div>
              <div className="space-y-0.5 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-1">
                {connectedClients.length === 0 && unconnectedInstalled.length === 0 ? (
                  <button
                    type="button"
                    onClick={() => onTabChange("network")}
                    className="flex w-full items-center justify-between rounded-lg px-2 py-2 text-left text-xs text-[var(--ink-muted)] hover:bg-[var(--bg-subtle)] hover:text-[var(--ink-primary)]"
                  >
                    <span>未检测到本机工具 · 去扫描</span>
                    <span>→</span>
                  </button>
                ) : connectedClients.length > 0 ? (
                  <>
                    {connectedClients.map((c) => {
                      const synced = c.sync_status === "synced";
                      return (
                        <button
                          key={c.id}
                          type="button"
                          onClick={() => onTabChange("network")}
                          className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-xs hover:bg-[var(--bg-subtle)]"
                        >
                          <span className="flex min-w-0 items-center gap-2 font-medium text-[var(--ink-primary)]">
                            <BrandGlyph brand={c.id} className="h-3.5 w-3.5" />
                            <span className="truncate">
                              {c.id === "dsh" ? "DSH" : c.name.replace(/\s*\(.*\)$/, "")}
                            </span>
                          </span>
                          <span
                            className={`h-2 w-2 shrink-0 rounded-full ${
                              synced ? "bg-emerald-500" : "bg-amber-400"
                            }`}
                            title={synced ? "已连接" : "待更新"}
                          />
                        </button>
                      );
                    })}
                    {unconnectedInstalled.length > 0 && (
                      <div className="mt-0.5 border-t border-[var(--border-subtle)] pt-0.5">
                        <button
                          type="button"
                          onClick={() => setShowMoreInstalled((v) => !v)}
                          className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-[11px] font-medium text-[var(--ink-muted)] hover:bg-[var(--bg-subtle)] hover:text-[var(--ink-primary)]"
                        >
                          {showMoreInstalled ? (
                            <ChevronDown className="h-3 w-3 shrink-0" />
                          ) : (
                            <ChevronRight className="h-3 w-3 shrink-0" />
                          )}
                          <span className="truncate">
                            本机另发现 {unconnectedInstalled.length} 个未连接
                          </span>
                        </button>
                        {showMoreInstalled &&
                          unconnectedInstalled.map((c) => (
                            <button
                              key={c.id}
                              type="button"
                              onClick={() => onTabChange("network")}
                              className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 pl-4 text-left text-xs text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
                            >
                              <span className="flex min-w-0 items-center gap-2">
                                <BrandGlyph brand={c.id} className="h-3.5 w-3.5" />
                                <span className="truncate">
                                  {c.id === "dsh" ? "DSH" : c.name.replace(/\s*\(.*\)$/, "")}
                                </span>
                              </span>
                              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400/70" />
                            </button>
                          ))}
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    {(showMoreInstalled ? unconnectedInstalled : unconnectedInstalled.slice(0, 6)).map(
                      (c) => (
                        <button
                          key={c.id}
                          type="button"
                          onClick={() => onTabChange("network")}
                          className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-xs text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
                        >
                          <span className="flex min-w-0 items-center gap-2">
                            <BrandGlyph brand={c.id} className="h-3.5 w-3.5" />
                            <span className="truncate">
                              {c.id === "dsh" ? "DSH" : c.name.replace(/\s*\(.*\)$/, "")}
                            </span>
                          </span>
                          <span
                            className="h-2 w-2 shrink-0 rounded-full bg-amber-400"
                            title="未连接"
                          />
                        </button>
                      ),
                    )}
                    {unconnectedInstalled.length > 6 && (
                      <button
                        type="button"
                        onClick={() => setShowMoreInstalled((v) => !v)}
                        className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-left text-[11px] font-medium text-[var(--accent)] hover:bg-[var(--bg-subtle)]"
                      >
                        {showMoreInstalled ? (
                          <ChevronDown className="h-3 w-3 shrink-0" />
                        ) : (
                          <ChevronRight className="h-3 w-3 shrink-0" />
                        )}
                        <span>
                          {showMoreInstalled
                            ? "收起"
                            : `展开其余 ${unconnectedInstalled.length - 6} 个已安装工具`}
                        </span>
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>

            {/* Zero-Hit Missing Concepts Radar */}
            <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-2.5">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-[var(--ink-primary)]">AI 未查到的提问</span>
                <span className="rounded-md bg-[var(--accent-soft)] px-1.5 py-0.5 font-mono text-xs font-semibold text-[var(--accent)]">
                  {stats?.top_missing_concepts?.length ?? 0}
                </span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-[var(--ink-muted)]">
                近 24 小时 AI 没找到答案的问题，点一下可去测试搜索。
              </p>
              {missingTerms.length > 0 && (
                <div className="mt-2 space-y-1">
                  {missingTerms.map((m) => (
                    <button
                      key={m.term}
                      type="button"
                      onClick={() =>
                        onQuickGapQuery(m.vaults[0] || selectedVault?.id || "", m.term)
                      }
                      className="flex w-full items-center justify-between rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-2 py-1.5 text-xs hover:border-[var(--accent)]"
                    >
                      <span className="truncate font-medium text-[var(--ink-primary)]">
                        {m.term}{" "}
                        <span className="font-semibold text-rose-600 dark:text-rose-400">
                          {m.count} 次
                        </span>
                      </span>
                      <span className="ml-1 shrink-0 font-semibold text-[var(--accent)]">试搜</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </aside>

        {/* Main Workspace Container (Center Stage + Right Inspector) */}
        <div className="flex min-w-0 flex-1 overflow-hidden bg-[var(--bg-surface)]">{children}</div>
      </div>
    </div>
  );
};
