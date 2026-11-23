"""Self-contained Single-Page Web Console HTML/CSS/JS for `wctx ui` (zero external CDN dependencies)."""

from __future__ import annotations

import json
from typing import Any


def render_console_html(boot: dict[str, Any]) -> str:
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__WCTX_BOOT_JSON__", boot_json)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>wiki-context 控制台 · 知识库与 MCP 权限中心</title>
<style>
  :root {
    --bg: #f6f8fb;
    --surface: #ffffff;
    --surface-2: #f1f5f9;
    --border: #e2e8f0;
    --border-strong: #cbd5e1;
    --text: #0f172a;
    --muted: #64748b;
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --primary-soft: #eff6ff;
    --success: #059669;
    --success-soft: #ecfdf5;
    --warn: #d97706;
    --warn-soft: #fffbeb;
    --danger: #dc2626;
    --danger-soft: #fef2f2;
    --purple: #7c3aed;
    --purple-soft: #f5f3ff;
    --radius: 12px;
    --shadow: 0 1px 3px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04);
    --shadow-lg: 0 18px 40px rgba(15, 23, 42, 0.16);
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.55;
  }
  /* Header */
  .topbar {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 30;
  }
  .topbar-inner {
    max-width: 1280px;
    margin: 0 auto;
    padding: 14px 24px 0 24px;
  }
  .brand-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    padding-bottom: 12px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-logo {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    background: linear-gradient(135deg, #1e40af, #3b82f6);
    color: #fff;
    font-weight: 700;
    font-family: var(--mono);
    font-size: 15px;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 8px rgba(37, 99, 235, 0.28);
  }
  .brand h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .brand-sub {
    font-size: 12px;
    color: var(--muted);
  }
  .top-stats {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .stat-pill {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 4px 12px;
    font-size: 12px;
    color: var(--muted);
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .stat-pill strong { color: var(--text); font-weight: 600; }
  /* Tabs */
  .tabs {
    display: flex;
    gap: 6px;
    overflow-x: auto;
  }
  .tab-btn {
    border: none;
    background: transparent;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 600;
    color: var(--muted);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: color 0.15s, border-color 0.15s;
    white-space: nowrap;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active {
    color: var(--primary);
    border-bottom-color: var(--primary);
  }
  .tab-count {
    background: var(--surface-2);
    color: var(--muted);
    border-radius: 999px;
    padding: 1px 7px;
    font-size: 11px;
    font-weight: 600;
  }
  .tab-btn.active .tab-count {
    background: var(--primary-soft);
    color: var(--primary);
  }
  /* Main Container */
  .container {
    max-width: 1280px;
    margin: 0 auto;
    padding: 22px 24px 60px;
  }
  .panel { display: none; }
  .panel.active { display: block; }
  .section-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 18px;
    flex-wrap: wrap;
  }
  .section-head h2 {
    margin: 0 0 4px 0;
    font-size: 18px;
    font-weight: 700;
  }
  .section-head p {
    margin: 0;
    color: var(--muted);
    font-size: 13px;
  }
  .btn-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }
  button, .btn {
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    padding: 7px 14px;
    border-radius: 8px;
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: all 0.14s;
  }
  button:hover:not(:disabled) { background: var(--surface-2); }
  button:disabled { opacity: 0.55; cursor: not-allowed; }
  .btn-primary {
    background: var(--primary);
    border-color: var(--primary);
    color: #fff;
  }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  .btn-danger {
    color: var(--danger);
    border-color: #fecaca;
    background: #fff;
  }
  .btn-danger:hover:not(:disabled) { background: var(--danger-soft); }
  .btn-sm { padding: 4px 10px; font-size: 12px; border-radius: 6px; }
  /* Cards & Grids */
  .grid-2 {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
    gap: 16px;
  }
  @media (max-width: 640px) {
    .grid-2 { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
    box-shadow: var(--shadow);
  }
  .card-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 10px;
  }
  .card-title {
    font-size: 16px;
    font-weight: 700;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .mono {
    font-family: var(--mono);
    font-size: 12px;
  }
  .path-text {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
    word-break: break-all;
    margin: 4px 0 12px;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    border: 1px solid transparent;
  }
  .badge-blue { background: var(--primary-soft); color: var(--primary); border-color: #bfdbfe; }
  .badge-green { background: var(--success-soft); color: var(--success); border-color: #a7f3d0; }
  .badge-amber { background: var(--warn-soft); color: var(--warn); border-color: #fde68a; }
  .badge-red { background: var(--danger-soft); color: var(--danger); border-color: #fecaca; }
  .badge-purple { background: var(--purple-soft); color: var(--purple); border-color: #ddd6fe; }
  .badge-gray { background: var(--surface-2); color: var(--muted); border-color: var(--border); }
  .metrics-bar {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    background: var(--surface-2);
    border-radius: 8px;
    padding: 10px 12px;
    margin: 12px 0;
  }
  .metric-cell { text-align: center; }
  .metric-val { font-size: 17px; font-weight: 700; color: var(--text); }
  .metric-lbl { font-size: 11px; color: var(--muted); }
  /* Forms */
  .form-group { margin-bottom: 14px; }
  .form-group label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 5px;
  }
  .form-hint {
    font-size: 12px;
    color: var(--muted);
    margin-top: 4px;
  }
  input[type="text"], input[type="number"], select, textarea {
    width: 100%;
    padding: 8px 11px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    font-family: inherit;
    font-size: 13px;
    color: var(--text);
    background: #fff;
  }
  input:focus, select:focus, textarea:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
  }
  textarea { resize: vertical; min-height: 92px; }
  .check-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .check-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 11px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: #fff;
    font-size: 12.5px;
    cursor: pointer;
    user-select: none;
  }
  .check-pill:hover { background: var(--surface-2); }
  .callout {
    padding: 12px 14px;
    border-radius: 9px;
    font-size: 13px;
    margin: 10px 0;
    border: 1px solid var(--border);
    background: var(--surface-2);
  }
  .callout-blue { background: var(--primary-soft); border-color: #bfdbfe; color: #1e3a8a; }
  .callout-green { background: var(--success-soft); border-color: #a7f3d0; color: #065f46; }
  .callout-amber { background: var(--warn-soft); border-color: #fde68a; color: #92400e; }
  /* Tables */
  .table-wrap { overflow-x: auto; }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th, td {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  th {
    font-size: 12px;
    color: var(--muted);
    font-weight: 600;
    background: var(--surface-2);
  }
  /* Code & Previews */
  pre.codebox {
    background: #0f172a;
    color: #e2e8f0;
    padding: 14px 16px;
    border-radius: 9px;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.55;
    overflow-x: auto;
    margin: 8px 0;
    white-space: pre-wrap;
    word-break: break-word;
  }
  /* Folder Browser */
  .fs-box {
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    overflow: hidden;
    background: #fff;
  }
  .fs-toolbar {
    background: var(--surface-2);
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
  }
  .fs-list {
    max-height: 220px;
    overflow-y: auto;
    padding: 6px;
  }
  .fs-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  .fs-item:hover { background: var(--primary-soft); }
  /* Modal */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(15, 23, 42, 0.45);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 60;
    padding: 20px;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    background: var(--surface);
    border-radius: 14px;
    width: 100%;
    max-width: 720px;
    max-height: 90vh;
    overflow-y: auto;
    box-shadow: var(--shadow-lg);
    padding: 22px 24px;
  }
  .modal-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .modal-head h3 { margin: 0; font-size: 17px; }
  /* Toast */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 90;
    background: #0f172a;
    color: #fff;
    padding: 11px 18px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 500;
    box-shadow: var(--shadow-lg);
    display: none;
    max-width: 440px;
  }
  .empty-state {
    text-align: center;
    padding: 36px 20px;
    color: var(--muted);
    border: 1px dashed var(--border-strong);
    border-radius: var(--radius);
    background: var(--surface);
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11.5px;
    font-family: var(--mono);
    background: var(--surface-2);
    border: 1px solid var(--border);
    cursor: pointer;
  }
  .chip:hover { border-color: var(--primary); color: var(--primary); }
  .two-col {
    display: grid;
    grid-template-columns: 1.05fr 1fr;
    gap: 18px;
    align-items: start;
  }
  @media (max-width: 920px) { .two-col { grid-template-columns: 1fr; } }
  .folder-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 6px;
  }
  .folder-toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 11px;
    border-radius: 8px;
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    user-select: none;
    transition: all 0.12s ease;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
  }
  .folder-toggle:hover { transform: translateY(-1px); box-shadow: 0 2px 6px rgba(0,0,0,0.06); }
  .folder-allow { background: #ecfdf5; border-color: #6ee7b7; color: #065f46; }
  .folder-deny { background: #fef2f2; border-color: #fca5a5; color: #991b1b; }
  .folder-closed { background: #f8fafc; border-color: #cbd5e1; color: #64748b; }
  .folder-sub { font-size: 12px; padding: 4px 9px; border-style: dashed; }
  .mode-switch {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    background: var(--surface);
  }
  .mode-switch button {
    border: none;
    border-radius: 0;
    padding: 5px 10px;
    font-size: 12px;
    background: transparent;
    color: var(--muted);
  }
  .mode-switch button.active-mode {
    background: var(--primary);
    color: #fff;
    font-weight: 600;
  }
  .preset-card {
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 9px 12px;
    cursor: pointer;
    background: var(--surface-2);
    transition: all 0.12s ease;
  }
  .preset-card:hover, .preset-card.selected {
    border-color: var(--primary);
    background: #eff6ff;
  }
</style>
</head>
<body>

<header class="topbar">
  <div class="topbar-inner">
    <div class="brand-row">
      <div class="brand">
        <div class="brand-logo">wctx</div>
        <div>
          <h1>wiki-context 控制中心</h1>
          <div class="brand-sub">确定性 AI 知识库 · 令牌与目录权限 · 多客户端 MCP 配置 · 第 4 层记忆管理</div>
        </div>
      </div>
      <div class="top-stats" id="topStats"></div>
    </div>
    <nav class="tabs">
      <button class="tab-btn active" data-tab="vaults" onclick="switchTab('vaults')">
        📁 知识库与目录纳管 <span class="tab-count" id="cntVaults">0</span>
      </button>
      <button class="tab-btn" data-tab="tokens" onclick="switchTab('tokens')">
        🔑 令牌与访问权限 <span class="tab-count" id="cntTokens">0</span>
      </button>
      <button class="tab-btn" data-tab="clients" onclick="switchTab('clients')">
        🔌 MCP 客户端配置 <span class="tab-count" id="cntClients">0</span>
      </button>
      <button class="tab-btn" data-tab="memory" onclick="switchTab('memory')">
        🧠 记忆与勘误模块 <span class="tab-count" id="cntMemory">0</span>
      </button>
    </nav>
  </div>
</header>

<main class="container">
  <!-- TAB 1: VAULTS & FOLDERS -->
  <section class="panel active" id="panel-vaults">
    <div class="section-head">
      <div>
        <h2>已纳管知识库（本地目录 & 远程网关）</h2>
        <p>既可将本机文件夹初始化为四层确定性知识库，也可直接填入局域网其他电脑的网关地址与令牌一键接入远程知识库。</p>
      </div>
      <div class="btn-row">
        <button onclick="openRemoteConnectModal()">🌐 连接远程知识库</button>
        <button onclick="openImportFolderModal()">📥 导入外部资料夹</button>
        <button class="btn-primary" onclick="openOnboardModal()">＋ 纳管本地文件夹</button>
      </div>
    </div>
    <div id="vaultNextStepBanner"></div>
    <div id="vaultsContainer" class="grid-2"></div>
  </section>

  <!-- TAB 2: TOKENS & ACL -->
  <section class="panel" id="panel-tokens">
    <div class="section-head">
      <div>
        <h2>局域网共享网关 · 访问令牌与目录权限控制</h2>
        <p>在提供方电脑一键开启局域网网关并签发令牌；其他电脑只需在「连接远程知识库」填入地址与令牌即可自动挂载，无需复制配置片段。</p>
      </div>
    </div>

    <div id="lanGatewayBar" style="margin-bottom:14px;"></div>

    <div id="newTokenBanner" style="display:none;" class="callout callout-green"></div>

    <div class="two-col">
      <!-- Create Token + Token Table -->
      <div class="card">
        <h3 style="margin:0 0 12px 0;font-size:15px;">签发新访问令牌</h3>
        <div class="form-group">
          <label>身份标识 (uid) *</label>
          <input type="text" id="tokUid" placeholder="例如：alice、frontend-agent、partner-bot" />
        </div>
        <div class="form-group">
          <label>备注名称</label>
          <input type="text" id="tokLabel" placeholder="例如：研发组只读网关令牌" />
        </div>
        <div class="form-group">
          <label>允许访问的知识库范围</label>
          <div class="check-grid" id="tokVaultChecks"></div>
          <div class="form-hint">不勾选具体知识库或勾选「全部知识库 (*)」则允许访问当前及后续纳管的所有库。</div>
        </div>
        <div class="form-group">
          <label>选择访问角色组（点击勾选，免手动输入）</label>
          <div class="check-grid" id="tokRoleChecks"></div>
          <div class="form-hint">默认勾选「完整访问权限 (all)」；如需限制可读取的子目录，请在右侧新建受限角色组并点选文件夹权限后在此勾选。</div>
        </div>
        <button class="btn-primary" onclick="createToken()">🔑 生成新令牌（仅明文显示一次）</button>

        <hr style="border:none;border-top:1px solid var(--border);margin:20px 0 14px;" />
        <h3 style="margin:0 0 10px 0;font-size:15px;">已签发令牌列表</h3>
        <div class="table-wrap" id="tokensTableWrap"></div>
      </div>

      <!-- Visual Role Folder ACL + Simulator -->
      <div style="display:flex;flex-direction:column;gap:16px;">
        <div class="card">
          <div class="card-header">
            <div>
              <h3 style="margin:0;font-size:15px;">🛡️ 受限角色组与文件夹可视权限</h3>
              <div class="form-hint">直接点击下方真实文件夹即可切换「允许 / 禁止」，自动保存并毫秒级热生效，同时自动拦截引用了受限原件的 Wiki 综述。</div>
            </div>
            <button class="btn-sm btn-primary" onclick="openCreateRoleModal(false)">＋ 新建受限角色组</button>
          </div>
          <div id="aclRolesEditor"></div>
        </div>

        <div class="card">
          <h3 style="margin:0 0 8px 0;font-size:15px;">🔍 令牌权限沙盒验证器</h3>
          <p style="margin:0 0 12px;font-size:12.5px;color:var(--muted);">选择一枚令牌与目标知识库，即时模拟该令牌可读取哪些原件、哪些原件与 Wiki 综述被权限规则拦截。</p>
          <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:end;">
            <div>
              <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">选择令牌</label>
              <select id="simTokenSelect"></select>
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">选择知识库</label>
              <select id="simVaultSelect"></select>
            </div>
            <button onclick="runAclSimulation()">验证权限</button>
          </div>
          <div id="simResultBox" style="margin-top:12px;"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- TAB 3: MCP CLIENTS -->
  <section class="panel" id="panel-clients">
    <div class="section-head">
      <div>
        <h2>AI 客户端 MCP 插件一键配置中心</h2>
        <p>自动检测本机 <strong>DeepSeek Harness (DSH)</strong>、<strong>Codex CLI</strong>、<strong>Claude Code</strong>、<strong>WorkBuddy</strong>、<strong>Cursor</strong> 与 <strong>Antigravity (AGY)</strong>，无损合并写入并保留备份。</p>
      </div>
    </div>

    <div class="card" style="margin-bottom:18px;">
      <div class="two-col">
        <div>
          <div class="form-group">
            <label>1. 选择要挂载到客户端的知识库</label>
            <div class="check-grid" id="mcpVaultChecks"></div>
          </div>
          <div class="form-group" style="margin-bottom:0;">
            <label>2. 选择连接方式</label>
            <select id="mcpTransportMode" onchange="toggleMcpGatewayFields()">
              <option value="auto">智能自动挂载（推荐：本地库走 Stdio 直连，远程库自动使用已存网关地址与令牌）</option>
              <option value="http">手动指定网关覆盖（高级：强制将所选知识库切到下方自定义网关与令牌）</option>
            </select>
          </div>
        </div>
        <div>
          <div id="mcpGatewayFields" style="display:none;">
            <div class="form-group">
              <label>网关基础地址</label>
              <input type="text" id="mcpGatewayUrl" />
            </div>
            <div class="form-group" style="margin-bottom:0;">
              <label>网关 Bearer Token（粘贴刚生成的明文令牌）</label>
              <input type="text" id="mcpGatewayToken" placeholder="wctx_..." />
            </div>
          </div>
          <div id="mcpStdioNote" class="callout callout-blue" style="margin:0;">
            <strong>智能自动挂载说明：</strong>本机文件夹知识库自动以 <code id="wctxCmdLabel">wctx</code> <code>serve</code> 直连（完整 9 工具）；通过「🌐 连接远程知识库」接入的他人电脑知识库自动携带已保存的地址与令牌挂载，<strong>无需复制粘贴任何配置片段</strong>，且支持本地私人库与远程团队库混搭共存！
          </div>
          <div style="margin-top:12px;display:flex;justify-content:flex-end;gap:8px;">
            <button class="btn-primary" onclick="syncAllInstalledClients()">⚡ 一键同步至全部已安装客户端</button>
          </div>
        </div>
      </div>
    </div>

    <div id="clientsGrid" class="grid-2"></div>
  </section>

  <!-- TAB 4: MEMORY & ERRATA -->
  <section class="panel" id="panel-memory">
    <div class="section-head">
      <div>
        <h2>第 4 层运行记忆与原件勘误钉（Layer-4 Memory & Errata）</h2>
        <p>在不污染只读原件的前提下，记录实战排障洞察、术语口径与 <code>📌 原件勘误钉</code>；关联原件后自动继承其目录 ACL 权限。</p>
      </div>
      <div class="btn-row">
        <button onclick="openImportMemoryModal()">📂 批量导入 JSON</button>
        <button class="btn-primary" onclick="openMemoryEditor()">＋ 新建记忆 / 勘误钉</button>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div style="display:grid;grid-template-columns:1.2fr 1fr 1fr 1.5fr auto;gap:10px;align-items:end;">
        <div>
          <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">当前知识库</label>
          <select id="memVaultSelect" onchange="loadMemories()"></select>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">记忆类型</label>
          <select id="memKindFilter" onchange="loadMemories()">
            <option value="all">全部类型</option>
            <option value="errata">📌 errata（原件勘误钉）</option>
            <option value="insight">💡 insight（实战洞察）</option>
            <option value="decision">⚖️ decision（架构决策）</option>
            <option value="glossary">📖 glossary（术语口径）</option>
          </select>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">生命周期状态</label>
          <select id="memStatusFilter" onchange="loadMemories()">
            <option value="all">全部状态</option>
            <option value="active" selected>active（生效中）</option>
            <option value="consolidated">consolidated（已归并 Wiki）</option>
            <option value="archived">archived（已归档遗忘）</option>
          </select>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px;">搜索正文 / 标签 / 关联史料</label>
          <input type="text" id="memQueryInput" placeholder="输入关键词回车或即时过滤..." oninput="debouncedLoadMemories()" />
        </div>
        <button onclick="loadMemories()">刷新</button>
      </div>
      <div id="memSummaryBar" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;"></div>
    </div>

    <div id="memoriesList" style="display:flex;flex-direction:column;gap:12px;"></div>
  </section>
</main>

<!-- UNIVERSAL MODAL -->
<div class="modal-backdrop" id="modalBackdrop" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-head">
      <h3 id="modalTitle">弹窗</h3>
      <button class="btn-sm" onclick="closeModal()">✕ 关闭</button>
    </div>
    <div id="modalBody"></div>
  </div>
</div>

<div id="toast"></div>

<script>
window.__WCTX_BOOT__ = __WCTX_BOOT_JSON__;
let STATE = { vaults: [], tokens: [], acl: {}, clients: [], gateway_base_url: "", wctx_command: "" };
let memTimer = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toast(msg, isErr = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.background = isErr ? "#dc2626" : "#0f172a";
  el.style.display = "block";
  clearTimeout(window.__toastT);
  window.__toastT = setTimeout(() => { el.style.display = "none"; }, 3600);
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "X-WCTX-CSRF": window.__WCTX_BOOT__.csrfToken }, opts.headers || {});
  if (opts.body && typeof opts.body === "object") {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

function switchTab(tab) {
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === `panel-${tab}`));
  if (tab === "memory") loadMemories();
}

function openModal(title, html) {
  document.getElementById("modalTitle").textContent = title;
  document.getElementById("modalBody").innerHTML = html;
  document.getElementById("modalBackdrop").classList.add("open");
}
function closeModal() {
  document.getElementById("modalBackdrop").classList.remove("open");
}

async function copyText(txt, tip = "已复制到剪贴板") {
  try {
    await navigator.clipboard.writeText(txt);
    toast(tip);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = txt;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast(tip);
  }
}

async function refreshAll() {
  try {
    STATE = await api("/api/overview");
    renderHeader();
    renderVaults();
    renderTokensAndAcl();
    renderClients();
    syncMemoryVaultSelector();
  } catch (e) {
    toast("加载状态失败: " + e.message, true);
  }
}

function renderHeader() {
  const totalNotes = STATE.vaults.reduce((a, v) => a + (v.active_memory_notes || 0), 0);
  const syncedClients = STATE.clients.filter(c => c.sync_status === "synced").length;
  document.getElementById("cntVaults").textContent = STATE.vaults.length;
  document.getElementById("cntTokens").textContent = STATE.tokens.length;
  document.getElementById("cntClients").textContent = `${syncedClients}/${STATE.clients.length}`;
  document.getElementById("cntMemory").textContent = totalNotes;
  document.getElementById("wctxCmdLabel").textContent = STATE.wctx_command || "wctx";
  const gwInput = document.getElementById("mcpGatewayUrl");
  if (gwInput && !gwInput.value) gwInput.value = STATE.gateway_base_url;

  document.getElementById("topStats").innerHTML = `
    <span class="stat-pill">内置 MCP 网关: <strong class="mono">${esc(STATE.gateway_base_url)}/mcp/&lt;id&gt;</strong></span>
    <span class="stat-pill">版本: <strong>v${esc(STATE.version)}</strong></span>
    <button class="btn-sm" onclick="openDoctorModal()">🩺 环境自检</button>
    <button class="btn-sm" onclick="refreshAll()">↻ 刷新状态</button>
  `;
}

/* ==================== TAB 1: VAULTS & ONBOARDING ==================== */

function renderVaults() {
  const box = document.getElementById("vaultsContainer");
  const nextBanner = document.getElementById("vaultNextStepBanner");
  const installedClients = (STATE.clients || []).filter(c => c.installed);
  const syncedClients = installedClients.filter(c => c.sync_status === "synced");

  if (!STATE.vaults.length) {
    if (nextBanner) nextBanner.innerHTML = "";
    box.innerHTML = `<div class="empty-state" style="grid-column:1/-1;text-align:left;padding:24px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
        <div>
          <div style="font-size:17px;font-weight:700;color:var(--text);margin-bottom:4px;">👋 欢迎使用 wiki-context 控制中心 — 选择你的接入角色</div>
          <div style="font-size:13px;color:var(--muted);">全程无需手写或复制粘贴任何 JSON / TOML / YAML 配置文件，直接在本界面一键挂载至所有 AI 客户端：</div>
        </div>
        <div class="btn-row">
          <button onclick="openDoctorModal()">🩺 环境自检</button>
          <button onclick="openRemoteConnectModal()">🌐 连接远程知识库（填地址+令牌）</button>
          <button class="btn-primary" onclick="openOnboardModal()">＋ 纳管本地文件夹</button>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;">
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;">
          <div style="font-weight:700;color:var(--text);margin-bottom:4px;">📁 方式 A：纳管本机文件夹（个人 / 提供方）</div>
          <div style="font-size:12.5px;color:var(--muted);">选择本机空白目录、资料夹或 Obsidian 库，自动初始化四层结构与 FTS5 索引；如需共享给同事，可在「🔑 令牌与访问权限」一键开启局域网网关并签发令牌。</div>
        </div>
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;">
          <div style="font-weight:700;color:var(--text);margin-bottom:4px;">🌐 方式 B：连接远程知识库（团队使用方）</div>
          <div style="font-size:12.5px;color:var(--muted);">点击「🌐 连接远程知识库」，直接填入提供方电脑的地址（如 <code>http://192.168.x.x:8090</code>）和令牌，自动拉取授权库并一键配置本机全部 AI 客户端！</div>
        </div>
      </div>
    </div>`;
    return;
  }

  if (nextBanner) {
    if (installedClients.length > 0 && syncedClients.length === 0) {
      nextBanner.innerHTML = `<div class="callout callout-blue" style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
        <div><strong>下一步建议：</strong> 检测到本机已安装 ${installedClients.length} 个 AI 客户端，尚未同步当前知识库配置。点右侧按钮即可一键写入，免复制任何配置片段。</div>
        <button class="btn-sm btn-primary" onclick="switchTab('clients')">🔌 前往一键同步 MCP 客户端 →</button>
      </div>`;
    } else {
      nextBanner.innerHTML = "";
    }
  }
  box.innerHTML = STATE.vaults.map(v => {
    if (v.kind === "remote") {
      const stBadge = v.ready
        ? `<span class="badge badge-green">● 远端在线${v.remote_uid ? ' · ' + esc(v.remote_uid) : ''}</span>`
        : `<span class="badge badge-amber">⚠️ 连接异常</span>`;
      const grpText = (v.remote_groups || []).join(", ") || "默认";
      return `<div class="card">
        <div class="card-header">
          <div>
            <h3 class="card-title">
              ${esc(v.name)}
              <span class="badge badge-gray mono" style="cursor:pointer;" title="点击修改 MCP 服务实例 ID" onclick="promptRenameVaultId('${esc(v.id)}')">ID: ${esc(v.id)} ✏️</span>
              <span class="badge badge-purple">🌐 远程网关</span>
              ${stBadge}
            </h3>
            <div class="path-text">${esc(v.mcp_url)} · 令牌 ${esc(v.token_masked || '未设置')}</div>
          </div>
        </div>
        ${(!v.ready && v.online_error) ? `<div class="callout callout-amber" style="margin-bottom:10px;padding:8px 10px;font-size:12px;">${esc(v.online_error)}</div>` : ""}
        <div class="metrics-bar">
          <div class="metric-cell"><div class="metric-val">${v.docs ?? 0}</div><div class="metric-lbl">远端原件</div></div>
          <div class="metric-cell"><div class="metric-val">${v.sections ?? 0}</div><div class="metric-lbl">检索切片</div></div>
          <div class="metric-cell"><div class="metric-val" style="font-size:13px;">${esc(grpText)}</div><div class="metric-lbl">令牌角色组</div></div>
          <div class="metric-cell"><div class="metric-val" style="font-size:13px;">自动网关</div><div class="metric-lbl">客户端挂载</div></div>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
          <div class="btn-row">
            <button class="btn-sm btn-primary" onclick="openQueryPlayground('${esc(v.id)}')">🔎 远程检索测试</button>
            <button class="btn-sm" onclick="openEditRemoteModal('${esc(v.id)}')">🔑 修改地址/令牌</button>
            <button class="btn-sm" onclick="refreshAll()">↻ 刷新状态</button>
          </div>
          <button class="btn-sm btn-danger" onclick="removeVault('${esc(v.id)}', '${esc(v.name)}')">断开移除</button>
        </div>
      </div>`;
    }

    const layBadge = v.layout === "zh"
      ? `<span class="badge badge-blue">📁 本地 · 中文布局</span>`
      : `<span class="badge badge-purple">📁 本地 · EN 布局</span>`;
    const stBadge = v.ready
      ? `<span class="badge badge-green">● FTS5 在线</span>`
      : `<span class="badge badge-amber">待构建索引</span>`;
    return `<div class="card">
      <div class="card-header">
        <div>
          <h3 class="card-title">
            ${esc(v.name)}
            <span class="badge badge-gray mono" style="cursor:pointer;" title="点击修改 MCP 服务实例 ID" onclick="promptRenameVaultId('${esc(v.id)}')">ID: ${esc(v.id)} ✏️</span>
            ${layBadge}
            ${stBadge}
          </h3>
          <div class="path-text">${esc(v.path)}</div>
        </div>
      </div>
      <div class="metrics-bar">
        <div class="metric-cell"><div class="metric-val">${v.docs ?? 0}</div><div class="metric-lbl">原件镜像</div></div>
        <div class="metric-cell"><div class="metric-val">${v.sections ?? 0}</div><div class="metric-lbl">检索切片</div></div>
        <div class="metric-cell"><div class="metric-val">${v.wiki_docs ?? 0}</div><div class="metric-lbl">Wiki 综述页</div></div>
        <div class="metric-cell"><div class="metric-val">${v.active_memory_notes ?? 0}</div><div class="metric-lbl">生效记忆/勘误</div></div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
        <div class="btn-row">
          <button class="btn-sm btn-primary" onclick="syncVault('${esc(v.id)}', this)">↻ 同步并更新索引</button>
          <button class="btn-sm" onclick="openQueryPlayground('${esc(v.id)}')">🔎 检索测试</button>
          <button class="btn-sm" onclick="runVaultLint('${esc(v.id)}')">🩺 体检</button>
          <button class="btn-sm" onclick="jumpToMemory('${esc(v.id)}')">🧠 记忆 (${v.active_memory_notes ?? 0})</button>
        </div>
        <button class="btn-sm btn-danger" onclick="removeVault('${esc(v.id)}', '${esc(v.name)}')">移除纳管</button>
      </div>
    </div>`;
  }).join("");
}

async function syncVault(vid, btn) {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "同步提取中...";
  try {
    const r = await api("/api/vaults/sync", { method: "POST", body: { vault_id: vid } });
    toast(`同步完成：扫描 ${r.scanned} 份，新增转换 ${r.converted} 份，共 ${r.index?.docs ?? 0} 篇在线`);
    await refreshAll();
  } catch (e) {
    toast("同步失败: " + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function removeVault(vid, vname) {
  if (!confirm(`确定从控制台移除「${vname} (${vid})」吗？\n（仅取消控制台纳管，磁盘上的任何知识库原件与索引文件均不会被删除）`)) return;
  try {
    await api("/api/vaults/remove", { method: "POST", body: { vault_id: vid } });
    toast(`已取消纳管 ${vid}`);
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

function promptRenameVaultId(oldId) {
  const suggested = oldId === "kb" ? "wiki" : (oldId.startsWith("kb-") ? "wiki-" + oldId.slice(3) : oldId);
  openModal(`✏️ 修改 MCP 实例 ID · ${oldId}`, `
    <p class="hint" style="margin-bottom:12px;">
      修改后将<strong>自动更新令牌授权范围并同步所有已安装的本地 AI 客户端</strong>（例如从 <code>kb</code> 统一为 <code>wiki</code>，客户端工具名即为 <code>mcp__wiki__wiki_context</code>）。
    </p>
    <div class="field">
      <label>新的 MCP 实例 ID（仅限字母、数字、连字符或下划线）</label>
      <input type="text" id="rnVaultInput" value="${esc(suggested)}" placeholder="例如 wiki 或 wiki-personal" />
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px;">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" onclick="confirmRenameVaultId('${esc(oldId)}', this)">确认重命名并同步客户端</button>
    </div>
  `);
  setTimeout(() => document.getElementById("rnVaultInput")?.select(), 40);
}

async function confirmRenameVaultId(oldId, btn) {
  const newId = (document.getElementById("rnVaultInput")?.value || "").trim();
  if (!newId || newId === oldId) {
    closeModal();
    return;
  }
  btn.disabled = true;
  btn.textContent = "同步更新中...";
  try {
    const r = await api("/api/vaults/rename", { method: "POST", body: { old_id: oldId, new_id: newId, sync_clients: true } });
    closeModal();
    toast(`已重命名为 ${r.new_id}，并自动同步 ${r.client_sync?.length || 0} 个 AI 客户端`);
    await refreshAll();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "确认重命名并同步客户端";
    toast(e.message, true);
  }
}

async function runVaultLint(vid) {
  try {
    const rep = await api("/api/vaults/lint", { method: "POST", body: { vault_id: vid } });
    openModal(`🩺 知识库健康体检 · ${vid}`, `
      <div class="callout ${rep.clean ? 'callout-green' : 'callout-amber'}">
        ${rep.clean ? '✅ 结构健康检查全部通过！' : `⚠️ 发现需要关注的条目（断链 ${rep.broken_wikilinks?.length||0}，孤儿原件 ${rep.orphan_sources?.length||0}，过期综述 ${rep.stale_wiki_pages?.length||0}）`}
      </div>
      <pre class="codebox">${esc(JSON.stringify(rep, null, 2))}</pre>
    `);
  } catch (e) {
    toast("体检失败: " + e.message, true);
  }
}

function openQueryPlayground(vid) {
  openModal(`🔎 实时测试 wiki_context 组装 · ${vid}`, `
    <div class="form-group">
      <label>输入自然语言问题（零运行时 LLM 开销，毫秒级组装 Wiki + 一手史料 + 勘误钉）</label>
      <div style="display:flex;gap:8px;">
        <input type="text" id="pgQuestion" placeholder="输入问题测试召回与表格表头钉住效果..." onkeydown="if(event.key==='Enter')execPlayground('${esc(vid)}')" />
        <button class="btn-primary" onclick="execPlayground('${esc(vid)}')">执行检索</button>
      </div>
    </div>
    <div id="pgResult"></div>
  `);
}

async function execPlayground(vid) {
  const q = document.getElementById("pgQuestion").value.trim();
  if (!q) return;
  const box = document.getElementById("pgResult");
  box.innerHTML = `<div class="callout">检索中...</div>`;
  try {
    const t0 = performance.now();
    const res = await api("/api/vaults/query", { method: "POST", body: { vault_id: vid, question: q } });
    const ms = (performance.now() - t0).toFixed(1);
    box.innerHTML = `
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
        <span class="badge badge-green">端到端耗时 ${ms} ms</span>
        <span class="badge badge-blue">分流: ${esc(res.route)}</span>
        <span class="badge badge-purple">一手证据: ${res.primary_count ?? 0} 段</span>
        <span class="badge badge-gray">Wiki 导读: ${res.wiki_count ?? 0} 段</span>
      </div>
      <pre class="codebox" style="max-height:420px;">${esc(res.context_markdown || JSON.stringify(res, null, 2))}</pre>
    `;
  } catch (e) {
    box.innerHTML = `<div class="callout callout-amber">${esc(e.message)}</div>`;
  }
}

/* Folder Browser Helper inside Modals */
async function loadFsBrowser(targetPath, inputId, afterSelectFnName) {
  const data = await api("/api/fs/list?path=" + encodeURIComponent(targetPath || ""));
  const inp = document.getElementById(inputId);
  if (inp) {
    inp.value = data.current;
    if (afterSelectFnName && window[afterSelectFnName]) window[afterSelectFnName](data.current);
  }
  const shortcutsHtml = (data.shortcuts || []).map(s =>
    `<span class="chip" onclick="loadFsBrowser('${esc(s.path)}','${inputId}','${afterSelectFnName||""}')">${esc(s.label)}</span>`
  ).join(" ");
  const upHtml = data.parent
    ? `<div class="fs-item" onclick="loadFsBrowser('${esc(data.parent)}','${inputId}','${afterSelectFnName||""}')"><span>⬆️ 返回上级目录 (..)</span><span class="mono" style="color:var(--muted)">${esc(data.parent)}</span></div>`
    : "";
  const dirsHtml = (data.directories || []).map(d => `
    <div class="fs-item" onclick="loadFsBrowser('${esc(d.path)}','${inputId}','${afterSelectFnName||""}')">
      <span>📁 ${esc(d.name)}</span>
      ${d.is_vault ? '<span class="badge badge-green">已有知识库</span>' : '<span style="color:var(--muted);font-size:12px;">进入 →</span>'}
    </div>
  `).join("");
  document.getElementById("fsBrowserMount").innerHTML = `
    <div class="fs-box">
      <div class="fs-toolbar">
        ${shortcutsHtml}
        <button type="button" class="btn-sm" style="margin-left:auto;" onclick="promptMkdir('${esc(data.current)}','${inputId}','${afterSelectFnName||""}')">＋ 新建子文件夹</button>
      </div>
      <div style="padding:6px 12px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--border);">
        当前选中：<strong class="mono" style="color:var(--text)">${esc(data.current)}</strong>（当前层含 ${data.file_count_here} 个文件）
      </div>
      <div class="fs-list">${upHtml}${dirsHtml || '<div style="padding:12px;color:var(--muted);text-align:center;">（无子文件夹，可直接纳管此目录）</div>'}</div>
    </div>
  `;
}

async function promptMkdir(parentPath, inputId, afterFn) {
  const name = prompt("在 " + parentPath + " 下新建文件夹名称：");
  if (!name || !name.trim()) return;
  try {
    const r = await api("/api/fs/mkdir", { method: "POST", body: { parent: parentPath, name: name.trim() } });
    await loadFsBrowser(r.created, inputId, afterFn);
  } catch (e) {
    toast(e.message, true);
  }
}

function openOnboardModal() {
  openModal("📁 纳管本地文件夹并执行 wiki-context 初始化", `
    <div class="form-group">
      <label>1. 浏览或输入目标文件夹路径 *</label>
      <div style="display:flex;gap:8px;margin-bottom:8px;">
        <input type="text" id="obPath" placeholder="/Users/.../我的知识库" oninput="debouncedInspectOb()" />
        <button type="button" onclick="inspectObPath(document.getElementById('obPath').value)">检测目录</button>
      </div>
      <div id="fsBrowserMount"></div>
    </div>
    <div id="obInspectHint"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;">
      <div class="form-group">
        <label>知识库 ID（MCP 服务名）</label>
        <input type="text" id="obId" placeholder="如 wiki-product" />
      </div>
      <div class="form-group">
        <label>知识库显示名称</label>
        <input type="text" id="obName" placeholder="如 产品研发知识库" />
      </div>
      <div class="form-group">
        <label>目录命名布局</label>
        <select id="obLayout">
          <option value="zh">中文（史料 / 知识库 / 记忆）</option>
          <option value="en">英文（sources / wiki / notes）</option>
        </select>
      </div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" id="btnConfirmOnboard" onclick="submitOnboard()">🚀 纳入并执行初始化与索引构建</button>
    </div>
  `);
  loadFsBrowser("", "obPath", "inspectObPath");
}

let obTimer = null;
function debouncedInspectOb() {
  clearTimeout(obTimer);
  obTimer = setTimeout(() => inspectObPath(document.getElementById("obPath").value), 300);
}

async function inspectObPath(p) {
  if (!p || !p.trim()) return;
  try {
    const info = await api("/api/fs/inspect", { method: "POST", body: { path: p.trim() } });
    document.getElementById("obId").value = info.suggested_id || "";
    document.getElementById("obName").value = info.suggested_name || "";
    document.getElementById("obLayout").value = info.detected_layout || "zh";
    const hint = document.getElementById("obInspectHint");
    if (info.kind === "existing_vault") {
      hint.innerHTML = `<div class="callout callout-green">✅ <strong>识别为已有 wiki-context / Obsidian 知识库</strong>：将保留全部现有原件与综述，补齐缺失的第 4 层记忆目录并执行增量同步与 FTS5 索引挂载。</div>`;
    } else if (info.kind === "raw_documents_folder") {
      hint.innerHTML = `<div class="callout callout-amber">📦 <strong>识别为包含 ${info.loose_total} 个现有文件/子目录的资料夹</strong>（如 <code>${esc((info.loose_entries||[]).slice(0,3).join(", "))}</code>）：点击初始化后，会将现有资料安全归入只读 <code>史料/</code>（或 <code>sources/</code>）层，自动提取结构化镜像并构建 FTS5 索引。</div>`;
    } else {
      hint.innerHTML = `<div class="callout callout-blue">✨ <strong>空白或新目录</strong>：将执行标准 <code>wctx init</code> 创建完整四层目录规范与初始 FTS5 快照。</div>`;
    }
  } catch (e) {
    document.getElementById("obInspectHint").innerHTML = `<div class="callout callout-amber">${esc(e.message)}</div>`;
  }
}

async function submitOnboard() {
  const path = document.getElementById("obPath").value.trim();
  if (!path) return toast("请选择或输入文件夹路径", true);
  const btn = document.getElementById("btnConfirmOnboard");
  btn.disabled = true;
  btn.textContent = "正在初始化并构建索引...";
  try {
    const res = await api("/api/vaults/onboard", {
      method: "POST",
      body: {
        path,
        vault_id: document.getElementById("obId").value.trim(),
        name: document.getElementById("obName").value.trim(),
        layout: document.getElementById("obLayout").value,
        adopt_loose_files: true,
      },
    });
    closeModal();
    toast(`已成功纳管「${res.name} (${res.vault_id})」！可在「MCP 客户端配置」一键挂载至各客户端`);
    await refreshAll();
  } catch (e) {
    toast("初始化失败: " + e.message, true);
    btn.disabled = false;
    btn.textContent = "🚀 纳入并执行初始化与索引构建";
  }
}

function openRemoteConnectModal() {
  openModal("🌐 连接远程知识库（填入提供方地址 + 令牌即可自动接入）", `
    <div class="callout callout-blue" style="margin-bottom:14px;">
      只需填写提供方电脑（A 电脑）在「🔑 令牌与访问权限」里显示的<strong>局域网网关地址</strong>与<strong>令牌</strong>，系统会自动验证并拉取授权给你的所有知识库，并一键写好本机 AI 客户端配置，<strong>无需复制粘贴任何配置片段</strong>。
    </div>
    <div class="form-group">
      <label>1. 提供方网关地址 *</label>
      <input type="text" id="remUrl" placeholder="例如：http://192.168.1.25:8090（直接填 IP:端口 也可自动识别）" />
    </div>
    <div class="form-group">
      <label>2. 访问令牌 (Bearer Token) *</label>
      <div style="display:flex;gap:8px;">
        <input type="text" id="remToken" placeholder="wctx_..." />
        <button type="button" onclick="probeRemoteInModal()">🔍 测试连接并查看可用库</button>
      </div>
    </div>
    <div id="remProbeBox" style="margin-bottom:12px;"></div>
    <div class="form-group">
      <label class="check-pill" style="display:inline-flex;">
        <input type="checkbox" id="remAutoSync" checked />
        <span>⚡ 接入后立即自动同步至本机全部已安装 AI 客户端（推荐）</span>
      </label>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" id="btnConfirmRemote" onclick="submitRemoteConnect()">🌐 立即连接并自动配置</button>
    </div>
  `);
}

async function probeRemoteInModal() {
  const gateway_url = document.getElementById("remUrl").value.trim();
  const token = document.getElementById("remToken").value.trim();
  const box = document.getElementById("remProbeBox");
  if (!gateway_url || !token) return toast("请先填写网关地址和令牌", true);
  box.innerHTML = `<div class="callout">正在连接远程网关并查询授权知识库...</div>`;
  try {
    const res = await api("/api/remotes/probe", { method: "POST", body: { gateway_url, token } });
    document.getElementById("remUrl").value = res.gateway_url;
    if (!res.vaults || !res.vaults.length) {
      box.innerHTML = `<div class="callout callout-amber">连接成功（身份: <strong>${esc(res.uid)}</strong>），但该令牌当前没有任何授权知识库。</div>`;
      return;
    }
    const chks = res.vaults.map(v => `
      <label class="check-pill">
        <input type="checkbox" class="rem-v-chk" value="${esc(v.remote_vault_id)}" checked />
        <span><strong>${esc(v.name)}</strong> <span class="mono">(${esc(v.remote_vault_id)})</span> · ${v.docs} 篇原件 / ${v.sections} 段切片</span>
      </label>
    `).join("");
    box.innerHTML = `
      <div class="callout callout-green">
        <div style="margin-bottom:8px;">✅ <strong>网关鉴权通过！</strong> 当前身份：<code>${esc(res.uid)}</code> · 角色组：<code>${esc((res.groups||[]).join(", ") || "all")}</code></div>
        <div style="font-size:12px;margin-bottom:6px;font-weight:600;">勾选要接入本机的远程知识库：</div>
        <div class="check-grid">${chks}</div>
      </div>
    `;
  } catch (e) {
    box.innerHTML = `<div class="callout callout-amber">❌ ${esc(e.message)}</div>`;
  }
}

async function submitRemoteConnect() {
  const gateway_url = document.getElementById("remUrl").value.trim();
  const token = document.getElementById("remToken").value.trim();
  if (!gateway_url || !token) return toast("请填写远程网关地址和访问令牌", true);
  const checkedBoxes = Array.from(document.querySelectorAll(".rem-v-chk:checked")).map(c => c.value);
  const sync_clients = document.getElementById("remAutoSync").checked;
  const btn = document.getElementById("btnConfirmRemote");
  btn.disabled = true;
  btn.textContent = "正在连接并写入客户端配置...";
  try {
    const res = await api("/api/remotes/connect", {
      method: "POST",
      body: {
        gateway_url,
        token,
        remote_vault_ids: checkedBoxes.length ? checkedBoxes : null,
        sync_clients,
      },
    });
    closeModal();
    const vCount = (res.connected_vaults || []).length;
    const cCount = (res.client_sync || []).length;
    toast(
      cCount > 0
        ? `🎉 已接入 ${vCount} 个远程知识库，并自动配置到本机 ${cCount} 个 AI 客户端！`
        : `🎉 已成功接入 ${vCount} 个远程知识库！`
    );
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = "🌐 立即连接并自动配置";
  }
}

function openEditRemoteModal(vid) {
  const v = STATE.vaults.find(x => x.id === vid);
  if (!v) return;
  openModal(`🔑 修改远程知识库连接 · ${v.name} (${v.id})`, `
    <div class="form-group">
      <label>远程网关基础地址 *</label>
      <input type="text" id="edRemUrl" value="${esc(v.gateway_url || '')}" />
    </div>
    <div class="form-group">
      <label>新访问令牌（留空则继续使用已保存令牌 ${esc(v.token_masked || '')}）</label>
      <input type="text" id="edRemToken" placeholder="如需轮换密钥请粘贴新 wctx_... 令牌" />
    </div>
    <div class="form-group">
      <label class="check-pill" style="display:inline-flex;">
        <input type="checkbox" id="edRemResync" checked />
        <span>⚡ 保存后同步刷新本机已安装 AI 客户端配置</span>
      </label>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" onclick="submitEditRemote('${esc(vid)}')">保存并验证连接</button>
    </div>
  `);
}

async function submitEditRemote(vid) {
  const gateway_url = document.getElementById("edRemUrl").value.trim();
  const token = document.getElementById("edRemToken").value.trim();
  const resync = document.getElementById("edRemResync").checked;
  try {
    await api("/api/remotes/update", {
      method: "POST",
      body: { vault_id: vid, gateway_url, token: token || null },
    });
    if (resync) {
      const installedIds = STATE.clients.filter(c => c.installed).map(c => c.id);
      if (installedIds.length) {
        await api("/api/clients/sync", {
          method: "POST",
          body: { client_ids: installedIds, mode: "auto", vault_ids: STATE.vaults.map(x => x.id), remove_unlisted_wctx: true },
        });
      }
    }
    closeModal();
    toast("远程连接配置已更新生效！");
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

function openImportFolderModal() {
  const localVaults = STATE.vaults.filter(v => v.kind !== "remote");
  if (!localVaults.length) return toast("请先在本机纳管至少一个本地文件夹知识库", true);
  const opts = localVaults.map(v => `<option value="${esc(v.id)}">${esc(v.name)} (${esc(v.id)})</option>`).join("");
  openModal("📥 将外部资料夹导入现有知识库", `
    <div class="form-group">
      <label>1. 目标知识库</label>
      <select id="impVaultId">${opts}</select>
    </div>
    <div class="form-group">
      <label>2. 选择要导入的外部文件夹（文件将只读复制入史料层并自动提取索引）</label>
      <input type="text" id="impExtPath" placeholder="/Users/.../外部资料文件夹" style="margin-bottom:8px;" />
      <div id="fsBrowserMount"></div>
    </div>
    <div class="form-group">
      <label>3. 史料层目标子目录名（留空则默认使用原文件夹名）</label>
      <input type="text" id="impSubfolder" placeholder="例如：2026招标方案" />
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" id="btnConfirmImport" onclick="submitImportFolder()">📥 开始导入并更新索引</button>
    </div>
  `);
  loadFsBrowser("", "impExtPath", "");
}

async function submitImportFolder() {
  const vid = document.getElementById("impVaultId").value;
  const ext = document.getElementById("impExtPath").value.trim();
  const sub = document.getElementById("impSubfolder").value.trim();
  if (!ext) return toast("请选择外部文件夹路径", true);
  const btn = document.getElementById("btnConfirmImport");
  btn.disabled = true;
  btn.textContent = "正在复制并增量提取镜像...";
  try {
    const r = await api("/api/vaults/import-folder", {
      method: "POST",
      body: { vault_id: vid, external_folder: ext, target_subfolder: sub },
    });
    closeModal();
    toast(`成功导入 ${r.copied_count} 个文件至 ${r.target_subfolder} 并完成索引更新！`);
    await refreshAll();
  } catch (e) {
    toast("导入失败: " + e.message, true);
    btn.disabled = false;
    btn.textContent = "📥 开始导入并更新索引";
  }
}

/* ==================== TAB 2: TOKENS & GRAPHICAL ACL ==================== */

const ROLE_PRESETS = [
  { id: "partner", title: "🤝 外部合作方 (partner)", desc: "默认全部禁止，仅点选开放指定目录", mode: "whitelist" },
  { id: "rd-team", title: "👨‍💻 研发成员 (rd-team)", desc: "默认全库可读，点选屏蔽财务/人事等敏感目录", mode: "blacklist" },
  { id: "sales", title: "📊 业务/销售组 (sales)", desc: "默认全库可读，点选屏蔽敏感研发与财务目录", mode: "blacklist" },
  { id: "guest", title: "🎓 访客/实习生 (guest)", desc: "默认全部禁止，仅开放公开培训与手册目录", mode: "whitelist" },
];

function summarizeRoleSpec(spec) {
  const allow = (spec && spec.allow) || [];
  const deny = (spec && spec.deny) || [];
  const isAllowAll = allow.includes("**");
  if (isAllowAll) {
    return deny.length ? `全库可读 · 屏蔽 ${deny.length} 个目录` : `全库可读（点击文件夹可屏蔽）`;
  }
  return allow.length ? `白名单 · 仅开放 ${allow.length} 个目录` : `未开放任何目录（请点选开放）`;
}

function buildRoleChecksHtml(clsName, selectedGroups, includeCreateBtn) {
  const sel = new Set((selectedGroups && selectedGroups.length) ? selectedGroups : ["all"]);
  const hasFull = sel.has("all") || sel.has("admin");
  const roleEntries = Object.entries(STATE.acl || {});
  let html = `
    <label class="check-pill" style="${hasFull ? 'border-color:#10b981;background:#ecfdf5;' : ''}">
      <input type="checkbox" value="all" class="${clsName}" ${hasFull ? "checked" : ""} onchange="onRolePillChange(this, '${clsName}')" />
      <span>🛡️ <strong>完整访问权限 (all)</strong></span>
    </label>
  `;
  html += roleEntries.map(([rname, spec]) => {
    const checked = !hasFull && sel.has(rname);
    return `
      <label class="check-pill" style="${checked ? 'border-color:var(--primary);background:#eff6ff;' : ''}">
        <input type="checkbox" value="${esc(rname)}" class="${clsName}" ${checked ? "checked" : ""} onchange="onRolePillChange(this, '${clsName}')" />
        <span>🏷️ <strong>${esc(rname)}</strong> <span style="font-size:11.5px;color:var(--muted);">(${esc(summarizeRoleSpec(spec))})</span></span>
      </label>
    `;
  }).join("");
  if (includeCreateBtn) {
    html += `<button type="button" class="btn-sm" style="border-style:dashed;" onclick="openCreateRoleModal(true)">＋ 新建受限角色组</button>`;
  }
  return html;
}

function onRolePillChange(el, clsName) {
  const boxes = Array.from(document.querySelectorAll("." + clsName));
  if (el.value === "all" && el.checked) {
    boxes.forEach(c => { if (c.value !== "all") c.checked = false; });
  } else if (el.value !== "all" && el.checked) {
    const allBox = boxes.find(c => c.value === "all");
    if (allBox) allBox.checked = false;
  }
  if (!boxes.some(c => c.checked)) {
    const allBox = boxes.find(c => c.value === "all");
    if (allBox) allBox.checked = true;
  }
  // Refresh visual highlight on parent pill
  boxes.forEach(c => {
    const lbl = c.closest(".check-pill");
    if (!lbl) return;
    if (c.checked) {
      lbl.style.borderColor = c.value === "all" ? "#10b981" : "var(--primary)";
      lbl.style.background = c.value === "all" ? "#ecfdf5" : "#eff6ff";
    } else {
      lbl.style.borderColor = "";
      lbl.style.background = "";
    }
  });
}

function renderTokensAndAcl() {
  const localVaults = STATE.vaults.filter(v => v.kind !== "remote");

  // 1. Global LAN Sharing Gateway Master Switch (Top of Tab 2 — independent of tokens!)
  const lan = STATE.lan_gateway || {};
  const lanBar = document.getElementById("lanGatewayBar");
  if (lanBar) {
    const recUrl = lan.recommended_url || STATE.gateway_base_url;
    const tokCnt = lan.active_token_count ?? STATE.tokens.filter(t => !t.disabled).length;
    const statusHtml = lan.running
      ? `<span class="badge badge-green">● 局域网共享总开关：已开启 (${esc(lan.bind_host)}:${lan.port})</span>`
      : `<span class="badge badge-gray">○ 局域网共享总开关：未开启（当前仅本机 127.0.0.1 可用）</span>`;
    const tokHint = tokCnt === 0
      ? `<span class="badge badge-amber">当前生效令牌: 0 枚（未持令牌连接时看不到任何知识库）</span>`
      : `<span class="badge badge-blue">当前生效令牌: ${tokCnt} 枚（按令牌隔离可见知识库与目录）</span>`;
    const actionBtn = lan.running
      ? (lan.mode === "embedded"
          ? `<button class="btn-sm btn-danger" onclick="toggleLanGateway(false)">⏹ 关闭局域网共享</button>`
          : `<span class="badge badge-blue">独立守护网关运行中</span>`)
      : `<button class="btn-sm btn-primary" onclick="toggleLanGateway(true)">🚀 一键开启局域网共享 (:${lan.port || 8090})</button>`;
    lanBar.innerHTML = `<div class="card" style="padding:14px 18px;border-color:${lan.running ? '#86efac' : 'var(--border)'};background:${lan.running ? '#f0fdf4' : 'var(--surface)'};">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
        <div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:5px;">
            <strong style="font-size:14.5px;">📡 局域网共享网关总开关</strong> ${statusHtml} ${tokHint}
          </div>
          <div style="font-size:12.5px;color:var(--muted);">
            本机共享地址：<code class="mono" style="font-weight:700;color:var(--text);">${esc(recUrl)}</code> —— 可随时开启共享（无令牌时默认不暴露任何知识库）；下方签发令牌后，另一台电脑带上对应令牌即可访问该令牌授权的知识库。
          </div>
        </div>
        <div class="btn-row">
          <button class="btn-sm" onclick="copyText('${esc(recUrl)}', '已复制本机局域网网关地址！')">📋 复制本机地址</button>
          ${actionBtn}
        </div>
      </div>
    </div>`;
  }

  // 2. Vault checkboxes for creating token (local hosted vaults only)
  const prevTokVaults = new Set(Array.from(document.querySelectorAll(".tok-v-chk:checked")).map(c => c.value));
  const tokDefaultAll = prevTokVaults.size === 0 || prevTokVaults.has("*");
  const vc = document.getElementById("tokVaultChecks");
  vc.innerHTML = `<label class="check-pill"><input type="checkbox" value="*" class="tok-v-chk" ${tokDefaultAll ? "checked" : ""} onchange="onTokAllChange(this)" /> 全部本机知识库 (*)</label>` +
    localVaults.map(v => `<label class="check-pill"><input type="checkbox" value="${esc(v.id)}" class="tok-v-chk" ${(!tokDefaultAll && prevTokVaults.has(v.id)) ? "checked" : ""} onchange="onTokSpecificChange()" /> ${esc(v.name)} (${esc(v.id)})</label>`).join("");

  // 3. Graphical Role Picker for creating token (preserve current selection or newly created role)
  const prevRoles = window.__pendingSelectRole
    ? [window.__pendingSelectRole]
    : Array.from(document.querySelectorAll(".tok-r-chk:checked")).map(c => c.value);
  window.__pendingSelectRole = null;
  document.getElementById("tokRoleChecks").innerHTML = buildRoleChecksHtml("tok-r-chk", prevRoles, true);

  // 4. Tokens table
  const wrap = document.getElementById("tokensTableWrap");
  if (!STATE.tokens.length) {
    wrap.innerHTML = `<div style="color:var(--muted);padding:14px 0;font-size:13px;">暂无已签发令牌（此时外部设备即使连上网关也看不到任何知识库）。在上方填写标识并点选权限即可签发。</div>`;
  } else {
    wrap.innerHTML = `<table>
      <thead><tr><th>身份 / 备注</th><th>脱敏密钥</th><th>授权知识库</th><th>角色组</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${STATE.tokens.map(t => {
        const avBadges = (t.allowed_vaults || ["*"]).map(v =>
          v === "*" ? `<span class="badge badge-blue">全部 (*)</span>` : `<span class="badge badge-purple mono">${esc(v)}</span>`
        ).join(" ");
        const grpBadges = (t.groups || []).map(g =>
          (g === "all" || g === "admin") ? `<span class="badge badge-green">🛡️ 完整权限 (${esc(g)})</span>` : `<span class="badge badge-amber">🏷️ ${esc(g)}</span>`
        ).join(" ");
        return `<tr>
          <td><strong>${esc(t.uid)}</strong><div style="font-size:11.5px;color:var(--muted)">${esc(t.label)}</div></td>
          <td class="mono">${esc(t.token_preview)}</td>
          <td>${avBadges}</td>
          <td>${grpBadges}</td>
          <td>${t.disabled ? '<span class="badge badge-red">已禁用</span>' : '<span class="badge badge-green">生效中</span>'}</td>
          <td>
            <div class="btn-row">
              <button class="btn-sm" onclick="openEditTokenModal('${esc(t.id)}')">配置权限</button>
              <button class="btn-sm" onclick="toggleToken('${esc(t.id)}', ${!t.disabled})">${t.disabled ? '启用' : '禁用'}</button>
              <button class="btn-sm btn-danger" onclick="deleteToken('${esc(t.id)}', '${esc(t.uid)}')">吊销</button>
            </div>
          </td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
  }

  // 5. Visual Folder ACL Editor
  renderAclEditor();

  // 6. Simulator dropdowns
  document.getElementById("simTokenSelect").innerHTML = STATE.tokens.length
    ? STATE.tokens.map(t => `<option value="${esc(t.id)}">${esc(t.uid)} (${(t.groups||[]).join(",")})</option>`).join("")
    : `<option value="">-- 请先创建令牌 --</option>`;
  document.getElementById("simVaultSelect").innerHTML = localVaults.length
    ? localVaults.map(v => `<option value="${esc(v.id)}">${esc(v.name)} (${esc(v.id)})</option>`).join("")
    : `<option value="">-- 无本机知识库 --</option>`;
}

async function toggleLanGateway(enable) {
  try {
    if (enable) {
      const r = await api("/api/lan-gateway/start", { method: "POST", body: { host: "0.0.0.0", port: 8090 } });
      toast(`已开启局域网共享网关：${r.recommended_url}`);
    } else {
      await api("/api/lan-gateway/stop", { method: "POST", body: {} });
      toast("已关闭局域网共享网关");
    }
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

function onTokAllChange(el) {
  if (el.checked) {
    document.querySelectorAll(".tok-v-chk").forEach(c => { if (c.value !== "*") c.checked = false; });
  }
}
function onTokSpecificChange() {
  const anySpec = Array.from(document.querySelectorAll(".tok-v-chk")).some(c => c.value !== "*" && c.checked);
  if (anySpec) {
    const allChk = document.querySelector('.tok-v-chk[value="*"]');
    if (allChk) allChk.checked = false;
  }
}

async function createToken() {
  const uid = document.getElementById("tokUid").value.trim();
  if (!uid) return toast("请填写令牌身份标识 (uid)", true);
  const label = document.getElementById("tokLabel").value.trim();
  const groups = Array.from(document.querySelectorAll(".tok-r-chk:checked")).map(c => c.value);
  const allowed_vaults = Array.from(document.querySelectorAll(".tok-v-chk:checked")).map(c => c.value);
  try {
    const res = await api("/api/tokens/create", {
      method: "POST",
      body: { uid, label, groups: groups.length ? groups : ["all"], allowed_vaults: allowed_vaults.length ? allowed_vaults : ["*"] },
    });
    document.getElementById("tokUid").value = "";
    document.getElementById("tokLabel").value = "";
    await refreshAll();
    const recUrl = (STATE.lan_gateway && STATE.lan_gateway.recommended_url) || STATE.gateway_base_url;
    const banner = document.getElementById("newTokenBanner");
    banner.style.display = "block";
    banner.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
        <div>
          <strong>🎉 新令牌已签发（明文仅显示这一次，请复制发给使用方电脑）：</strong>
          <div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
            <span style="font-size:12.5px;">网关地址：<code class="mono" style="background:#fff;padding:4px 8px;border-radius:5px;border:1px solid #a7f3d0;">${esc(recUrl)}</code></span>
            <span style="font-size:12.5px;">令牌：<code class="mono" style="background:#fff;padding:4px 8px;border-radius:5px;border:1px solid #a7f3d0;">${esc(res.token)}</code></span>
          </div>
          <div style="font-size:12px;margin-top:6px;color:#065f46;">💡 确保顶部「📡 局域网共享网关总开关」已开启，在另一台电脑点「🌐 连接远程知识库」填入此地址与令牌，即可直接访问该令牌授权的知识库！</div>
        </div>
        <div class="btn-row">
          <button class="btn-sm btn-primary" onclick="copyText('${esc(res.token)}', '令牌明文已复制！')">📋 复制 Token</button>
          <button class="btn-sm" onclick="copyText('网关地址: ${esc(recUrl)}\\n访问令牌: ${esc(res.token)}', '地址与令牌已一起复制！')">📋 复制地址+令牌</button>
          <button class="btn-sm" onclick="document.getElementById('newTokenBanner').style.display='none'">收起</button>
        </div>
      </div>
    `;
  } catch (e) {
    toast(e.message, true);
  }
}

function useTokenInMcpTab(rawToken) {
  document.getElementById("mcpTransportMode").value = "http";
  toggleMcpGatewayFields();
  document.getElementById("mcpGatewayToken").value = rawToken;
  switchTab("clients");
  toast("已切至手动网关覆盖模式并填入新令牌");
}

function openEditTokenModal(tid) {
  const t = STATE.tokens.find(x => x.id === tid);
  if (!t) return;
  const localVaults = STATE.vaults.filter(v => v.kind !== "remote");
  const hasAll = (t.allowed_vaults || []).includes("*");
  const vChecks = `<label class="check-pill"><input type="checkbox" class="edit-tok-v" value="*" ${hasAll ? "checked" : ""} /> 全部本机知识库 (*)</label>` +
    localVaults.map(v => `<label class="check-pill"><input type="checkbox" class="edit-tok-v" value="${esc(v.id)}" ${(!hasAll && (t.allowed_vaults||[]).includes(v.id)) ? "checked" : ""} /> ${esc(v.name)} (${esc(v.id)})</label>`).join("");
  const rChecks = buildRoleChecksHtml("edit-tok-r", t.groups || ["all"], false);
  openModal(`编辑令牌权限范围 · ${t.uid}`, `
    <div class="form-group">
      <label>备注名称</label>
      <input type="text" id="edTokLabel" value="${esc(t.label)}" />
    </div>
    <div class="form-group">
      <label>可访问知识库范围</label>
      <div class="check-grid">${vChecks}</div>
    </div>
    <div class="form-group">
      <label>选择访问角色组（点击勾选）</label>
      <div class="check-grid">${rChecks}</div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" onclick="submitEditToken('${esc(t.id)}')">保存修改（立即热生效）</button>
    </div>
  `);
}

async function submitEditToken(tid) {
  const label = document.getElementById("edTokLabel").value.trim();
  const groups = Array.from(document.querySelectorAll(".edit-tok-r:checked")).map(c => c.value);
  let av = Array.from(document.querySelectorAll(".edit-tok-v:checked")).map(c => c.value);
  if (av.length > 1 && av.includes("*")) av = av.filter(x => x !== "*");
  try {
    await api("/api/tokens/update", { method: "POST", body: { id: tid, label, groups: groups.length ? groups : ["all"], allowed_vaults: av.length ? av : ["*"] } });
    closeModal();
    toast("令牌权限已热更新生效");
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

async function toggleToken(tid, disabled) {
  await api("/api/tokens/update", { method: "POST", body: { id: tid, disabled } });
  toast(disabled ? "令牌已禁用" : "令牌已重新启用");
  await refreshAll();
}

async function deleteToken(tid, uid) {
  if (!confirm(`确定永久吊销令牌「${uid}」吗？吊销后立即返回 401。`)) return;
  await api("/api/tokens/delete", { method: "POST", body: { id: tid } });
  toast("令牌已吊销");
  await refreshAll();
}

function renderAclEditor() {
  const box = document.getElementById("aclRolesEditor");
  const roles = Object.entries(STATE.acl || {});
  const localVaults = STATE.vaults.filter(v => v.kind !== "remote");
  if (!roles.length) {
    box.innerHTML = `<div class="empty-state" style="padding:22px 16px;">
      <div style="font-weight:600;color:var(--text);margin-bottom:4px;">当前尚未创建受限角色组（默认令牌拥有完整目录读取权限）</div>
      <div style="font-size:12.5px;margin-bottom:12px;">如需让外部合作方或不同部门成员只看指定文件夹（或屏蔽财务/简历等敏感文件夹），点击下方按钮以图形化方式点选目录即可。</div>
      <button class="btn-primary btn-sm" onclick="openCreateRoleModal(false)">＋ 新建第一个受限角色组</button>
    </div>`;
    return;
  }

  // Collect all known prefixes on disk so we can also surface any legacy custom prefixes gracefully
  const knownPrefixes = new Set(["**"]);
  localVaults.forEach(v => (v.source_prefixes || []).forEach(p => knownPrefixes.add(p)));

  box.innerHTML = roles.map(([rname, spec]) => {
    const allowList = Array.isArray(spec.allow) ? spec.allow : [];
    const denyList = Array.isArray(spec.deny) ? spec.deny : [];
    const isBlacklistMode = allowList.includes("**");
    const allowSet = new Set(allowList);
    const denySet = new Set(denyList);

    const vaultsHtml = localVaults.length ? localVaults.map(v => {
      const tree = v.folder_tree || [];
      const rootPrefix = `${v.sources_dir || '史料'}/**`;
      if (!tree.length) {
        // Vault has no subfolders yet: render whole-vault toggle
        let cls = "folder-closed", icon = "🔒", stateTxt = "未开放";
        if (isBlacklistMode) {
          if (denySet.has(rootPrefix)) { cls = "folder-deny"; icon = "🚫"; stateTxt = "整库已屏蔽"; }
          else { cls = "folder-allow"; icon = "✅"; stateTxt = "整库可读"; }
        } else {
          if (allowSet.has(rootPrefix)) { cls = "folder-allow"; icon = "✅"; stateTxt = "整库已开放"; }
        }
        return `<div style="margin-top:8px;">
          <div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:4px;">📚 ${esc(v.name)} <span class="mono">(${esc(v.id)})</span></div>
          <div class="folder-grid">
            <div class="folder-toggle ${cls}" onclick="toggleFolderRule('${esc(rname)}', '${esc(rootPrefix)}', '')">
              <span>${icon} 📁 ${esc(v.sources_dir || '史料')}（暂无子目录 · ${stateTxt}）</span>
            </div>
          </div>
        </div>`;
      }

      const nodesHtml = tree.map(node => {
        const pfx = node.prefix;
        let cls = "folder-closed", icon = "🔒", tip = "未开放（点击开放）";
        if (isBlacklistMode) {
          if (denySet.has(pfx)) { cls = "folder-deny"; icon = "🚫"; tip = "已禁止（点击恢复允许）"; }
          else { cls = "folder-allow"; icon = "✅"; tip = "允许（点击屏蔽）"; }
        } else {
          if (allowSet.has(pfx)) { cls = "folder-allow"; icon = "✅"; tip = "已开放（点击关闭）"; }
        }

        const parentAllowed = isBlacklistMode ? !denySet.has(pfx) : allowSet.has(pfx);
        const subHtml = (node.children || []).map(sub => {
          const sp = sub.prefix;
          let sCls = "folder-closed", sIcon = "🔒", sLbl = "未开放";
          if (isBlacklistMode) {
            if (denySet.has(pfx) || denySet.has(sp)) { sCls = "folder-deny"; sIcon = "🚫"; sLbl = denySet.has(pfx) ? "随上级禁止" : "已屏蔽"; }
            else { sCls = "folder-allow"; sIcon = "✅"; sLbl = "允许"; }
          } else {
            if (parentAllowed) {
              if (denySet.has(sp)) { sCls = "folder-deny"; sIcon = "🚫"; sLbl = "例外屏蔽"; }
              else { sCls = "folder-allow"; sIcon = "✅"; sLbl = "随上级开放"; }
            } else {
              if (allowSet.has(sp)) { sCls = "folder-allow"; sIcon = "✅"; sLbl = "单独开放"; }
            }
          }
          return `<div class="folder-toggle folder-sub ${sCls}" title="点击切换该子文件夹权限" onclick="toggleFolderRule('${esc(rname)}', '${esc(sp)}', '${esc(pfx)}')">
            <span>${sIcon} ↳ ${esc(sub.name)} <small style="opacity:0.8">(${sLbl})</small></span>
          </div>`;
        }).join("");

        return `<div class="folder-toggle ${cls}" title="${tip}" onclick="toggleFolderRule('${esc(rname)}', '${esc(pfx)}', '')">
          <span>${icon} 📁 ${esc(node.name)}</span>
        </div>${subHtml}`;
      }).join("");

      return `<div style="margin-top:10px;">
        <div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:4px;">📚 ${esc(v.name)} <span class="mono">(${esc(v.id)})</span> — 点击文件夹直接切换：</div>
        <div class="folder-grid">${nodesHtml}</div>
      </div>`;
    }).join("") : `<div style="font-size:12px;color:var(--muted);margin-top:8px;">请先在「知识库与目录纳管」添加本地知识库，文件夹将自动展示在此处供点选。</div>`;

    // Render any non-standard custom prefixes (if present from external JSON edits) as removable chips
    const extraAllows = allowList.filter(p => !knownPrefixes.has(p));
    const extraDenies = denyList.filter(p => !knownPrefixes.has(p));
    const extrasHtml = (extraAllows.length || extraDenies.length) ? `
      <div style="margin-top:8px;padding-top:6px;border-top:1px dashed var(--border);font-size:11.5px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
        <span>其他规则项：</span>
        ${extraAllows.map(p => `<span class="badge badge-green mono">允许: ${esc(p)} <span style="cursor:pointer;margin-left:4px;" onclick="removeRawRulePrefix('${esc(rname)}','allow','${esc(p)}')">×</span></span>`).join("")}
        ${extraDenies.map(p => `<span class="badge badge-red mono">禁止: ${esc(p)} <span style="cursor:pointer;margin-left:4px;" onclick="removeRawRulePrefix('${esc(rname)}','deny','${esc(p)}')">×</span></span>`).join("")}
      </div>` : "";

    return `<div style="border:1px solid var(--border);border-radius:10px;padding:13px 14px;margin-bottom:12px;background:var(--surface-2);">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding-bottom:8px;border-bottom:1px solid var(--border);">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <strong>角色组：<span class="badge badge-amber" style="font-size:13px;">🏷️ ${esc(rname)}</span></strong>
          <div class="mode-switch">
            <button type="button" class="${isBlacklistMode ? 'active-mode' : ''}" onclick="switchRoleMode('${esc(rname)}', 'blacklist')">🌐 默认全库可读 · 点选屏蔽</button>
            <button type="button" class="${!isBlacklistMode ? 'active-mode' : ''}" onclick="switchRoleMode('${esc(rname)}', 'whitelist')">🔒 默认全部禁止 · 点选开放</button>
          </div>
        </div>
        <button class="btn-sm btn-danger" onclick="removeAclRole('${esc(rname)}')">删除角色</button>
      </div>
      ${vaultsHtml}
      ${extrasHtml}
    </div>`;
  }).join("");
}

async function persistAclState(tipMsg) {
  try {
    await api("/api/acl/save", { method: "POST", body: { acl: STATE.acl || {} } });
    if (tipMsg) toast(tipMsg);
    renderTokensAndAcl();
  } catch (e) {
    toast("保存权限失败: " + e.message, true);
  }
}

async function switchRoleMode(rname, targetMode) {
  const cur = (STATE.acl && STATE.acl[rname]) || { allow: [], deny: [] };
  if (targetMode === "blacklist") {
    const nextAllow = Array.from(new Set(["**", ...(cur.allow || []).filter(x => x !== "**")]));
    STATE.acl[rname] = { allow: ["**"], deny: cur.deny || [] };
  } else {
    STATE.acl[rname] = { allow: (cur.allow || []).filter(x => x !== "**"), deny: cur.deny || [] };
  }
  await persistAclState(`已切换「${rname}」权限模式并热生效`);
}

async function toggleFolderRule(rname, prefix, parentPrefix) {
  const spec = (STATE.acl && STATE.acl[rname]) || { allow: ["**"], deny: [] };
  const allow = new Set(spec.allow || []);
  const deny = new Set(spec.deny || []);
  const isBlacklist = allow.has("**");

  if (isBlacklist) {
    if (parentPrefix && deny.has(parentPrefix)) {
      toast("上级目录已整体禁止，如需单独控制子目录请先点击恢复上级目录", true);
      return;
    }
    if (deny.has(prefix)) deny.delete(prefix);
    else deny.add(prefix);
  } else {
    if (parentPrefix && allow.has(parentPrefix)) {
      // Parent is open in whitelist mode: clicking child toggles exception block in `deny`
      if (deny.has(prefix)) deny.delete(prefix);
      else deny.add(prefix);
    } else {
      if (allow.has(prefix)) {
        allow.delete(prefix);
      } else {
        allow.add(prefix);
        deny.delete(prefix);
      }
    }
  }
  STATE.acl[rname] = { allow: Array.from(allow), deny: Array.from(deny) };
  await persistAclState("✓ 目录权限已自动保存并热生效");
}

async function removeRawRulePrefix(rname, field, prefix) {
  const spec = STATE.acl && STATE.acl[rname];
  if (!spec) return;
  spec[field] = (spec[field] || []).filter(x => x !== prefix);
  await persistAclState("已移除规则项");
}

function openCreateRoleModal(selectForNewToken) {
  window.__createRoleSelectAfter = Boolean(selectForNewToken);
  window.__selectedPresetId = "partner";
  window.__selectedRoleMode = "whitelist";
  const presetsHtml = ROLE_PRESETS.map((p, idx) => `
    <div class="preset-card ${idx === 0 ? 'selected' : ''}" data-pid="${esc(p.id)}" onclick="pickRolePreset('${esc(p.id)}', '${esc(p.mode)}')">
      <div style="font-weight:700;font-size:13.5px;margin-bottom:2px;">${esc(p.title)}</div>
      <div style="font-size:12px;color:var(--muted);">${esc(p.desc)}</div>
    </div>
  `).join("");
  openModal("🛡️ 新建受限角色组（点选预设，无需手敲规则）", `
    <div class="form-group">
      <label>1. 点击选择常用角色模板</label>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">${presetsHtml}</div>
    </div>
    <div class="form-group">
      <label>2. 角色标识名称（已由上方模板自动填入，也可自定义）</label>
      <input type="text" id="newRoleNameInput" value="partner" placeholder="例如：partner、rd-team" />
    </div>
    <div class="form-group">
      <label>3. 默认目录授权方式（创建后直接点击文件夹即可开关）</label>
      <select id="newRoleModeSelect">
        <option value="whitelist" selected>🔒 白名单模式：默认全部禁止，仅开放点选的文件夹（适合外部合作方/访客）</option>
        <option value="blacklist">🌐 黑名单模式：默认全库可读，仅屏蔽点选的敏感文件夹（适合内部团队）</option>
      </select>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" onclick="submitCreateRole()">创建并开始点选文件夹</button>
    </div>
  `);
}

function pickRolePreset(pid, mode) {
  document.querySelectorAll(".preset-card").forEach(el => {
    el.classList.toggle("selected", el.dataset.pid === pid);
  });
  document.getElementById("newRoleNameInput").value = pid;
  document.getElementById("newRoleModeSelect").value = mode;
}

async function submitCreateRole() {
  const raw = document.getElementById("newRoleNameInput").value.trim();
  const clean = raw.replace(/[^a-zA-Z0-9_\u4e00-\u9fff-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!clean) return toast("请选择或输入角色组名称", true);
  if (clean === "all" || clean === "admin") return toast("all 与 admin 为系统内置完整权限组，请使用其他名称", true);
  const mode = document.getElementById("newRoleModeSelect").value;
  STATE.acl = STATE.acl || {};
  if (!STATE.acl[clean]) {
    STATE.acl[clean] = mode === "blacklist" ? { allow: ["**"], deny: [] } : { allow: [], deny: [] };
  }
  if (window.__createRoleSelectAfter) {
    window.__pendingSelectRole = clean;
  }
  closeModal();
  await persistAclState(`角色组「${clean}」已创建！请在右侧点击文件夹设置允许或禁止`);
}

async function removeAclRole(rname) {
  if (!confirm(`确定删除角色组「${rname}」吗？`)) return;
  delete STATE.acl[rname];
  await persistAclState(`已删除角色组「${rname}」`);
}

async function runAclSimulation() {
  const tid = document.getElementById("simTokenSelect").value;
  const vid = document.getElementById("simVaultSelect").value;
  if (!tid || !vid) return toast("请先选择令牌与知识库", true);
  const box = document.getElementById("simResultBox");
  try {
    const r = await api("/api/acl/simulate", { method: "POST", body: { token_id: tid, vault_id: vid } });
    if (!r.vault_allowed) {
      box.innerHTML = `<div class="callout callout-amber">🚫 <strong>拦截：</strong>${esc(r.reason)}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="callout callout-green" style="margin-bottom:8px;">
        ✅ <strong>${esc(r.reason)}</strong><br/>
        允许原件：<strong>${r.allowed_sources_count}</strong> 篇 ｜ 拦截原件：<strong style="color:var(--danger)">${r.denied_sources_count}</strong> 篇 ｜
        可见 Wiki：<strong>${r.allowed_wiki_count}</strong> 篇 ｜ 溯源拦截 Wiki：<strong style="color:var(--danger)">${r.denied_wiki_count}</strong> 篇
      </div>
      ${r.denied_sources_count ? `<div style="font-size:12px;margin-top:6px;"><strong>被拦截的原件示例：</strong> <span class="mono" style="color:var(--danger)">${esc(r.denied_sources.slice(0,8).join(" , "))}</span></div>` : ""}
      ${r.denied_wiki_count ? `<div style="font-size:12px;margin-top:4px;"><strong>因溯源继承自动隐藏的 Wiki：</strong> <span class="mono" style="color:var(--danger)">${esc(r.denied_wiki.join(" , "))}</span></div>` : ""}
    `;
  } catch (e) {
    box.innerHTML = `<div class="callout callout-amber">${esc(e.message)}</div>`;
  }
}

/* ==================== TAB 3: MCP CLIENTS ==================== */

function toggleMcpGatewayFields() {
  const mode = document.getElementById("mcpTransportMode").value;
  const isManualOverride = mode !== "auto" && mode !== "stdio";
  document.getElementById("mcpGatewayFields").style.display = isManualOverride ? "block" : "none";
  document.getElementById("mcpStdioNote").style.display = isManualOverride ? "none" : "block";
}

function getSelectedMcpVaultIds() {
  return Array.from(document.querySelectorAll(".mcp-v-chk:checked")).map(c => c.value);
}

function renderClients() {
  const prevChecked = new Set(getSelectedMcpVaultIds());
  const useAllDefault = prevChecked.size === 0;
  document.getElementById("mcpVaultChecks").innerHTML = STATE.vaults.length
    ? STATE.vaults.map(v => {
        const tag = v.kind === "remote" ? "🌐 远程" : "📁 本地";
        return `<label class="check-pill"><input type="checkbox" class="mcp-v-chk" value="${esc(v.id)}" ${(useAllDefault || prevChecked.has(v.id)) ? "checked" : ""} /> <span>${tag} · <strong>${esc(v.name)}</strong> <span class="mono">(${esc(v.id)})</span></span></label>`;
      }).join("")
    : `<span style="color:var(--muted);font-size:13px;">请先在「知识库与目录纳管」纳管本地目录或连接远程知识库</span>`;

  const grid = document.getElementById("clientsGrid");
  grid.innerHTML = STATE.clients.map(c => {
    let statusBadge = `<span class="badge badge-gray">未配置</span>`;
    if (c.sync_status === "synced") statusBadge = `<span class="badge badge-green">✓ 已全部同步 (${c.matched_count}/${c.total_vaults})</span>`;
    else if (c.sync_status === "partial") statusBadge = `<span class="badge badge-amber">部分同步 (${c.matched_count}/${c.total_vaults})</span>`;

    const instBadge = c.installed
      ? `<span class="badge badge-blue">本机已安装</span>`
      : `<span class="badge badge-gray">未检测到目录</span>`;

    const srvs = Object.values(c.configured_servers || {});
    const srvHtml = srvs.length
      ? srvs.map(s => `<div style="font-size:12px;padding:5px 8px;background:var(--surface-2);border-radius:6px;margin-bottom:4px;display:flex;justify-content:space-between;gap:8px;">
          <span><strong class="mono">${esc(s.server_name)}</strong> <span class="badge badge-gray">${esc(s.mode)}</span></span>
          <span class="mono" style="color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(s.vault_path || s.url)}</span>
        </div>`).join("")
      : `<div style="font-size:12.5px;color:var(--muted);padding:8px 0;">当前客户端尚未写入 wiki-context MCP 条目</div>`;

    return `<div class="card">
      <div class="card-header">
        <div>
          <h3 class="card-title">${esc(c.name)} ${instBadge} ${statusBadge}</h3>
          <div class="path-text">${esc(c.config_path)}</div>
        </div>
      </div>
      <div style="font-size:12.5px;color:var(--muted);margin-bottom:10px;">${esc(c.description)}</div>
      <div style="margin-bottom:12px;">${srvHtml}</div>
      <div class="btn-row">
        <button class="btn-sm btn-primary" onclick="syncSingleClient('${esc(c.id)}', '${esc(c.name)}')">⚡ 写入 ${esc(c.name)}</button>
        <button class="btn-sm" onclick="previewClientConfig('${esc(c.id)}', '${esc(c.name)}')">👁 预览 / 复制配置片段</button>
      </div>
    </div>`;
  }).join("");
}

function buildMcpSyncPayload(clientIds) {
  const mode = document.getElementById("mcpTransportMode").value;
  const vault_ids = getSelectedMcpVaultIds();
  const gateway_url = document.getElementById("mcpGatewayUrl").value.trim();
  const token = document.getElementById("mcpGatewayToken").value.trim();
  return { client_ids: clientIds, mode, vault_ids, gateway_url, token, remove_unlisted_wctx: true };
}

async function syncSingleClient(cid, cname) {
  const payload = buildMcpSyncPayload([cid]);
  if (payload.mode !== "auto" && payload.mode !== "stdio" && !payload.token) {
    return toast("手动网关覆盖模式下请填写 Bearer Token", true);
  }
  try {
    const r = await api("/api/clients/sync", { method: "POST", body: payload });
    const item = (r.results || [])[0] || {};
    toast(`已同步写入 ${cname}：挂载 [${(item.written||[]).join(", ")}]（原文件已自动备份 .wctx.bak）`);
    await refreshAll();
  } catch (e) {
    toast("写入失败: " + e.message, true);
  }
}

async function syncAllInstalledClients() {
  const installedIds = STATE.clients.filter(c => c.installed).map(c => c.id);
  if (!installedIds.length) return toast("未检测到已安装客户端，请单独点击卡片写入", true);
  const payload = buildMcpSyncPayload(installedIds);
  if (payload.mode !== "auto" && payload.mode !== "stdio" && !payload.token) {
    return toast("手动网关覆盖模式下请先填写 Bearer Token", true);
  }
  try {
    const r = await api("/api/clients/sync", { method: "POST", body: payload });
    toast(`已一键同步至 ${(r.results||[]).length} 个 AI 客户端！`);
    await refreshAll();
  } catch (e) {
    toast("同步失败: " + e.message, true);
  }
}

async function previewClientConfig(cid, cname) {
  const p = buildMcpSyncPayload([cid]);
  try {
    const r = await api("/api/clients/preview", {
      method: "POST",
      body: { client_id: cid, mode: p.mode, vault_ids: p.vault_ids, gateway_url: p.gateway_url, token: p.token },
    });
    window.__lastPreviewSnippet = r.snippet;
    openModal(`配置预览 · ${cname}`, `
      <div class="form-hint" style="margin-bottom:6px;">目标配置文件：<code class="mono">${esc(r.config_path)}</code></div>
      <pre class="codebox">${esc(r.snippet)}</pre>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px;">
        <button onclick="copyText(window.__lastPreviewSnippet, '配置片段已复制')">📋 复制片段</button>
        <button class="btn-primary" onclick="closeModal();syncSingleClient('${esc(cid)}','${esc(cname)}')">⚡ 直接写入该配置文件</button>
      </div>
    `);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ==================== TAB 4: LAYER-4 MEMORY & ERRATA ==================== */

function syncMemoryVaultSelector() {
  const sel = document.getElementById("memVaultSelect");
  const cur = sel.value;
  const localVaults = STATE.vaults.filter(v => v.kind !== "remote");
  sel.innerHTML = localVaults.length
    ? localVaults.map(v => `<option value="${esc(v.id)}">${esc(v.name)} (${esc(v.id)})</option>`).join("")
    : `<option value="">-- 请先在本机纳管本地知识库 --</option>`;
  if (cur && localVaults.some(v => v.id === cur)) sel.value = cur;
}

function jumpToMemory(vid) {
  syncMemoryVaultSelector();
  document.getElementById("memVaultSelect").value = vid;
  switchTab("memory");
}

function debouncedLoadMemories() {
  clearTimeout(memTimer);
  memTimer = setTimeout(loadMemories, 220);
}

const KIND_META = {
  errata: { label: "📌 原件勘误钉 (errata)", cls: "badge-red" },
  insight: { label: "💡 实战洞察 (insight)", cls: "badge-blue" },
  decision: { label: "⚖️ 架构决策 (decision)", cls: "badge-purple" },
  glossary: { label: "📖 术语口径 (glossary)", cls: "badge-green" },
};

async function loadMemories() {
  const vid = document.getElementById("memVaultSelect").value;
  const listEl = document.getElementById("memoriesList");
  const sumEl = document.getElementById("memSummaryBar");
  if (!vid) {
    listEl.innerHTML = `<div class="empty-state">请先在「知识库与目录纳管」添加知识库。</div>`;
    sumEl.innerHTML = "";
    return;
  }
  const kind = document.getElementById("memKindFilter").value;
  const status = document.getElementById("memStatusFilter").value;
  const q = document.getElementById("memQueryInput").value.trim();
  try {
    const data = await api(`/api/memory/list?vault_id=${encodeURIComponent(vid)}&kind=${encodeURIComponent(kind)}&status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`);
    window.__currentMemories = data.notes || [];
    const c = data.counts || {};
    sumEl.innerHTML = `
      <span class="badge badge-green">生效中 (active): ${c.active || 0}</span>
      <span class="badge badge-red">📌 生效勘误钉: ${c.errata || 0}</span>
      <span class="badge badge-purple">已归并 Wiki: ${c.consolidated || 0}</span>
      <span class="badge badge-gray">已归档遗忘: ${c.archived || 0}</span>
      <span class="mono" style="font-size:11.5px;color:var(--muted);margin-left:auto;">存储目录: ${esc(data.notes_dir)}</span>
    `;
    if (!data.notes.length) {
      listEl.innerHTML = `<div class="empty-state">
        <div style="font-weight:600;color:var(--text);margin-bottom:4px;">当前筛选条件下暂无记忆条目</div>
        <div style="margin-bottom:12px;">无论是 Agent 通过 MCP <code>wiki_remember</code> 沉淀的经验，还是在此手动添加的原件勘误钉，都会集中显示在这里。</div>
        <button class="btn-primary" onclick="openMemoryEditor()">＋ 新建第一条记忆 / 原件勘误钉</button>
      </div>`;
      return;
    }
    listEl.innerHTML = data.notes.map(n => {
      const km = KIND_META[n.kind] || { label: n.kind, cls: "badge-gray" };
      const stCls = n.status === "active" ? "badge-green" : (n.status === "consolidated" ? "badge-purple" : "badge-gray");
      const srcs = (n.related_sources || []).map(s => `<span class="chip">📄 ${esc(s)}</span>`).join(" ");
      const tags = (n.tags || []).map(t => `<span class="badge badge-gray">#${esc(t)}</span>`).join(" ");
      return `<div class="card" style="padding:15px 18px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <span class="badge ${km.cls}">${km.label}</span>
            <span class="badge ${stCls}">${esc(n.status)}</span>
            <span class="mono" style="color:var(--muted);">${esc(n.note_id)}</span>
            <span style="font-size:12px;color:var(--muted);">by ${esc(n.author)} · ${esc(n.created_at)}</span>
          </div>
          <div class="btn-row">
            <button class="btn-sm" onclick="openMemoryEditor('${esc(n.note_id)}')">编辑</button>
            ${n.status === "active"
              ? `<button class="btn-sm" onclick="setMemoryStatus('${esc(n.note_id)}','archived')">归档遗忘 (Forget)</button>`
              : `<button class="btn-sm" onclick="setMemoryStatus('${esc(n.note_id)}','active')">恢复生效</button>`
            }
            <button class="btn-sm btn-danger" onclick="deleteMemory('${esc(n.note_id)}')">彻底删除</button>
          </div>
        </div>
        <div style="white-space:pre-wrap;font-size:14px;margin:8px 0;">${esc(n.text)}</div>
        ${(srcs || tags) ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;">${srcs} ${tags}</div>` : ""}
      </div>`;
    }).join("");
  } catch (e) {
    toast("读取记忆失败: " + e.message, true);
  }
}

function openMemoryEditor(noteId = "") {
  const vid = document.getElementById("memVaultSelect").value;
  if (!vid) return toast("请先选择知识库", true);
  const vinfo = STATE.vaults.find(v => v.id === vid) || {};
  const existing = noteId ? (window.__currentMemories || []).find(x => x.note_id === noteId) : null;
  const sampleOpts = (vinfo.sample_sources || []).map(s => `<option value="${esc(s)}"></option>`).join("");

  openModal(existing ? `编辑记忆 · ${existing.note_id}` : `＋ 新建记忆 / 原件勘误钉 · ${vinfo.name}`, `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
      <div class="form-group">
        <label>记忆类型 *</label>
        <select id="edMemKind">
          <option value="errata" ${existing?.kind==="errata"?"selected":""}>📌 errata — 原件勘误钉（紧贴命中史料下方注入纠正）</option>
          <option value="insight" ${(!existing || existing?.kind==="insight")?"selected":""}>💡 insight — 实战排障洞察与经验</option>
          <option value="decision" ${existing?.kind==="decision"?"selected":""}>⚖️ decision — 团队架构/业务口径决议</option>
          <option value="glossary" ${existing?.kind==="glossary"?"selected":""}>📖 glossary — 领域缩写与黑话释义</option>
        </select>
      </div>
      <div class="form-group">
        <label>状态</label>
        <select id="edMemStatus" ${existing ? "" : "disabled"}>
          <option value="active" ${(!existing || existing?.status==="active")?"selected":""}>active（在线检索生效中）</option>
          <option value="consolidated" ${existing?.status==="consolidated"?"selected":""}>consolidated（已编译沉淀至 Wiki）</option>
          <option value="archived" ${existing?.status==="archived"?"selected":""}>archived（已归档遗忘，不注入上下文）</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label>记忆 / 勘误正文 *（内置自动密钥脱敏保护）</label>
      <textarea id="edMemText" placeholder="例如：【勘误】2026Q2 起受电弓边缘触发延迟上限已由 5ms 收紧为 2ms，旧版立项报告第 14 页参数已废止。">${esc(existing?.text || "")}</textarea>
    </div>
    <div class="form-group">
      <label>关联只读原件路径（可输入搜索下拉补全，多条用换行分隔；勘误钉必填以精准锚定原件并继承 ACL）</label>
      <div style="display:flex;gap:8px;margin-bottom:6px;">
        <input type="text" id="edMemSrcPicker" list="vaultSourcesDatalist" placeholder="输入文件名搜索知识库内原件，点右侧添加..." />
        <datalist id="vaultSourcesDatalist">${sampleOpts}</datalist>
        <button type="button" onclick="appendPickedSource()">＋ 添加关联原件</button>
      </div>
      <textarea id="edMemSources" class="mono" style="min-height:60px;" placeholder="${vinfo.sources_dir || '史料'}/...">${esc((existing?.related_sources || []).join("\n"))}</textarea>
    </div>
    <div class="form-group">
      <label>标签（逗号分隔）</label>
      <input type="text" id="edMemTags" value="${esc((existing?.tags || []).join(", "))}" placeholder="例如：受电弓, 阈值修订, 2026Q2" />
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" onclick="submitMemoryEditor('${esc(vid)}', '${esc(noteId)}')">💾 保存记忆条目</button>
    </div>
  `);
}

function appendPickedSource() {
  const picker = document.getElementById("edMemSrcPicker");
  const v = picker.value.trim();
  if (!v) return;
  const ta = document.getElementById("edMemSources");
  const cur = ta.value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!cur.includes(v)) cur.push(v);
  ta.value = cur.join("\n");
  picker.value = "";
}

async function submitMemoryEditor(vid, noteId) {
  const kind = document.getElementById("edMemKind").value;
  const status = document.getElementById("edMemStatus").value;
  const text = document.getElementById("edMemText").value.trim();
  const related_sources = document.getElementById("edMemSources").value.split("\n").map(s => s.trim()).filter(Boolean);
  const tags = document.getElementById("edMemTags").value.split(",").map(s => s.trim()).filter(Boolean);
  if (!text) return toast("记忆正文不能为空", true);
  try {
    if (noteId) {
      await api("/api/memory/update", { method: "POST", body: { vault_id: vid, note_id: noteId, kind, status, text, related_sources, tags } });
      toast("记忆条目已更新");
    } else {
      await api("/api/memory/create", { method: "POST", body: { vault_id: vid, kind, text, related_sources, tags } });
      toast("新记忆条目已写入并立即生效！");
    }
    closeModal();
    await loadMemories();
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

async function setMemoryStatus(noteId, status) {
  const vid = document.getElementById("memVaultSelect").value;
  await api("/api/memory/update", { method: "POST", body: { vault_id: vid, note_id: noteId, status } });
  toast(status === "archived" ? "已归档遗忘（不再注入检索上下文）" : "已恢复生效");
  await loadMemories();
  await refreshAll();
}

async function deleteMemory(noteId) {
  if (!confirm(`确定永久删除记忆文件 ${noteId}.md 吗？此操作不可撤销。`)) return;
  const vid = document.getElementById("memVaultSelect").value;
  await api("/api/memory/delete", { method: "POST", body: { vault_id: vid, note_id: noteId } });
  toast("记忆文件已删除");
  await loadMemories();
  await refreshAll();
}

function openImportMemoryModal() {
  const vid = document.getElementById("memVaultSelect").value;
  if (!vid) return toast("请先选择目标知识库", true);
  openModal("📂 批量导入外部 JSON 记忆", `
    <div class="form-group">
      <label>粘贴 JSON 数组（格式：<code>[{"text": "...", "kind": "insight|errata", "related_sources": [...], "tags": [...]}]</code>）</label>
      <textarea id="impMemJson" class="mono" style="min-height:160px;" placeholder='[\n  {\n    "kind": "errata",\n    "text": "示例勘误内容",\n    "related_sources": ["史料/示例文档.pdf"],\n    "tags": ["勘误"]\n  }\n]'></textarea>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;">
      <button onclick="closeModal()">取消</button>
      <button class="btn-primary" onclick="submitImportMemory('${esc(vid)}')">导入</button>
    </div>
  `);
}

async function submitImportMemory(vid) {
  try {
    const parsed = JSON.parse(document.getElementById("impMemJson").value);
    const r = await api("/api/memory/import", { method: "POST", body: { vault_id: vid, items: parsed } });
    closeModal();
    toast(`成功导入 ${r.imported_count} 条记忆`);
    await loadMemories();
    await refreshAll();
  } catch (e) {
    toast("JSON 解析或导入失败: " + e.message, true);
  }
}

async function openDoctorModal() {
  openModal("🩺 本机运行环境与部署自检 (wctx doctor)", `<div style="padding:12px;color:var(--muted);">正在检测 Python、CLI、poppler/PDF 与后台服务状态...</div>`);
  try {
    const rep = await api("/api/doctor");
    const checksHtml = (rep.checks || []).map(c => {
      const badgeCls = c.level === "ok" ? "badge-green" : (c.level === "warn" ? "badge-amber" : (c.level === "error" ? "badge-red" : "badge-gray"));
      const badgeTxt = c.level === "ok" ? "✓ 正常" : (c.level === "warn" ? "⚠ 建议补充" : (c.level === "error" ? "✕ 异常" : "ℹ 提示"));
      const fixHtml = c.fix
        ? `<div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
             <code class="mono" style="background:#f1f5f9;padding:3px 7px;border-radius:6px;">${esc(c.fix)}</code>
             <button class="btn-sm" onclick="copyText('${esc(c.fix)}','已复制修复命令')">复制命令</button>
           </div>`
        : "";
      return `<div style="padding:10px 12px;border:1px solid var(--border);border-radius:9px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <strong style="font-size:13px;">${esc(c.label)}</strong>
          <span class="badge ${badgeCls}">${badgeTxt}</span>
        </div>
        <div style="font-size:12.5px;color:var(--muted);margin-top:4px;">${esc(c.detail)}</div>
        ${fixHtml}
      </div>`;
    }).join("");

    const uiSvc = rep.services?.ui || {};
    const gwSvc = rep.services?.gateway || {};
    const cmdGw = "wctx service install --target gateway --host 127.0.0.1 --port 8090";
    const cmdUi = "wctx service install --target ui --host 127.0.0.1 --port 8765";
    document.getElementById("modalBody").innerHTML = `
      ${checksHtml}
      <div class="callout callout-blue" style="margin-top:12px;">
        <div style="font-weight:700;margin-bottom:6px;">🚀 常驻后台服务与生产网关部署</div>
        <div style="font-size:12.5px;line-height:1.65;">
          <div>• 控制中心守护状态 (<code>ui</code>)：<strong>${esc(uiSvc.detail || "unknown")}</strong></div>
          <div>• 独立网关守护状态 (<code>gateway</code>)：<strong>${esc(gwSvc.detail || "unknown")}</strong></div>
          <div style="margin-top:6px;">如需开机自启团队多库网关（纯 Token 鉴权、无管理界面），可执行：</div>
          <div style="margin-top:4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <code class="mono" style="background:#fff;padding:3px 8px;border-radius:6px;">${esc(cmdGw)}</code>
            <button class="btn-sm" onclick="copyText('${esc(cmdGw)}','已复制网关守护安装命令')">复制</button>
          </div>
          <div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <code class="mono" style="background:#fff;padding:3px 8px;border-radius:6px;">${esc(cmdUi)}</code>
            <button class="btn-sm" onclick="copyText('${esc(cmdUi)}','已复制控制中心守护安装命令')">复制</button>
          </div>
        </div>
      </div>
    `;
  } catch (e) {
    document.getElementById("modalBody").innerHTML = `<div class="callout callout-amber">自检失败: ${esc(e.message)}</div>`;
  }
}

refreshAll();
</script>
</body>
</html>
"""
