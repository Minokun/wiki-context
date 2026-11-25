/**
 * Prompts that teammates paste straight into their own AI agent (Claude Code /
 * Codex / Cursor …) so the agent installs wctx and connects through wctx's
 * native path — never by hand-writing raw HTTP MCP configs:
 *
 *   install wctx → wctx doctor → wctx remote pair|add (auto-sync local AI clients)
 *   → only for undetected clients: stdio entry `wctx bridge --vault <id>`
 */

export const WCTX_REPO_URL = "https://github.com/Minokun/wiki-context.git";

export interface PromptVault {
  id: string;
  name: string;
}

const INTRO =
  "请帮我把团队知识库接入到本机的 AI 工具中。必须使用 wiki-context（命令 wctx）官方的接入方式完成：不要手动拼写 HTTP 型 MCP 配置、不要自行编写转发脚本、不要直接修改 ~/.wiki-context 下的文件。";

function installSteps(): string[] {
  return [
    "1. 检查是否已安装：运行 `wctx --version`。",
    `   - 未安装：用 pipx 安装（需要 Python ≥ 3.11）：pipx install git+${WCTX_REPO_URL}`,
    `   - 没有 pipx：可用 uv tool install git+${WCTX_REPO_URL}，或在独立的 Python ≥ 3.11 venv 中 pip install；不要装进系统 Python。`,
    "   - 已安装：用对应方式升级到最新版（如 pipx upgrade wiki-context）。",
    "2. 运行 `wctx doctor`，确认 wctx 在 PATH 中、Python 版本满足要求；有问题先修复再继续。",
  ];
}

function syncCheckSteps(startNo: number): string[] {
  const a = startNo;
  return [
    `${a}. 查看上一步输出 JSON 中的 connected_vaults（接入的知识库，含本地 id）和 client_sync（已自动同步的 AI 工具）。检查你自己（当前这个 AI 工具）是否在 client_sync 中且同步成功：`,
    "   - 是：跳到下一步。",
    "   - 否（未被识别的工具）：在它的 MCP 配置里为每个 connected_vaults 条目添加一个 stdio 服务器，由 wctx 从本机已登记的凭据中读取地址与密钥（配置里不出现明文密钥）：",
    '     command: "wctx"，args: ["bridge", "--vault", "<connected_vaults 中的 id>"]，env: { "PYTHONUTF8": "1" }',
    "     （command 找不到时用 `which wctx` / `where wctx` 得到的绝对路径。）修改前先备份原配置文件，只合并、不要覆盖已有条目。",
    `${a + 1}. 运行 \`wctx remote list\` 确认已登记，然后告诉我：接入了哪些知识库、同步了哪些 AI 工具、改动了哪些配置文件，并提醒我重启或刷新 AI 工具的 MCP 列表。`,
  ];
}

function vaultLines(vaults: PromptVault[]): string {
  return vaults.map((v) => `  - ${v.name}（ID: ${v.id}）`).join("\n");
}

/** Long-lived token variant: works with any reachable gateway (domain, LAN, VPN). */
export function buildTokenAgentPrompt(
  baseUrl: string,
  token: string,
  targetVaults: PromptVault[],
  fallbackBaseUrl: string,
): string {
  const effBase = (baseUrl.trim() || fallbackBaseUrl).replace(/\/+$/, "");
  const effToken = token || "wctx_请替换为你的密钥";
  const vaults = targetVaults.length > 0 ? targetVaults : [{ id: "wiki", name: "知识库" }];
  const vaultFlags = vaults.map((v) => `--vault ${v.id}`).join(" ");

  return [
    INTRO,
    "",
    "【连接信息】",
    `- 网关地址：${effBase}`,
    `- 访问密钥：${effToken}`,
    "  （这是我的个人密钥：不要在回复中完整回显，不要写入任何会提交到 Git 的文件。）",
    "- 授权知识库：",
    vaultLines(vaults),
    "",
    "【请按顺序执行】",
    ...installSteps(),
    `3. 探测网关：wctx remote probe --url ${effBase} --token <访问密钥>`,
    "   应能看到上述授权知识库；连接失败时检查网络（同一局域网 / Tailscale / VPN），把报错原文告诉我，不要反复重试。",
    `4. 正式接入：wctx remote add --url ${effBase} --token <访问密钥>`,
    `   （只接入部分库时追加：${vaultFlags}）`,
    "   该命令会登记远程知识库，并自动把 MCP 配置同步到本机已安装的 AI 工具（Claude Code、Codex、Cursor、VS Code、Windsurf、Trae、Gemini CLI 等）。",
    ...syncCheckSteps(5),
  ].join("\n");
}

/** One-time pairing-code variant: LAN / Tailscale only, single-use, short-lived. */
export function buildPairingAgentPrompt(
  code: string,
  expiresAt: number,
  hostLabel: string,
  targetVaults: PromptVault[],
): string {
  const exp = new Date(expiresAt * 1000);
  const hh = String(exp.getHours()).padStart(2, "0");
  const mm = String(exp.getMinutes()).padStart(2, "0");
  const vaults = targetVaults.length > 0 ? targetVaults : [];

  return [
    INTRO,
    "",
    "【连接信息】",
    `- 共享电脑：${hostLabel || "团队知识库服务器"}（需与本机处于同一局域网或同一 Tailscale 网络）`,
    `- 一次性配对码（${hh}:${mm} 前有效，只能使用一次）：`,
    `  ${code}`,
    ...(vaults.length > 0 ? ["- 共享知识库：", vaultLines(vaults)] : []),
    "",
    "【请按顺序执行】",
    ...installSteps(),
    "3. 完成上面的安装与检查后，再执行配对（配对码只能用一次，务必放在最后、只执行一次）：",
    "   wctx remote pair <上面的配对码>",
    "   该命令会校验对方身份指纹、用配对码换取本机专属访问密钥、登记远程知识库，并自动把 MCP 配置同步到本机已安装的 AI 工具。",
    "   如果提示配对码过期、已使用或无效：停止，告诉我重新生成配对码，不要重试；如果提示连接失败：检查是否在同一局域网 / Tailscale，把报错原文告诉我。",
    ...syncCheckSteps(4),
  ].join("\n");
}
