import React, { useEffect, useState } from "react";
import { Check, Cpu, KeyRound, Loader2, X, Zap } from "lucide-react";
import { api } from "../api/client";
import type { LlmCompilerStatus } from "../types";

interface AgentConfigModalProps {
  open: boolean;
  llmCompiler: LlmCompilerStatus | null;
  onClose: () => void;
  onSaved: () => void;
  onToast: (kind: "success" | "error" | "info", msg: string) => void;
}

const DEFAULT_PRESETS = [
  {
    id: "deepseek",
    label: "DeepSeek",
    protocol: "openai",
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-chat",
  },
  {
    id: "openai",
    label: "OpenAI",
    protocol: "openai",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
  },
  {
    id: "qwen",
    label: "通义千问 (DashScope)",
    protocol: "openai",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
  },
  {
    id: "zhipu",
    label: "智谱 GLM (BigModel)",
    protocol: "openai",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    model: "glm-4-flash",
  },
  {
    id: "anthropic",
    label: "Anthropic Claude",
    protocol: "anthropic",
    base_url: "https://api.anthropic.com/v1",
    model: "claude-sonnet-4-20250514",
  },
  {
    id: "local",
    label: "本地私有模型 (llama.cpp / vLLM / Ollama)",
    protocol: "openai",
    base_url: "http://127.0.0.1:8080/v1",
    model: "local-model",
  },
  {
    id: "custom",
    label: "自定义兼容接口",
    protocol: "openai",
    base_url: "",
    model: "",
  },
];

function isLoopbackUrl(raw: string): boolean {
  return /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\]|0\.0\.0\.0)(?::\d+)?(\/|$)/i.test(raw.trim());
}

