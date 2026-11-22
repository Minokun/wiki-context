"""Bilingual BM25 + SQLite FTS5 hybrid search engine & query complexity router.

Features:
- Bilingual zero-dependency tokenizer: preserves exact alphanumeric/technical codes
  (`v1.2`, `tb/t-2075`, `rfc-9110`, `350mm`, `p99`), English words, and CJK sliding
  bigrams while stripping conversational fillers & trailing modal particles.
- Lock-free thread-local read-only SQLite FTS5 backend (`?mode=ro&immutable=1`) with
  two-stage exact per-source IDF + concept-coverage (`COVERAGE_ALPHA`) reranking.
- Exact parity in-memory JSONL fallback backend (`InMemoryBackend`).
- Bilingual query complexity router (`fact` vs `complex`) for dynamic token budgeting.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import sqlite3
import threading
import zlib
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from .config import VaultConfig


class IndexUnavailableError(RuntimeError):
    """Raised when a vault's index has not been built or is empty."""


LATIN_NUM_RE = re.compile(r"[a-z0-9]+(?:[._\-/+][a-z0-9]+)*", re.I)
_UNIT_SPLIT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)(mm|cm|km|ms|us|ns|hz|khz|mhz|ghz|gb|mb|kb|tb|kv|mv|ma|kw|mw|db|fps|rps|qps)$",
    re.I,
)
_ALPHA_NUM_SPLIT_RE = re.compile(r"^([a-z]{1,8})(\d{1,6})$", re.I)

NUM_MEASURE_RE = re.compile(
    r"(\d{1,6})\s*(号线|车|期|标段|批次|号|段|站|页|章|节|条|款|项|天|日|月|年|倍|次|个|台|套|万|亿)"
)
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{1,}")

EN_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "did", "do", "does",
    "for", "from", "give", "has", "have", "how", "i", "if", "in", "into", "is", "it",
    "its", "me", "my", "of", "on", "or", "our", "out", "please", "should", "show", "tell",
    "that", "the", "their", "them", "there", "these", "this", "those", "to", "us", "was",
    "we", "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your",
})

CJK_STOP_CHARS = frozenset(
    "的了是在有和与及或把被让给向从到对在着过吗呢吧啊呀哦喔呗嘛啦这那之其并而但又也还就都只各每某"
)

STOP_BIGRAMS = frozenset({
    "的是", "是在", "什么", "怎么", "如何", "可以", "需要", "一个", "这个", "那个",
    "我们", "他们", "你们", "以及", "并且", "或者", "是否", "有哪", "哪些", "多少",
    "为什", "么样", "如果", "因为", "所以", "但是", "进行", "么处",
})

_STOP_QUERY_PHRASE_RE = re.compile(
    r"(?:为什么|是什么|什么是|可不可以|能不能|是不是|怎么|如何|哪些|多少|是否|以及|并且|或者|我们|他们|这个|那个|一个)"
)

_MODAL_PARTICLE_SUFFIX_RE = re.compile(
    r"(?<![网贴酒泥氧水唱茶泡咖])[吧呢吗呀啊呗嘛啦喔哦]+(?=$|[？?！!，,。；;：:\s”\"’'])"
)

_WIKI_FOOTER_HEADINGS = frozenset({
    "来源", "参考来源", "引用来源", "资料来源", "数据来源", "关联页面", "相关页面", "延伸阅读", "参见",
    "sources", "references", "related pages", "see also", "citations", "further reading",
})


def _is_wiki_footer_heading(heading: str) -> bool:
    if not heading:
        return False
    leaf = heading.rsplit(">", 1)[-1].strip().lower()
    return leaf in _WIKI_FOOTER_HEADINGS


