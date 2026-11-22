# wiki-context

**面向 AI Agent 的确定性上下文引擎（Deterministic Context Engine）** —— 离线编译式 Wiki + 表格感知 SQLite FTS5 + 零幻觉 MCP 知识网关。

[English](README.md) | 简体中文

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)
[![Dependencies: Zero](https://img.shields.io/badge/第三方依赖-零-brightgreen.svg)](pyproject.toml)
[![Protocol: MCP](https://img.shields.io/badge/MCP-2025--03--26-purple.svg)](docs/gateway-and-acl.md)

---

## 为什么做 `wiki-context`？

在人手一个强力 Agent（**Claude Code、Codex CLI、Cursor、DeepSeek Harness**）的今天，团队真正缺的不是再搭一套需要 8GB Docker 全家桶（向量数据库 + Postgres + 蹩脚网页聊天框）的传统 RAG 一体机，而是一位**毫秒级响应、字字可回溯原件的「AI 图书管理员」**：

- **写链路（离线编译沉淀）**：将散乱的 PDF、Word、PPT、Excel、CSV 与 Markdown 增量提取为结构化镜像，由离线 AI 编译沉淀跨文档综述页（`wiki/` / `知识库/`）并维护源文件 SHA-256 新鲜度指纹；
- **读链路（零大模型极速供料）**：网关本身 **0 Token 消耗**，单次 `kb_context` 调用（**~3–35 ms**）一次性组装「二级 Wiki 导读 + 一跳溯源的一手原件段落（自动置顶表头）+ 0 命中缺词防幻觉预警」，喂给你手头最强的 Agent。

> **清晰边界定位**：`wiki-context` 专注提供**只读背景认知、技术规范、选型手册、报价红线与历史复盘（Context）**；实时库存、订单流转、多级 BOM 展开等事务计算请交给并联的 SQL / ERP MCP（State & Action），由 Agent 先查 `wiki-context` 懂规矩，再调业务系统做动作。

---

## 核心特性

1. **零第三方依赖，开箱即跑**：纯 Python 3.10+ 标准库实现（内置 DOCX/PPTX/XLSX/CSV 原生提取器 + SQLite FTS5 全文引擎 + Streamable HTTP 服务），无需安装 Milvus、ES 或任何重型框架。
2. **原生中英双语目录自适应**：
   - 英文布局（`--layout en`）：`sources/`、`mirrors/`、`wiki/`、`index.md`、`log.md`
   - 中文 Obsidian 布局（`--layout zh`）：`史料/`、`史料_md/`、`知识库/`、`目录.md`、`日志.md`（对存量中文 Obsidian LLM Wiki 库零配置自动识别）。
3. **长表格不丢表头 + 跨行精准定位**：按段落与表格整行边界切块（`≤400` 行），滑窗截取深层数据行时自动置顶 `table_header`，并对表头已有列名自动降权防锚定偏移。
4. **口语同义词热重载 + 防幻觉双护栏**：`synonyms.json` 支持口语俗称向标准术语单向收敛（保存即热生效）；提问核心词零命中时自动发出 `⚠️ 缺词预警` 并声明未镜像文件边界。
5. **不可变快照热切换 + 单网关多库与目录级 ACL**：按代发布 `gen-<id>/bm25.sqlite` 并通过 `?mode=ro&immutable=1` 多线程无锁只读查询；单网关支持挂载多个知识库（`/mcp/<vault_id>`）及热重载 Token / `PrefixACLPolicy`。

---

## 60 秒快速开始

```bash
git clone https://github.com/your-org/wiki-context.git
cd wiki-context
pip install -e .

# 1. 运行内置公开评测集（对比传统 600 字切片 vs ContextPack 单次组装）
python3 examples/benchmark/run_benchmark.py

# 2. 初始化一个中文（或英文）三层知识库
wctx init ./我的知识库 --name "研发技术库" --layout zh

# 3. 放入 PDF / Word / Excel / CSV / MD 到 史料/，执行增量提取与建索引
cp ~/Documents/*.docx ./我的知识库/史料/
wctx sync --vault ./我的知识库

# 4. 终端直接测试单次证据包组装
wctx context --vault ./我的知识库 -q "系统整体架构与限流规则是什么？"
```

---

## 接入 AI Agent（MCP 配置）

### 方式一：本机单用户 Stdio 接入（Claude Code / Codex / Cursor / DSH）

**Claude Code (`~/.claude.json`) / Cursor (`~/.cursor/mcp.json`)**：

```json
{
  "mcpServers": {
    "kb": {
      "command": "wctx",
      "args": ["serve", "--vault", "/path/to/我的知识库"],
      "env": { "KB_USER": "dev" }
    }
  }
}
```

**Codex CLI (`~/.codex/config.toml`)**：

```toml
[mcp_servers.kb]
command = "wctx"
args = ["serve", "--vault", "/path/to/我的知识库"]
env = { KB_USER = "dev" }
```

### 方式二：团队多人多库 HTTP 网关（`wctx gateway`）

```bash
WCTX_TOKENS="alice:secret_1,bob:secret_2" \
wctx gateway \
  --vault tech=./研发技术库 \
  --vault rules=./制度规范库 \
  --host 127.0.0.1 --port 8090
```

- 团队成员在各自客户端直接配置 Streamable HTTP 地址 `http://<host>:8090/mcp/tech` 与 `Bearer <token>` 即可并发共享，零宿主机权限暴露。
- 仅支持本地 stdio 的桌面客户端可通过 `wctx bridge --url http://<host>:8090/mcp/tech --token <token>` 零依赖转发接入。

---

## 离线 Wiki 编译整理（两种方式任选）

1. **人机交互式精修**：在知识库目录打开 Claude Code / Codex / DSH，直接说「新增了文档，整理一下」，Agent 会按自动生成的 `AGENTS.md` 完成 `sync → 更新知识页与目录 → freshness snapshot → lint → index`。
2. **命令行批量自动编译（`wctx compile`）**：
   ```bash
   # 先预览待编译的新镜像与已过期（stale）的 Wiki 页
   wctx compile --vault ./我的知识库 --dry-run

   # 调用任意 OpenAI / DeepSeek / Anthropic 兼容接口自动生成并刷新 FTS5
   WCTX_API_KEY="sk-..." \
   WCTX_BASE_URL="https://api.deepseek.com/v1" \
   WCTX_MODEL="deepseek-chat" \
   wctx compile --vault ./我的知识库
   ```

---

## 详细文档

- [核心架构与五步检索供料原理](docs/architecture.md)
- [知识库目录规范、同义词与新鲜度机制](docs/vault-spec.md)
- [多库 HTTP 网关、鉴权与目录级权限（ACL）指南](docs/gateway-and-acl.md)

## 开源协议

[MIT License](LICENSE)
