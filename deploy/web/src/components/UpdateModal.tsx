import React, { useEffect } from "react";
import { AlertTriangle, ArrowUpCircle, Download, Loader2, X } from "lucide-react";
import type { AppUpdateInfo, UpdateProgress } from "../platform/adapter";

interface UpdateModalProps {
  open: boolean;
  updateInfo: AppUpdateInfo | null;
  installing: boolean;
  progress: UpdateProgress | null;
  error: string;
  onInstall: () => void;
  onClose: () => void;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export const UpdateModal: React.FC<UpdateModalProps> = ({
  open,
  updateInfo,
  installing,
  progress,
  error,
  onInstall,
  onClose,
}) => {
  useEffect(() => {
    if (!open || installing) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, installing, onClose]);

  if (!open || !updateInfo) return null;

  const pct = progress?.percent;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-xs"
      onClick={() => {
        if (!installing) onClose();
      }}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b border-[var(--border-default)] bg-[var(--bg-subtle)] px-5 py-3.5">
          <div>
            <div className="flex items-center gap-2">
              <ArrowUpCircle className="h-4 w-4 text-[var(--accent)]" />
              <h3 className="text-[14px] font-bold text-[var(--ink-primary)]">
                发现新版本 v{updateInfo.version}
              </h3>
            </div>
            <p className="mt-1 font-mono text-xs text-[var(--ink-muted)]">
              当前 v{updateInfo.currentVersion} → 新版 v{updateInfo.version}
            </p>
          </div>
          {!installing && (
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-muted)] hover:text-[var(--ink-primary)]"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>

        <div className="space-y-4 overflow-y-auto p-5 text-xs">
          {updateInfo.body && (
            <div className="rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-3.5">
              <div className="mb-1.5 font-semibold text-[var(--ink-secondary)]">更新内容</div>
              <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap font-sans leading-relaxed text-[var(--ink-secondary)]">
                {updateInfo.body}
              </pre>
            </div>
          )}

          {installing && progress && (
            <div className="space-y-2 rounded-xl border border-[var(--accent)]/30 bg-[var(--accent-soft)]/50 p-3.5">
              <div className="flex items-center justify-between font-medium text-[var(--ink-primary)]">
                <span className="inline-flex items-center gap-1.5">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--accent)]" />
                  {progress.phase === "installing"
                    ? "正在校验签名并安装重启..."
                    : pct !== undefined
                      ? `正在下载更新 ${pct}%`
                      : "正在下载更新..."}
                </span>
                {progress.downloaded > 0 && (
                  <span className="font-mono text-[11px] text-[var(--ink-muted)]">
                    {formatBytes(progress.downloaded)}
                    {progress.total ? ` / ${formatBytes(progress.total)}` : ""}
                  </span>
                )}
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--bg-muted)]">
                <div
                  className="h-full rounded-full bg-[var(--accent)] transition-all duration-200"
                  style={{ width: `${pct !== undefined ? Math.max(4, pct) : 65}%` }}
                />
              </div>
            </div>
          )}

          {error && (
            <div className="flex items-start gap-2 rounded-xl border border-rose-300 bg-rose-50 px-3.5 py-2.5 text-rose-800 dark:border-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0 break-all">{error}</div>
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-[var(--border-default)] bg-[var(--bg-subtle)] px-5 py-3 text-xs">
          <button
            type="button"
            disabled={installing}
            onClick={onClose}
            className="rounded-lg border border-[var(--border-default)] bg-[var(--bg-surface)] px-3.5 py-1.5 font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)] disabled:opacity-40"
          >
            稍后再说
          </button>
          <button
            type="button"
            disabled={installing}
            onClick={onInstall}
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-1.5 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" />
            {installing ? "正在更新..." : error ? "重试更新" : "立即更新并重启"}
          </button>
        </div>
      </div>
    </div>
  );
};
