import React from "react";
import { CheckCircle2, AlertCircle, Info, X } from "lucide-react";

export interface ToastItem {
  id: string;
  kind: "success" | "error" | "info";
  message: string;
}

export const ToastHost: React.FC<{ toasts: ToastItem[]; onDismiss: (id: string) => void }> = ({
  toasts,
  onDismiss,
}) => {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed right-5 bottom-5 z-50 flex max-w-md flex-col gap-2">
      {toasts.map((t) => {
        const borderCls =
          t.kind === "success"
            ? "border-emerald-300 dark:border-emerald-700"
            : t.kind === "error"
              ? "border-rose-300 dark:border-rose-700"
              : "border-[var(--border-strong)]";
        return (
          <div
            key={t.id}
            className={`flex items-start gap-2.5 rounded-xl border bg-[var(--bg-surface)] px-3.5 py-2.5 text-[13px] text-[var(--ink-primary)] shadow-lg shadow-black/15 ${borderCls}`}
          >
            {t.kind === "success" ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
            ) : t.kind === "error" ? (
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-600 dark:text-rose-400" />
            ) : (
              <Info className="mt-0.5 h-4 w-4 shrink-0 text-[var(--accent)]" />
            )}
            <span className="flex-1 leading-relaxed">{t.message}</span>
            <button
              onClick={() => onDismiss(t.id)}
              className="rounded p-0.5 text-[var(--ink-muted)] hover:bg-[var(--bg-muted)] hover:text-[var(--ink-primary)]"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
};
