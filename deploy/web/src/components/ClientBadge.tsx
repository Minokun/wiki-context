import React from "react";
import { BrandGlyph } from "./BrandIcons";

const CLIENT_LABELS: Record<string, string> = {
  dsh: "DSH",
  cursor: "Cursor",
  claude: "Claude Code",
  "claude-desktop": "Claude Desktop",
  codex: "Codex CLI",
  workbuddy: "WorkBuddy",
  agy: "Antigravity",
  vscode: "VS Code",
  windsurf: "Windsurf",
  trae: "Trae",
  kiro: "Kiro",
  cline: "Cline",
  "roo-code": "Roo Code",
  "gemini-cli": "Gemini CLI",
  "qwen-code": "Qwen Code",
  kimi: "Kimi CLI",
  opencode: "OpenCode",
  zed: "Zed",
  lmstudio: "LM Studio",
  playground: "检索测试",
  cli: "CLI",
};

export const ClientBadge: React.FC<{ client: string; surface?: "light" | "dark" }> = ({
  client,
  surface,
}) => {
  const raw = client || "cli";
  const key = raw.toLowerCase();
  if (key.startsWith("remote:")) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-md border border-teal-200 bg-teal-50 px-2 py-0.5 text-xs font-medium text-teal-800 dark:border-cyan-500/30 dark:bg-cyan-950/60 dark:text-cyan-300">
        共享设备: {raw.slice(7)}
      </span>
    );
  }
  const label = CLIENT_LABELS[key] || raw;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-default)] bg-[var(--bg-subtle)] px-2 py-0.5 text-xs font-medium text-[var(--ink-primary)]">
      <BrandGlyph brand={key} surface={surface} className="h-3.5 w-3.5" />
      <span>{label}</span>
    </span>
  );
};
