import React, { useEffect, useState } from "react";
import { Folder, FolderPlus, ArrowUp, Check, X } from "lucide-react";
import { api } from "../api/client";

interface FolderPickerModalProps {
  open: boolean;
  title?: string;
  onClose: () => void;
  onSelect: (path: string) => void;
}

export const FolderPickerModal: React.FC<FolderPickerModalProps> = ({
  open,
  title = "选择本地文件夹",
  onClose,
  onSelect,
}) => {
  const [current, setCurrent] = useState("");
  const [parent, setParent] = useState("");
  const [dirs, setDirs] = useState<{ name: string; path: string; is_vault: boolean }[]>([]);
  const [shortcuts, setShortcuts] = useState<{ label: string; path: string }[]>([]);
  const [fileCount, setFileCount] = useState(0);
  const [newName, setNewName] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const browse = async (target = "") => {
    setLoading(true);
    setErr("");
    try {
      const res = await api.fsList(target);
      setCurrent(res.current);
      setParent(res.parent);
      setDirs(res.directories || []);
      setShortcuts(res.shortcuts || []);
      setFileCount(res.file_count_here || 0);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void browse("");
  }, [open]);

  if (!open) return null;

  const handleMkdir = async () => {
    const clean = newName.trim();
    if (!clean || !current) return;
    try {
      const res = await api.fsMkdir(current, clean);
      setNewName("");
      await browse(res.created);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-xs">
      <div className="flex max-h-[84vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xl">
        <div className="flex items-center justify-between border-b border-[var(--border-default)] bg-[var(--bg-subtle)] px-5 py-3.5">
          <h3 className="text-[14px] font-semibold text-[var(--ink-primary)]">{title}</h3>
          <button
            onClick={onClose}
            className="rounded-lg p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-muted)] hover:text-[var(--ink-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex flex-wrap gap-1.5 border-b border-[var(--border-subtle)] bg-[var(--bg-subtle)] px-5 py-2.5">
          {shortcuts.map((s) => (
            <button
              key={s.path}
              onClick={() => void browse(s.path)}
              className="rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2.5 py-1 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--accent)] hover:text-[var(--accent)]"
            >
              {s.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-5 py-2.5 text-xs">
          <button
            disabled={!parent}
            onClick={() => parent && void browse(parent)}
            className="inline-flex items-center gap-1 rounded-md border border-[var(--border-default)] bg-[var(--bg-surface)] px-2.5 py-1.5 font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-subtle)] disabled:opacity-40"
          >
            <ArrowUp className="h-3.5 w-3.5" /> 上一级
          </button>
          <div className="min-w-0 flex-1 truncate rounded-md border border-[var(--border-default)] bg-[var(--bg-subtle)] px-3 py-1.5 font-mono text-xs text-[var(--ink-primary)]">
            {current || "加载中..."}
          </div>
          <span className="shrink-0 text-xs text-[var(--ink-muted)]">文件 {fileCount}</span>
        </div>

        {err && (
          <div className="bg-rose-50 px-5 py-2 text-xs text-rose-700 dark:bg-rose-950/50 dark:text-rose-300">
            {err}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-5 py-3">
          {loading ? (
            <div className="py-10 text-center text-[13px] text-[var(--ink-muted)]">正在读取目录...</div>
          ) : dirs.length === 0 ? (
            <div className="py-10 text-center text-[13px] text-[var(--ink-muted)]">此目录下没有子文件夹</div>
          ) : (
            <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {dirs.map((d) => (
                <button
                  key={d.path}
                  onClick={() => void browse(d.path)}
                  className="flex items-center justify-between rounded-lg border border-[var(--border-default)] bg-[var(--bg-subtle)] px-3 py-2 text-left text-[13px] transition hover:border-[var(--accent)] hover:bg-[var(--accent-soft)]"
                >
                  <span className="flex min-w-0 items-center gap-2 text-[var(--ink-primary)]">
                    <Folder className="h-4 w-4 shrink-0 text-[var(--accent)]" />
                    <span className="truncate font-medium">{d.name}</span>
                  </span>
                  {d.is_vault && (
                    <span className="ml-2 shrink-0 rounded bg-emerald-50 px-1.5 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300">
                      知识库
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border-default)] bg-[var(--bg-subtle)] px-5 py-3">
          <div className="flex items-center gap-2">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="新建子文件夹名称..."
              className="w-44 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
            />
            <button
              onClick={() => void handleMkdir()}
              className="inline-flex items-center gap-1 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs font-medium text-[var(--ink-secondary)] hover:bg-[var(--bg-muted)]"
            >
              <FolderPlus className="h-3.5 w-3.5" /> 新建
            </button>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 text-xs font-medium text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
            >
              取消
            </button>
            <button
              disabled={!current}
              onClick={() => {
                onSelect(current);
                onClose();
              }}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-1.5 text-xs font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-40"
            >
              <Check className="h-3.5 w-3.5" /> 选择当前目录
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
