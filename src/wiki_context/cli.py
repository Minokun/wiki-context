"""Unified CLI (`wctx` / `wiki-context`) for initializing, syncing, indexing,
compiling, linting, querying, benchmarking, and serving knowledge vaults.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .compiler import compile_vault
from .eval_harness import evaluate_vault
from .freshness import check_vault_freshness, snapshot_wiki_hashes
from .gateway import MultiVaultGateway, TokenRegistry, create_http_server, parse_vault_specs, selftest_gateway
from .indexer import check_index, rebuild_index
from .lint import lint_vault
from .mcp_server import KBService, run_stdio_server, scrub_secrets, selftest_mcp
from .memory import MemoryStore
from .remote_stdio import run_bridge
from .scaffold import init_vault
from .search import BM25Index, route
from .sync import sync_vault
from .synonym_miner import mine_synonyms


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wctx",
        description="wiki-context: Deterministic Context Engine for AI Agents",
    )
    p.add_argument("-V", "--version", action="version", version=f"wiki-context {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    p_init = sub.add_parser("init", help="Initialize a new three-layer knowledge vault")
    p_init.add_argument("path", help="Target directory for the new vault")
    p_init.add_argument("--name", help="Human-readable vault name")
    p_init.add_argument("--layout", choices=["en", "zh"], default="en", help="Directory naming preset (default: en)")
    p_init.add_argument("--dry-run", action="store_true", help="Preview files without writing")

    # sync
    p_sync = sub.add_parser("sync", help="Incrementally extract raw sources into Markdown mirrors & rebuild FTS5")
    p_sync.add_argument("--vault", default=".", help="Vault root path (default: .)")
    p_sync.add_argument("--source", help="Convert a single specific source file")
    p_sync.add_argument("--list", action="store_true", help="List pending changes without converting")
    p_sync.add_argument("--force", action="store_true", help="Re-extract even if SHA-256 is unchanged")
    p_sync.add_argument("--all-images", action="store_true", help="Include image files via VLM/OCR/visual-asset provider")
    p_sync.add_argument("--ocr-cmd", help="External OCR command template (e.g. 'marker_single {input}')")
    p_sync.add_argument("--asr-cmd", help="External ASR command template for audio/video (or set WCTX_ASR_CMD)")

    # index
    p_idx = sub.add_parser("index", help="Build or verify the versioned SQLite FTS5 index")
    p_idx.add_argument("--vault", default=".", help="Vault root path")
    p_idx.add_argument("--check", action="store_true", help="Verify index integrity without writing")
    p_idx.add_argument("--deep", action="store_true", help="Verify full SHA-256 and JSONL line framing")
    p_idx.add_argument("--force", action="store_true", help="Force full index rebuild")

    # search
    p_srch = sub.add_parser("search", help="Run fine-grained BM25/FTS5 search or route classification")
    p_srch.add_argument("--vault", default=".", help="Vault root path")
    p_srch.add_argument("-q", "--query", required=True, help="Search query or question")
    p_srch.add_argument("-k", type=int, default=5, help="Top-K hits (default: 5)")
    p_srch.add_argument("--kind", choices=["section", "page", "wiki"], help="Filter unit type")
    p_srch.add_argument("--route", action="store_true", help="Only classify query complexity (fact vs complex)")

    # context
    p_ctx = sub.add_parser("context", help="Assemble a one-shot citation-anchored Context Pack")
    p_ctx.add_argument("--vault", default=".", help="Vault root path")
    p_ctx.add_argument("-q", "--question", required=True, help="Natural-language question")
    p_ctx.add_argument("--expand", nargs="*", help="Optional expanded canonical terms")
    p_ctx.add_argument("--max-tokens", type=int, help="Token budget override (1000-8000)")
    p_ctx.add_argument("--json", action="store_true", help="Output structured JSON instead of Markdown")

    # lint
    p_lint = sub.add_parser("lint", help="Run mechanical health checks on Wiki links, catalog, and sources")
    p_lint.add_argument("--vault", default=".", help="Vault root path")
    p_lint.add_argument("--json", action="store_true", help="Output JSON report")

    # freshness
    p_fresh = sub.add_parser("freshness", help="Check or snapshot SHA-256 source_hashes on Wiki pages")
    p_fresh.add_argument("action", choices=["check", "snapshot"], nargs="?", default="check")
    p_fresh.add_argument("--vault", default=".", help="Vault root path")
    p_fresh.add_argument("--page", help="Specific Wiki page name for snapshot")
    p_fresh.add_argument("--only-stale", action="store_true", help="Only list stale/missing pages")

    # compile
    p_comp = sub.add_parser("compile", help="Offline AI Librarian: compile unindexed mirrors & refresh stale Wiki pages")
    p_comp.add_argument("--vault", default=".", help="Vault root path")
    p_comp.add_argument("--model", help="Model ID (or set WCTX_MODEL)")
    p_comp.add_argument("--base-url", help="API base URL (or set WCTX_BASE_URL)")
    p_comp.add_argument("--protocol", choices=["openai", "anthropic"], default="openai")
    p_comp.add_argument("--max-items", type=int, default=10, help="Max pages to compile in one batch")
    p_comp.add_argument("--dry-run", action="store_true", help="Show pending mirrors and stale pages without calling LLM")

    # serve (stdio MCP)
    p_srv = sub.add_parser("serve", help="Start local stdio MCP server (for Claude Code, Codex, Cursor, DSH)")
    p_srv.add_argument("--vault", default=".", help="Vault root path")
    p_srv.add_argument("--test", action="store_true", help="Run protocol & security self-test and exit")

    # gateway (multi-user Streamable HTTP MCP + REST)
    p_gw = sub.add_parser("gateway", help="Start multi-user, multi-vault HTTP MCP + REST context gateway")
    p_gw.add_argument(
        "--vault",
        action="append",
        required=True,
        help="Vault path or `id=path` (repeatable for hosting multiple vaults on `/mcp/<id>`)",
    )
    p_gw.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p_gw.add_argument("--port", type=int, default=8090, help="Bind port (default: 8090)")
    p_gw.add_argument("--token-file", help="Path to hot-reloaded JSON token & ACL config")
    p_gw.add_argument("--test", action="store_true", help="Run gateway HTTP self-test and exit")

    # bridge (local stdio -> remote HTTP MCP)
    p_br = sub.add_parser("bridge", help="Local stdio -> remote Streamable HTTP MCP bridge")
    p_br.add_argument("--url", help="Remote `/mcp` endpoint URL (or WCTX_REMOTE_URL)")
    p_br.add_argument("--token", help="Bearer token (or WCTX_TOKEN)")

    # remember (Layer-4 operational memory & errata)
    p_rem = sub.add_parser("remember", help="Record an operational insight or source-anchored errata note into Layer-4")
    p_rem.add_argument("--vault", default=".", help="Vault root path")
    p_rem.add_argument("-t", "--text", required=True, help="Fact, troubleshooting note, or errata text")
    p_rem.add_argument("--kind", choices=["insight", "errata", "decision", "glossary"], default="insight")
    p_rem.add_argument("--source", action="append", default=[], help="Related primary source path (repeatable)")
    p_rem.add_argument("--tag", action="append", default=[], help="Topic tag (repeatable)")
    p_rem.add_argument("--author", default="cli", help="Author identifier")

    # notes (inspect / archive / import Layer-4 memory notes)
    p_notes = sub.add_parser("notes", help="List, archive (forget), or import Layer-4 operational memory notes")
    p_notes.add_argument("action", choices=["list", "forget", "import"], nargs="?", default="list")
    p_notes.add_argument("--vault", default=".", help="Vault root path")
    p_notes.add_argument("--status", choices=["active", "consolidated", "archived", "all"], default="active")
    p_notes.add_argument("--id", help="Note ID for `forget`")
    p_notes.add_argument("--file", help="JSON file path for `import` (e.g. exported magpie-memory items)")

    # synonyms (auto-mine domain glossaries & acronyms)
    p_syn = sub.add_parser("synonyms", help="Mine bilingual glossary tables & inline acronyms into synonyms.json")
    p_syn.add_argument("action", choices=["mine"], nargs="?", default="mine")
    p_syn.add_argument("--vault", default=".", help="Vault root path")
    p_syn.add_argument("--apply", action="store_true", help="Write discovered groups into synonyms.json")

    # eval (quantitative retrieval & guardrail benchmark)
    p_eval = sub.add_parser("eval", help="Benchmark Strict Recall, MRR, Latency, and Anti-Hallucination Guardrails")
    p_eval.add_argument("--vault", default=".", help="Vault root path")
    p_eval.add_argument("--questions", help="Optional gold QA JSON path (auto-generates smoke suite if omitted)")
    p_eval.add_argument("--json", action="store_true", help="Print full JSON report")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "init":
            res = init_vault(args.path, name=args.name, layout=args.layout, dry_run=args.dry_run)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "sync":
            res = sync_vault(
                args.vault,
                source=args.source,
                list_only=args.list,
                force=args.force,
                include_all_images=args.all_images,
                ocr_cmd=args.ocr_cmd,
                asr_cmd=args.asr_cmd,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "index":
            if args.check:
                ok, msgs = check_index(args.vault, deep=args.deep)
                for m in msgs:
                    print(m)
                print("Index check: OK" if ok else "Index check: FAILED")
                return 0 if ok else 1
            summary = rebuild_index(args.vault, force=args.force)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "search":
            if args.route:
                print(json.dumps(route(args.query), ensure_ascii=False, indent=2))
                return 0
            idx = BM25Index(args.vault)
            try:
                hits = idx.search(args.query, k=args.k, kind=args.kind)
                for h in hits:
                    h.pop("_q_idf", None)
                print(json.dumps(hits, ensure_ascii=False, indent=2))
                return 0
            finally:
                idx.close()

        if args.cmd == "context":
            svc = KBService(args.vault)
            try:
                out = svc.tool_context({
                    "question": args.question,
                    "expanded_terms": args.expand,
                    "max_tokens": args.max_tokens,
                })
                if args.json or "error" in out:
                    print(json.dumps(out, ensure_ascii=False, indent=2))
                    return 1 if "error" in out else 0
                print(out["context_markdown"])
                return 0
            finally:
                svc.close()

        if args.cmd == "lint":
            rep = lint_vault(args.vault)
            if args.json:
                print(json.dumps(rep, ensure_ascii=False, indent=2))
            else:
                for e in rep["errors"]:
                    print(f"ERROR [{e['code']}] {e['file']}: {e['detail']}")
                for w in rep["warnings"]:
                    print(f"WARN  [{w['code']}] {w['file']}: {w['detail']}")
                print(f"Lint summary: {rep['error_count']} error(s), {rep['warning_count']} warning(s)")
            return 0 if rep["ok"] else 1

        if args.cmd == "freshness":
            if args.action == "snapshot":
                updated = snapshot_wiki_hashes(args.vault, page=args.page)
                print(json.dumps({"snapshotted_pages": updated}, ensure_ascii=False, indent=2))
                return 0
            rows = check_vault_freshness(args.vault, only_stale=args.only_stale)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 1 if any(r["state"] in {"stale", "missing"} for r in rows) else 0

        if args.cmd == "compile":
            res = compile_vault(
                args.vault,
                model=args.model,
                base_url=args.base_url,
                protocol=args.protocol,
                max_items=args.max_items,
                dry_run=args.dry_run,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "serve":
            if args.test:
                return selftest_mcp(args.vault)
            return run_stdio_server(args.vault)

        if args.cmd == "gateway":
            vmap = parse_vault_specs(args.vault)
            if args.test:
                return selftest_gateway(vmap)
            reg = TokenRegistry(token_file=args.token_file)
            gw = MultiVaultGateway(vmap, tokens=reg)
            srv = create_http_server(gw, host=args.host, port=args.port)
            print(
                f"wiki-context gateway listening on http://{args.host}:{args.port} "
                f"(vaults={list(vmap)}, default='{gw.default_vault_id}')",
                flush=True,
            )
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                srv.server_close()
                gw.close()
            return 0

        if args.cmd == "bridge":
            return run_bridge(url=args.url, token=args.token)

        if args.cmd == "remember":
            store = MemoryStore(args.vault, scrub_fn=scrub_secrets)
            note = store.remember(
                args.text,
                kind=args.kind,
                related_sources=args.source,
                tags=args.tag,
                author=args.author,
            )
            print(json.dumps({
                "note_id": note.note_id,
                "kind": note.kind,
                "status": note.status,
                "related_sources": list(note.related_sources),
                "file": str(note.file_path),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "notes":
            store = MemoryStore(args.vault, scrub_fn=scrub_secrets)
            if args.action == "forget":
                if not args.id:
                    raise ValueError("--id is required for `wctx notes forget`")
                ok = store.update_status(args.id, "archived")
                print(json.dumps({"archived": ok, "note_id": args.id}, ensure_ascii=False, indent=2))
                return 0 if ok else 1
            if args.action == "import":
                if not args.file:
                    raise ValueError("--file is required for `wctx notes import`")
                raw_items = json.loads(Path(args.file).read_text(encoding="utf-8"))
                items = raw_items.get("memories", raw_items) if isinstance(raw_items, dict) else raw_items
                created = store.import_json_items(items if isinstance(items, list) else [])
                print(json.dumps({"imported_count": len(created), "note_ids": created}, ensure_ascii=False, indent=2))
                return 0
            st_filter = None if args.status == "all" else args.status
            rows = [
                {
                    "note_id": n.note_id,
                    "kind": n.kind,
                    "status": n.status,
                    "author": n.author,
                    "created_at": n.created_at,
                    "related_sources": list(n.related_sources),
                    "text": n.text,
                }
                for n in store.list_notes(status=st_filter)
            ]
            print(json.dumps({"vault": store.cfg.name, "count": len(rows), "notes": rows}, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "synonyms":
            rep = mine_synonyms(args.vault, apply=args.apply)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "eval":
            rep = evaluate_vault(args.vault, questions_path=args.questions)
            if args.json:
                print(json.dumps(rep, ensure_ascii=False, indent=2))
            else:
                for r in rep["rows"]:
                    st = ("PASS" if r["context_ok"] else "MISS") if r["answerable"] else ("GUARD_OK" if r["guard_ok"] else "GUARD_MISS")
                    print(f"{r['id']:<16} {r['route']:<8} {st:<10} {r['elapsed_ms']:>6.2f}ms  {r['question'][:54]}")
                print("-" * 88)
                print(
                    f"Strict Recall: {rep['context_pack_hits']}/{rep['answerable_total']} ({rep['strict_recall']:.1%}) "
                    f"[Naive BM25: {rep['naive_bm25_recall']:.1%}] | MRR: {rep['mrr']:.3f} | "
                    f"Guardrail: {rep['guardrail_accuracy']:.1%} | Median: {rep['median_latency_ms']:.2f}ms | "
                    f"Composite: {rep['composite_score']}"
                )
            return 0 if (rep["strict_recall"] >= 1.0 and rep["guardrail_accuracy"] >= 1.0) else 1

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