def _expand_latin_token(tok: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(piece: str) -> None:
        if piece and piece not in EN_STOPWORDS and piece not in seen:
            seen.add(piece)
            out.append(piece)

    add(tok)
    parts = re.split(r"[.\-/+_]+", tok)
    if len(parts) > 1:
        for p in parts:
            add(p)
            m_u = _UNIT_SPLIT_RE.match(p)
            if m_u:
                add(m_u.group(1))
                add(m_u.group(2))
            m_a = _ALPHA_NUM_SPLIT_RE.match(p)
            if m_a:
                add(m_a.group(1))
                add(m_a.group(2))
    else:
        m_u = _UNIT_SPLIT_RE.match(tok)
        if m_u:
            add(m_u.group(1))
            add(m_u.group(2))
        m_a = _ALPHA_NUM_SPLIT_RE.match(tok)
        if m_a:
            add(m_a.group(1))
            add(m_a.group(2))
    return out


def tokenize(text: str) -> list[str]:
    """Bilingual document tokenizer (English words/codes + CJK bigrams)."""
    if not text:
        return []
    out: list[str] = []
    low = text.lower()
    for m in LATIN_NUM_RE.finditer(low):
        out.extend(_expand_latin_token(m.group(0)))
    for m in NUM_MEASURE_RE.finditer(low):
        out.append(m.group(1) + m.group(2))
    for run in CJK_RUN_RE.findall(low):
        if len(run) == 1:
            if run not in CJK_STOP_CHARS:
                out.append(run)
            continue
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in STOP_BIGRAMS:
                out.append(bg)
    return out


def tokenize_query(query: str) -> tuple[list[str], list[set[str]]]:
    """Query tokenizer returning ``(tokens, concept_groups)``."""
    if not query:
        return [], []
    low = query.lower()
    out: list[str] = []
    groups: list[set[str]] = []

    for m in LATIN_NUM_RE.finditer(low):
        expanded = _expand_latin_token(m.group(0))
        if expanded:
            out.extend(expanded)
            groups.append(set(expanded))
    for m in NUM_MEASURE_RE.finditer(low):
        comp = m.group(1) + m.group(2)
        out.append(comp)
        groups.append({comp})

    cleaned = _MODAL_PARTICLE_SUFFIX_RE.sub(" ", low)
    cleaned = _STOP_QUERY_PHRASE_RE.sub(" ", cleaned)
    for run in CJK_RUN_RE.findall(cleaned):
        if len(run) == 1:
            if run not in CJK_STOP_CHARS:
                out.append(run)
                groups.append({run})
            continue
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in STOP_BIGRAMS:
                out.append(bg)
                groups.append({bg})
    return out, groups


def encode_fts_token(tok: str) -> str:
    return (
        tok.replace("_", "_u_")
        .replace("/", "_s_")
        .replace("-", "_d_")
        .replace(".", "_p_")
        .replace("+", "_a_")
    )


def _source_filename_tokens(source: str) -> list[str]:
    if not source:
        return []
    return tokenize(PurePosixPath(source).stem)


def load_exclude_patterns(vault: Path | str) -> tuple[str, ...]:
    cfg = VaultConfig.load(vault)
    env_raw = os.environ.get("WCTX_EXCLUDE_SOURCES", "").strip()
    env_pats = tuple(x.strip() for x in env_raw.split(",") if x.strip()) if env_raw else ()
    return tuple(dict.fromkeys([*cfg.exclude_patterns, *env_pats]))


def is_excluded_source(source: str, patterns: tuple[str, ...]) -> bool:
    if not source or not patterns:
        return False
    low = source.lower()
    return any(p.lower() in low for p in patterns)


def _compute_distinctive_groups(
    groups: list[set[str]],
    q_idf: dict[str, float],
    q_df: dict[str, int],
    n_sources: int,
    idf_ratio: float,
) -> tuple[list[set[str]], set[str]]:
    if not q_idf:
        return [], set()
    if n_sources >= 20:
        stable = [v for t, v in q_idf.items() if q_df.get(t, 0) >= 2]
        max_idf = max(stable) if stable else max(q_idf.values())
    else:
        max_idf = max(q_idf.values())
    thresh = max_idf * idf_ratio
    dist_groups: list[set[str]] = []
    dist_tokens: set[str] = set()
    seen: set[frozenset[str]] = set()
    for grp in groups:
        matched = {t for t in grp if t in q_idf and q_idf[t] >= thresh}
        if not matched:
            continue
        fz = frozenset(matched)
        if fz in seen:
            continue
        seen.add(fz)
        dist_groups.append(matched)
        dist_tokens.update(matched)
    return dist_groups, dist_tokens


def _dedup_per_source(
    scored_items: list[tuple[int, float, dict]],
    *,
    k: int,
    max_per_source: int,
) -> list[tuple[int, float, dict]]:
    if k <= 0 or not scored_items:
        return []
    picked: list[tuple[int, float, dict]] = []
    counts: dict[str, int] = defaultdict(int)
    deferred: list[tuple[int, float, dict]] = []
    for item in scored_items:
        src = item[2].get("source") or ""
        if counts[src] < max_per_source:
            picked.append(item)
            counts[src] += 1
            if len(picked) >= k:
                return picked
        else:
            deferred.append(item)
    for item in deferred:
        if len(picked) >= k:
            break
        picked.append(item)
    return picked


# -- SQLite FTS5 Builder ---------------------------------------------------- #


def build_fts_sqlite(db_path: Path, layer2_rows: list[dict], wiki_rows: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = db_path.with_name(f".{db_path.name}.tmp-{os.getpid()}")
    tmp_db.unlink(missing_ok=True)

    con = sqlite3.connect(str(tmp_db))
    try:
        con.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE vocab (tok TEXT PRIMARY KEY, df_sources INTEGER NOT NULL);
            CREATE TABLE units (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                source TEXT NOT NULL,
                heading TEXT NOT NULL,
                table_header TEXT NOT NULL,
                line_start INTEGER,
                line_end INTEGER,
                page_num INTEGER,
                mirror TEXT NOT NULL,
                dl INTEGER NOT NULL,
                tf_z BLOB NOT NULL,
                body_toks_z BLOB NOT NULL,
                text_z BLOB NOT NULL
            );
            CREATE INDEX idx_units_source ON units(source);
            CREATE VIRTUAL TABLE fts_units USING fts5(head, body, tokenize='ascii');

            CREATE TABLE wiki_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE wiki_vocab (tok TEXT PRIMARY KEY, df_sources INTEGER NOT NULL);
            CREATE TABLE wiki_units (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                heading TEXT NOT NULL,
                table_header TEXT NOT NULL,
                line_start INTEGER,
                line_end INTEGER,
                freshness TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                dl INTEGER NOT NULL,
                tf_z BLOB NOT NULL,
                text_z BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE fts_wiki USING fts5(tokens, tokenize='ascii');
            """
        )

        # Populate Layer-2 primary source units
        l2_source_sets: dict[str, set[str]] = defaultdict(set)
        all_l2_sources: set[str] = set()
        total_dl = 0

        for uid, row in enumerate(layer2_rows, 1):
            src = row.get("source") or ""
            all_l2_sources.add(src)
            heading = row.get("heading") or ""
            text = row.get("text") or ""
            body_toks = tokenize(text)
            head_toks = tokenize(heading)
            fname_toks = _source_filename_tokens(src)

            tf: dict[str, float] = defaultdict(float)
            for t in body_toks:
                tf[t] += 1.0
            for t in head_toks:
                tf[t] += BM25Index.HEADING_BOOST
            for t in fname_toks:
                tf[t] += BM25Index.FILENAME_BOOST

            dl = max(1, len(body_toks) + len(head_toks))
            total_dl += dl
            for t in tf:
                l2_source_sets[t].add(src)

            body_set_list = sorted(set(body_toks) | set(head_toks))
            con.execute(
                "INSERT INTO units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid,
                    row.get("kind") or "section",
                    src,
                    heading,
                    row.get("table_header") or "",
                    row.get("line_start"),
                    row.get("line_end"),
                    row.get("page_num"),
                    row.get("mirror") or "",
                    dl,
                    zlib.compress(json.dumps(tf, separators=(",", ":")).encode("utf-8"), level=3),
                    zlib.compress(json.dumps(body_set_list, separators=(",", ":")).encode("utf-8"), level=3),
                    zlib.compress(text.encode("utf-8"), level=3),
                ),
            )
            fts_head = " ".join(encode_fts_token(t) for t in (*head_toks, *fname_toks))
            fts_body = " ".join(encode_fts_token(t) for t in body_toks)
            con.execute("INSERT INTO fts_units(rowid, head, body) VALUES (?, ?, ?)", (uid, fts_head, fts_body))

        avg_dl = (total_dl / len(layer2_rows)) if layer2_rows else 1.0
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("n_units", str(len(layer2_rows))),
                ("n_sources", str(len(all_l2_sources))),
                ("avg_len", f"{avg_dl:.4f}"),
            ],
        )
        con.executemany(
            "INSERT INTO vocab VALUES (?, ?)",
            [(tok, len(srcs)) for tok, srcs in l2_source_sets.items()],
        )

        # Populate Layer-3 Wiki units
        w_source_sets: dict[str, set[str]] = defaultdict(set)
        all_w_sources: set[str] = set()
        total_w_dl = 0

        for wid, row in enumerate(wiki_rows, 1):
            wsrc = row.get("source") or ""
            all_w_sources.add(wsrc)
            title = row.get("title") or ""
            heading = row.get("heading") or ""
            text = row.get("text") or ""
            body_toks = tokenize(text)
            head_toks = tokenize(f"{title} {heading}")

            tf_w: dict[str, float] = defaultdict(float)
            for t in body_toks:
                tf_w[t] += 1.0
            for t in head_toks:
                tf_w[t] += BM25Index.HEADING_BOOST

            dl_w = max(1, len(body_toks) + len(head_toks))
            total_w_dl += dl_w
            for t in tf_w:
                w_source_sets[t].add(wsrc)

            con.execute(
                "INSERT INTO wiki_units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wid,
                    wsrc,
                    title,
                    heading,
                    row.get("table_header") or "",
                    row.get("line_start"),
                    row.get("line_end"),
                    row.get("freshness") or "unverified",
                    json.dumps(row.get("sources") or [], ensure_ascii=False),
                    dl_w,
                    zlib.compress(json.dumps(tf_w, separators=(",", ":")).encode("utf-8"), level=3),
                    zlib.compress(text.encode("utf-8"), level=3),
                ),
            )
            fts_toks = " ".join(encode_fts_token(t) for t in (*head_toks, *body_toks))
            con.execute("INSERT INTO fts_wiki(rowid, tokens) VALUES (?, ?)", (wid, fts_toks))

        avg_w_dl = (total_w_dl / len(wiki_rows)) if wiki_rows else 1.0
        con.executemany(
            "INSERT INTO wiki_meta VALUES (?, ?)",
            [
                ("n_units", str(len(wiki_rows))),
                ("n_sources", str(len(all_w_sources))),
                ("avg_len", f"{avg_w_dl:.4f}"),
            ],
        )
        con.executemany(
            "INSERT INTO wiki_vocab VALUES (?, ?)",
            [(tok, len(srcs)) for tok, srcs in w_source_sets.items()],
        )
        con.commit()
    finally:
        con.close()

    tmp_db.replace(db_path)


# -- BM25Index (SQLite FTS5 + InMemory Fallback) ---------------------------- #


class BM25Index:
    K1 = 1.2
    B = 0.75
    HEADING_BOOST = 2.5
    FILENAME_BOOST = 1.6
    COVERAGE_ALPHA = 1.6
    COVERAGE_IDF_RATIO = 0.45

    def __init__(
        self,
        vault: Path | str,
        *,
        backend: str = "auto",
        exclude_patterns: tuple[str, ...] | None = None,
    ):
        self.cfg = VaultConfig.load(vault)
        self.vault = self.cfg.root
        self.exclude_patterns = (
            exclude_patterns if exclude_patterns is not None else load_exclude_patterns(self.vault)
        )
        self._backend_pref = backend
        self.backend_name = "memory"

        self._sqlite_path: Path | None = None
        self._tls = threading.local()
        self._open_cons: list[sqlite3.Connection] = []
        self._con_lock = threading.Lock()

        # Memory backend fields
        self.units: list[dict] = []
        self.lengths: list[int] = []
        self.avg_len = 1.0
        self.n_sources = 0
        self.df_sources: dict[str, int] = {}
        self.postings: dict[str, dict[int, float]] = defaultdict(dict)
        self.body_tok_sets: list[set[str]] = []

        self.wiki_units: list[dict] = []
        self.wiki_lengths: list[int] = []
        self.wiki_avg_len = 1.0
        self.wiki_n_sources = 0
        self.wiki_df_sources: dict[str, int] = {}
        self.wiki_postings: dict[str, dict[int, float]] = defaultdict(dict)

        self._load()

    def _excluded(self, source: str) -> bool:
        return is_excluded_source(source, self.exclude_patterns)

    @property
    def _con(self) -> sqlite3.Connection | None:
        if self._sqlite_path is None:
            return None
        con = getattr(self._tls, "con", None)
        if con is None:
            uri = f"{self._sqlite_path.resolve().as_uri()}?mode=ro&immutable=1"
            con = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._tls.con = con
            with self._con_lock:
                self._open_cons.append(con)
        return con

    def close(self) -> None:
        with self._con_lock:
            cons = list(self._open_cons)
            self._open_cons.clear()
        self._tls = threading.local()
        for c in cons:
            try:
                c.close()
            except sqlite3.Error:
                pass

    def _resolve_generation_dir(self) -> Path:
        cur_file = self.cfg.index_root / "current.json"
        if cur_file.is_file():
            try:
                meta = json.loads(cur_file.read_text(encoding="utf-8"))
                gen_id = meta.get("generation_id")
                if isinstance(gen_id, str) and "/" not in gen_id and ".." not in gen_id:
                    cand = self.cfg.index_root / gen_id
                    if cand.is_dir():
                        return cand
            except (OSError, ValueError):
                pass
        return self.cfg.index_root

    def _load(self) -> None:
        gen_dir = self._resolve_generation_dir()
        db_file = gen_dir / "bm25.sqlite"
        if self._backend_pref in {"auto", "sqlite"} and db_file.is_file():
            self._sqlite_path = db_file
            con = self._con
            assert con is not None
            meta_map = dict(con.execute("SELECT key, value FROM meta").fetchall())
            n_u = int(meta_map.get("n_units", "0"))
            w_meta = dict(con.execute("SELECT key, value FROM wiki_meta").fetchall())
            n_w = int(w_meta.get("n_units", "0"))
            if n_u <= 0 and n_w <= 0:
                self.close()
                raise IndexUnavailableError("Index is empty; run `wctx sync` or `wctx index` first")
            self.backend_name = "sqlite"
            return

        if self._backend_pref == "sqlite":
            raise IndexUnavailableError(f"SQLite FTS5 index missing at {db_file}")
        self._load_memory(gen_dir)

    def _load_memory(self, gen_dir: Path) -> None:
        sec_path = gen_dir / "sections.jsonl"
        page_path = gen_dir / "pages.jsonl"
        wiki_path = gen_dir / "wiki_sections.jsonl"

        sources_with_sec: set[str] = set()
        raw_l2: list[dict] = []
        if sec_path.is_file():
            for line in sec_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                src = row.get("source") or ""
                if self._excluded(src):
                    continue
                sources_with_sec.add(src)
                raw_l2.append({
                    "kind": "section",
                    "source": src,
                    "heading": row.get("heading_path") or row.get("heading") or "",
                    "table_header": row.get("table_header") or "",
                    "line_start": row.get("line_start"),
                    "line_end": row.get("line_end"),
                    "page_num": None,
                    "mirror": row.get("mirror") or "",
                    "text": row.get("text") or "",
                })

        if page_path.is_file():
            for line in page_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                src = row.get("source") or ""
                if self._excluded(src) or src in sources_with_sec:
                    continue
                txt = row.get("text") or ""
                if not txt.strip():
                    continue
                raw_l2.append({
                    "kind": "page",
                    "source": src,
                    "heading": row.get("page_label") or "",
                    "table_header": "",
                    "line_start": None,
                    "line_end": None,
                    "page_num": row.get("page_num"),
                    "mirror": row.get("mirror") or "",
                    "text": txt,
                })

        src_sets: dict[str, set[str]] = defaultdict(set)
        all_srcs: set[str] = set()
        for idx, u in enumerate(raw_l2):
            src = u["source"]
            all_srcs.add(src)
            btoks = tokenize(u["text"])
            htoks = tokenize(u["heading"])
            ftoks = _source_filename_tokens(src)
            tf: dict[str, float] = defaultdict(float)
            for t in btoks:
                tf[t] += 1.0
            for t in htoks:
                tf[t] += self.HEADING_BOOST
            for t in ftoks:
                tf[t] += self.FILENAME_BOOST
            dl = max(1, len(btoks) + len(htoks))
            self.units.append(u)
            self.lengths.append(dl)
            self.body_tok_sets.append(set(btoks) | set(htoks))
            for tok, freq in tf.items():
                self.postings[tok][idx] = freq
                src_sets[tok].add(src)

        self.n_sources = len(all_srcs)
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0
        self.df_sources = {t: len(s) for t, s in src_sets.items()}

        if wiki_path.is_file():
            w_src_sets: dict[str, set[str]] = defaultdict(set)
            all_w_srcs: set[str] = set()
            for line in wiki_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                wsrc = row.get("source") or ""
                if self._excluded(wsrc):
                    continue
                widx = len(self.wiki_units)
                all_w_srcs.add(wsrc)
                btoks = tokenize(row.get("text") or "")
                htoks = tokenize(f"{row.get('title') or ''} {row.get('heading') or ''}")
                tf_w: dict[str, float] = defaultdict(float)
                for t in btoks:
                    tf_w[t] += 1.0
                for t in htoks:
                    tf_w[t] += self.HEADING_BOOST
                dl_w = max(1, len(btoks) + len(htoks))
                self.wiki_units.append(row)
                self.wiki_lengths.append(dl_w)
                for tok, freq in tf_w.items():
                    self.wiki_postings[tok][widx] = freq
                    w_src_sets[tok].add(wsrc)
            self.wiki_n_sources = len(all_w_srcs)
            self.wiki_avg_len = (sum(self.wiki_lengths) / len(self.wiki_lengths)) if self.wiki_lengths else 1.0
            self.wiki_df_sources = {t: len(s) for t, s in w_src_sets.items()}

        if not self.units and not self.wiki_units:
            raise IndexUnavailableError("Index is empty; run `wctx sync` or `wctx index` first")

    # -- Public Search APIs ------------------------------------------------- #

    def search(self, query: str, *, k: int = 8, kind: str | None = None, max_per_source: int = 2) -> list[dict]:
        if kind == "wiki":
            return self.search_wiki(query, k=k, max_per_source=max_per_source)
        q_toks, groups = tokenize_query(query)
        if not q_toks:
            return []
        q_counts = {t: float(c) for t, c in Counter(q_toks).items()}
        return self.search_weighted(q_counts, groups, k=k, kind=kind, max_per_source=max_per_source)

    def search_weighted(
        self,
        q_counts: dict[str, float],
        groups: list[set[str]],
        *,
        k: int = 8,
        kind: str | None = None,
        max_per_source: int = 2,
    ) -> list[dict]:
        if not q_counts or k <= 0:
            return []
        if self.backend_name == "sqlite":
            return self._search_l2_sqlite(q_counts, groups, k=k, kind=kind, max_per_source=max_per_source)
        return self._search_l2_memory(q_counts, groups, k=k, kind=kind, max_per_source=max_per_source)

    def _search_l2_memory(
        self,
        q_counts: dict[str, float],
        groups: list[set[str]],
        *,
        k: int,
        kind: str | None,
        max_per_source: int,
    ) -> list[dict]:
        if not self.units:
            return []
        n = self.n_sources or len(self.units)
        q_idf: dict[str, float] = {}
        q_df: dict[str, int] = {}
        for tok in q_counts:
            df = self.df_sources.get(tok, 0)
            if df > 0:
                q_df[tok] = df
                q_idf[tok] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        if not q_idf:
            return []

        dist_groups, dist_tokens = _compute_distinctive_groups(groups, q_idf, q_df, n, self.COVERAGE_IDF_RATIO)
        scores: dict[int, float] = defaultdict(float)
        covered: dict[int, set[str]] = defaultdict(set)

        for tok, qw in q_counts.items():
            idf = q_idf.get(tok)
            if idf is None:
                continue
            is_dist = tok in dist_tokens
            for idx, tf in self.postings.get(tok, {}).items():
                if kind and self.units[idx].get("kind") != kind:
                    continue
                dl = self.lengths[idx] or 1
                denom = tf + self.K1 * (1 - self.B + self.B * dl / (self.avg_len or 1.0))
                scores[idx] += qw * idf * (tf * (self.K1 + 1)) / denom
                if is_dist and tok in self.body_tok_sets[idx]:
                    covered[idx].add(tok)

        for idx in list(scores):
            cov = covered.get(idx, ())
            if dist_groups and cov:
                hit_g = sum(1 for g in dist_groups if g & cov)
                if hit_g:
                    scores[idx] *= 1 + self.COVERAGE_ALPHA * (hit_g / len(dist_groups))

        top_pairs = heapq.nlargest(max(k * 6, 40), scores.items(), key=lambda kv: kv[1])
        scored_meta = [(idx, sc, self.units[idx]) for idx, sc in top_pairs]
        picked = _dedup_per_source(scored_meta, k=k, max_per_source=max_per_source)
        qt_set = set(q_counts)
        out: list[dict] = []
        for idx, sc, meta in picked:
            u = dict(meta)
            u["score"] = round(sc, 4)
            u["matched"] = sorted(t for t in qt_set if idx in self.postings.get(t, ()))[:12]
            u["_q_idf"] = q_idf
            out.append(u)
        return out

    def _search_l2_sqlite(
        self,
        q_counts: dict[str, float],
        groups: list[set[str]],
        *,
        k: int,
        kind: str | None,
        max_per_source: int,
    ) -> list[dict]:
        con = self._con
        assert con is not None
        meta_map = dict(con.execute("SELECT key, value FROM meta").fetchall())
        n_units = int(meta_map.get("n_units", "0"))
        if n_units <= 0:
            return []
        n = int(meta_map.get("n_sources", "0")) or n_units
        avg_len = float(meta_map.get("avg_len", "1.0")) or 1.0

        unique_q = list(q_counts)
        ph = ",".join("?" for _ in unique_q)
        rows = con.execute(f"SELECT tok, df_sources FROM vocab WHERE tok IN ({ph})", unique_q).fetchall()
        if not rows:
            return []

        q_idf: dict[str, float] = {}
        q_df: dict[str, int] = {}
        for tok, df in rows:
            if df > 0:
                q_df[tok] = df
                q_idf[tok] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        if not q_idf:
            return []

        dist_groups, dist_tokens = _compute_distinctive_groups(groups, q_idf, q_df, n, self.COVERAGE_IDF_RATIO)
        probe_toks = list(dist_tokens) if dist_tokens else list(q_idf)
        or_expr = " OR ".join(encode_fts_token(t) for t in probe_toks)
        cand_set: set[int] = {
            rid for (rid,) in con.execute(
                "SELECT rowid FROM fts_units WHERE fts_units MATCH ? ORDER BY bm25(fts_units, 3.0, 1.0) LIMIT 800",
                (or_expr,),
            )
        }
        if len(dist_groups) >= 2:
            and_clauses = ["(" + " OR ".join(encode_fts_token(t) for t in g if t in q_idf) + ")" for g in dist_groups]
            and_clauses = [c for c in and_clauses if c != "()"]
            if len(and_clauses) >= 2:
                and_expr = " AND ".join(and_clauses)
                cand_set.update(
                    rid for (rid,) in con.execute(
                        "SELECT rowid FROM fts_units WHERE fts_units MATCH ? ORDER BY bm25(fts_units, 3.0, 1.0) LIMIT 400",
                        (and_expr,),
                    )
                )
        if not cand_set:
            all_or = " OR ".join(encode_fts_token(t) for t in q_idf)
            cand_set.update(
                rid for (rid,) in con.execute(
                    "SELECT rowid FROM fts_units WHERE fts_units MATCH ? ORDER BY bm25(fts_units, 3.0, 1.0) LIMIT 500",
                    (all_or,),
                )
            )
        if not cand_set:
            return []

        c_ids = list(cand_set)
        c_ph = ",".join("?" for _ in c_ids)
        u_rows = con.execute(
            f"SELECT id, kind, source, heading, table_header, line_start, line_end, page_num, mirror, dl, tf_z, body_toks_z "
            f"FROM units WHERE id IN ({c_ph})",
            c_ids,
        ).fetchall()

        scored_meta: list[tuple[int, float, dict]] = []
        matched_map: dict[int, list[str]] = {}
        for uid, ukind, source, heading, table_header, ls, le, pnum, mirror, dl, tf_z, btoks_z in u_rows:
            if kind and ukind != kind:
                continue
            if self._excluded(source):
                continue
            tf_map: dict[str, float] = json.loads(zlib.decompress(tf_z).decode("utf-8"))
            body_set = set(json.loads(zlib.decompress(btoks_z).decode("utf-8")))
            sc = 0.0
            cov_toks: set[str] = set()
            matched_toks: list[str] = []
            norm_base = self.K1 * (1 - self.B + self.B * (dl or 1) / avg_len)
            for tok, qw in q_counts.items():
                tf = tf_map.get(tok)
                if not tf:
                    continue
                idf = q_idf.get(tok)
                if idf is None:
                    continue
                sc += qw * idf * (tf * (self.K1 + 1)) / (tf + norm_base)
                matched_toks.append(tok)
                if tok in dist_tokens and tok in body_set:
                    cov_toks.add(tok)
            if sc <= 0.0:
                continue
            if dist_groups and cov_toks:
                hit_g = sum(1 for g in dist_groups if g & cov_toks)
                if hit_g:
                    sc *= 1 + self.COVERAGE_ALPHA * (hit_g / len(dist_groups))
            scored_meta.append((uid, sc, {
                "kind": ukind,
                "source": source,
                "heading": heading,
                "table_header": table_header or "",
                "line_start": ls,
                "line_end": le,
                "page_num": pnum,
                "mirror": mirror,
            }))
            matched_map[uid] = sorted(matched_toks)[:12]

        scored_meta.sort(key=lambda x: x[1], reverse=True)
        picked = _dedup_per_source(scored_meta, k=k, max_per_source=max_per_source)
        if not picked:
            return []

        win_ids = [uid for uid, _, _ in picked]
        w_ph = ",".join("?" for _ in win_ids)
        text_map = {
            uid: zlib.decompress(tz).decode("utf-8")
            for uid, tz in con.execute(f"SELECT id, text_z FROM units WHERE id IN ({w_ph})", win_ids)
        }
        out: list[dict] = []
        for uid, sc, meta in picked:
            u = dict(meta)
            u["text"] = text_map.get(uid, "")
            u["score"] = round(sc, 4)
            u["matched"] = matched_map.get(uid, [])
            u["_q_idf"] = q_idf
            out.append(u)
        return out

    # -- Layer-3 Wiki Search ------------------------------------------------ #

    def search_wiki(self, query: str, *, k: int = 2, max_per_source: int = 1) -> list[dict]:
        q_toks, groups = tokenize_query(query)
        if not q_toks:
            return []
        q_counts = {t: float(c) for t, c in Counter(q_toks).items()}
        return self.search_wiki_weighted(q_counts, groups, k=k, max_per_source=max_per_source)

    def search_wiki_weighted(
        self,
        q_counts: dict[str, float],
        groups: list[set[str]],
        *,
        k: int = 2,
        max_per_source: int = 1,
    ) -> list[dict]:
        if not q_counts or k <= 0:
            return []
        if self.backend_name == "sqlite":
            return self._search_wiki_sqlite(q_counts, groups, k=k, max_per_source=max_per_source)
        return self._search_wiki_memory(q_counts, groups, k=k, max_per_source=max_per_source)

    def _search_wiki_memory(
        self, q_counts: dict[str, float], groups: list[set[str]], *, k: int, max_per_source: int
    ) -> list[dict]:
        if not self.wiki_units:
            return []
        n = self.wiki_n_sources or len(self.wiki_units)
        q_idf: dict[str, float] = {}
        q_df: dict[str, int] = {}
        for tok in q_counts:
            df = self.wiki_df_sources.get(tok, 0)
            if df > 0:
                q_df[tok] = df
                q_idf[tok] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        if not q_idf:
            return []

        dist_groups, dist_tokens = _compute_distinctive_groups(groups, q_idf, q_df, n, self.COVERAGE_IDF_RATIO)
        scores: dict[int, float] = defaultdict(float)
        covered: dict[int, set[str]] = defaultdict(set)
        for tok, qw in q_counts.items():
            idf = q_idf.get(tok)
            if idf is None:
                continue
            is_dist = tok in dist_tokens
            for idx, tf in self.wiki_postings.get(tok, {}).items():
                dl = self.wiki_lengths[idx] or 1
                denom = tf + self.K1 * (1 - self.B + self.B * dl / (self.wiki_avg_len or 1.0))
                scores[idx] += qw * idf * (tf * (self.K1 + 1)) / denom
                if is_dist:
                    covered[idx].add(tok)

        for idx in list(scores):
            cov = covered.get(idx, ())
            if dist_groups and cov:
                hit_g = sum(1 for g in dist_groups if g & cov)
                if hit_g:
                    scores[idx] *= 1 + self.COVERAGE_ALPHA * (hit_g / len(dist_groups))
            if self.wiki_units[idx].get("freshness") in {"stale", "missing"}:
                scores[idx] *= 0.5
            if _is_wiki_footer_heading(self.wiki_units[idx].get("heading") or ""):
                scores[idx] *= 0.30

        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        picked = _dedup_per_source([(i, s, self.wiki_units[i]) for i, s in ordered], k=k, max_per_source=max_per_source)
        qt_set = set(q_counts)
        out: list[dict] = []
        for idx, sc, meta in picked:
            u = dict(meta)
            u["kind"] = "wiki"
            u["score"] = round(sc, 4)
            u["matched"] = sorted(t for t in qt_set if idx in self.wiki_postings.get(t, ()))[:12]
            u["_q_idf"] = q_idf
            out.append(u)
        return out

    def _search_wiki_sqlite(
        self, q_counts: dict[str, float], groups: list[set[str]], *, k: int, max_per_source: int
    ) -> list[dict]:
        con = self._con
        assert con is not None
        meta_map = dict(con.execute("SELECT key, value FROM wiki_meta").fetchall())
        n_units = int(meta_map.get("n_units", "0"))
        if n_units <= 0:
            return []
        n = int(meta_map.get("n_sources", "0")) or n_units
        avg_len = float(meta_map.get("avg_len", "1.0")) or 1.0

        unique_q = list(q_counts)
        ph = ",".join("?" for _ in unique_q)
        rows = con.execute(f"SELECT tok, df_sources FROM wiki_vocab WHERE tok IN ({ph})", unique_q).fetchall()
        if not rows:
            return []

        q_idf: dict[str, float] = {}
        q_df: dict[str, int] = {}
        for tok, df in rows:
            if df > 0:
                q_df[tok] = df
                q_idf[tok] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        if not q_idf:
            return []

        dist_groups, dist_tokens = _compute_distinctive_groups(groups, q_idf, q_df, n, self.COVERAGE_IDF_RATIO)
        or_expr = " OR ".join(encode_fts_token(t) for t in q_idf)
        cand_ids = [
            rid for (rid,) in con.execute(
                "SELECT rowid FROM fts_wiki WHERE fts_wiki MATCH ? ORDER BY bm25(fts_wiki) LIMIT 200",
                (or_expr,),
            )
        ]
        if not cand_ids:
            return []

        c_ph = ",".join("?" for _ in cand_ids)
        w_rows = con.execute(
            f"SELECT id, source, title, heading, table_header, line_start, line_end, freshness, sources_json, dl, tf_z "
            f"FROM wiki_units WHERE id IN ({c_ph})",
            cand_ids,
        ).fetchall()

        scored_meta: list[tuple[int, float, dict]] = []
        matched_map: dict[int, list[str]] = {}
        for uid, source, title, heading, table_header, ls, le, freshness, sources_json, dl, tf_z in w_rows:
            srcs = json.loads(sources_json) if sources_json else []
            if self._excluded(source) or any(self._excluded(s) for s in srcs if isinstance(s, str)):
                continue
            tf_map: dict[str, float] = json.loads(zlib.decompress(tf_z).decode("utf-8"))
            sc = 0.0
            cov_toks: set[str] = set()
            matched_toks: list[str] = []
            norm_base = self.K1 * (1 - self.B + self.B * (dl or 1) / avg_len)
            for tok, qw in q_counts.items():
                tf = tf_map.get(tok)
                if not tf:
                    continue
                idf = q_idf.get(tok)
                if idf is None:
                    continue
                sc += qw * idf * (tf * (self.K1 + 1)) / (tf + norm_base)
                matched_toks.append(tok)
                if tok in dist_tokens:
                    cov_toks.add(tok)
            if sc <= 0.0:
                continue
            if dist_groups and cov_toks:
                hit_g = sum(1 for g in dist_groups if g & cov_toks)
                if hit_g:
                    sc *= 1 + self.COVERAGE_ALPHA * (hit_g / len(dist_groups))
            if freshness in {"stale", "missing"}:
                sc *= 0.5
            if _is_wiki_footer_heading(heading or ""):
                sc *= 0.30
            scored_meta.append((uid, sc, {
                "kind": "wiki",
                "source": source,
                "title": title,
                "heading": heading,
                "table_header": table_header or "",
                "line_start": ls,
                "line_end": le,
                "freshness": freshness,
                "sources": [s for s in srcs if isinstance(s, str)],
            }))
            matched_map[uid] = sorted(matched_toks)[:12]

        scored_meta.sort(key=lambda x: x[1], reverse=True)
        picked = _dedup_per_source(scored_meta, k=k, max_per_source=max_per_source)
        if not picked:
            return []
        win_ids = [uid for uid, _, _ in picked]
        w_ph = ",".join("?" for _ in win_ids)
        text_map = {
            uid: zlib.decompress(tz).decode("utf-8")
            for uid, tz in con.execute(f"SELECT id, text_z FROM wiki_units WHERE id IN ({w_ph})", win_ids)
        }
        out: list[dict] = []
        for uid, sc, meta in picked:
            u = dict(meta)
            u["text"] = text_map.get(uid, "")
            u["score"] = round(sc, 4)
            u["matched"] = matched_map.get(uid, [])
            u["_q_idf"] = q_idf
            out.append(u)
        return out

    def score_source_units(
        self,
        source: str,
        q_counts: dict[str, float],
        groups: list[set[str]],
        *,
        top_n: int = 1,
    ) -> list[dict]:
        """Targeted scoring of units within a single ``source`` (used for 1-hop Wiki provenance)."""
        if not source or not q_counts or self._excluded(source):
            return []
        if self.backend_name == "sqlite":
            con = self._con
            assert con is not None
            meta_map = dict(con.execute("SELECT key, value FROM meta").fetchall())
            n = int(meta_map.get("n_sources", "0")) or 1
            avg_len = float(meta_map.get("avg_len", "1.0")) or 1.0
            unique_q = list(q_counts)
            ph = ",".join("?" for _ in unique_q)
            v_rows = con.execute(f"SELECT tok, df_sources FROM vocab WHERE tok IN ({ph})", unique_q).fetchall()
            q_idf = {t: math.log(1 + (n - df + 0.5) / (df + 0.5)) for t, df in v_rows if df > 0}
            q_df = {t: df for t, df in v_rows if df > 0}
            if not q_idf:
                return []
            dist_groups, dist_tokens = _compute_distinctive_groups(groups, q_idf, q_df, n, self.COVERAGE_IDF_RATIO)
            u_rows = con.execute(
                "SELECT id, kind, source, heading, table_header, line_start, line_end, page_num, mirror, dl, tf_z, body_toks_z, text_z "
                "FROM units WHERE source = ?",
                (source,),
            ).fetchall()
            candidates: list[tuple[float, dict]] = []
            for uid, ukind, src, heading, table_header, ls, le, pnum, mirror, dl, tf_z, btoks_z, text_z in u_rows:
                tf_map: dict[str, float] = json.loads(zlib.decompress(tf_z).decode("utf-8"))
                body_set = set(json.loads(zlib.decompress(btoks_z).decode("utf-8")))
                sc = 0.0
                cov: set[str] = set()
                matched: list[str] = []
                norm_base = self.K1 * (1 - self.B + self.B * (dl or 1) / avg_len)
                for tok, qw in q_counts.items():
                    if tok not in body_set:
                        continue
                    tf = tf_map.get(tok, 0.0)
                    idf = q_idf.get(tok)
                    if tf > 0 and idf is not None:
                        sc += qw * idf * (tf * (self.K1 + 1)) / (tf + norm_base)
                        matched.append(tok)
                        if tok in dist_tokens:
                            cov.add(tok)
                if sc <= 0.0:
                    continue
                if dist_groups and cov:
                    hit_g = sum(1 for g in dist_groups if g & cov)
                    if hit_g:
                        sc *= 1 + self.COVERAGE_ALPHA * (hit_g / len(dist_groups))
                candidates.append((sc, {
                    "kind": ukind,
                    "source": src,
                    "heading": heading,
                    "table_header": table_header or "",
                    "line_start": ls,
                    "line_end": le,
                    "page_num": pnum,
                    "mirror": mirror,
                    "text": zlib.decompress(text_z).decode("utf-8"),
                    "score": round(sc, 4),
                    "matched": sorted(matched)[:12],
                    "_q_idf": q_idf,
                }))
            candidates.sort(key=lambda x: x[0], reverse=True)
            return [c[1] for c in candidates[:top_n]]

        # Memory fallback
        n = self.n_sources or max(1, len(self.units))
        q_idf = {
            t: math.log(1 + (n - self.df_sources[t] + 0.5) / (self.df_sources[t] + 0.5))
            for t in q_counts
            if self.df_sources.get(t, 0) > 0
        }
        if not q_idf:
            return []
        scored: list[tuple[float, dict]] = []
        for idx, u in enumerate(self.units):
            if u.get("source") != source:
                continue
            sc = 0.0
            matched = []
            dl = self.lengths[idx] or 1
            norm_base = self.K1 * (1 - self.B + self.B * dl / (self.avg_len or 1.0))
            for tok, qw in q_counts.items():
                if tok not in self.body_tok_sets[idx]:
                    continue
                tf = self.postings.get(tok, {}).get(idx, 0.0)
                idf = q_idf.get(tok)
                if tf > 0 and idf is not None:
                    sc += qw * idf * (tf * (self.K1 + 1)) / (tf + norm_base)
                    matched.append(tok)
            if sc > 0:
                copy_u = dict(u)
                copy_u["score"] = round(sc, 4)
                copy_u["matched"] = sorted(matched)[:12]
                copy_u["_q_idf"] = q_idf
                scored.append((sc, copy_u))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in scored[:top_n]]


# -- Bilingual Query Complexity Router -------------------------------------- #

COMPLEX_PATTERNS: list[tuple[str, str]] = [
    (r"对比|比较|差异|区别(?!是什么)|相比|不一致|哪个更好|优劣|\b(?:compare|comparison|versus|vs\.?|difference\s+between|trade-?offs?)\b", "compare"),
    (
        r"梳理|汇总|总结|归纳|盘点|罗列|列一下|列出全部|讲解|介绍|概述|全貌|来龙去脉|整体方案|全局|背景[与和及]需求|(?:详细|全面|系统地?)说明|"
        r"\b(?:summarize|summary|overview|walk\s+me\s+through|explain\s+the\s+overall|list\s+all|audit|architecture\s+of)\b",
        "aggregate",
    ),
    (r"为什么|原因|根因|排查|诊断|如何设计|设计方案|怎么处理|如何处理|\b(?:why\s+did|root\s+cause|troubleshoot|diagnose|how\s+to\s+design)\b", "reasoning"),
    (r"规划|路线图|演进|实施计划|怎么落地|\b(?:roadmap|migration\s+plan|rollout)\b", "planning"),
    (r"评估|评审|风险|可行性|论证|\b(?:evaluate|feasibility|risk\s+assessment)\b", "evaluation"),
    (r"分别|各自|两者|三者|多个|各方面|\b(?:respectively|both\s+.*\s+and)\b", "multi_entity"),
]

FACT_PATTERNS: list[tuple[str, str]] = [
    (r"是什么|什么是|含义|定义|概念|原理|\b(?:what\s+is|definition\s+of|meaning\s+of)\b", "definition"),
    (r"多少|多大|几[个种项]|数值|阈值|范围|取值|标准值|限值|\b(?:how\s+many|how\s+much|threshold|limit|timeout|range|default\s+value)\b", "value"),
    (r"哪个|哪些|哪几|何时|什么时候|多久|周期|\b(?:which|when|how\s+often)\b", "enumeration"),
    (r"是否|能不能|可不可以|有没有|是不是|\b(?:is\s+it|can\s+we|does\s+it|whether)\b", "verification"),
    (r"标准|规范|条款|依据|出处|规定|\b(?:standard|specification|clause|rfc)\b", "standard"),
]

MULTI_Q_RE = re.compile(r"[？?].*[？?]|并且|同时|顺便|另外|以及|\b(?:and\s+also|in\s+addition)\b", re.I)
LENGTH_COMPLEX_ZH = 60
LENGTH_COMPLEX_EN = 160


def route(question: str) -> dict:
    q = (question or "").strip()
    if not q:
        return {"mode": "fact", "reason": ["empty_query"], "confidence": 0.0, "complex_hits": [], "fact_hits": []}

    complex_hits = [tag for pat, tag in COMPLEX_PATTERNS if re.search(pat, q, re.I)]
    fact_hits = [tag for pat, tag in FACT_PATTERNS if re.search(pat, q, re.I)]
    reasons: list[str] = []
    if complex_hits:
        reasons.append("complex:" + ",".join(complex_hits))
    if fact_hits:
        reasons.append("fact:" + ",".join(fact_hits))
    if MULTI_Q_RE.search(q):
        reasons.append("multi_question")
        if "multi_entity" not in complex_hits:
            complex_hits.append("multi_question")

    cjk_count = sum(1 for ch in q if "\u4e00" <= ch <= "\u9fff")
    len_limit = LENGTH_COMPLEX_ZH if cjk_count >= 4 else LENGTH_COMPLEX_EN
    if len(q) > len_limit:
        reasons.append(f"len>{len_limit}")

    pure_definition = fact_hits == ["definition"] and not complex_hits
    if complex_hits and not pure_definition:
        mode = "complex"
        conf = min(1.0, 0.5 + 0.15 * len(complex_hits))
    elif fact_hits:
        mode = "fact"
        conf = min(1.0, 0.5 + 0.15 * len(fact_hits))
    else:
        mode = "complex" if len(q) > len_limit else "fact"
        conf = 0.35
        reasons.append("length_fallback")

    return {
        "mode": mode,
        "reason": reasons,
        "confidence": round(conf, 2),
        "complex_hits": complex_hits,
        "fact_hits": fact_hits,
    }
