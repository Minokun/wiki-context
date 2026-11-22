"""Self-serve vault retrieval evaluator & synthetic benchmark generator (`wctx eval`).

Computes deterministic metrics on any vault:
- `strict_recall`: fraction of answerable questions where `<kb_context>` contains every required fact group
- `naive_bm25_recall`: baseline fixed-600c top-3 BM25 recall for head-to-head comparison
- `mrr`: Mean Reciprocal Rank of expected primary sources in `[S1..Sn]`
- `guardrail_accuracy`: fraction of unanswerable probes triggering `missing_concepts` alerts
- `composite_score`: `0.0 .. 100.0` weighted score (`75 * strict_recall + 15 * guardrail_accuracy + 10 * mrr`)
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from .config import VaultConfig
from .mcp_server import KBService


def _has_all_groups(text: str, groups: list[list[str]]) -> bool:
    low = (text or "").lower()
    return all(any(str(term).lower() in low for term in grp) for grp in groups)


def generate_smoke_questions(vault: Path | str, *, max_questions: int = 8) -> dict:
    """Auto-generate a deterministic self-check benchmark from a vault's indexed mirrors."""
    cfg = VaultConfig.load(vault)
    svc = KBService(cfg.root)
    questions: list[dict] = []
    try:
        snap = svc._get_snapshot()
        units = snap.index.units if snap.index.backend_name != "sqlite" else []
        if not units and snap.index._con is not None:
            import zlib
            rows = snap.index._con.execute(
                "SELECT source, heading, table_header, text_z FROM units LIMIT 60"
            ).fetchall()
            for src, h, th, tz in rows:
                units.append({
                    "source": src,
                    "heading": h,
                    "table_header": th,
                    "text": zlib.decompress(tz).decode("utf-8"),
                })

        code_re = re.compile(r"\b([A-Za-z]{2,8}[-_/]\d{2,6}|\d+(?:\.\d+)?(?:ms|s|MB|GB|RPS|%))\b")
        q_idx = 1
        seen_src: set[str] = set()
        for u in units:
            if len(questions) >= max(1, max_questions - 1):
                break
            src = u.get("source") or ""
            if src in seen_src:
                continue
            txt = u.get("text") or ""
            heading = (u.get("heading") or Path(src).stem).split(">")[-1].strip()
            codes = list(dict.fromkeys(code_re.findall(txt)))
            if heading and len(codes) >= 1:
                seen_src.add(src)
                questions.append({
                    "id": f"S{q_idx}",
                    "question": f"What are the specifications and details for {heading} ({codes[0]})?",
                    "answerable": True,
                    "expected_sources": [src],
                    "strict_groups": [[codes[0]]],
                })
                q_idx += 1

        questions.append({
            "id": f"S{q_idx}",
            "question": "What is the QuantumHyperdrive999flux calibration interval?",
            "answerable": False,
            "missing_expect": ["QuantumHyperdrive999flux"],
        })
        return {"vault": cfg.name, "generated": True, "questions": questions}
    finally:
        svc.close()


def evaluate_vault(
    vault: Path | str,
    *,
    questions_path: Path | str | None = None,
    questions_data: dict | None = None,
) -> dict:
    cfg = VaultConfig.load(vault)
    if questions_data is not None:
        suite = questions_data
    elif questions_path is not None:
        suite = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    else:
        default_q = cfg.root / "eval_questions.json"
        if default_q.is_file():
            suite = json.loads(default_q.read_text(encoding="utf-8"))
        else:
            suite = generate_smoke_questions(cfg.root)

    svc = KBService(cfg.root)
    try:
        ans_total = 0
        naive_hits = 0
        cpack_hits = 0
        rr_sum = 0.0
        rr_count = 0
        gap_total = 0
        gap_hits = 0
        latencies: list[float] = []
        rows: list[dict] = []

        for item in suite.get("questions", ()):
            q = item["question"]
            snap = svc._get_snapshot()
            raw_bm = snap.index.search(q, k=3)
            naive_blob = "\n".join((h.get("text") or "")[:600] for h in raw_bm)

            cp = svc.tool_context({"question": q})
            lat = float(cp.get("elapsed_ms") or 0.0)
            latencies.append(lat)
            cp_md = cp.get("context_markdown") or ""

            if item.get("answerable", True):
                ans_total += 1
                groups = item.get("strict_groups") or []
                n_ok = _has_all_groups(naive_blob, groups) if groups else bool(raw_bm)
                c_ok = _has_all_groups(cp_md, groups) if groups else bool(cp.get("primary_hits"))
                naive_hits += int(n_ok)
                cpack_hits += int(c_ok)

                exp_srcs = [cfg.normalize_source_ref(s) for s in (item.get("expected_sources") or ())]
                rr = 0.0
                if exp_srcs:
                    rr_count += 1
                    ev_list = cp.get("primary_hits") or []
                    for rank_idx, ev in enumerate(ev_list, 1):
                        ev_src = ev.get("source") or ""
                        also = ev.get("also_at") or []
                        if ev_src in exp_srcs or any(a in exp_srcs for a in also):
                            rr = 1.0 / rank_idx
                            break
                    rr_sum += rr
                rows.append({
                    "id": item.get("id", ""),
                    "route": cp.get("route", "fact"),
                    "answerable": True,
                    "naive_ok": n_ok,
                    "context_ok": c_ok,
                    "rr": round(rr, 3) if exp_srcs else None,
                    "elapsed_ms": round(lat, 2),
                    "question": q,
                })
            else:
                gap_total += 1
                missing_low = {m.lower() for m in (cp.get("missing_concepts") or ())}
                flagged = any(
                    exp.lower() in missing_low or any(exp.lower() in m for m in missing_low)
                    for exp in (item.get("missing_expect") or ())
                )
                gap_hits += int(flagged)
                rows.append({
                    "id": item.get("id", ""),
                    "route": cp.get("route", "fact"),
                    "answerable": False,
                    "guard_ok": flagged,
                    "elapsed_ms": round(lat, 2),
                    "question": q,
                })

        strict_recall = (cpack_hits / ans_total) if ans_total else 1.0
        naive_recall = (naive_hits / ans_total) if ans_total else 1.0
        guard_acc = (gap_hits / gap_total) if gap_total else 1.0
        mrr = (rr_sum / rr_count) if rr_count else strict_recall
        composite = round(75.0 * strict_recall + 15.0 * guard_acc + 10.0 * mrr, 2)

        return {
            "vault": cfg.name,
            "answerable_total": ans_total,
            "naive_bm25_hits": naive_hits,
            "naive_bm25_recall": round(naive_recall, 4),
            "context_pack_hits": cpack_hits,
            "strict_recall": round(strict_recall, 4),
            "mrr": round(mrr, 4),
            "unanswerable_total": gap_total,
            "guardrail_hits": gap_hits,
            "guardrail_accuracy": round(guard_acc, 4),
            "median_latency_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
            "p95_latency_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2) if latencies else 0.0,
            "composite_score": composite,
            "rows": rows,
        }
    finally:
        svc.close()
