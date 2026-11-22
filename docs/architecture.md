# Architecture & Retrieval Design

`wiki-context` decouples **offline knowledge comprehension** from **online deterministic retrieval**, combining the synthesis strengths of an LLM Wiki with the literal precision of table-aware BM25/FTS5.

---

## 1. Three-Layer Storage Layout

1. **Layer 1 — Raw Sources (`sources/` or `史料/`)**: Strictly read-only ground truth (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.csv`, `.md`). Never mutated by the engine or agents.
2. **Layer 2 — Derived Mirrors & Immutable Index (`mirrors/` or `史料_md/`)**:
   - `<rel_path>.md` + `<rel_path>.json` sidecar storing page/slide/sheet locators and visual-page warnings.
   - `.index/gen-<timestamp>-<hash>/bm25.sqlite`: Versioned immutable SQLite FTS5 database containing `fts_units` (primary chunks) and `fts_wiki` (compiled Wiki sections).
   - `.index/current.json`: Atomic pointer to the active generation.
3. **Layer 3 — Compiled Wiki (`wiki/` or `知识库/`)**: Flat Markdown synthesis pages linked via `[[wikilinks]]`, declaring `sources:` and SHA-256 `source_hashes:` in YAML frontmatter.

---

## 2. Bilingual & Code-Preserving FTS5 Indexing

Default SQLite FTS5 tokenizers split technical identifiers (`RFC-042`, `TB/T-2075`, `HTTP-429`) on punctuation and cannot segment unspaced CJK text.

`wiki-context` solves both without external NLP libraries (`search.py`):
- **Pre-tokenization + `tokenize='ascii'`**: Alphanumeric codes are kept intact and safely escaped (`-` -> `_d_`, `/` -> `_s_`, `.` -> `_p_`) alongside component sub-tokens grouped into a single concept equivalence set; CJK text is segmented into overlapping 2-character bigrams with trailing modal-particle stripping.
- **Two-Stage Per-Source BM25 Reranking**:
  1. Stage 1 queries SQLite C-level `fts_units` (`bm25(fts_units, 3.0, 1.0)`) and `fts_wiki` to fetch candidate rowids in ~1 ms.
  2. Stage 2 decompresses compact term-frequency maps (`tf_z`) and scores candidates using **document-level DF (`vocab.df_sources`)** so a single 300-page manual cannot flatten term IDF, applying a `COVERAGE_ALPHA` multiplier for multi-concept coverage.

---

## 3. Five-Step `ContextPackEngine` (`context.py`)

When an agent calls `kb_context(question)`, the engine executes five deterministic steps in ~3–35 ms:

1. **Complexity Routing & One-Way Synonym Alignment**: Classifies the question into `fact` (2,600 tokens) or `complex` (4,800 tokens) and injects canonical domain terms from hot-reloaded `synonyms.json` into the triggering concept group without diluting coverage ratios.
2. **Parallel Dual-Layer Retrieval & Multi-Facet Rescue**: Queries `fts_wiki` and `fts_units` simultaneously. If a query spans multiple distinct entities (e.g. comparing System A vs. System B), under-represented facets are rescued via targeted sub-queries.
3. **Controlled 1-Hop Wiki Provenance**: Extracts inline and frontmatter `sources:` from high-confidence Wiki hits, applying a `1.25x` boost to primary chunks **only** if the primary chunk independently matches a high-IDF core query token (preventing collateral noise from broad overview pages).
4. **Bounded Chunk Merging & Header-Discounted Table Windowing**:
   - Adjacent chunks from the same file (`gap <= 20` lines, combined span `<= 95` lines) are merged seamlessly.
   - Oversized chunks are sliced via a linear-time distinct-IDF sliding window that **pins `table_header` automatically** at the top while discounting tokens already satisfied by `table_header` so row-specific keys deep in a 100-row table are never missed.
5. **Zero-Hit Missing-Concept & Coverage Guardrails**: Any core query concept absent from all retrieved text (including synonyms) triggers an explicit `⚠️ Missing-Concept Alert`, accompanied by unmirrored-file and visual-page boundary notices.

---

## 4. Lock-Free Concurrency & Zero-Downtime Hot Reload

- Readers open `gen-<id>/bm25.sqlite` using `?mode=ro&immutable=1` on per-thread `threading.local()` connections — zero SQLite write locks or cross-thread mutex contention.
- Before each request, `KBService` checks `stat()` on `current.json`. When `wctx sync` or `wctx index` publishes a new generation, `KBService` atomically swaps its active snapshot and places the previous snapshot into a 30-second grace drain queue so in-flight requests finish cleanly.
