# wiki-context

**The Deterministic Context Engine for AI Agents** — Offline Compiled LLM Wiki + Table-Aware SQLite FTS5 + Zero-Hallucination MCP Gateway.

English | [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)
[![Dependencies: Zero](https://img.shields.io/badge/Dependencies-Zero-brightgreen.svg)](pyproject.toml)
[![Protocol: MCP](https://img.shields.io/badge/MCP-2025--03--26-purple.svg)](docs/gateway-and-acl.md)

---

## Why `wiki-context`? (Core Value)

Modern developers and teams already work inside capable AI agents (**Claude Code, Codex CLI, Cursor, DeepSeek Harness**). They do **not** need another bloated RAG chat webapp backed by an 8 GB Docker stack (vector DB + embedding service + reranker + Postgres + rigid Web UI).

What agents actually lack is a **millisecond-latency, deterministic, citation-anchored librarian**:
- **Decoupled Write vs. Read Paths**: Heavy comprehension happens **offline** (native document extraction + AI-compiled cross-document Wiki synthesis + SHA-256 freshness tracking). Online retrieval uses **zero runtime LLM calls**, **zero external APIs**, and **zero third-party Python packages**.
- **One Call, Complete Context (`~2–35 ms`)**: A single `kb_context` tool call deterministically assembles **high-level Wiki synthesis (24% budget)** + **1-hop provenance-verified primary source excerpts with pinned table headers (76% budget)** + **explicit missing-concept hallucination guardrails**.
- **Clear Architectural Boundary (`Context` vs. `State & Action`)**: `wiki-context` serves read-only ground truth — RFCs, ADRs, product specs, pricing tiers, SOPs, and postmortems. Pair it alongside transactional MCP servers (SQL / ERP / GitHub / Jira MCP) so your agent reads the rules from `wiki-context` in 3 ms before querying live state or executing mutations.

```text
Raw Sources (`sources/` or `史料/` — PDF, DOCX, PPTX, XLSX, CSV, MD; Strictly Read-Only)
  │  1. wctx sync  (Zero-dep native extraction + pluggable CLI OCR + SHA-256 manifest)
  ▼
Derived Mirrors (`mirrors/` or `史料_md/`)    Compiled Wiki (`wiki/` or `知识库/`)
  │  (Page/line/sheet locators)                │  (Agent/Obsidian curated or `wctx compile`
  │                                            │   + SHA-256 `source_hashes` + `wctx lint`)
  └────────────────────┬───────────────────────┘
                       │  2. wctx index (Table-aware chunks + dup folding + freshness check)
                       ▼
        Immutable `gen-<id>/bm25.sqlite`  ◄── Atomic `current.json` swap (lock-free `?immutable=1`)
                       │
                       │  3. One-shot `kb_context` (~2–35 ms, 0 runtime LLM tokens)
                       ▼
     Claude Code / Codex / Cursor / DSH / Custom Agents (Stdio MCP, Streamable HTTP `/mcp`, REST)
```

---

## `wiki-context` vs. Traditional RAG vs. Pure LLM Wiki

Today's agent knowledge stacks generally fall into two camps — **Traditional Chunk/Vector RAG** and **Pure Agent-Browsed LLM Wiki** (e.g., the Karpathy-style Markdown Wiki pattern). Both break down on real engineering and enterprise workloads, for opposite reasons:

### 1. Why Traditional Chunk + Vector RAG Falls Short
1. **Embedding Blur on Exact Identifiers**: Dense embeddings smooth over exact technical codes (`RFC-042`, `TB/T-2075`, `HTTP-429`, SKUs, part numbers, version flags), retrieving semantically "similar" chunks for the wrong component or tier.
2. **Headerless Table Slicing**: Fixed-size chunkers (e.g., 500–800 chars) slice multi-hundred-row CSV, Excel, and Markdown tables mid-sheet. Row 80 matches the query, while Row 1's column headers are left behind — leaving the LLM to guess what `[12000, 36000, 64]` mean.
3. **Top-K Myopia & Mega-Manual IDF Flattening**: Isolated chunks lack cross-document architectural evolution ("which ADR superseded RFC-012 and why?"). Meanwhile, a single 300-page manual can flood chunk-level term frequencies and crowd out concise specs.
4. **Nearest-Neighbor Hallucination & Infra Bloat**: Even when a queried concept (`Kafka`) does not exist anywhere in the vault, vector search still returns the top-K closest neighbors, nudging the agent to fabricate answers — all while requiring external vector DBs and embedding APIs.

### 2. Why Pure LLM Wiki (Agent-Browsed Markdown) Hits Production Bottlenecks
The "LLM Wiki" pattern replaces vector DBs with LLM-maintained Markdown synthesis pages (`wiki/*.md` + `index.md`) that agents browse via `read`/`grep`. It nails global narrative, but introduces four production pain points:
1. **Multi-Turn Latency & 10× Token Burn**: Answering one question often takes **3–6 tool rounds** (`read index.md` → open 2–3 Wiki pages → `grep` raw files for missing figures), burning **15–40 seconds** and **30k–40k context tokens** before the agent even starts solving your task.
2. **Lossy Compression Trap**: Wiki summaries excel at structure and trade-offs, but inevitably drop long-tail clauses and individual rows of a 500-row quota or BOM spreadsheet. Reading only the Wiki loses exact numbers; grepping only raw files loses the big picture.
3. **Silent Staleness (Wiki Drift)**: When an underlying Word spec or CSV table updates a limit from `5,000` to `12,000`, prose Wiki pages silently go stale and actively mislead the agent unless every cited source hash is cryptographically tracked.
4. **Confidentiality Downgrade & No Hard Guardrails**: Exposing a raw Wiki folder to multi-user agents lacks provenance-aware RBAC — a user blocked from `sources/finance/` can inadvertently read confidential figures summarized inside a high-level `wiki/Overview.md` page.

### 3. How `wiki-context` Synthesizes Both Worlds
`wiki-context` treats knowledge like a **compiler + deterministic linker**:
- **Keeps the Soul of LLM Wiki (Plain Markdown + Global Synthesis)**: Human-readable `wiki/*.md` pages with `[[wikilinks]]` (editable in Obsidian, Git-diffable), upgraded with **SHA-256 `source_hashes`** in YAML frontmatter. If any cited source changes by 1 byte, the indexer automatically flags the Wiki page `stale` (`0.5×` score demotion + explicit `⚠️ stale` warning badge in `<kb_context>`).
- **Exceeds RAG Precision in One Deterministic Call**:
  - **Code-Preserving Bilingual FTS5 + Per-Source IDF**: Keeps intact identifiers (`RFC-042`, `350mm`, `v2.4`), segments CJK bigrams without external NLP deps, and computes IDF over **distinct source documents (`df_sources`)** rather than chunks.
  - **Controlled 1-Hop Wiki Provenance**: High-confidence Wiki hits contribute synthesized guidance **and** boost (`1.25×`) their cited primary documents — *only* when the primary chunk independently matches a high-IDF query concept, preventing broad overview pages from dragging in noise.
  - **Header-Discounted Table Windowing**: Oversized table sections are sliced with `table_header` **pinned automatically at the top of every window**, while discounting header tokens during window scoring so deep target rows are never missed.
  - **Zero-Hit Anti-Hallucination & Provenance-Inherited ACL**: Unmatched core query terms trigger a mandatory `⚠️ Missing-Concept Alert`. Under `PrefixACLPolicy`, a Wiki page is visible *if and only if* the caller is authorized to read **every** primary document listed in its `sources:`.

### Side-by-Side Comparison

| Dimension | Traditional Vector RAG | Pure LLM Wiki (Agent-Browsed) | **`wiki-context`** |
| :--- | :--- | :--- | :--- |
| **Core Paradigm** | Chunking + Dense Embeddings + Reranker | Markdown Wiki + Multi-turn Agent `read`/`grep` | **Offline Compiled Wiki + Deterministic Dual-Layer FTS5** |
| **Exact Codes & IDs (`RFC-042`, `HTTP-429`)** | Weak (semantic embedding blur) | Exact only if agent guesses right `grep` | **Exact** (code-preserving FTS5 + per-source `df_sources` IDF) |
| **Deep Tables (CSV / XLSX / MD)** | Loses column headers mid-table | Wiki summaries omit long-tail rows | **Pinned `table_header`** on every window + header-discounted slicing |
| **Cross-Document Synthesis** | Weak (isolated top-K fragments) | **Strong** (curated synthesis pages) | **Strong** (24% Wiki synthesis + 76% 1-hop primary ground truth) |
| **Colloquial -> Domain Jargon** | Fuzzy vector proximity (black-box) | Depends on agent guessing keywords | **Hot-reloaded `synonyms.json`** (deterministic 1-way concept merge) |
| **Stale Summary Detection** | N/A (no synthesis layer) | Manual discipline (silent drift risk) | **Automatic SHA-256 `source_hashes`** (`stale` badge + `0.5×` demotion) |
| **Unanswerable Query Defense** | High hallucination risk (feeds nearest chunks) | Depends on agent stopping on time | **Explicit `⚠️ Missing-Concept Alert`** + unmirrored boundary notes |
| **Online Latency & Token Cost** | 1–5 s + Embedding/Rerank API calls | 15–40 s (3–6 tool calls, ~30k–40k tokens) | **1 tool call, ~2–35 ms, 2.6k–4.8k tokens, 0 LLM calls** |
| **Access Control (RBAC)** | Chunk metadata filter (often leaky) | File-level OS permissions only | **Hot-reloaded `PrefixACLPolicy` + 100% Wiki provenance inheritance** |
| **Runtime Footprint** | Heavy (Milvus/Qdrant + Docker + SDKs) | Plain folder | **Zero dependencies** (Python 3.10+ stdlib + immutable SQLite FTS5) |

---

## What One `kb_context` Call Actually Returns

### Case 1: Deep CSV Table Lookup + Wiki Provenance (`~3 ms`)

Query: *"What are the Sustained_RPS, Burst_Bucket, and Max_Payload_MB limits for the Enterprise-Dedicated tier?"*

```markdown
<kb_context vault="AuroraEdge Engineering KB" route="fact" generation="gen-20261122T133553Z-1486c582">
## 1. Compiled Wiki Synthesis (Verify exact figures & clauses against Primary Sources below)
### [W1] wiki/Gateway-Rate-Limiting-and-SLOs.md § Key Facts (L20-L24) [freshness:fresh]
- **Token Bucket Enforcement**: Edge nodes enforce per-tenant token buckets using local atomic counters synced every `250ms`...
- **Enterprise Dedicated Tier**: Exact per-tier RPS and burst ceilings are defined in `sources/specs/tier-quotas.csv`.

## 2. Primary Source Evidence (Authoritative Ground Truth)
### [S1] sources/specs/tier-quotas.csv | tier-quotas | L24-L47 (section L8-L48) [via Wiki provenance]
| Tier_Code | Sustained_RPS | Burst_Bucket | Max_Payload_MB | Support_SLA |
| --- | --- | --- | --- | --- |
...
| Scale-05 | 5000 | 12500 | 25 | 24x7-Priority |
| Enterprise-Dedicated | 12000 | 36000 | 64 | 24x7-15min-Page |

## 3. Gap Diagnostics & Grounding Guardrails (Mandatory)
- Grounding Rule: Every claim must be directly supported by [S1]~[Sn] above with explicit source citations...
</kb_context>
```
Notice how `[S1]` slices lines `L24–L47` deep inside `tier-quotas.csv` while **pinning the CSV header row automatically** at the top, boosted via `[W1]`'s verified provenance.

### Case 2: Unanswerable Question Guardrail (`~0.2 ms`)

Query: *"What is the Kafka partition retention period for billing events?"* (when the vault uses Raft/WAL, not Kafka)

```markdown
<kb_context vault="AuroraEdge Engineering KB" route="fact" generation="gen-20261122T133553Z-1486c582">
## 2. Primary Source Evidence (Authoritative Ground Truth)
*(No directly matching primary source chunks found)*

## 3. Gap Diagnostics & Grounding Guardrails (Mandatory)
- ⚠️ Missing-Concept Alert: Key query concept(s) 'Kafka', 'partition', 'retention', 'period', 'billing', 'events' had 0 hits across all retrieved chunks! This likely indicates a knowledge gap in the vault — do NOT hallucinate figures or clauses from pretraining.
</kb_context>
```

---

## Key Engineering Highlights

- **Zero Runtime & Build-Time Dependencies**: Pure Python 3.10+ standard library — including a self-contained PEP 517/660 wheel backend, native XML/ZIP extractors for `.docx`, `.pptx`, `.xlsx`, `.csv`/`.tsv`, `.md`/`.txt`, `pdftotext` integration for `.pdf`, and a pluggable `WCTX_OCR_CMD` hook for scanned pages.
- **Native English & Chinese Vault Layouts**:
  - English (`--layout en`): `sources/`, `mirrors/`, `wiki/`, `index.md`, `log.md`
  - Chinese Obsidian (`--layout zh`): `史料/`, `史料_md/`, `知识库/`, `目录.md`, `日志.md` (auto-detected with zero config on existing Obsidian LLM Wiki vaults).
- **Lock-Free Concurrency & Atomic Hot Reload**: Readers open versioned `gen-<id>/bm25.sqlite` snapshots via `?mode=ro&immutable=1` on thread-local connections. Publishing a new index atomically swaps `current.json` and drains old snapshots over a 30-second grace window with zero dropped requests.
- **Defense-in-Depth Security**: Automatic credential scrubbing (`PRIVATE KEY`, `sk-...`, `AKIA...`, `Bearer`, `Cookie`) before snippets leave the server, `O_NOFOLLOW` symlink-safe file reads, sensitive-vault guardrails (`sensitive: true`), and multi-vault Streamable HTTP RBAC.

---

## 60-Second Quickstart

### 1. Install

```bash
git clone https://github.com/Minokun/wiki-context.git
cd wiki-context
pip install -e .
```

*(Zero third-party packages are downloaded or installed. Optional: install `poppler` if you need native `pdftotext` PDF extraction.)*

### 2. Run the Reproducible Benchmark

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

### 3. Initialize Your Own Vault

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

`wiki-context` exposes **6 focused, read-only MCP tools** (with dynamic `[vault_name]` disambiguation when multiple vaults are mounted simultaneously):

| MCP Tool | Purpose |
| :--- | :--- |
| **`kb_context`** | **Preferred default (answers 95% of queries in 1 call)**: assembles Wiki synthesis + primary evidence + table headers + gap alerts |
| `kb_search` | Fine-grained BM25/FTS5 search with optional `section` / `page` / `wiki` unit filtering |
| `kb_read` | Sandboxed exact line-range (`<=400` lines) or `page_num` reader over mirrors, Wiki pages, and `index.md` |
| `kb_route` | Classifies query complexity (`fact` 2.6k budget vs. `complex` 4.8k budget) |
| `kb_list` | Lists indexed documents and extraction methods with path filtering |
| `kb_status` | Reports active index generation, document/chunk counts, skipped files, and coverage boundaries |

### Option A: Local Stdio MCP (Single User)

**Claude Code (`~/.claude.json`) / Cursor (`~/.cursor/mcp.json`) / DeepSeek Harness**:

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

Host multiple vaults behind a single lightweight process with SHA-256 Bearer tokens and hot-reloaded directory-prefix RBAC (`PrefixACLPolicy`):

```bash
WCTX_TOKENS="alice:secret_tok_1,bob:secret_tok_2" \
wctx gateway \
  --vault eng=./eng-kb \
  --vault policy=./policy-kb \
  --host 127.0.0.1 --port 8090
```

- **Streamable HTTP MCP (`2025-03-26`)**: `POST http://127.0.0.1:8090/mcp` (default) or `POST http://127.0.0.1:8090/mcp/eng`, `/mcp/policy`
- **REST JSON API**: `POST /v1/context`, `POST /v1/read`, `GET /v1/vaults`, `GET /v1/whoami`, `GET /healthz`
- **Stdio-to-HTTP Bridge**: Clients that only speak local stdio can connect via `wctx bridge --url https://kb.example.com/mcp/eng --token <TOKEN>`.

---

## Offline Wiki Compilation Workflows

You can build and maintain `wiki/*.md` pages in either of two ways (or mix both):

1. **Interactive Agent + Obsidian Curation**: Open Claude Code / Codex / DSH inside your vault and ask it to organize new documents. Guided by the scaffolded `AGENTS.md`, your agent runs `wctx sync` -> updates `wiki/*.md` & `index.md` -> snapshots `source_hashes` -> runs `wctx lint` -> rebuilds FTS5.
2. **Batch CLI Compiler (`wctx compile`)**: Run the built-in zero-SDK compiler against any OpenAI-compatible (`/v1/chat/completions`, including DeepSeek / local vLLM) or Anthropic (`/v1/messages`) endpoint:

```bash
# Preview uncompiled mirrors and stale Wiki pages without calling an LLM
wctx compile --vault ./my-kb --dry-run

# Compile new pages, refresh stale pages, snapshot SHA-256 hashes, lint, and hot-swap FTS5
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
| `wctx search --vault <V> -q "<Q>"` | Run raw BM25/FTS5 search or `--route` complexity classification |
| `wctx compile --vault <V> [--dry-run]` | Compile unindexed mirrors & refresh `stale` Wiki pages via LLM API |
| `wctx freshness --vault <V> [check\|snapshot]` | Detect `stale` Wiki pages when underlying primary sources change |
| `wctx lint --vault <V>` | Verify broken `[[wikilinks]]`, missing sources, and catalog drift |
| `wctx serve --vault <V> [--test]` | Run local stdio MCP server (6 tools, secret-scrubbed) |
| `wctx gateway --vault id=path [--test]` | Run multi-user, multi-vault Streamable HTTP MCP + REST gateway |
| `wctx bridge --url <URL> --token <T>` | Bridge local stdio MCP clients to a remote HTTP gateway |

---

## Documentation

- [Architecture & 5-Step Retrieval Design](docs/architecture.md)
- [Vault Layout, Synonyms & SHA-256 Freshness Spec](docs/vault-spec.md)
- [Multi-Vault Gateway, Authentication & Prefix ACL](docs/gateway-and-acl.md)

## License

[MIT](LICENSE)
