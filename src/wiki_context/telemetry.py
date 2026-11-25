"""Cross-process low-overhead SQLite WAL telemetry & missing-concept radar (`telemetry.sqlite`).

Design:
- Long-lived HTTP/Console servers enqueue invocation events via a lock-free `SimpleQueue`
  and commit batches in a single background writer thread (`< 0.01 ms` hot-path enqueue).
- Short-lived `wctx serve` (`stdio`) child processes append directly with `WAL + synchronous=NORMAL +
  busy_timeout=3000` so every invocation is visible immediately to the live SSE `/api/logs/stream`.
- Shared telemetry NEVER persists ACL-denied document paths; only permitted hit summaries and
  the numeric `acl_filtered_count` are recorded, and `scrub_telemetry_text` redacts `wctx_*` /
  `wctx1_*` / API secrets before insertion.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

MAX_TELEMETRY_ROWS = 10000
MAX_TELEMETRY_AGE_DAYS = 30
QUEUE_MAX_SIZE = 4096
BATCH_FLUSH_SIZE = 32
BATCH_FLUSH_INTERVAL_SEC = 0.10

SECRET_PATTERNS = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"\bwctx1_[A-Za-z0-9_-]{12,}\b"),
        "[REDACTED_PAIR_CODE]",
    ),
    (
        re.compile(r"\bwctx_[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED_WCTX_TOKEN]",
    ),
    (
        re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+|bearer\s+)[A-Za-z0-9._\-]{12,}\b"),
        r"\1[REDACTED_TOKEN]",
    ),
    (
        re.compile(r"(?i)\b(cookie\s*:\s*)[^\r\n]{8,}"),
        r"\1[REDACTED_COOKIE]",
    ),
    (
        # Aliyun AccessKey ID
        re.compile(r"\bLTAI[A-Za-z0-9]{12,30}\b"),
        "[REDACTED_ACCESS_KEY_ID]",
    ),
    (
        # Labeled credentials: `密码：xxx`, `AccessKey Secret: xxx`, `password=xxx`, `token: xxx` ...
        re.compile(
            r"(?i)((?:登录密码|密码|口令|私钥|助记词|password|passwd|pwd|passphrase|"
            r"access\s*key\s*secret|secret\s*(?:access\s*)?key|client[_\s-]*secret|app[_\s-]*secret|"
            r"api[_\s-]*key|auth[_\s-]*key|access[_\s-]*token|refresh[_\s-]*token|token)"
            r"[\s*_`]*[:：=][\s*_`]*)([^\s|`*\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]{4,})"
        ),
        r"\1[REDACTED_SECRET]",
    ),
    (
        # Host credential pair: `1.2.3.4：ubuntu / p@ss`, `host.example.com: root/p@ss`
        re.compile(
            r"((?:\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,})"
            r"[\]\)]?[^\n|]{0,40}?[:：]\s*`?[A-Za-z_][\w.@-]{0,31}`?\s*/\s*)(?!\s)([^\s|`\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]{4,})"
        ),
        r"\1[REDACTED_SECRET]",
    ),
    (
        # Chinese label separated only by whitespace: `密码 abcd1234`, `口令 p@ss`
        re.compile(r"((?:登录密码|密码|口令)[ \t]+)([!-~]{4,})"),
        r"\1[REDACTED_SECRET]",
    ),
    (
        # Unlabeled but strongly password-shaped value after a colon: `sats量化： ^1234Qwer$`
        re.compile(
            r"([:：][ \t]*)"
            r"(?=[^\s]*[A-Za-z])(?=[^\s]*\d)(?=[^\s]*[!@#$%^~])"
            r"([^\s|`./:&=*+\\\u3000-\u303f\u4e00-\u9fff\uff00-\uffef()\[\]{}<>\"',;]{8,64})(?=\s|$)"
        ),
        r"\1[REDACTED_SECRET]",
    ),
    (
        # Env-style labels: `API_V3_KEY: xxx`, `DB_PASSWORD=xxx`, `GITHUB_TOKEN=xxx`
        re.compile(
            r"(\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD)\b\s*[:：=]\s*)"
            r"([^\s|`\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]{4,})",
            re.I,
        ),
        r"\1[REDACTED_SECRET]",
    ),
    (
        # Whitespace host credential triple: `1.2.3.4 root p@ss`, `[1.2.3.4](http://…) ubuntu p@ss`
        re.compile(
            r"(\b(?:\d{1,3}\.){3}\d{1,3}\S*\s+(?:root|ubuntu|admin|administrator|user|pi|ec2-user|centos|debian|oracle)\s+)"
            r"([^\s|`\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]{4,})"
        ),
        r"\1[REDACTED_SECRET]",
    ),
    (
        # Account + password-shaped token: `13800000000 p@ss1234`, `[a@b.com](mailto:a@b.com) Pass1234`
        re.compile(
            r"((?:\b1[3-9]\d{9}\b|[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\)?)[ \t]+)"
            r"(?![^\s]*@[\w-]+\.[A-Za-z])(?![A-Za-z][\w+.-]*://)"
            r"(?=[^\s]*(?:[!@#$%^&*~+=?]|\d[^\s]*[A-Za-z]|[A-Za-z][^\s]*\d))"
            r"([^\s|`\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]{6,})"
        ),
        r"\1[REDACTED_SECRET]",
    ),
)


def apply_secret_scrub(text: str) -> str:
    if not text:
        return ""
    out = str(text)
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def scrub_telemetry_text(text: str) -> str:
    return apply_secret_scrub(text)


def is_telemetry_enabled() -> bool:
    return os.environ.get("WCTX_TELEMETRY", "1").strip().lower() not in {"0", "false", "no"}


def is_telemetry_query_logging_enabled() -> bool:
    return os.environ.get("WCTX_TELEMETRY_LOG_QUERIES", "1").strip().lower() not in {"0", "false", "no"}


def default_telemetry_db_path(config_dir: Path | str | None = None) -> Path:
    base = config_dir or os.environ.get("WCTX_HOME") or (Path.home() / ".wiki-context")
    return Path(base).expanduser().resolve() / "telemetry.sqlite"


def _ensure_private_file(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        if path.exists():
            os.chmod(path, 0o600)
    except OSError:
        pass


def _open_wal_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists() and os.name != "nt":
        try:
            fd = os.open(str(db_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        except OSError:
            pass
    con = sqlite3.connect(str(db_path), timeout=3.0, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA wal_autocheckpoint=1000")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ts_epoch REAL NOT NULL,
            client TEXT NOT NULL,
            mode TEXT NOT NULL,
            principal_uid TEXT NOT NULL,
            vault_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            query_redacted TEXT NOT NULL,
            route TEXT NOT NULL,
            elapsed_ms REAL NOT NULL,
            wiki_count INTEGER NOT NULL,
            primary_count INTEGER NOT NULL,
            memory_count INTEGER NOT NULL,
            acl_filtered_count INTEGER NOT NULL,
            missing_concepts_json TEXT NOT NULL,
            hit_sources_json TEXT NOT NULL,
            is_error INTEGER NOT NULL,
            error_text TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_inv_ts ON mcp_invocations(ts_epoch DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_inv_vault ON mcp_invocations(vault_id, id DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_inv_client ON mcp_invocations(client, id DESC)")
    existing_cols = {str(r[1]) for r in con.execute("PRAGMA table_info(mcp_invocations)").fetchall()}
    if "focus_terms_json" not in existing_cols:
        con.execute("ALTER TABLE mcp_invocations ADD COLUMN focus_terms_json TEXT NOT NULL DEFAULT '[]'")
    if "expanded_terms_json" not in existing_cols:
        con.execute("ALTER TABLE mcp_invocations ADD COLUMN expanded_terms_json TEXT NOT NULL DEFAULT '[]'")
    con.commit()
    _ensure_private_file(db_path)
    return con


_INSERT_SQL = """
INSERT INTO mcp_invocations (
    ts, ts_epoch, client, mode, principal_uid, vault_id, tool,
    query_redacted, route, elapsed_ms,
    wiki_count, primary_count, memory_count, acl_filtered_count,
    missing_concepts_json, hit_sources_json, is_error, error_text,
    focus_terms_json, expanded_terms_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class TelemetryStore:
    """SQLite WAL invocation telemetry store supporting async single-writer queue or direct append."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        async_writer: bool = False,
        enabled: bool = True,
    ):
        self.enabled = bool(enabled)
        self.db_path = Path(db_path).expanduser().resolve() if db_path else default_telemetry_db_path()
        self.async_writer = async_writer
        self._closed = False
        self._write_lock = threading.Lock()
        self._write_count = 0
        self._direct_con: sqlite3.Connection | None = None
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._writer_thread: threading.Thread | None = None

        if not self.enabled:
            return

        # Ensure schema exists upfront
        init_con = _open_wal_connection(self.db_path)
        init_con.close()

        if self.async_writer:
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                name="wctx-telemetry-writer",
                daemon=True,
            )
            self._writer_thread.start()

    def _build_row(self, event: dict[str, Any]) -> tuple[Any, ...]:
        now = time.time()
        ts = str(event.get("ts") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ts_epoch = float(event.get("ts_epoch") or now)
        client = str(event.get("client") or "unknown")[:48]
        mode = str(event.get("mode") or "stdio")[:24]
        principal_uid = str(event.get("principal_uid") or "local")[:64]
        vault_id = str(event.get("vault_id") or "")[:64]
        tool = str(event.get("tool") or "wiki_context")[:48]
        if event.get("omit_query") or not is_telemetry_query_logging_enabled():
            query_redacted = ""
        else:
            query_redacted = scrub_telemetry_text(str(event.get("query") or ""))[:400]
        route = str(event.get("route") or "")[:24]
        elapsed_ms = round(float(event.get("elapsed_ms") or 0.0), 2)
        wiki_count = int(event.get("wiki_count") or 0)
        primary_count = int(event.get("primary_count") or 0)
        memory_count = int(event.get("memory_count") or 0)
        acl_filtered_count = int(event.get("acl_filtered_count") or 0)
        missing = [str(x)[:80] for x in (event.get("missing_concepts") or [])[:16]]
        hits = [str(x)[:200] for x in (event.get("hit_sources") or [])[:20]]
        focus_terms = (
            []
            if (event.get("omit_query") or not is_telemetry_query_logging_enabled())
            else [scrub_telemetry_text(str(x))[:80] for x in (event.get("focus_terms") or ()) if str(x).strip()][:8]
        )
        expanded_terms = (
            []
            if (event.get("omit_query") or not is_telemetry_query_logging_enabled())
            else [scrub_telemetry_text(str(x))[:80] for x in (event.get("expanded_terms") or ()) if str(x).strip()][:8]
        )
        is_error = 1 if event.get("is_error") else 0
        error_text = scrub_telemetry_text(str(event.get("error_text") or ""))[:300]
        return (
            ts,
            ts_epoch,
            client,
            mode,
            principal_uid,
            vault_id,
            tool,
            query_redacted,
            route,
            elapsed_ms,
            wiki_count,
            primary_count,
            memory_count,
            acl_filtered_count,
            json.dumps(missing, ensure_ascii=False),
            json.dumps(hits, ensure_ascii=False),
            is_error,
            error_text,
            json.dumps(focus_terms, ensure_ascii=False),
            json.dumps(expanded_terms, ensure_ascii=False),
        )

    def record(self, event: dict[str, Any]) -> None:
        if not self.enabled or self._closed:
            return
        try:
            row = self._build_row(event)
        except Exception:
            return

        if self.async_writer:
            if self._queue.qsize() >= QUEUE_MAX_SIZE:
                return  # Drop under extreme burst rather than blocking hot query path
            self._queue.put(row)
            return

        with self._write_lock:
            try:
                if self._direct_con is None:
                    self._direct_con = _open_wal_connection(self.db_path)
                self._direct_con.execute(_INSERT_SQL, row)
                self._direct_con.commit()
                self._write_count += 1
                if self._write_count % 200 == 0:
                    self._prune_locked(self._direct_con)
            except sqlite3.Error:
                pass

    def _prune_locked(self, con: sqlite3.Connection) -> None:
        try:
            cutoff = time.time() - (MAX_TELEMETRY_AGE_DAYS * 86400.0)
            con.execute("DELETE FROM mcp_invocations WHERE ts_epoch < ?", (cutoff,))
            con.execute(
                "DELETE FROM mcp_invocations WHERE id <= (SELECT COALESCE(MAX(id), 0) - ? FROM mcp_invocations)",
                (MAX_TELEMETRY_ROWS,),
            )
            con.commit()
        except sqlite3.Error:
            pass

    def _writer_loop(self) -> None:
        con: sqlite3.Connection | None = None
        batch: list[tuple[Any, ...]] = []
        last_flush = time.monotonic()
        writes_since_prune = 0
        try:
            con = _open_wal_connection(self.db_path)
            while True:
                timeout = max(0.01, BATCH_FLUSH_INTERVAL_SEC - (time.monotonic() - last_flush))
                stop_now = False
                flush_events: list[threading.Event] = []
                try:
                    item = self._queue.get(timeout=timeout)
                    if item is None:
                        stop_now = True
                    elif isinstance(item, threading.Event):
                        flush_events.append(item)
                    else:
                        batch.append(item)
                        while len(batch) < BATCH_FLUSH_SIZE:
                            try:
                                nxt = self._queue.get_nowait()
                            except queue.Empty:
                                break
                            if nxt is None:
                                stop_now = True
                                break
                            if isinstance(nxt, threading.Event):
                                flush_events.append(nxt)
                            else:
                                batch.append(nxt)
                except queue.Empty:
                    pass

                now_m = time.monotonic()
                should_commit = bool(
                    batch
                    and (
                        len(batch) >= BATCH_FLUSH_SIZE
                        or (now_m - last_flush) >= BATCH_FLUSH_INTERVAL_SEC
                        or stop_now
                        or flush_events
                    )
                )
                if should_commit:
                    try:
                        con.executemany(_INSERT_SQL, batch)
                        con.commit()
                        writes_since_prune += len(batch)
                        if writes_since_prune >= 200:
                            self._prune_locked(con)
                            writes_since_prune = 0
                    except sqlite3.Error:
                        pass
                    batch.clear()
                    last_flush = now_m

                for ev in flush_events:
                    ev.set()

                if stop_now:
                    break
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def flush(self, timeout: float = 1.5) -> None:
        if not self.enabled or not self.async_writer or self._closed:
            return
        ev = threading.Event()
        self._queue.put(ev)
        ev.wait(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.enabled:
            return
        if self.async_writer and self._writer_thread is not None:
            self._queue.put(None)
            self._writer_thread.join(timeout=1.5)
        with self._write_lock:
            if self._direct_con is not None:
                try:
                    self._direct_con.close()
                except sqlite3.Error:
                    pass
                self._direct_con = None

    @staticmethod
    def _row_to_dict(r: tuple[Any, ...]) -> dict[str, Any]:
        try:
            missing = json.loads(r[15]) if r[15] else []
        except ValueError:
            missing = []
        try:
            hits = json.loads(r[16]) if r[16] else []
        except ValueError:
            hits = []
        try:
            focus_terms = json.loads(r[19]) if (len(r) > 19 and r[19]) else []
        except ValueError:
            focus_terms = []
        try:
            expanded_terms = json.loads(r[20]) if (len(r) > 20 and r[20]) else []
        except ValueError:
            expanded_terms = []
        return {
            "id": int(r[0]),
            "ts": r[1],
            "ts_epoch": float(r[2]),
            "client": r[3],
            "mode": r[4],
            "principal_uid": r[5],
            "vault_id": r[6],
            "tool": r[7],
            "query": r[8],
            "route": r[9],
            "elapsed_ms": float(r[10]),
            "wiki_count": int(r[11]),
            "primary_count": int(r[12]),
            "memory_count": int(r[13]),
            "acl_filtered_count": int(r[14]),
            "missing_concepts": missing,
            "hit_sources": hits,
            "is_error": bool(r[17]),
            "error_text": r[18],
            "focus_terms": focus_terms,
            "expanded_terms": expanded_terms,
        }

    def _open_ro(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=3.0, check_same_thread=False)
        con.execute("PRAGMA busy_timeout=3000")
        return con

    def list_invocations(
        self,
        *,
        limit: int = 100,
        before_id: int | None = None,
        client: str = "",
        vault_id: str = "",
        tool: str = "",
        only_missing: bool = False,
        only_errors: bool = False,
        min_elapsed_ms: float = 0.0,
        query: str = "",
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"items": [], "latest_id": 0, "next_before_id": None}
        lim = max(1, min(int(limit), 500))
        clauses = ["1=1"]
        params: list[Any] = []
        if before_id is not None and before_id > 0:
            clauses.append("id < ?")
            params.append(int(before_id))
        if client.strip():
            clauses.append("client = ?")
            params.append(client.strip())
        if vault_id.strip():
            clauses.append("vault_id = ?")
            params.append(vault_id.strip())
        if tool.strip():
            clauses.append("tool = ?")
            params.append(tool.strip())
        if only_missing:
            clauses.append("missing_concepts_json != '[]'")
        if only_errors:
            clauses.append("is_error = 1")
        if min_elapsed_ms > 0:
            clauses.append("elapsed_ms >= ?")
            params.append(float(min_elapsed_ms))
        if query.strip():
            clauses.append("(query_redacted LIKE ? OR missing_concepts_json LIKE ?)")
            pat = f"%{query.strip()}%"
            params.extend([pat, pat])

        sql = (
            "SELECT id, ts, ts_epoch, client, mode, principal_uid, vault_id, tool, "
            "query_redacted, route, elapsed_ms, wiki_count, primary_count, memory_count, "
            "acl_filtered_count, missing_concepts_json, hit_sources_json, is_error, error_text, "
            "focus_terms_json, expanded_terms_json "
            f"FROM mcp_invocations WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
        )
        params.append(lim)
        con = self._open_ro()
        try:
            rows = [self._row_to_dict(r) for r in con.execute(sql, params).fetchall()]
            max_id_row = con.execute("SELECT COALESCE(MAX(id), 0) FROM mcp_invocations").fetchone()
            latest_id = int(max_id_row[0]) if max_id_row else 0
        finally:
            con.close()
        next_cursor = rows[-1]["id"] if len(rows) == lim else None
        return {"items": rows, "latest_id": latest_id, "next_before_id": next_cursor}

    def tail_after(self, after_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        con = self._open_ro()
        try:
            rows = con.execute(
                "SELECT id, ts, ts_epoch, client, mode, principal_uid, vault_id, tool, "
                "query_redacted, route, elapsed_ms, wiki_count, primary_count, memory_count, "
                "acl_filtered_count, missing_concepts_json, hit_sources_json, is_error, error_text, "
                "focus_terms_json, expanded_terms_json "
                "FROM mcp_invocations WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(after_id), max(1, min(int(limit), 200))),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            con.close()

    def latest_id(self) -> int:
        if not self.enabled:
            return 0
        con = self._open_ro()
        try:
            row = con.execute("SELECT COALESCE(MAX(id), 0) FROM mcp_invocations").fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()

    def stats(self, *, hours: int = 24) -> dict[str, Any]:
        hrs = max(1, min(int(hours), 24 * 30))
        since = time.time() - hrs * 3600.0
        if not self.enabled:
            rows = []
        else:
            con = self._open_ro()
            try:
                rows = con.execute(
                    "SELECT ts_epoch, client, mode, vault_id, tool, elapsed_ms, wiki_count, primary_count, "
                    "acl_filtered_count, missing_concepts_json, is_error "
                    "FROM mcp_invocations WHERE ts_epoch >= ? ORDER BY id DESC",
                    (since,),
                ).fetchall()
            finally:
                con.close()

        total = len(rows)
        errors = 0
        zero_hits = 0
        stdio_calls = 0
        http_calls = 0
        acl_blocked_calls = 0
        latencies: list[float] = []
        by_client: dict[str, int] = {}
        by_vault: dict[str, int] = {}
        by_tool: dict[str, int] = {}
        missing_counts: dict[str, dict[str, Any]] = {}
        buckets: dict[int, int] = {}
        buckets_stdio: dict[int, int] = {}
        buckets_http: dict[int, int] = {}
        buckets_acl: dict[int, int] = {}
        buckets_lat: dict[int, list[float]] = {}

        now_epoch = time.time()
        for ts_ep, cli, mode_s, vid, tool, el_ms, wc, pc, acl_f, m_json, is_err in rows:
            if is_err:
                errors += 1
            if int(wc) == 0 and int(pc) == 0 and tool in {"wiki_context", "wiki_search"}:
                zero_hits += 1
            is_http = str(mode_s or "").lower() in {"http", "gateway", "remote"}
            if is_http:
                http_calls += 1
            else:
                stdio_calls += 1
            if int(acl_f or 0) > 0:
                acl_blocked_calls += 1
            f_lat = float(el_ms)
            latencies.append(f_lat)
            by_client[cli] = by_client.get(cli, 0) + 1
            if vid:
                by_vault[vid] = by_vault.get(vid, 0) + 1
            by_tool[tool] = by_tool.get(tool, 0) + 1
            hr_idx = int((now_epoch - float(ts_ep)) // 3600)
            if 0 <= hr_idx < hrs:
                buckets[hr_idx] = buckets.get(hr_idx, 0) + 1
                if is_http:
                    buckets_http[hr_idx] = buckets_http.get(hr_idx, 0) + 1
                else:
                    buckets_stdio[hr_idx] = buckets_stdio.get(hr_idx, 0) + 1
                if int(acl_f or 0) > 0:
                    buckets_acl[hr_idx] = buckets_acl.get(hr_idx, 0) + 1
                buckets_lat.setdefault(hr_idx, []).append(f_lat)
            if m_json and m_json != "[]":
                try:
                    terms = json.loads(m_json)
                    for t in terms:
                        ts_clean = str(t).strip()
                        if not ts_clean:
                            continue
                        entry = missing_counts.setdefault(ts_clean, {"term": ts_clean, "count": 0, "vaults": set()})
                        entry["count"] += 1
                        if vid:
                            entry["vaults"].add(vid)
                except ValueError:
                    pass

        latencies.sort()
        p50 = latencies[len(latencies) // 2] if latencies else 0.0
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0.0
        top_missing = [
            {"term": v["term"], "count": v["count"], "vaults": sorted(v["vaults"])}
            for v in sorted(missing_counts.values(), key=lambda x: (-x["count"], x["term"]))[:15]
        ]
        hourly: list[dict[str, Any]] = []
        for i in range(min(hrs, 24) - 1, -1, -1):
            b_lats = sorted(buckets_lat.get(i, []))
            b_p50 = round(b_lats[len(b_lats) // 2], 2) if b_lats else 0.0
            hourly.append({
                "hour_offset": -i,
                "count": buckets.get(i, 0),
                "stdio": buckets_stdio.get(i, 0),
                "http": buckets_http.get(i, 0),
                "acl_blocked": buckets_acl.get(i, 0),
                "p50_ms": b_p50,
            })

        return {
            "hours": hrs,
            "total_calls": total,
            "stdio_calls": stdio_calls,
            "http_calls": http_calls,
            "acl_blocked_calls": acl_blocked_calls,
            "error_count": errors,
            "error_rate": round((errors / total) * 100, 1) if total else 0.0,
            "zero_hit_count": zero_hits,
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "by_client": [{"client": k, "count": v} for k, v in sorted(by_client.items(), key=lambda kv: -kv[1])],
            "by_vault": [{"vault_id": k, "count": v} for k, v in sorted(by_vault.items(), key=lambda kv: -kv[1])],
            "by_tool": [{"tool": k, "count": v} for k, v in sorted(by_tool.items(), key=lambda kv: -kv[1])],
            "top_missing_concepts": top_missing,
            "hourly": hourly,
        }

    def clear(self) -> int:
        if not self.enabled:
            return 0
        with self._write_lock:
            con = sqlite3.connect(str(self.db_path), timeout=3.0, check_same_thread=False)
            try:
                cur = con.execute("DELETE FROM mcp_invocations")
                con.commit()
                return int(cur.rowcount or 0)
            finally:
                con.close()
