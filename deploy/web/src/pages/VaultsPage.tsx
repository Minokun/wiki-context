import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Edit3,
  ExternalLink,
  Eye,
  EyeOff,
  FolderInput,
  FolderOpen,
  FolderTree,
  HardDrive,
  KeyRound,
  Link2,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Square,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import { api } from "../api/client";
import { BrandGlyph } from "../components/BrandIcons";
import { FolderPickerModal } from "../components/FolderPickerModal";
import {
  copyToClipboard,
  isTauriRuntime,
  openNativeDirectory,
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
  vaultsDir?: string;
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
    case "cancelling":
      return "正在安全停止并保存已完成内容";
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
  vaultsDir,
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

  // Picker mode: "onboard" (new vault) vs "mount" (zero-copy extra mount) vs "relink" (update moved source dir)
  const [pickerMode, setPickerMode] = useState<"onboard" | "mount" | "relink">("onboard");
  const [relinkMountName, setRelinkMountName] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);

  // Onboarding state
  const [selectedPath, setSelectedPath] = useState("");
  const [inspected, setInspected] = useState<{
    kind: string;
    suggested_name: string;
    suggested_id: string;
    detected_layout: string;
    recommended_storage_mode?: "managed" | "in_place";
    file_count: number;
    loose_total?: number;
    warnings?: string[];
  } | null>(null);
  const [vaultIdInput, setVaultIdInput] = useState("");
  const [vaultNameInput, setVaultNameInput] = useState("");
  const [layoutInput, setLayoutInput] = useState("zh");
  const [storageModeInput, setStorageModeInput] = useState<"managed" | "in_place">("managed");
  const [onboardAutoCompile, setOnboardAutoCompile] = useState(false);
  const [onboarding, setOnboarding] = useState(false);

  // Safe removal confirmation <dialog> state
  const [removeTarget, setRemoveTarget] = useState<VaultInfo | null>(null);
  const [removeCleanupWs, setRemoveCleanupWs] = useState(true);
  const removeDialogRef = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dlg = removeDialogRef.current;
    if (!dlg) return;
    if (removeTarget) {
      setRemoveCleanupWs(removeTarget.storage_mode === "managed");
      if (!dlg.open) {
        dlg.showModal();
      }
    } else if (dlg.open) {
      dlg.close();
    }
  }, [removeTarget]);

  // Sync, Plan Preview, Lint & Rename state
  const [syncingJobs, setSyncingJobs] = useState<Record<string, string>>({});
  const [compilePlan, setCompilePlan] = useState<{
    vaultId: string;
    uncompiled_sources: string[];
    stale_pages: string[];
    cluster_count?: number;
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

  // Domain Profile state (AI auto-maintained or manually locked)
  const [profileDomain, setProfileDomain] = useState("");
  const [profileHints, setProfileHints] = useState("");
  const [profileTips, setProfileTips] = useState("");
  const [profileBusy, setProfileBusy] = useState<"" | "save" | "generate" | "unlock">("");

  useEffect(() => {
    const dp = activeVault?.domain_profile;
    setProfileDomain(dp?.domain || "");
    setProfileHints((dp?.entity_hints || []).join(" / "));
    setProfileTips(dp?.query_tips || "");
  }, [activeVault?.id, activeVault?.domain_profile?.domain, activeVault?.domain_profile?.updated_at, activeVault?.domain_profile?.locked]);

  const handleDomainProfileAction = async (action: "save" | "generate" | "unlock") => {
    if (!activeVault || activeVault.kind !== "local" || profileBusy) return;
    setProfileBusy(action);
    try {
      const hints = profileHints
        .split(/[/、,，\n]+/)
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 6);
      const res = await api.saveDomainProfile({
        vault_id: activeVault.id,
        action,
        ...(action === "save"
          ? { domain: profileDomain.trim(), entity_hints: hints, query_tips: profileTips.trim() }
          : {}),
      });
      if (action === "generate") {
        onToast("success", `已通过 AI 生成「${activeVault.name}」的领域画像`);
      } else if (action === "unlock") {
        onToast("info", "已恢复 AI 自动维护，目录/文档增长时将自动更新");
      } else {
        onToast("success", `已保存「${activeVault.name}」的手动领域画像（已锁定）`);
      }
      if (res.domain_profile) {
        setProfileDomain(res.domain_profile.domain || "");
        setProfileHints((res.domain_profile.entity_hints || []).join(" / "));
        setProfileTips(res.domain_profile.query_tips || "");
      }
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setProfileBusy("");
    }
  };

  // Remote connection & credential management state
  const [remoteGatewayInput, setRemoteGatewayInput] = useState("");
  const [remoteTokenInput, setRemoteTokenInput] = useState("");
  const [remoteShowToken, setRemoteShowToken] = useState(false);
  const [remoteSyncClients, setRemoteSyncClients] = useState(true);
  const [remoteBusy, setRemoteBusy] = useState<"" | "test" | "update">("");

  useEffect(() => {
    setRemoteGatewayInput(activeVault?.kind === "remote" ? activeVault.gateway_url || "" : "");
    setRemoteTokenInput("");
    setRemoteShowToken(false);
    setRemoteSyncClients(true);
  }, [activeVault?.id, activeVault?.kind, activeVault?.gateway_url]);

  const handleTestRemote = async () => {
    if (!activeVault || activeVault.kind !== "remote" || remoteBusy) return;
    setRemoteBusy("test");
    try {
      const res = await api.testRemote(activeVault.id);
      if (res.ok) {
        onToast(
          "success",
          `连接正常（${res.uid || activeVault.remote_uid || "ok"} · ${res.latency_ms}ms · ${res.docs ?? activeVault.docs} 篇文档）`,
        );
      } else {
        onToast("error", `连接异常：${res.error || "认证失败"}`);
      }
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setRemoteBusy("");
    }
  };

  const handleUpdateRemote = async () => {
    if (!activeVault || activeVault.kind !== "remote" || remoteBusy) return;
    const nextUrl = remoteGatewayInput.trim();
    const nextTok = remoteTokenInput.trim();
    if (!nextUrl && !nextTok) {
      onToast("info", "请填写新的访问密钥或网关地址");
      return;
    }
    setRemoteBusy("update");
    try {
      const res = await api.updateRemote({
        vault_id: activeVault.id,
        gateway_url: nextUrl || undefined,
        token: nextTok || undefined,
        sync_clients: remoteSyncClients,
      });
      setRemoteTokenInput("");
      onToast(
        "success",
        `已更新「${activeVault.name}」（${res.uid} · ${res.token_masked}）${remoteSyncClients ? "，并同步至已安装 AI 工具" : ""}`,
      );
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setRemoteBusy("");
    }
  };

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
            cluster_count: res.cluster_count,
          });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [
    activeVault?.id,
    activeVault?.generation_id,
    activePendingCount,
    llmCompiler?.skip_csv,
    llmCompiler?.project_rollup,
  ]);

  const [cancellingJobId, setCancellingJobId] = useState("");

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
        onToast("info", "已开始用 AI 整理全部待处理文档，完成后会自动更新索引");
      } else {
        onToast("info", "当前没有需要整理的新增或变更文档");
        onRefresh();
      }
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleCancelCompile = async (jobId: string) => {
    if (!jobId || cancellingJobId === jobId) return;
    setCancellingJobId(jobId);
    try {
      await api.cancelJob(jobId);
      onToast("info", "已请求停止整理，正在保存当前已完成页面并更新索引...");
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setCancellingJobId("");
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
      setStorageModeInput(
        info.recommended_storage_mode || (info.kind === "existing_vault" ? "in_place" : "managed"),
      );
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleMountExternalFolder = async (folderPath: string) => {
    if (!activeVault) return;
    try {
      const res = await api.mountFolder({
        vault_id: activeVault.id,
        external_folder: folderPath,
      });
      if (res.compile_job?.job_id && res.compile_job.status === "running") {
        onTrackCompileJob(activeVault.id, res.compile_job);
        onToast("success", `已零拷贝挂载「${res.mount_name}」（原目录只读），正在后台整理并更新索引`);
      } else {
        onToast("success", `已零拷贝挂载「${res.mount_name}」（原目录只读）并更新搜索索引`);
      }
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleUnmountFolder = async (vaultId: string, mountName: string) => {
    try {
      await api.unmountFolder({
        vault_id: vaultId,
        mount_name: mountName,
        purge_mirrors: true,
      });
      onToast("info", `已取消挂载「${mountName}」（原文件夹不受任何影响）`);
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleRelinkSource = async (folderPath: string, mountName = "") => {
    if (!activeVault) return;
    try {
      const res = await api.relinkVault({
        vault_id: activeVault.id,
        new_source_dir: folderPath,
        ...(mountName ? { mount_name: mountName } : {}),
      });
      onToast(
        "success",
        mountName
          ? `已重定位挂载点「${mountName}」到新目录（历史索引已无缝复用）`
          : `已重定位主源目录至 ${res.new_path}（未变更文件 0 重复提取）`,
      );
      onRefresh();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    }
  };

  const handleOpenOrCopyPath = async (targetPath: string, label = "路径") => {
    if (!targetPath) return;
    if (isTauriRuntime()) {
      const opened = await openNativeDirectory(targetPath);
      if (opened) return;
    }
    const copied = await copyToClipboard(targetPath);
    if (copied) {
      onToast("info", `已复制${label}：${targetPath}`);
    }
  };

  const executeConfirmedRemove = async (v: VaultInfo, cleanupWs: boolean) => {
    try {
      const res = await api.removeVault(v.id, cleanupWs);
      if (res.archived_to) {
        onToast(
          "info",
          `已移除「${v.name}」（原始文件夹完好无损；已整理的知识库/记忆已备份至 ${res.archived_to}）`,
        );
      } else {
        onToast("info", `已移除「${v.name}」（本地原始文档不会被删除）`);
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

  const chooseFolder = async (
    mode: "onboard" | "mount" | "relink" = "onboard",
    mountName = "",
  ) => {
    setPickerMode(mode);
    setRelinkMountName(mountName);
    const title =
      mode === "mount"
        ? "选择要挂载到当前知识库的文件夹（只读零拷贝）"
        : mode === "relink"
          ? mountName
            ? `为挂载点「${mountName}」选择新位置`
            : "选择移动/改名后的原始文档文件夹"
          : "选择要添加的本地文件夹";
    if (isTauriRuntime()) {
      const p = await pickNativeDirectory(title);
      if (p) {
        if (mode === "mount") {
          await handleMountExternalFolder(p);
        } else if (mode === "relink") {
          await handleRelinkSource(p, mountName);
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
        storage_mode: inspected?.kind === "existing_vault" ? "in_place" : storageModeInput,
        auto_compile: onboardAutoCompile,
      });

      const modeHint = res.storage_mode === "managed" ? "（原目录保持只读干净）" : "";
      if (res.compile_job?.job_id && res.compile_job.status === "running") {
        onTrackCompileJob(res.vault_id, res.compile_job);
        onToast("success", `知识库已添加${modeHint}，正在后台自动整理内容摘要`);
      } else if (onboardAutoCompile && !res.llm_ready && (res.uncompiled_count || 0) > 0) {
        onToast(
          "info",
          `知识库已添加${modeHint}（含 ${res.uncompiled_count} 篇待整理文档，配置顶部整理模型后即可自动整理）`,
        );
      } else {
        onToast("success", `知识库已添加${modeHint}并建立搜索索引`);
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
                        ? activeVault.ready
                          ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                          : "bg-rose-50 text-rose-700 dark:bg-rose-950/60 dark:text-rose-300"
                        : activeVault.source_available === false
                          ? "bg-rose-50 text-rose-700 dark:bg-rose-950/60 dark:text-rose-300"
                          : activeVault.ready
                            ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                            : "bg-amber-50 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                    }`}
                  >
                    {activeVault.kind === "remote"
                      ? activeVault.ready
                        ? "共享知识库 · 已连接"
                        : "共享知识库 · 连接异常"
                      : activeVault.source_available === false
                        ? "源目录离线"
                        : activeVault.ready
                          ? "就绪"
                          : "正在建立索引"}
                  </span>
                )}
                {activeVault && activeVault.kind === "local" && (
                  <span
                    className={`rounded-md px-2 py-0.5 text-xs font-medium ${
                      activeVault.storage_mode === "managed"
                        ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                        : "bg-[var(--bg-muted)] text-[var(--ink-secondary)]"
                    }`}
                  >
                    {activeVault.storage_mode === "managed" ? "原目录只读" : "原地自包含"}
                  </span>
                )}
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-2 font-mono text-xs text-[var(--ink-muted)]">
                <span className="truncate">
                  {activeVault?.kind === "remote"
                    ? activeVault.gateway_url || activeVault.mcp_url
                    : activeVault?.sources_path ||
                      activeVault?.path ||
                      "点击「添加文件夹」即可接入本地 Markdown、PDF、Word、PPT 等文档"}
                </span>
                {activeVault && activeVault.kind === "local" && (activeVault.sources_path || activeVault.path) && (
                  <button
                    type="button"
                    onClick={() =>
                      void handleOpenOrCopyPath(
                        activeVault.sources_path || activeVault.path,
                        "原始目录路径",
                      )
                    }
                    className="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 font-sans text-[11px] font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)] hover:text-[var(--accent)]"
                    title={isTauriRuntime() ? "在访达/资源管理器中打开" : "复制路径"}
                  >
                    {isTauriRuntime() ? (
                      <ExternalLink className="h-3 w-3" />
                    ) : (
                      <Copy className="h-3 w-3" />
                    )}
                    {isTauriRuntime() ? "打开" : "复制"}
                  </button>
                )}
                {activeVault &&
                  activeVault.kind === "local" &&
                  activeVault.storage_mode === "managed" &&
                  activeVault.source_available === false && (
                    <button
                      type="button"
                      onClick={() => void chooseFolder("relink")}
                      className="inline-flex shrink-0 items-center gap-1 rounded bg-amber-100 px-2 py-0.5 font-sans text-[11px] font-semibold text-amber-900 hover:bg-amber-200 dark:bg-amber-950/70 dark:text-amber-200"
                    >
                      <Link2 className="h-3 w-3" /> 重定位源目录
                    </button>
                  )}
              </div>
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
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono font-semibold text-[var(--ink-secondary)]">
                        {activeCompileJob.progress?.done ?? 0} /{" "}
                        {Math.max(1, activeCompileJob.progress?.total ?? 1)}
                        {(activeCompileJob.progress?.failed_count ?? 0) > 0 && (
                          <span className="ml-2 font-sans font-normal text-rose-600 dark:text-rose-400">
                            失败 {activeCompileJob.progress?.failed_count}
                          </span>
                        )}
                        {(activeCompileJob.progress?.batch_limit ?? 0) > 0 &&
                          (activeCompileJob.progress?.pending_total ?? activePendingCount) >
                            (activeCompileJob.progress?.total ?? 0) && (
                            <span className="ml-2 font-sans font-normal text-[var(--ink-muted)]">
                              本批上限 {activeCompileJob.progress?.batch_limit} 篇 · 共{" "}
                              {activeCompileJob.progress?.pending_total ?? activePendingCount} 篇待整理
                            </span>
                          )}
                        {(activeCompileJob.progress?.batch_limit ?? 0) === 0 &&
                          (activeCompileJob.progress?.pending_total ?? 0) >
                            (activeCompileJob.progress?.total ?? 0) && (
                            <span className="ml-2 font-sans font-normal text-[var(--ink-muted)]">
                              已归并覆盖 {activeCompileJob.progress?.pending_total} 篇待整理文档
                            </span>
                          )}
                      </span>
                      <button
                        type="button"
                        disabled={
                          cancellingJobId === activeCompileJob.job_id ||
                          activeCompileJob.progress?.phase === "cancelling"
                        }
                        onClick={() => void handleCancelCompile(activeCompileJob.job_id)}
                        className="inline-flex items-center gap-1 rounded-md border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2 py-0.5 text-[11px] font-medium text-[var(--ink-secondary)] hover:border-rose-400 hover:text-rose-600 disabled:opacity-50 dark:hover:text-rose-400"
                      >
                        <Square className="h-2.5 w-2.5 fill-current" />
                        {activeCompileJob.progress?.phase === "cancelling" ? "正在停止" : "停止"}
                      </button>
                    </div>
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
                      ? (activeVault.cluster_count ?? 0) > 0 &&
                        (activeVault.cluster_count ?? 0) < activeVault.uncompiled_count
                        ? `${activeVault.uncompiled_count} 份待整理 → 归并 ${activeVault.cluster_count} 篇`
                        : `${activeVault.uncompiled_count} 篇待整理`
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
                  {inspected.kind === "existing_vault"
                    ? `接入已有知识库（检测到 ${inspected.file_count ?? inspected.loose_total ?? 0} 个文档）`
                    : `添加本地文件夹（检测到 ${inspected.file_count ?? inspected.loose_total ?? 0} 个文档）`}
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

              {/* Storage Mode Selection */}
              {inspected.kind === "existing_vault" ? (
                <div className="mt-3 rounded-xl border border-emerald-300/70 bg-emerald-50/70 px-3.5 py-2 text-xs text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">
                  检测到该目录已是完整的知识库工作区，将直接原地接入，保留原有目录结构与历史数据。
                </div>
              ) : (
                <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label
                    className={`flex cursor-pointer items-start gap-2.5 rounded-xl border p-3 text-xs ${
                      storageModeInput === "managed"
                        ? "border-[var(--accent)] bg-[var(--bg-surface)] shadow-2xs"
                        : "border-[var(--border-default)] bg-[var(--bg-surface)]/70 hover:border-[var(--border-strong)]"
                    }`}
                  >
                    <input
                      type="radio"
                      name="onboardStorageMode"
                      checked={storageModeInput === "managed"}
                      onChange={() => setStorageModeInput("managed")}
                      className="mt-0.5"
                    />
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5 font-bold text-[var(--ink-primary)]">
                        <span>零侵入托管模式</span>
                        <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300">
                          推荐
                        </span>
                      </div>
                      <p className="mt-0.5 leading-relaxed text-[var(--ink-muted)]">
                        不移动、不改写原文件夹任何内容；索引与 AI 知识库存放在{" "}
                        <code className="font-mono">{vaultsDir || "~/.wiki-context/vaults"}</code>
                      </p>
                    </div>
                  </label>
                  <label
                    className={`flex cursor-pointer items-start gap-2.5 rounded-xl border p-3 text-xs ${
                      storageModeInput === "in_place"
                        ? "border-[var(--accent)] bg-[var(--bg-surface)] shadow-2xs"
                        : "border-[var(--border-default)] bg-[var(--bg-surface)]/70 hover:border-[var(--border-strong)]"
                    }`}
                  >
                    <input
                      type="radio"
                      name="onboardStorageMode"
                      checked={storageModeInput === "in_place"}
                      onChange={() => setStorageModeInput("in_place")}
                      className="mt-0.5"
                    />
                    <div className="min-w-0">
                      <div className="font-bold text-[var(--ink-primary)]">原地自包含模式</div>
                      <p className="mt-0.5 leading-relaxed text-[var(--ink-muted)]">
                        在所选文件夹内创建 史料 / 知识库 / 记忆 等目录（适合随身移动硬盘或独立 Git 仓库）
                      </p>
                    </div>
                  </label>
                </div>
              )}

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
                              {v.kind === "local" && v.storage_mode === "managed" && (
                                <span className="shrink-0 whitespace-nowrap rounded bg-[var(--accent-soft)] px-1.5 py-0.5 text-[11px] font-medium text-[var(--accent)]">
                                  原目录只读
                                </span>
                              )}
                              {v.kind === "local" && v.source_available === false && (
                                <span className="shrink-0 whitespace-nowrap rounded bg-rose-100 px-1.5 py-0.5 text-[11px] font-semibold text-rose-700 dark:bg-rose-950/60 dark:text-rose-300">
                                  源目录离线
                                </span>
                              )}
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
                              {v.kind === "remote" ? v.gateway_url : v.sources_path || v.path}
                            </div>
                            {v.kind === "local" && (v.source_mounts?.length || 0) > 0 && (
                              <div className="mt-0.5 text-[11px] text-[var(--ink-muted)]">
                                另挂载 {v.source_mounts?.length} 个外部文件夹
                              </div>
                            )}
                          </div>
                          <span
                            title={v.kind === "remote" && !v.ready ? v.online_error || "连接异常" : undefined}
                            className={`shrink-0 rounded-md px-2 py-0.5 text-xs font-medium ${
                              v.kind === "remote"
                                ? v.ready
                                  ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                                  : "bg-rose-50 text-rose-700 dark:bg-rose-950/60 dark:text-rose-300"
                                : v.ready
                                  ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
                                  : "bg-amber-50 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                            }`}
                          >
                            {v.kind === "remote"
                              ? v.ready
                                ? `共享库 · ${v.docs} 篇`
                                : "连接异常"
                              : `${v.docs} 篇文档 · ${v.wiki_docs} 篇整理`}
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
                              onClick={() => setRemoveTarget(v)}
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
                                  {node.mounted && (
                                    <span className="shrink-0 rounded bg-[var(--accent-soft)] px-1.5 py-0.5 font-sans text-[10px] font-semibold text-[var(--accent)]">
                                      挂载
                                    </span>
                                  )}
                                  <span className="rounded bg-[var(--bg-muted)] px-1.5 py-0.5 text-xs text-[var(--ink-muted)]">
                                    {node.children.length}
                                  </span>
                                </button>
                              ) : (
                                <span className="flex min-w-0 items-center gap-1.5 font-mono font-semibold text-[var(--ink-primary)]">
                                  <span className="truncate">{node.name}/</span>
                                  {node.mounted && (
                                    <span className="shrink-0 rounded bg-[var(--accent-soft)] px-1.5 py-0.5 font-sans text-[10px] font-semibold text-[var(--accent)]">
                                      挂载
                                    </span>
                                  )}
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

              {/* Collapsible Domain Profile Panel (AI Auto-Maintained + Manual Override) */}
              {activeVault && activeVault.kind === "local" && (
                <details className="group rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-4">
                  <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 text-[13px] font-bold text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]">
                    <span className="inline-flex items-center gap-2">
                      <Sparkles className="h-4 w-4 text-[var(--accent)]" />
                      <span>领域画像（用于引导 AI 精准提词）</span>
                      <span
                        className={`rounded px-2 py-0.5 text-[11px] font-semibold ${
                          activeVault.domain_profile?.locked || activeVault.domain_profile?.source === "manual"
                            ? "bg-amber-100 text-amber-800 dark:bg-amber-950/60 dark:text-amber-300"
                            : activeVault.domain_profile?.domain
                              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
                              : "bg-[var(--bg-muted)] text-[var(--ink-muted)]"
                        }`}
                      >
                        {activeVault.domain_profile?.locked || activeVault.domain_profile?.source === "manual"
                          ? "已手动锁定"
                          : activeVault.domain_profile?.domain
                            ? "AI 自动维护"
                            : "待生成"}
                      </span>
                    </span>
                    <span className="max-w-full truncate font-normal text-xs text-[var(--ink-muted)]">
                      {activeVault.domain_profile?.domain || "展开查看或手动配置"}
                    </span>
                  </summary>

                  <form
                    method="POST"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void handleDomainProfileAction("save");
                    }}
                    className="mt-4 space-y-3 border-t border-[var(--border-subtle)] pt-3 text-xs"
                  >
                    <div>
                      <label
                        htmlFor={`dp-domain-${activeVault.id}`}
                        className="mb-1 block font-medium text-[var(--ink-secondary)]"
                      >
                        行业与业务范围（≤60字，勿包含机密客户或金额）
                      </label>
                      <input
                        id={`dp-domain-${activeVault.id}`}
                        name="domain"
                        value={profileDomain}
                        onChange={(e) => setProfileDomain(e.target.value)}
                        placeholder="例：轨道交通弓网在线监测、接触网/靴轨检测及软硬件交付项目"
                        className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
                      />
                    </div>
                    <div>
                      <label
                        htmlFor={`dp-hints-${activeVault.id}`}
                        className="mb-1 block font-medium text-[var(--ink-secondary)]"
                      >
                        核心实体命名格式（用 / 分隔，3–5 项）
                      </label>
                      <input
                        id={`dp-hints-${activeVault.id}`}
                        name="entity_hints"
                        value={profileHints}
                        onChange={(e) => setProfileHints(e.target.value)}
                        placeholder="例：项目号(JCxx-xx) / 线路与车型 / 核心器件与指标(CTA120/导高/拉出值)"
                        className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
                      />
                    </div>
                    <div>
                      <label
                        htmlFor={`dp-tips-${activeVault.id}`}
                        className="mb-1 block font-medium text-[var(--ink-secondary)]"
                      >
                        AI 提词建议
                      </label>
                      <textarea
                        id={`dp-tips-${activeVault.id}`}
                        name="query_tips"
                        rows={2}
                        value={profileTips}
                        onChange={(e) => setProfileTips(e.target.value)}
                        placeholder="例：优先提炼项目号、线路、设备名与指标，剔除「简要说明、最近进展」等泛化问法"
                        className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-2 text-xs text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
                      />
                    </div>

                    <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          disabled={Boolean(profileBusy) || !llmCompiler?.ready}
                          onClick={() => void handleDomainProfileAction("generate")}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-muted)] disabled:opacity-50"
                        >
                          {profileBusy === "generate" ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <RefreshCw className="h-3.5 w-3.5" />
                          )}
                          AI 重新生成
                        </button>
                        {(activeVault.domain_profile?.locked || activeVault.domain_profile?.source === "manual") && (
                          <button
                            type="button"
                            disabled={Boolean(profileBusy)}
                            onClick={() => void handleDomainProfileAction("unlock")}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] px-3 py-1.5 font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)] disabled:opacity-50"
                          >
                            恢复 AI 自动维护
                          </button>
                        )}
                      </div>
                      <button
                        type="submit"
                        disabled={Boolean(profileBusy) || !profileDomain.trim()}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3.5 py-1.5 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
                      >
                        {profileBusy === "save" ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Check className="h-3.5 w-3.5" />
                        )}
                        保存手动画像
                      </button>
                    </div>
                  </form>
                </details>
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
                    (llmCompiler?.max_items_per_run ?? 0) > 0 &&
                    activePendingCount > (llmCompiler?.max_items_per_run ?? 0) && (
                      <p className="text-[11px] leading-relaxed text-[var(--ink-muted)]">
                        当前限制每次最多整理 {llmCompiler?.max_items_per_run} 篇，可在顶部「整理模型设置」切换为「全部跑完」
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
                            待整理文件（新增 {compilePlan.uncompiled_sources.length}
                            {(compilePlan.cluster_count ?? 0) > 0 &&
                            (compilePlan.cluster_count ?? 0) < compilePlan.uncompiled_sources.length
                              ? ` → 归并 ${compilePlan.cluster_count} 篇`
                              : ""}{" "}
                            · 需更新 {compilePlan.stale_pages.length}）
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
                    onClick={() => void chooseFolder("mount")}
                    className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] py-2 font-semibold text-[var(--ink-primary)] hover:bg-[var(--bg-subtle)]"
                  >
                    <FolderInput className="h-3.5 w-3.5 text-[var(--accent)]" />
                    挂载更多文件夹（零拷贝只读）
                  </button>

                  {/* Source Mapping & Extra Mounts Panel */}
                  <div className="space-y-2 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-3">
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1.5 font-bold text-[var(--ink-secondary)]">
                        <HardDrive className="h-3.5 w-3.5 text-[var(--accent)]" />
                        源目录与挂载点
                      </span>
                      <span className="text-[11px] text-[var(--ink-muted)]">只读映射</span>
                    </div>

                    {/* Primary Source Root */}
                    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-2">
                      <div className="flex items-center justify-between gap-1.5">
                        <span className="flex items-center gap-1.5 font-semibold text-[var(--ink-primary)]">
                          <span
                            className={`h-2 w-2 rounded-full ${
                              activeVault.source_available === false
                                ? "bg-rose-500"
                                : "bg-emerald-500"
                            }`}
                          />
                          主源目录
                        </span>
                        <div className="flex items-center gap-1">
                          {activeVault.storage_mode === "managed" && (
                            <button
                              type="button"
                              onClick={() => void chooseFolder("relink")}
                              className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[11px] font-medium text-[var(--accent)] hover:bg-[var(--accent-soft)]"
                              title="原文件夹移动或改名后重新绑定路径，无需重新整理"
                            >
                              <Link2 className="h-3 w-3" /> 重定位
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() =>
                              void handleOpenOrCopyPath(
                                activeVault.sources_path || activeVault.path,
                                "主源目录路径",
                              )
                            }
                            className="rounded p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-surface)] hover:text-[var(--ink-primary)]"
                            title={isTauriRuntime() ? "打开目录" : "复制路径"}
                          >
                            {isTauriRuntime() ? (
                              <ExternalLink className="h-3 w-3" />
                            ) : (
                              <Copy className="h-3 w-3" />
                            )}
                          </button>
                        </div>
                      </div>
                      <div className="mt-1 truncate font-mono text-[11px] text-[var(--ink-muted)]">
                        {activeVault.sources_path || activeVault.path}
                      </div>
                    </div>

                    {/* Extra Zero-Copy Mounts */}
                    {(activeVault.source_mounts || []).map((mnt) => (
                      <div
                        key={mnt.name}
                        className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-2"
                      >
                        <div className="flex items-center justify-between gap-1.5">
                          <span className="flex min-w-0 items-center gap-1.5 font-mono font-semibold text-[var(--ink-primary)]">
                            <span
                              className={`h-2 w-2 shrink-0 rounded-full ${
                                mnt.available ? "bg-emerald-500" : "bg-rose-500"
                              }`}
                            />
                            <span className="truncate">{mnt.name}/</span>
                          </span>
                          <div className="flex shrink-0 items-center gap-1">
                            <button
                              type="button"
                              onClick={() => void chooseFolder("relink", mnt.name)}
                              className="rounded p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-surface)] hover:text-[var(--accent)]"
                              title="重定位此挂载目录"
                            >
                              <Link2 className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => void handleUnmountFolder(activeVault.id, mnt.name)}
                              className="rounded p-1 text-[var(--ink-muted)] hover:bg-rose-50 hover:text-rose-600 dark:hover:bg-rose-950/50 dark:hover:text-rose-400"
                              title="取消挂载（不删除原文件夹）"
                            >
                              <Unlink className="h-3 w-3" />
                            </button>
                          </div>
                        </div>
                        <div className="mt-1 truncate font-mono text-[11px] text-[var(--ink-muted)]">
                          {mnt.path}
                        </div>
                      </div>
                    ))}
                  </div>

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

              {/* Remote Connection & Token Management (for kind === "remote") */}
              {activeVault.kind === "remote" && (
                <div className="space-y-3 rounded-xl border border-[var(--border-default)] bg-[var(--bg-surface)] p-3.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5 font-bold text-[var(--ink-primary)]">
                      <KeyRound className="h-3.5 w-3.5 text-[var(--accent)]" />
                      远程连接与密钥管理
                    </span>
                    <span
                      className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ${
                        activeVault.ready
                          ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
                          : "bg-rose-100 text-rose-700 dark:bg-rose-950/60 dark:text-rose-300"
                      }`}
                    >
                      {activeVault.ready ? "已连接" : "连接异常"}
                    </span>
                  </div>

                  {!activeVault.ready && activeVault.online_error && (
                    <div className="flex items-start gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-2.5 py-2 text-[11px] leading-relaxed text-rose-800 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-300">
                      <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <span>{activeVault.online_error}</span>
                    </div>
                  )}

                  <div className="space-y-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-2.5 text-[11px]">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[var(--ink-muted)]">授权账号</span>
                      <span className="font-mono font-semibold text-[var(--ink-primary)]">
                        {activeVault.remote_uid || "—"}
                        {(activeVault.remote_groups || []).length > 0
                          ? ` (${(activeVault.remote_groups || []).join(", ")})`
                          : ""}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[var(--ink-muted)]">当前密钥</span>
                      <span className="font-mono text-[var(--ink-primary)]">
                        {activeVault.token_masked || "未配置"}
                      </span>
                    </div>
                    {activeVault.mcp_url && (
                      <div className="pt-1">
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--ink-muted)]">MCP 地址</span>
                          <button
                            type="button"
                            onClick={() => {
                              void copyToClipboard(activeVault.mcp_url || "").then((ok) => {
                                if (ok) onToast("info", "已复制 MCP 地址");
                              });
                            }}
                            className="inline-flex items-center gap-1 font-medium text-[var(--accent)] hover:underline"
                          >
                            <Copy className="h-3 w-3" /> 复制
                          </button>
                        </div>
                        <div className="mt-0.5 truncate font-mono text-[11px] text-[var(--ink-secondary)]">
                          {activeVault.mcp_url}
                        </div>
                      </div>
                    )}
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <button
                      type="button"
                      disabled={Boolean(remoteBusy)}
                      onClick={() => void handleTestRemote()}
                      className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] py-1.5 font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)] disabled:opacity-50"
                    >
                      {remoteBusy === "test" ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--accent)]" />
                      ) : (
                        <RefreshCw className="h-3.5 w-3.5 text-[var(--accent)]" />
                      )}
                      测试连接
                    </button>
                    <button
                      type="button"
                      disabled={Boolean(remoteBusy)}
                      onClick={() => setRemoveTarget(activeVault)}
                      className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50/70 py-1.5 font-semibold text-rose-700 hover:bg-rose-100 dark:border-rose-800 dark:bg-rose-950/35 dark:text-rose-300 disabled:opacity-50"
                    >
                      <Unlink className="h-3.5 w-3.5" />
                      断开连接
                    </button>
                  </div>

                  <details open={!activeVault.ready} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)] p-2.5">
                    <summary className="flex cursor-pointer list-none items-center justify-between font-semibold text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]">
                      <span>更新访问密钥 / 网关地址</span>
                      <ChevronDown className="h-3.5 w-3.5 text-[var(--ink-muted)]" />
                    </summary>

                    <form
                      method="POST"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void handleUpdateRemote();
                      }}
                      className="mt-2.5 space-y-2.5 border-t border-[var(--border-subtle)] pt-2.5"
                    >
                      <div>
                        <label
                          htmlFor={`vault-remote-url-${activeVault.id}`}
                          className="mb-1 block font-medium text-[var(--ink-secondary)]"
                        >
                          网关地址
                        </label>
                        <input
                          id={`vault-remote-url-${activeVault.id}`}
                          name="gateway_url"
                          type="url"
                          value={remoteGatewayInput}
                          onChange={(e) => setRemoteGatewayInput(e.target.value)}
                          placeholder="http://100.64.195.4:8090"
                          className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 font-mono text-xs text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
                        />
                      </div>
                      <div>
                        <label
                          htmlFor={`vault-remote-tok-${activeVault.id}`}
                          className="mb-1 block font-medium text-[var(--ink-secondary)]"
                        >
                          新访问密钥（仅改地址时可留空）
                        </label>
                        <div className="relative flex items-center">
                          <input
                            id={`vault-remote-tok-${activeVault.id}`}
                            name="token"
                            type={remoteShowToken ? "text" : "password"}
                            value={remoteTokenInput}
                            onChange={(e) => setRemoteTokenInput(e.target.value)}
                            placeholder="粘贴服务端新发放的 wctx_..."
                            className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] py-1.5 pr-8 pl-2.5 font-mono text-xs text-[var(--ink-primary)] focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
                          />
                          <button
                            type="button"
                            aria-label={remoteShowToken ? "隐藏密钥" : "显示密钥"}
                            onClick={() => setRemoteShowToken((s) => !s)}
                            className="absolute right-2 text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
                          >
                            {remoteShowToken ? (
                              <EyeOff className="h-3.5 w-3.5" />
                            ) : (
                              <Eye className="h-3.5 w-3.5" />
                            )}
                          </button>
                        </div>
                      </div>
                      <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-[var(--ink-secondary)]">
                        <input
                          type="checkbox"
                          checked={remoteSyncClients}
                          onChange={(e) => setRemoteSyncClients(e.target.checked)}
                        />
                        <span>同时同步更新已连接的 AI 工具配置</span>
                      </label>
                      <button
                        type="submit"
                        disabled={Boolean(remoteBusy)}
                        className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-[var(--accent)] py-1.5 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
                      >
                        {remoteBusy === "update" ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Check className="h-3.5 w-3.5" />
                        )}
                        {remoteBusy === "update" ? "验证更新中..." : "验证并更新密钥"}
                      </button>
                    </form>
                  </details>
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
                        {activeVault.kind === "remote"
                          ? "其他电脑共享"
                          : activeVault.storage_mode === "managed"
                            ? "零侵入托管（原目录只读）"
                            : "原地自包含目录"}
                      </span>
                    </div>
                    {activeVault.kind === "local" && (
                      <div className="py-2">
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--ink-muted)]">工作区目录</span>
                          <button
                            type="button"
                            onClick={() =>
                              void handleOpenOrCopyPath(
                                activeVault.workspace_path || activeVault.path,
                                "工作区路径",
                              )
                            }
                            className="inline-flex items-center gap-1 font-medium text-[var(--accent)] hover:underline"
                          >
                            {isTauriRuntime() ? "打开工作区" : "复制路径"}
                          </button>
                        </div>
                        <div className="mt-0.5 truncate font-mono text-[11px] text-[var(--ink-secondary)]">
                          {activeVault.workspace_path || activeVault.path}
                        </div>
                      </div>
                    )}
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
        title={
          pickerMode === "mount"
            ? "选择要挂载的文件夹（零拷贝只读）"
            : pickerMode === "relink"
              ? relinkMountName
                ? `为挂载点「${relinkMountName}」选择新目录`
                : "选择移动或改名后的原始文档目录"
              : "选择要添加的本地文件夹"
        }
        onClose={() => setPickerOpen(false)}
        onSelect={(p) => {
          if (pickerMode === "mount") {
            void handleMountExternalFolder(p);
          } else if (pickerMode === "relink") {
            void handleRelinkSource(p, relinkMountName);
          } else {
            void inspectSelectedPath(p);
          }
        }}
      />

      {/* Native <dialog> Safe Removal Confirmation */}
      <dialog
        ref={removeDialogRef}
        aria-labelledby="remove-vault-title"
        onClose={() => {
          const dlg = removeDialogRef.current;
          const choice = dlg?.returnValue || "";
          if (dlg) dlg.returnValue = "";
          const target = removeTarget;
          const cleanup = removeCleanupWs;
          setRemoveTarget(null);
          if (choice === "confirm" && target) {
            void executeConfirmedRemove(target, cleanup);
          }
        }}
        className="m-auto w-full max-w-md rounded-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] p-5 text-[var(--ink-primary)] shadow-2xl"
      >
        {removeTarget && (
          <form method="dialog" className="space-y-3">
            <h1 id="remove-vault-title" className="text-[15px] font-bold text-[var(--ink-primary)]">
              移除知识库「{removeTarget.name}」？
            </h1>
            <p className="text-xs leading-relaxed text-[var(--ink-secondary)]">
              {removeTarget.kind === "remote"
                ? "仅断开此远程共享知识库的本地连接，不会影响提供端电脑上的任何数据。"
                : "绝对不会删除或修改你的原始文档文件夹。"}
            </p>
            {removeTarget.kind === "local" && (
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-3 py-2 font-mono text-[11px] text-[var(--ink-secondary)]">
                <div className="truncate">
                  原目录（完好保留）：{removeTarget.sources_path || removeTarget.path}
                </div>
              </div>
            )}
            {removeTarget.kind === "local" && removeTarget.storage_mode === "managed" && (
              <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)] p-2.5 text-xs">
                <input
                  type="checkbox"
                  checked={removeCleanupWs}
                  onChange={(e) => setRemoveCleanupWs(e.target.checked)}
                  className="mt-0.5"
                />
                <span className="leading-relaxed text-[var(--ink-secondary)]">
                  同时清理可重建缓存（<code className="font-mono">史料_md/</code>）；若包含已整理的{" "}
                  <code className="font-mono">知识库/</code> 或{" "}
                  <code className="font-mono">记忆/</code>，将自动安全归档至{" "}
                  <code className="font-mono">.trash/</code> 目录方便恢复
                </span>
              </label>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="submit"
                value="cancel"
                autoFocus
                className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3.5 py-1.5 text-xs font-semibold text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)]"
              >
                取消
              </button>
              <button
                type="submit"
                value="confirm"
                className="rounded-lg bg-rose-600 px-4 py-1.5 text-xs font-semibold text-white hover:bg-rose-700"
              >
                确认移除
              </button>
            </div>
          </form>
        )}
      </dialog>

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
