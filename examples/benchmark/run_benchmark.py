#!/usr/bin/env python3
"""Reproducible head-to-head benchmark on `examples/demo-vault`.

Compares:
1. Fixed 600-char BM25 prefix chunks (naive chunk RAG)
2. `wiki-context` one-shot `ContextPack` (`kb_context`)
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from wiki_context.freshness import snapshot_wiki_hashes  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


def _has_all_groups(text: str, groups: list[list[str]]) -> bool:
    low = text.lower()
    return all(any(term.lower() in low for term in grp) for grp in groups)


def main() -> int:
    vault = ROOT / "examples" / "demo-vault"
    gold_path = ROOT / "examples" / "benchmark" / "gold_questions.json"

    sync_vault(vault)
    snapshot_wiki_hashes(vault)
    sync_vault(vault)

    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    svc = KBService(vault)
    try:
        ans_total = 0
        naive_hits = 0
        cpack_hits = 0
        gap_detected = 0
        gap_total = 0
        latencies: list[float] = []

        print(f"{'ID':<4} {'Route':<8} {'NaiveBM25@3':<13} {'ContextPack':<13} {'Latency(ms)':<12} Question")
        print("-" * 88)

        for item in gold["questions"]:
            q = item["question"]
            # 1) Naive fixed-prefix 600-char top-3 BM25 chunks (no synonyms, no table-header pinning)
            raw_bm = svc._snapshot.index.search(q, k=3)
            naive_blob = "\n".join((h.get("text") or "")[:600] for h in raw_bm)

            # 2) One-shot ContextPack
            cp = svc.tool_context({"question": q})
            latencies.append(cp["elapsed_ms"])
            cp_md = cp["context_markdown"]

            if item["answerable"]:
                ans_total += 1
                n_ok = _has_all_groups(naive_blob, item["strict_groups"])
                c_ok = _has_all_groups(cp_md, item["strict_groups"])
                naive_hits += int(n_ok)
                cpack_hits += int(c_ok)
                print(
                    f"{item['id']:<4} {cp['route']:<8} "
                    f"{('PASS' if n_ok else 'MISS'):<13} {('PASS' if c_ok else 'MISS'):<13} "
                    f"{cp['elapsed_ms']:<12.2f} {q[:46]}"
                )
            else:
                gap_total += 1
                flagged = any(
                    exp.lower() in {m.lower() for m in cp["missing_concepts"]}
                    for exp in item.get("missing_expect", [])
                )
                gap_detected += int(flagged)
                print(
                    f"{item['id']:<4} {cp['route']:<8} "
                    f"{'(halluc.risk)':<13} {('GUARD_OK' if flagged else 'MISSED'):<13} "
                    f"{cp['elapsed_ms']:<12.2f} {q[:46]} [Unanswerable]"
                )

        print("-" * 88)
        print(
            f"Strict Complete-Evidence Recall : Naive 600c BM25 = {naive_hits}/{ans_total} "
            f"({naive_hits / max(1, ans_total):.1%})  vs  ContextPack = {cpack_hits}/{ans_total} "
            f"({cpack_hits / max(1, ans_total):.1%})"
        )
        print(f"Unanswerable Gap Guardrail      : {gap_detected}/{gap_total} ({gap_detected / max(1, gap_total):.1%})")
        print(f"ContextPack Median Latency      : {statistics.median(latencies):.2f} ms")
        return 0 if (cpack_hits == ans_total and gap_detected == gap_total) else 1
    finally:
        svc.close()


if __name__ == "__main__":
    raise SystemExit(main())
