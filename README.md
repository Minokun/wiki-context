# wiki-context

**The Deterministic Context Engine for AI Agents** — Compiled LLM Wiki + Table-Aware SQLite FTS5 + Zero-Hallucination MCP Gateway.

English | [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)
[![Dependencies: Zero](https://img.shields.io/badge/Dependencies-Zero-brightgreen.svg)](pyproject.toml)
[![Protocol: MCP](https://img.shields.io/badge/MCP-2025--03--26-purple.svg)](docs/gateway-and-acl.md)

---

## Why `wiki-context`?

Modern developers and teams already work inside capable AI agents (**Claude Code, Codex CLI, Cursor, DeepSeek Harness**). They do **not** need another bloated RAG chat webapp with an 8 GB Docker stack (vector DB + Postgres + Redis + fixed LLM wrapper).

They need a **fast, deterministic, citation-anchored librarian** that turns messy PDFs, Word specs, Excel/CSV tables, and Markdown notes into verifiable context in **< 35 ms** — with **zero runtime LLM calls** and **zero external Python dependencies**.

```text
Raw Sources (`sources/` or `史料/` — PDF, DOCX, PPTX, XLSX, CSV, MD)
  │  1. wctx sync  (Zero-dep native extraction + pluggable OCR + SHA-256 tracking)
  ▼
Derived Mirrors (`mirrors/` or `史料_md/`)    Compiled Wiki (`wiki/` or `知识库/`)
  │                                            │  (Human/Agent curated or `wctx compile`)
  └────────────────────┬───────────────────────┘
                       │  2. wctx index (Table-aware chunks + dup folding + source_hashes check)
                       ▼
        Immutable `gen-<id>/bm25.sqlite`  ◄── Atomic `current.json` hot-swap (zero lock contention)
                       │
                       │  3. One-shot `kb_context` (~3–35 ms, 0 LLM tokens)
                       ▼
     Claude Code / Codex / Cursor / DSH / Custom Agents (via Stdio MCP, Streamable HTTP `/mcp`, or REST)
```

### How It Compares

| Capability | Naive Vector RAG | Pure Agent-Browsed LLM Wiki | **`wiki-context`** |
| :--- | :--- | :--- | :--- |
| **Exact codes & IDs (`RFC-042`, `HTTP-429`, `v2.4`)** | Weak (embedding blur) | Multi-turn grep needed | **Exact** (code-preserving FTS5 + per-source IDF) |
| **100-row Markdown / Excel / CSV tables** | Loses table headers mid-chunk | Summaries drop row figures | **Pinned `table_header`** on every sliced window |
| **Cross-document synthesis & history** | Blind beyond top-K chunks | **Strong** | **Strong** (24% Wiki synthesis + 76% 1-hop primary evidence) |
| **Unanswerable query hallucination** | High (feeds nearest neighbors) | Depends on agent discipline | **Explicit `Missing-Concept Alert`** + coverage boundary notes |
| **Latency & online token overhead** | 1–5 s + embedding/rerank API | 15–40 s (3–5 tool rounds, ~35k tok) | **1 tool call, ~3–35 ms, 2.6k–4.8k tokens** |
| **Infrastructure footprint** | Heavy (Milvus/Qdrant + Docker) | Local folder only | **Zero dependencies** (Python stdlib + single-file SQLite FTS5) |

> **Scope Philosophy (Context vs. State/Action):** `wiki-context` is purpose-built for **background knowledge, specs, policies, architecture docs, and postmortems**. Pair it alongside transactional MCP servers (SQL / ERP / Jira / GitHub MCP) so your agent reads the rules from `wiki-context` before querying live state or executing actions.

---

## 60-Second Quickstart

### 1. Install

```bash
git clone https://github.com/your-org/wiki-context.git
cd wiki-context
pip install -e .
```

*(Zero third-party packages are installed. Install `poppler` if you want native `pdftotext` PDF extraction.)*

### 2. Run the Included Demo Vault & Benchmark

```bash
python3 examples/benchmark/run_benchmark.py
```

Output on `examples/demo-vault`:

```text
ID   Route    NaiveBM25@3   ContextPack   Latency(ms)  Question
----------------------------------------------------------------------------------------
Q1   fact     MISS          PASS          2.94         What are the Sustained_RPS, Burst_Bucket, and
Q2   fact     PASS          PASS          3.54         When requests get throttled at the edge, how o
Q3   fact     PASS          PASS          1.87         How fast is container warmup p95 latency and h
Q4   complex  PASS          PASS          2.13         Compare Gateway circuit-breaker trip threshold
Q5   fact     (halluc.risk) GUARD_OK      0.20         What is the Kafka partition retention period f [Unanswerable]
----------------------------------------------------------------------------------------
Strict Complete-Evidence Recall : Naive 600c BM25 = 3/4 (75.0%)  vs  ContextPack = 4/4 (100.0%)
Unanswerable Gap Guardrail      : 1/1 (100.0%)
ContextPack Median Latency      : 2.13 ms
```

### 3. Create Your Own Vault

```bash
# English layout (`sources/`, `mirrors/`, `wiki/`, `index.md`)
wctx init ./my-kb --name "Platform Engineering KB"

# Or Chinese Obsidian layout (`史料/`, `史料_md/`, `知识库/`, `目录.md`)
wctx init ./公司知识库 --name "研发知识库" --layout zh

# Drop PDFs, DOCX, XLSX, PPTX, CSV, or Markdown into `sources/`, then sync:
cp ~/Specs/*.docx ./my-kb/sources/
wctx sync --vault ./my-kb

# Test one-shot context assembly in terminal:
wctx context --vault ./my-kb -q "What are our API retry and rate-limit rules?"
```

---

## Connect to Your AI Agent (MCP)

### Option A: Local Stdio MCP (Single User)

**Claude Code (`~/.claude.json`) / Cursor (`~/.cursor/mcp.json`)**:

```json
{
  "mcpServers": {
    "kb": {
      "command": "wctx",
      "args": ["serve", "--vault", "/absolute/path/to/my-kb"],
      "env": { "KB_USER": "alice" }
    }
  }
}
```

**Codex CLI (`~/.codex/config.toml`)**:

```toml
[mcp_servers.kb]
command = "wctx"
args = ["serve", "--vault", "/absolute/path/to/my-kb"]
env = { KB_USER = "alice" }
```

### Option B: Multi-User, Multi-Vault Remote HTTP Gateway (Teams)

Host multiple vaults behind one lightweight server with SHA-256 Bearer tokens and directory-prefix RBAC (`PrefixACLPolicy`):

```bash
WCTX_TOKENS="alice:secret_tok_1,bob:secret_tok_2" \
wctx gateway \
  --vault eng=./eng-kb \
  --vault policy=./policy-kb \
  --host 127.0.0.1 --port 8090
```

- Default Streamable HTTP MCP endpoint: `POST http://127.0.0.1:8090/mcp`
- Per-vault Streamable HTTP MCP endpoints: `POST http://127.0.0.1:8090/mcp/eng`, `POST http://127.0.0.1:8090/mcp/policy`
- REST API: `POST /v1/context` (`{"vault": "eng", "question": "..."}`), `POST /v1/read`, `GET /v1/vaults`, `GET /healthz`
- Desktop clients that only speak stdio can bridge to the remote gateway via `wctx bridge --url https://kb.example.com/mcp/eng --token <TOKEN>`.

---

## Offline AI Wiki Compilation (`wctx compile`)

You can curate `wiki/*.md` pages interactively inside your favorite Coding Agent + Obsidian (guided by the generated `AGENTS.md`), **or** run the built-in zero-SDK batch compiler against any OpenAI-compatible or Anthropic endpoint:

```bash
# Preview uncompiled mirrors and stale Wiki pages without calling an LLM
wctx compile --vault ./my-kb --dry-run

# Compile new pages, snapshot SHA-256 source_hashes, lint, and hot-swap FTS5
WCTX_API_KEY="sk-..." \
WCTX_BASE_URL="https://api.openai.com/v1" \
WCTX_MODEL="gpt-4o-mini" \
wctx compile --vault ./my-kb
```

---

## CLI Reference (`wctx`)

| Command | Description |
| :--- | :--- |
| `wctx init <path> [--layout en\|zh]` | Initialize a three-layer vault (`sources/`, `mirrors/`, `wiki/`) |
| `wctx sync --vault <V> [--list]` | Incrementally extract changed documents & rebuild FTS5 |
| `wctx index --vault <V> [--check --deep]` | Build or verify immutable `gen-<id>/bm25.sqlite` snapshot |
| `wctx context --vault <V> -q "<Q>"` | Assemble a single-call citation-anchored `<kb_context>` pack |
| `wctx search --vault <V> -q "<Q>"` | Run raw BM25/FTS5 search or `--route` complexity check |
| `wctx compile --vault <V> [--dry-run]` | Compile unindexed mirrors & refresh `stale` Wiki pages via LLM API |
| `wctx freshness --vault <V> [check\|snapshot]` | Detect `stale` Wiki pages when underlying sources change |
| `wctx lint --vault <V>` | Check broken `[[wikilinks]]`, missing sources, and catalog drift |
| `wctx serve --vault <V> [--test]` | Run local stdio MCP server (6 tools, secret-scrubbed) |
| `wctx gateway --vault id=path [--test]` | Run multi-user, multi-vault HTTP MCP + REST gateway |
| `wctx bridge --url <URL> --token <T>` | Bridge local stdio MCP hosts to a remote HTTP gateway |

---

## Documentation

- [Architecture & Retrieval Design](docs/architecture.md)
- [Vault Layout, Synonyms & Freshness Spec](docs/vault-spec.md)
- [Multi-Vault Gateway, Authentication & Prefix ACL](docs/gateway-and-acl.md)

## License

[MIT](LICENSE)
