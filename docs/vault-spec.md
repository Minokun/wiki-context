# Vault Layout, Synonyms & Freshness Specification

## 1. Directory Layouts (`en` & `zh`)

`VaultConfig.load()` automatically detects English and Chinese vault layouts, or reads explicit overrides from `vault.json`:

| Role | English (`--layout en`) | Chinese (`--layout zh`) | Mutable by AI? |
| :--- | :--- | :--- | :--- |
| Raw ground-truth files | `sources/` | `史料/` | **Never (Read-Only)** |
| Derived Markdown mirrors & sidecars | `mirrors/` | `史料_md/` | Script-generated only |
| Versioned FTS5 & JSONL index | `mirrors/.index/` | `史料_md/.index/` | Script-generated only |
| Compiled Wiki synthesis pages | `wiki/` | `知识库/` | Yes (Agent / Human) |
| Topic navigation catalog | `index.md` | `目录.md` | Yes |
| Append-only operation timeline | `log.md` | `日志.md` | Yes (Append-only) |

---

## 2. `vault.json` Configuration

```json
{
  "name": "Platform Engineering KB",
  "layout": "en",
  "max_doc_bytes": 2097152,
  "sensitive": false,
  "exclude_patterns": ["archive/deprecated-"],
  "ocr_image_dirs": ["architecture-diagrams"],
  "anchor_terms": [],
  "acl": {
    "engineering": {
      "allow": ["sources/public/**", "sources/specs/**"],
      "deny": ["sources/finance/**"]
    }
  }
}
```

---

## 3. One-Way Canonical Synonyms (`synonyms.json`)

Edit `<vault>/synonyms.json` at any time — `ContextPackEngine` watches `st_mtime_ns` and hot-reloads changes immediately without restarting `wctx serve` or `wctx gateway`, and without rebuilding the FTS5 index:

```json
{
  "version": 1,
  "groups": [
    {
      "canonical": ["rate-limit", "HTTP-429"],
      "aliases": ["throttled", "too many requests", "限流"]
    }
  ]
}
```

When a user asks about `"throttled"`, `rate-limit` and `HTTP-429` are merged into the same query concept group so recall expands without penalizing multi-term coverage scores.

---

## 4. Wiki Frontmatter & SHA-256 Freshness (`wctx freshness`)

Every compiled Wiki page under `wiki/` (or `知识库/`) starts with flat YAML frontmatter:

```yaml
---
title: "Gateway-Rate-Limiting-and-SLOs"
type: synthesis
status: reviewed
updated: 2026-11-22
sources:
  - sources/specs/rfc-042-edge-gateway.md
source_hashes:
  "sources/specs/rfc-042-edge-gateway.md": "e3b0c44298fc1c149afbf4c8996fb924..."
---
```

- When `sources/specs/rfc-042-edge-gateway.md` is modified and `wctx sync` runs, its new SHA-256 no longer matches `source_hashes`.
- During index build, `wiki-context` automatically marks that Wiki page `stale` (`0.5x` ranking demotion + explicit `⚠️ stale` warning badge in `<wiki_context>`) without touching the raw file.
- After reviewing or running `wctx compile`, run `wctx freshness --vault . snapshot` to record the verified hashes.