export const AgentConfigModal: React.FC<AgentConfigModalProps> = ({
  open,
  llmCompiler,
  onClose,
  onSaved,
  onToast,
}) => {
  const presets = llmCompiler?.presets?.length ? llmCompiler.presets : DEFAULT_PRESETS;
  const [provider, setProvider] = useState(llmCompiler?.provider || "deepseek");
  const [protocol, setProtocol] = useState(llmCompiler?.protocol || "openai");
  const [baseUrl, setBaseUrl] = useState(
    llmCompiler?.base_url || "https://api.deepseek.com/v1",
  );
  const [model, setModel] = useState(llmCompiler?.model || "deepseek-chat");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [autoSync, setAutoSync] = useState(llmCompiler?.auto_compile_on_sync ?? false);
  const [maxItems, setMaxItems] = useState(llmCompiler?.max_items_per_run ?? 0);
  const [workers, setWorkers] = useState(llmCompiler?.workers ?? 4);
  const [maxTokens, setMaxTokens] = useState(llmCompiler?.max_tokens ?? 4096);
  const [disableThinking, setDisableThinking] = useState(llmCompiler?.disable_thinking ?? false);
  const [skipCsv, setSkipCsv] = useState(llmCompiler?.skip_csv ?? true);
  const [projectRollup, setProjectRollup] = useState(llmCompiler?.project_rollup ?? true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const keyOptional = provider === "local" || isLoopbackUrl(baseUrl);

  useEffect(() => {
    if (!open || !llmCompiler) return;
    setProvider(llmCompiler.provider || "deepseek");
    setProtocol(llmCompiler.protocol || "openai");
    setBaseUrl(llmCompiler.base_url || "https://api.deepseek.com/v1");
    setModel(llmCompiler.model || "deepseek-chat");
    setAutoSync(llmCompiler.auto_compile_on_sync ?? false);
    setMaxItems(llmCompiler.max_items_per_run ?? 0);
    setWorkers(llmCompiler.workers ?? 4);
    setMaxTokens(llmCompiler.max_tokens ?? 4096);
    setDisableThinking(llmCompiler.disable_thinking ?? llmCompiler.provider === "local");
    setSkipCsv(llmCompiler.skip_csv ?? true);
    setProjectRollup(llmCompiler.project_rollup ?? true);
    setApiKeyInput("");
    setTestResult(null);
  }, [open, llmCompiler]);

  useEffect(() => {
    if (!open) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const handleSelectPreset = (presetId: string) => {
    setProvider(presetId);
    const p = presets.find((x) => x.id === presetId);
    if (p) {
      setProtocol(p.protocol || "openai");
      if (p.base_url) setBaseUrl(p.base_url);
      if (p.model) setModel(p.model);
    }
    if (presetId === "local") {
      setDisableThinking(true);
    }
    setTestResult(null);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const saved = await api.saveLlmConfig({
        provider,
        protocol,
        base_url: baseUrl.trim(),
        model: model.trim(),
        ...(apiKeyInput.trim() ? { api_key: apiKeyInput.trim() } : {}),
        auto_compile_on_sync: autoSync,
        max_items_per_run: maxItems,
        workers,
        max_tokens: maxTokens,
        disable_thinking: disableThinking,
        skip_csv: skipCsv,
        project_rollup: projectRollup,
      });
      setApiKeyInput("");
      onToast(
        "success",
        saved.ready
          ? `模型设置已保存（当前使用 ${saved.model}，并发 ${saved.workers ?? workers}）`
          : "设置已保存，请补充 API Key 以启用自动整理",
      );
      onSaved();
      onClose();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleClearKey = async () => {
    setSaving(true);
    try {
      await api.saveLlmConfig({ clear_api_key: true });
      setApiKeyInput("");
      setTestResult(null);
      onToast("info", "已清除本机保存的 API Key");
      onSaved();
    } catch (e) {
      onToast("error", e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.testLlmConfig({
        provider,
        protocol,
        base_url: baseUrl.trim(),
        model: model.trim(),
        disable_thinking: disableThinking,
        ...(apiKeyInput.trim() ? { api_key: apiKeyInput.trim() } : {}),
      });
      setTestResult({
        ok: true,
        msg: `连接成功 (${res.latency_ms} ms，回复: ${res.reply})`,
      });
      if (apiKeyInput.trim() || keyOptional) {
        await api.saveLlmConfig({
          provider,
          protocol,
          base_url: baseUrl.trim(),
          model: model.trim(),
          ...(apiKeyInput.trim() ? { api_key: apiKeyInput.trim() } : {}),
          auto_compile_on_sync: autoSync,
          max_items_per_run: maxItems,
          workers,
          max_tokens: maxTokens,
          disable_thinking: disableThinking,
          skip_csv: skipCsv,
          project_rollup: projectRollup,
        });
        setApiKeyInput("");
        onSaved();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setTestResult({ ok: false, msg });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-xs"
      onClick={onClose}
    >
      <div
        className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-3 border-b border-[var(--border-default)] bg-[var(--bg-subtle)] px-5 py-3.5">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Cpu className="h-4 w-4 text-[var(--accent)]" />
              <h3 className="text-[14px] font-bold text-[var(--ink-primary)]">整理模型设置</h3>
              <span
                className={`rounded-md px-2 py-0.5 text-xs font-semibold ${
                  llmCompiler?.ready
                    ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-300"
                    : "bg-amber-100 text-amber-900 dark:bg-amber-950/60 dark:text-amber-300"
                }`}
              >
                {llmCompiler?.ready ? "已配置" : "未配置"}
              </span>
            </div>
            <p className="mt-1 text-xs text-[var(--ink-muted)]">
              配置一次即可。添加文件夹或新增文档时，将自动用该模型整理内容摘要并更新搜索索引。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-[var(--ink-muted)] hover:bg-[var(--bg-muted)] hover:text-[var(--ink-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 space-y-4 overflow-y-auto p-5 text-xs">
          {/* Provider Presets */}
          <div>
            <div className="mb-2 font-bold text-[var(--ink-primary)]">快捷选择模型厂商</div>
            <div className="flex flex-wrap gap-1.5">
              {presets.map((p) => {
                const active = provider === p.id;
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => handleSelectPreset(p.id)}
                    className={`rounded-lg border px-3 py-1.5 font-semibold ${
                      active
                        ? "border-[var(--accent)] bg-[var(--accent)] text-white"
                        : "border-[var(--border-strong)] bg-[var(--bg-subtle)] text-[var(--ink-secondary)] hover:border-[var(--accent)]"
                    }`}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
                接口类型
              </label>
              <select
                value={protocol}
                onChange={(e) => setProtocol(e.target.value)}
                className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-2 text-xs text-[var(--ink-primary)]"
              >
                <option value="openai">OpenAI 兼容接口</option>
                <option value="anthropic">Anthropic 接口</option>
              </select>
            </div>
            <div>
              <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
                模型名称
              </label>
              <input
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="如 deepseek-chat / qwen-plus"
                className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-2 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
              />
            </div>
          </div>

          <div>
            <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
              接口地址 (Base URL)
            </label>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.deepseek.com/v1"
              className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-2 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
            />
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <label className="flex items-center gap-1.5 font-medium text-[var(--ink-secondary)]">
                <KeyRound className="h-3.5 w-3.5 text-[var(--accent)]" />
                API 密钥 (API Key){keyOptional ? "（本地模型可留空）" : ""}
              </label>
              {llmCompiler?.has_api_key && (
                <span className="font-mono text-emerald-700 dark:text-emerald-400">
                  已配置: {llmCompiler.api_key_masked}
                  {llmCompiler.key_source === "saved" && (
                    <button
                      type="button"
                      onClick={() => void handleClearKey()}
                      className="ml-2 text-rose-600 underline hover:text-rose-700 dark:text-rose-400"
                    >
                      清除
                    </button>
                  )}
                </span>
              )}
            </div>
            <input
              type="password"
              value={apiKeyInput}
              onChange={(e) => setApiKeyInput(e.target.value)}
              placeholder={
                keyOptional
                  ? "本地免鉴权接口可直接留空（若服务端开启了校验再填写）"
                  : llmCompiler?.has_api_key
                    ? "已配置密钥；如需更换请粘贴新 API Key，留空则保持不变"
                    : "粘贴你的 API Key（仅保存在本机）"
              }
              className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-subtle)] px-3 py-2 font-mono text-xs text-[var(--ink-primary)] focus:border-[var(--accent)] focus:outline-none"
            />
          </div>

          {/* Global Automation Policy */}
          <div className="space-y-3 rounded-xl border border-[var(--border-default)] bg-[var(--bg-subtle)] p-3.5">
            <div className="font-bold text-[var(--ink-primary)]">自动整理与吞吐设置</div>
            <label className="flex min-h-[44px] cursor-pointer items-start gap-2.5 py-1">
              <input
                type="checkbox"
                name="auto_compile_on_sync"
                checked={autoSync}
                onChange={(e) => setAutoSync(e.target.checked)}
                className="mt-0.5 size-4 shrink-0 cursor-pointer"
              />
              <div>
                <div className="font-semibold text-[var(--ink-primary)]">
                  添加或更新文档后，自动用大模型整理摘要
                </div>
                <div className="mt-0.5 text-[var(--ink-muted)]">
                  关闭后只更新基础搜索索引，需要时可在知识库页面手动点击整理。
                </div>
              </div>
            </label>

            <label className="flex min-h-[44px] cursor-pointer items-start gap-2.5 border-t border-[var(--border-subtle)] pt-2.5 py-1">
              <input
                type="checkbox"
                name="skip_csv"
                checked={skipCsv}
                onChange={(e) => setSkipCsv(e.target.checked)}
                className="mt-0.5 size-4 shrink-0 cursor-pointer"
              />
              <div>
                <div className="font-semibold text-[var(--ink-primary)]">
                  跳过纯 CSV / TSV 数据表（默认开启，推荐）
                </div>
                <div className="mt-0.5 text-[var(--ink-muted)]">
                  表格已在 FTS5 索引中支持带表头精确检索；跳过文字改写可大幅节省调用并提升摘要信噪比（若全库仅有表格则自动保留）。
                </div>
              </div>
            </label>

            <label className="flex min-h-[44px] cursor-pointer items-start gap-2.5 border-t border-[var(--border-subtle)] pt-2.5 py-1">
              <input
                type="checkbox"
                name="project_rollup"
                checked={projectRollup}
                onChange={(e) => setProjectRollup(e.target.checked)}
                className="mt-0.5 size-4 shrink-0 cursor-pointer"
              />
              <div>
                <div className="font-semibold text-[var(--ink-primary)]">
                  大型工程目录自动聚合为《项目总览》（默认开启，推荐）
                </div>
                <div className="mt-0.5 text-[var(--ink-muted)]">
                  当 <code className="font-mono">svn/</code> 下的单个工程超过 8 份文档时，按权重精选核心方案浓缩为 1 篇总览页，避免拆成大量零碎子页。
                </div>
              </div>
            </label>

            <label className="flex min-h-[44px] cursor-pointer items-start gap-2.5 border-t border-[var(--border-subtle)] pt-2.5 py-1">
              <input
                type="checkbox"
                name="disable_thinking"
                checked={disableThinking}
                onChange={(e) => setDisableThinking(e.target.checked)}
                className="mt-0.5 size-4 shrink-0 cursor-pointer"
              />
              <div>
                <div className="font-semibold text-[var(--ink-primary)]">
                  关闭模型思考链（推荐本地推理模型开启）
                </div>
                <div className="mt-0.5 text-[var(--ink-muted)]">
                  发送 <code className="font-mono">enable_thinking=false</code> 跳过冗长思考过程，显著提升 Qwen3 / DeepSeek-R1 等模型整理速度（不支持时自动降级）。
                </div>
              </div>
            </label>

            <div className="grid grid-cols-1 gap-2.5 border-t border-[var(--border-subtle)] pt-2.5 sm:grid-cols-3">
              <div>
                <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
                  单次整理范围
                </label>
                <select
                  value={maxItems}
                  onChange={(e) => setMaxItems(Number(e.target.value))}
                  className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                >
                  <option value={0}>全部跑完（不限，推荐）</option>
                  <option value={15}>每次最多 15 篇</option>
                  <option value={30}>每次最多 30 篇</option>
                  <option value={60}>每次最多 60 篇</option>
                  <option value={100}>每次最多 100 篇</option>
                  <option value={300}>每次最多 300 篇</option>
                  <option value={500}>每次最多 500 篇</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
                  并发整理数
                </label>
                <select
                  value={workers}
                  onChange={(e) => setWorkers(Number(e.target.value))}
                  className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                >
                  <option value={1}>1 路（串行）</option>
                  <option value={2}>2 路并发</option>
                  <option value={4}>4 路并发（推荐）</option>
                  <option value={6}>6 路并发</option>
                  <option value={8}>8 路并发</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block font-medium text-[var(--ink-secondary)]">
                  单页输出上限
                </label>
                <select
                  value={maxTokens}
                  onChange={(e) => setMaxTokens(Number(e.target.value))}
                  className="w-full rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-xs text-[var(--ink-primary)]"
                >
                  {![1800, 3000, 4096, 8192, 16384].includes(maxTokens) && (
                    <option value={maxTokens}>{maxTokens} tokens（当前）</option>
                  )}
                  <option value={1800}>1800 tokens（极简）</option>
                  <option value={3000}>3000 tokens（详尽）</option>
                  <option value={4096}>4096 tokens（推荐）</option>
                  <option value={8192}>8192 tokens（思考模型）</option>
                  <option value={16384}>16384 tokens（超长推理）</option>
                </select>
              </div>
            </div>
          </div>

          {testResult && (
            <div
              className={`rounded-lg border px-3 py-2 font-mono text-xs ${
                testResult.ok
                  ? "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
                  : "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-300"
              }`}
            >
              {testResult.msg}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-default)] bg-[var(--bg-subtle)] px-5 py-3 text-xs">
          <button
            type="button"
            disabled={testing}
            onClick={() => void handleTest()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3.5 py-1.5 font-semibold text-[var(--ink-secondary)] hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:opacity-50"
          >
            {testing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Zap className="h-3.5 w-3.5 text-[var(--accent)]" />
            )}
            测试连接
          </button>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-3 py-1.5 font-medium text-[var(--ink-muted)] hover:text-[var(--ink-primary)]"
            >
              取消
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => void handleSave()}
              className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-4 py-1.5 font-semibold text-white hover:bg-[var(--accent-hover)] disabled:opacity-50"
            >
              <Check className="h-3.5 w-3.5" /> 保存设置
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
