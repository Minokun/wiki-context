import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, subscribeLogStream } from "./api/client";
import { AgentConfigModal } from "./components/AgentConfigModal";
import { ToastHost, type ToastItem } from "./components/ToastHost";
import { UpdateModal } from "./components/UpdateModal";
import { AppShell, type WorkspaceTab } from "./layouts/AppShell";
import { AccessPage } from "./pages/AccessPage";
import { NetworkPage } from "./pages/NetworkPage";
import { VaultsPage } from "./pages/VaultsPage";
import { WorkbenchPage } from "./pages/WorkbenchPage";
import {
  checkForAppUpdate,
  getBootstrapConfig,
  type AppUpdateHandle,
  type UpdateProgress,
} from "./platform/adapter";
import { ThemeProvider } from "./theme";
import type {
  AsyncJobRecord,
  LogInvocation,
  LogStatsResponse,
  OverviewResponse,
  RuntimeMode,
} from "./types";

const AppInner: React.FC = () => {
  const [tab, setTab] = useState<WorkspaceTab>("vaults");
  const [mode, setMode] = useState<RuntimeMode>("web");
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [stats, setStats] = useState<LogStatsResponse | null>(null);
  const [logs, setLogs] = useState<LogInvocation[]>([]);
  const [sseConnected, setSseConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [bootError, setBootError] = useState("");
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  // Active Vault, Global Agent Modal & Cross-Pane Deep Links
  const [activeVaultId, setActiveVaultId] = useState("");
  const [agentModalOpen, setAgentModalOpen] = useState(false);
  const [externalQuery, setExternalQuery] = useState("");
  const [openOnboardSignal, setOpenOnboardSignal] = useState(0);
  const [focusPairSignal, setFocusPairSignal] = useState(0);
  const [compileJobs, setCompileJobs] = useState<Record<string, AsyncJobRecord>>({});
  const [availableUpdate, setAvailableUpdate] = useState<AppUpdateHandle | null>(null);
  const [updateModalOpen, setUpdateModalOpen] = useState(false);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [installingUpdate, setInstallingUpdate] = useState(false);
  const [updateProgress, setUpdateProgress] = useState<UpdateProgress | null>(null);
  const [updateError, setUpdateError] = useState("");
  const compilePollErrorsRef = useRef<Record<string, number>>({});
  const vaultsRef = useRef(overview?.vaults || []);
  vaultsRef.current = overview?.vaults || [];

  const handleTrackCompileJob = useCallback((vid: string, job: AsyncJobRecord) => {
    if (!vid || !job?.job_id || job.status !== "running") return;
    compilePollErrorsRef.current[vid] = 0;
    setCompileJobs((prev) => ({ ...prev, [vid]: job }));
  }, []);

  const pushToast = useCallback((kind: "success" | "error" | "info", message: string) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setToasts((prev) => [...prev, { id, kind, message }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4500);
  }, []);

  const refreshStats = useCallback(async () => {
    try {
      const st = await api.logsStats(24);
      setStats(st);
    } catch {
      // non-fatal
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [ov, st] = await Promise.all([
        api.overview(),
        api.logsStats(24).catch(() => null),
      ]);
      setOverview(ov);
      if (st) setStats(st);
      if (ov.vaults.length > 0) {
        setActiveVaultId((prev) =>
          prev && ov.vaults.some((v) => v.id === prev) ? prev : ov.vaults[0].id,
        );
      }
      setCompileJobs((prev) => {
        const next = { ...prev };
        let changed = false;
        for (const v of ov.vaults) {
          if (v.compile_job?.status === "running") {
            const cur = next[v.id];
            const incomingDone = v.compile_job.progress?.done ?? 0;
            const curDone = cur?.progress?.done ?? -1;
            if (!cur || cur.job_id !== v.compile_job.job_id || incomingDone >= curDone) {
              next[v.id] = v.compile_job;
              changed = true;
            }
          }
        }
        return changed ? next : prev;
      });
      setBootError("");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setBootError(msg);
      pushToast("error", msg);
    } finally {
      setLoading(false);
    }
  }, [pushToast]);

  useEffect(() => {
    void (async () => {
      try {
        const boot = await getBootstrapConfig();
        setMode(boot.mode);
        await refresh();
      } catch (e) {
        setBootError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      }
    })();
  }, [refresh]);

  // Subscribe to real-time SSE telemetry stream once at root
  useEffect(() => {
    if (!overview) return;
    const stop = subscribeLogStream({
      onInit: (items) => {
        setLogs(items);
      },
      onLog: (item) => {
        setLogs((prev) => {
          if (prev.some((x) => x.id === item.id)) return prev;
          return [item, ...prev].slice(0, 250);
        });
        void refreshStats();
      },
      onStatus: (ok) => setSseConnected(ok),
    });
    return () => stop();
  }, [Boolean(overview), refreshStats]);

  // Poll hydrating vaults reliably via interval until all vaults finish hydrating
  const hasHydratingVault = Boolean(overview?.vaults?.some((v) => v.hydrating));
  useEffect(() => {
    if (!hasHydratingVault) return;
    const iv = setInterval(() => {
      void api
        .overview()
        .then((ov) => setOverview(ov))
        .catch(() => {});
    }, 1500);
    return () => clearInterval(iv);
  }, [hasHydratingVault]);

  // Root-level compile job polling so progress persists across tab switches & transient failures
  const runningCompileKey = Object.entries(compileJobs)
    .filter(([, job]) => job?.status === "running" && job.job_id)
    .map(([vid, job]) => `${vid}:${job.job_id}`)
    .sort()
    .join(",");

  useEffect(() => {
    if (!runningCompileKey) return;
    let cancelled = false;
    const entries = runningCompileKey.split(",").map((pair) => {
      const idx = pair.indexOf(":");
      return [pair.slice(0, idx), pair.slice(idx + 1)] as const;
    });

    const pollOnce = async () => {
      for (const [vid, jobId] of entries) {
        if (cancelled) return;
        try {
          const st = await api.getJob(jobId);
          if (cancelled) return;
          compilePollErrorsRef.current[vid] = 0;

          if (st.status === "running") {
            setCompileJobs((prev) => {
              const cur = prev[vid];
              if (cur && cur.job_id !== st.job_id) return prev;
              return { ...prev, [vid]: st };
            });
            continue;
          }

          setCompileJobs((prev) => {
            if (!prev[vid] || prev[vid].job_id !== jobId) return prev;
            const nxt = { ...prev };
            delete nxt[vid];
            return nxt;
          });

          const vaultName = vaultsRef.current.find((v) => v.id === vid)?.name || vid;
          if (st.status === "done" || st.status === "cancelled") {
            const resObj = (st.result || {}) as {
              compiled_pages?: string[];
              refreshed_pages?: string[];
              failed_units?: { title?: string; error?: string }[];
              remaining_uncompiled?: number;
              remaining_stale?: number;
              batch_limit?: number;
              cancelled?: boolean;
              noop?: boolean;
            };
            const cCnt = (resObj.compiled_pages || []).length;
            const rCnt = (resObj.refreshed_pages || []).length;
            const fUnits = resObj.failed_units || [];
            const rem = (resObj.remaining_uncompiled || 0) + (resObj.remaining_stale || 0);
            const batchLimit = resObj.batch_limit ?? 0;
            if (st.status === "cancelled" || resObj.cancelled) {
              pushToast(
                "info",
                cCnt + rCnt > 0
                  ? `「${vaultName}」已停止整理：已保存 ${cCnt + rCnt} 篇并更新索引${rem > 0 ? `，剩余 ${rem} 篇下次可继续` : ""}`
                  : `「${vaultName}」已停止整理`,
              );
            } else if (resObj.noop) {
              pushToast("info", `「${vaultName}」所有文档都已整理完成，无需重复处理`);
            } else if (rem > 0 && batchLimit > 0) {
              pushToast(
                "success",
                `「${vaultName}」本批已整理 ${cCnt + rCnt} 篇（单次上限 ${batchLimit} 篇），还剩 ${rem} 篇待整理，可在顶部模型设置切换为「全部跑完」`,
              );
            } else if (fUnits.length > 0) {
              const firstErr = fUnits[0]?.error ? `（首个错误：${fUnits[0].error.slice(0, 80)}）` : "";
              pushToast(
                "info",
                `「${vaultName}」整理完成：新增 ${cCnt} 篇、更新 ${rCnt} 篇，另有 ${fUnits.length} 篇失败${firstErr}`,
              );
            } else {
              pushToast(
                "success",
                `「${vaultName}」已全部整理完成：新增 ${cCnt} 篇、更新 ${rCnt} 篇，搜索索引已更新`,
              );
            }
            void refresh();
          } else if (st.status === "failed") {
            pushToast("error", `「${vaultName}」文档整理失败：${st.error || "未知错误"}`);
            void refresh();
          }
        } catch {
          if (cancelled) return;
          const cnt = (compilePollErrorsRef.current[vid] || 0) + 1;
          compilePollErrorsRef.current[vid] = cnt;
          if (cnt >= 8) {
            setCompileJobs((prev) => {
              const nxt = { ...prev };
              delete nxt[vid];
              return nxt;
            });
            const vaultName = vaultsRef.current.find((v) => v.id === vid)?.name || vid;
            pushToast("error", `获取「${vaultName}」整理进度失败，请点击右上角刷新查看最新状态`);
          }
        }
      }
    };

    const iv = setInterval(() => {
      void pollOnce();
    }, 1000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [runningCompileKey, pushToast, refresh]);

  const handleSelectVault = (vid: string) => {
    setActiveVaultId(vid);
  };

  const handleJumpWorkbench = (vid: string, query = "") => {
    if (vid) setActiveVaultId(vid);
    if (query) setExternalQuery(query);
    setTab("workbench");
  };

  const handleClearLogs = async () => {
    try {
      await api.logsClear();
      setLogs([]);
      await refreshStats();
      pushToast("info", "已清空查询记录");
    } catch (e) {
      pushToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  // Silent startup check in desktop mode (after initial load settles)
  useEffect(() => {
    if (mode !== "desktop") return;
    let cancelled = false;
    const t = setTimeout(() => {
      void checkForAppUpdate()
        .then((upd) => {
          if (cancelled || !upd) {
            void upd?.close();
            return;
          }
          setAvailableUpdate((prev) => {
            if (prev && prev.version !== upd.version) void prev.close();
            return upd;
          });
        })
        .catch(() => {
          // Offline or no published release yet — stay quiet on background check
        });
    }, 3000);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [mode]);

  const handleManualCheckUpdate = async () => {
    if (mode !== "desktop" || checkingUpdate) return;
    setCheckingUpdate(true);
    setUpdateError("");
    try {
      const upd = await checkForAppUpdate();
      if (upd) {
        setAvailableUpdate((prev) => {
          if (prev && prev.version !== upd.version) void prev.close();
          return upd;
        });
        setUpdateModalOpen(true);
      } else {
        pushToast("info", `当前已是最新版本 (v${overview?.version || "0.5.5"})`);
      }
    } catch {
      pushToast("info", "暂未检测到线上新版本（请确认网络或已发布 GitHub Release）");
    } finally {
      setCheckingUpdate(false);
    }
  };

  const handleInstallUpdate = async () => {
    if (!availableUpdate || installingUpdate) return;
    setInstallingUpdate(true);
    setUpdateError("");
    setUpdateProgress({ phase: "downloading", downloaded: 0, percent: 0 });
    try {
      await availableUpdate.install((p) => setUpdateProgress(p));
    } catch (e) {
      setUpdateError(e instanceof Error ? e.message : String(e));
      setInstallingUpdate(false);
    }
  };

  if (bootError && !overview) {
    return (
      <div className="flex h-screen w-screen flex-col items-center justify-center bg-[var(--bg-app)] p-6 text-center text-[var(--ink-primary)]">
        <div className="max-w-md rounded-2xl border border-rose-300 bg-[var(--bg-surface)] p-6 shadow-xl dark:border-rose-700">
          <h1 className="text-[15px] font-bold text-rose-700 dark:text-rose-300">
            无法连接本地 wiki-context 服务
          </h1>
          <p className="mt-2 font-mono text-xs text-[var(--ink-secondary)]">{bootError}</p>
          <button
            type="button"
            onClick={() => void refresh()}
            className="mt-4 rounded-lg bg-[var(--accent)] px-4 py-2 text-xs font-semibold text-white hover:bg-[var(--accent-hover)]"
          >
            重试连接
          </button>
        </div>
      </div>
    );
  }

  return (
    <>
      <AppShell
        activeTab={tab}
        onTabChange={setTab}
        overview={overview}
        stats={stats}
        mode={mode}
        loading={loading}
        activeVaultId={activeVaultId}
        onSelectVault={handleSelectVault}
        onQuickGapQuery={(vid, term) => handleJumpWorkbench(vid, term)}
        onQuickOnboard={() => {
          setTab("vaults");
          setOpenOnboardSignal((n) => n + 1);
        }}
        onOpenAgentConfig={() => setAgentModalOpen(true)}
        onQuickPair={() => {
          setTab("network");
          setFocusPairSignal((n) => n + 1);
        }}
        compileJobs={compileJobs}
        onRefresh={() => void refresh()}
        availableUpdateVersion={availableUpdate?.version}
        checkingUpdate={checkingUpdate}
        onCheckUpdate={() => void handleManualCheckUpdate()}
        onOpenUpdateModal={() => {
          setUpdateError("");
          setUpdateModalOpen(true);
        }}
      >
        {tab === "workbench" && (
          <WorkbenchPage
            vaults={overview?.vaults || []}
            activeVaultId={activeVaultId}
            onSelectVault={handleSelectVault}
            tokens={overview?.tokens || []}
            acl={overview?.acl || {}}
            stats={stats}
            logs={logs}
            sseConnected={sseConnected}
            onRefreshStats={() => void refreshStats()}
            onClearLogs={handleClearLogs}
            externalQuery={externalQuery}
            onConsumeExternalQuery={() => setExternalQuery("")}
            onToast={pushToast}
          />
        )}

        {tab === "vaults" && (
          <VaultsPage
            vaults={overview?.vaults || []}
            vaultsDir={overview?.vaults_dir}
            activeVaultId={activeVaultId}
            onSelectVault={handleSelectVault}
            llmCompiler={overview?.llm_compiler || null}
            stats={stats}
            compileJobs={compileJobs}
            onTrackCompileJob={handleTrackCompileJob}
            openOnboardSignal={openOnboardSignal}
            onOpenAgentConfig={() => setAgentModalOpen(true)}
            onRefresh={() => void refresh()}
            onToast={pushToast}
            onOpenPlayground={(vid, q) => handleJumpWorkbench(vid, q || "")}
          />
        )}

        {tab === "access" && (
          <AccessPage
            vaults={overview?.vaults || []}
            tokens={overview?.tokens || []}
            acl={overview?.acl || {}}
            lan={overview?.lan_gateway || null}
            gatewayBaseUrl={overview?.gateway_base_url || ""}
            onRefresh={() => void refresh()}
            onToast={pushToast}
          />
        )}

        {tab === "network" && (
          <NetworkPage
            lan={overview?.lan_gateway || null}
            vaults={overview?.vaults || []}
            clients={overview?.clients || []}
            acl={overview?.acl || {}}
            stats={stats}
            focusPairSignal={focusPairSignal}
            onRefresh={() => void refresh()}
            onToast={pushToast}
          />
        )}
      </AppShell>

      <AgentConfigModal
        open={agentModalOpen}
        llmCompiler={overview?.llm_compiler || null}
        onClose={() => setAgentModalOpen(false)}
        onSaved={() => void refresh()}
        onToast={pushToast}
      />

      <UpdateModal
        open={updateModalOpen}
        updateInfo={availableUpdate}
        installing={installingUpdate}
        progress={updateProgress}
        error={updateError}
        onInstall={() => void handleInstallUpdate()}
        onClose={() => setUpdateModalOpen(false)}
      />

      <ToastHost
        toasts={toasts}
        onDismiss={(id) => setToasts((prev) => prev.filter((t) => t.id !== id))}
      />
    </>
  );
};

export const App: React.FC = () => (
  <ThemeProvider>
    <AppInner />
  </ThemeProvider>
);

export default App;
