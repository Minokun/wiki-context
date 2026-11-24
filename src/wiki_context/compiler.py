"""Offline AI Librarian Compiler (`wctx compile`).

Incrementally identifies:
1. Mirrored primary documents not yet cited by any `wiki/*.md` page;
2. Existing `wiki/*.md` pages marked `stale` (whose underlying primary sources changed).

Calls any OpenAI-compatible (`/v1/chat/completions`) or Anthropic Messages
(`/v1/messages`) endpoint via Python stdlib `urllib.request` (zero SDK dependency),
or accepts a custom `llm_fn(system_prompt, user_prompt) -> str` callable.
Updates Wiki pages, `index.md`, `log.md`, snapshots SHA-256 `source_hashes`,
runs `lint_vault`, and refreshes the immutable FTS5 index.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse

from .config import VaultConfig
from .freshness import (
    check_vault_freshness,
    clean_source_ref,
    parse_wiki_frontmatter,
    snapshot_wiki_hashes,
    split_frontmatter,
)
from .indexer import rebuild_index
from .lint import _normalize_wikilink_target, lint_vault
from .memory import MemoryStore
from .net import (
    TLSUnavailable,
    describe_trust_store,
    format_network_error,
    ssl_context,
)
from .policy import IMAGE_EXTS, mirror_skip_reason
from .search import is_excluded_source

LLMCallable = Callable[[str, str], str]

LLM_PROVIDER_PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    {
        "id": "qwen",
        "label": "通义千问 (DashScope)",
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM (BigModel)",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
    },
    {
        "id": "local",
        "label": "本地私有模型 (Strata / vLLM / Ollama)",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "",
    },
    {
        "id": "custom",
        "label": "自定义兼容接口",
        "protocol": "openai",
        "base_url": "",
        "model": "",
    },
]


def is_local_llm_url(url: str) -> bool:
    """Return True when ``url`` points to a local loopback LLM inference server."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def test_llm_connection(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    protocol: str = "openai",
    timeout_sec: int = 20,
    disable_thinking: bool = False,
) -> dict[str, Any]:
    """Verify LLM credentials & endpoint connectivity with a minimal ping request."""
    caller = build_http_llm_caller(
        model=model,
        base_url=base_url,
        api_key=api_key,
        protocol=protocol,
        timeout_sec=timeout_sec,
        max_tokens=32,
        disable_thinking=disable_thinking,
        retries=0,
    )
    t0 = time.perf_counter()
    try:
        reply = caller("You are a connectivity check.", "Reply with the single word OK.").strip()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"大模型接口返回 HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            format_network_error(exc, target=base_url or "", timeout_sec=timeout_sec)
        ) from exc
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "ok": True,
        "latency_ms": elapsed_ms,
        "reply": reply[:60] or "OK",
        "model": model or "",
        "base_url": base_url or "",
        "tls": describe_trust_store(),
    }


def _slugify_title(name: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|#\[\]]+", "-", name).strip(" .-")
    return clean[:80] or "compiled-note"


_KNOWN_SOURCE_EXTS = {
    ".md", ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".txt",
    ".html", ".htm", ".rtf", ".eml",
}
_GENERIC_SHEET_STEM_RE = re.compile(r"^(?:\d+[_-].*|sheet\d*|表格[_-]?\d*)$", re.IGNORECASE)
_COPY_SUFFIX_RE = re.compile(r"[（(]\d+[）)]$")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_FENCED_CODE_RE = re.compile(r"(```[\s\S]*?```)")


def _strip_known_source_exts(name: str) -> str:
    cur = name.strip()
    while True:
        dot = cur.rfind(".")
        if dot <= 0:
            break
        if cur[dot:].lower() in _KNOWN_SOURCE_EXTS:
            cur = cur[:dot]
        else:
            break
    return cur or name.strip()


def _clean_source_stem(src_ref: str, cfg: VaultConfig) -> str:
    rel = cfg.strip_source_prefix(clean_source_ref(src_ref)).replace("\\", "/").lstrip("/")
    p = PurePosixPath(rel)
    stem = _strip_known_source_exts(p.name)
    if len(p.parts) >= 2 and (_GENERIC_SHEET_STEM_RE.match(stem) or len(stem) <= 6):
        parent_stem = _strip_known_source_exts(p.parent.name)
        if parent_stem and parent_stem not in {".", ".."}:
            stem = f"{parent_stem}-{stem}"
    return _slugify_title(stem)


_INLINE_CODE_RE = re.compile(r"(`+[^`\n]+`+)")


def _sanitize_dangling_wikilinks(md: str, valid_targets: set[str]) -> str:
    """Convert ``[[Target]]`` / ``[[Target|Alias]]`` links to plain text if Target does not exist."""
    def _replace_non_code(segment: str) -> str:
        def _repl(m: re.Match[str]) -> str:
            target_raw = m.group(1).strip()
            alias = m.group(2)
            if _normalize_wikilink_target(target_raw) in valid_targets:
                return m.group(0)
            if alias is not None and alias.strip():
                return alias.strip()
            return target_raw

        inline_parts = _INLINE_CODE_RE.split(segment)
        return "".join(
            sub if (j % 2 == 1) else _WIKILINK_RE.sub(_repl, sub)
            for j, sub in enumerate(inline_parts)
        )

    parts = _FENCED_CODE_RE.split(md)
    return "".join(part if (i % 2 == 1) else _replace_non_code(part) for i, part in enumerate(parts))


def _ensure_frontmatter_integrity(
    md: str,
    *,
    required_sources: list[str],
    title: str,
    doc_type: str,
    today: str,
    force_title: bool = False,
) -> str:
    """Ensure YAML frontmatter has required sources, unique title, and draft status."""
    fm, body = split_frontmatter(md)
    if not fm:
        srcs_yaml = "\n".join(f"  - {s}" for s in required_sources)
        return (
            f'---\ntitle: "{title}"\ntype: {doc_type}\nstatus: draft\nupdated: {today}\n'
            f"sources:\n{srcs_yaml}\n---\n\n{md.lstrip()}"
        )

    meta = parse_wiki_frontmatter(fm)
    existing_srcs = [clean_source_ref(s) for s in meta["sources"]]
    missing_srcs = [s for s in required_sources if s not in existing_srcs]

    fm_lines = fm.splitlines()
    out_lines: list[str] = []
    inserted_srcs = False
    has_title = False
    has_type = False
    has_updated = False
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line[:1].isspace() and ":" in line:
            k = line.split(":", 1)[0].strip()
            if k in {"title", "标题"}:
                has_title = True
                if force_title or not meta["title"]:
                    out_lines.append(f'{k}: "{title}"')
                else:
                    out_lines.append(line)
                i += 1
                continue
            if k in {"type", "类型"}:
                has_type = True
            elif k in {"updated", "更新"}:
                has_updated = True
            if k in {"sources", "来源"} and missing_srcs:
                rest = line.split(":", 1)[1].strip()
                if rest.startswith("[") and rest.endswith("]"):
                    all_srcs = [*meta["sources"], *missing_srcs]
                    out_lines.append(f"{k}:")
                    for s in all_srcs:
                        out_lines.append(f"  - {s}")
                    inserted_srcs = True
                    i += 1
                    continue
                out_lines.append(line)
                i += 1
                while i < len(fm_lines) and (fm_lines[i][:1].isspace() or not fm_lines[i].strip()):
                    out_lines.append(fm_lines[i])
                    i += 1
                for s in missing_srcs:
                    out_lines.append(f"  - {s}")
                inserted_srcs = True
                continue
        out_lines.append(line)
        i += 1

    if not has_title:
        out_lines.insert(0, f'title: "{title}"')
    if not has_type:
        out_lines.append(f"type: {doc_type}")
    if not has_updated:
        out_lines.append(f"updated: {today}")
    if missing_srcs and not inserted_srcs:
        out_lines.append("sources:")
        for s in [*meta["sources"], *missing_srcs]:
            out_lines.append(f"  - {s}")

    rebuilt = "---\n" + "\n".join(out_lines).rstrip() + "\n---\n" + body
    return _enforce_draft_status(rebuilt)


def _is_ssl_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLError)


def post_llm_json(
    url: str,
    payload: bytes,
    headers: dict[str, str],
    *,
    timeout_sec: float = 120,
    retries: int = 0,
) -> dict[str, Any]:
    """POST JSON to an LLM endpoint with a real trust store, transient retry, and readable failures.

    Every HTTPS call must pass ``context=`` explicitly: a packaged engine can start
    with an empty OpenSSL trust store, where the default context silently trusts
    nothing and ``urlopen`` only reports ``CERTIFICATE_VERIFY_FAILED``.
    """
    max_attempts = max(1, int(retries) + 1)
    raw = ""
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context()) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()[:300]
            except Exception:
                pass
            finally:
                exc.close()
            if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < max_attempts:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"大模型接口返回 HTTP {exc.code}：{detail or exc.reason}") from exc
        except TLSUnavailable as exc:
            raise RuntimeError(str(exc)) from exc
        except (urllib.error.URLError, OSError) as exc:
            if not _is_ssl_error(exc) and attempt + 1 < max_attempts:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(
                format_network_error(exc, target=url, timeout_sec=timeout_sec)
            ) from exc
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"大模型接口返回了非 JSON 响应（{url}）：{raw[:200]}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"大模型接口返回格式异常（{url}）：{str(body)[:200]}")
    return body


def build_http_llm_caller(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    protocol: str = "openai",
    timeout_sec: int = 120,
    max_tokens: int | None = None,
    disable_thinking: bool = False,
    retries: int = 2,
) -> LLMCallable:
    """Create a zero-dependency stdlib HTTP LLM caller (`openai` or `anthropic` wire format)."""
    proto = (protocol or os.environ.get("WCTX_LLM_PROTOCOL") or "openai").strip().lower()
    if proto == "anthropic":
        key = api_key or os.environ.get("WCTX_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
        url = (base_url or os.environ.get("WCTX_BASE_URL") or "https://api.anthropic.com/v1").rstrip("/")
        mdl = model or os.environ.get("WCTX_MODEL") or "claude-sonnet-4-20250514"
        if not key and not is_local_llm_url(url):
            raise ValueError("Missing API key: set WCTX_API_KEY or ANTHROPIC_API_KEY")
        eff_max_tok = int(max_tokens) if (max_tokens is not None and max_tokens > 0) else 2048

        def _call_anthropic(sys_prompt: str, usr_prompt: str) -> str:
            def _send_anth(tok_limit: int) -> dict[str, Any]:
                payload = json.dumps({
                    "model": mdl,
                    "max_tokens": tok_limit,
                    "temperature": 0.1,
                    "system": sys_prompt,
                    "messages": [{"role": "user", "content": usr_prompt}],
                }).encode("utf-8")
                hdrs = {
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                }
                if key:
                    hdrs["x-api-key"] = key
                return post_llm_json(
                    f"{url}/messages",
                    payload,
                    hdrs,
                    timeout_sec=timeout_sec,
                    retries=retries,
                )

            used_tok = eff_max_tok
            body = _send_anth(used_tok)
            if str(body.get("stop_reason") or "").strip().lower() == "max_tokens":
                boosted = min(8192, max(used_tok * 2, 3200))
                if boosted > used_tok:
                    used_tok = boosted
                    body = _send_anth(used_tok)
                if str(body.get("stop_reason") or "").strip().lower() == "max_tokens":
                    raise RuntimeError(
                        f"LLM output truncated at max_tokens={used_tok} (stop_reason=max_tokens); increase max_tokens"
                    )
            blocks = body.get("content") or []
            return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")

        return _call_anthropic

    key = (
        api_key
        or os.environ.get("WCTX_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()
    url = (base_url or os.environ.get("WCTX_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    mdl = model or os.environ.get("WCTX_MODEL") or "gpt-4o-mini"
    if not key and not is_local_llm_url(url):
        raise ValueError("Missing API key: set WCTX_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY")

    thinking_lock = threading.Lock()
    thinking_state = {"kwarg_supported": bool(disable_thinking)}

    def _call_openai(sys_prompt: str, usr_prompt: str) -> str:
        with thinking_lock:
            use_kwarg = thinking_state["kwarg_supported"]

        def _send(with_thinking_kwarg: bool, tok_limit: int | None) -> dict[str, Any]:
            body_obj: dict[str, Any] = {
                "model": mdl,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": usr_prompt},
                ],
            }
            if tok_limit is not None and tok_limit > 0:
                body_obj["max_tokens"] = int(tok_limit)
            if with_thinking_kwarg:
                body_obj["chat_template_kwargs"] = {"enable_thinking": False}
            hdrs = {"Content-Type": "application/json"}
            if key:
                hdrs["Authorization"] = f"Bearer {key}"
            return post_llm_json(
                f"{url}/chat/completions",
                json.dumps(body_obj).encode("utf-8"),
                hdrs,
                timeout_sec=timeout_sec,
                retries=retries,
            )

        def _send_with_thinking_fallback(tok_limit: int | None) -> dict[str, Any]:
            nonlocal use_kwarg
            try:
                return _send(use_kwarg, tok_limit)
            except RuntimeError as exc:
                cause = exc.__cause__
                if use_kwarg and isinstance(cause, urllib.error.HTTPError) and cause.code in {400, 422}:
                    with thinking_lock:
                        thinking_state["kwarg_supported"] = False
                    use_kwarg = False
                    return _send(False, tok_limit)
                raise

        used_tok = int(max_tokens) if (max_tokens is not None and max_tokens > 0) else None
        body = _send_with_thinking_fallback(used_tok)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM response contained no choices: {body}")

        finish_reason = str(choices[0].get("finish_reason") or "").strip().lower()
        if finish_reason == "length":
            if used_tok is not None:
                boosted = min(8192, max(used_tok * 2, 3200))
                if boosted > used_tok:
                    used_tok = boosted
                    body = _send_with_thinking_fallback(used_tok)
                    choices = body.get("choices") or []
                    if not choices:
                        raise RuntimeError(f"LLM response contained no choices: {body}")
                    finish_reason = str(choices[0].get("finish_reason") or "").strip().lower()
            if finish_reason == "length":
                raise RuntimeError(
                    f"LLM output truncated at max_tokens={used_tok or 'default'} (finish_reason=length); increase max_tokens"
                )
        return str((choices[0].get("message") or {}).get("content") or "")

    return _call_openai


def _strip_outer_markdown_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1 and s.endswith("```"):
            return s[first_nl + 1: -3].strip()
    return s


_OPERATIONAL_DIR_KEYWORDS = (
    "发票",
    "报销",
    "invoice",
    "receipt",
    "周报日报",
    "周报",
    "日报",
    "weekly-report",
    "daily-report",
    "考勤",
    "请假",
    "来料检验记录",
    "原始数据导出",
    "发货记录",
    "例行试验记录",
    "生产过程记录",
    "领料记录",
    "包装运输",
    "供应商现场评定",
    "23bom清单",
    "图纸版本记录",
    "项目问题台账",
    "变更管理台账",
    "项目干系人台账",
    "项目主控计划",
    "设备检查记录表",
    "设备出场调试记录",
    "现场调试记录",
    "财务类资料",
    "日常办公用品",
    "携程商旅",
    "开票信息",
)
_OPERATIONAL_FILE_PREFIXES = ("dzfp_", "inv20", "原始数据导出")
_OPERATIONAL_FILE_KEYWORDS = (
    "装箱单",
    "领料序时簿",
    "报验单",
    "签收单",
    "拆机记录",
    "干系表",
    "干系人台账",
    "铭牌",
)
_BODY_SOURCES_HEADING_RE = re.compile(r"^##\s+(?:来源|sources)\s*$", re.IGNORECASE)
_HIGH_VALUE_DIR_KEYWORDS = ("知识库", "系统部", "其他已有md")
_HIGH_VALUE_DOC_KEYWORDS = (
    "规范",
    "标准",
    "架构",
    "设计",
    "方案",
    "手册",
    "说明书",
    "制度",
    "指南",
    "需求",
    "prd",
    "rfc",
    "adr",
    "算法",
    "白皮书",
    "总结",
)
_TABULAR_EXTS = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm"}
_PROSE_EXTS = {".md", ".docx", ".doc", ".pdf", ".pptx", ".ppt"}


def _triage_priority_score(src_rel: str, m_path: Path) -> int:
    """Rank uncompiled sources so high-value knowledge docs compile before raw tables/logs."""
    rel_low = src_rel.replace("\\", "/").lower()
    score = 0
    if any(kw in rel_low for kw in _HIGH_VALUE_DIR_KEYWORDS):
        score += 40
    if any(kw in rel_low for kw in _HIGH_VALUE_DOC_KEYWORDS):
        score += 30
    orig_ext = PurePosixPath(rel_low).suffix
    if orig_ext in _PROSE_EXTS:
        score += 10
    elif orig_ext in _TABULAR_EXTS:
        score -= 15
    try:
        sz = m_path.stat().st_size
    except OSError:
        sz = 0
    if sz < 400:
        score -= 25
    elif sz >= 1500:
        score += 10
    return score


def _clean_cluster_seg(seg: str) -> str:
    cleaned = _COPY_SUFFIX_RE.sub("", seg.strip()).strip()
    return _strip_known_source_exts(cleaned) if cleaned else ""


def _group_uncompiled_into_clusters(
    uncompiled_items: list[dict],
    cfg: VaultConfig,
    *,
    cluster_topics: bool = True,
    max_sources_per_cluster: int = 8,
    project_rollup: bool = False,
    max_rollup_sources: int = 16,
    is_zh: bool = True,
) -> list[dict[str, Any]]:
    """Score, filter-order, and group uncompiled sources into project/topic synthesis units."""
    if not cluster_topics:
        units: list[dict[str, Any]] = []
        for item in uncompiled_items:
            src_ref = item["source"]
            rel = cfg.strip_source_prefix(src_ref).replace("\\", "/").lstrip("/")
            units.append({
                "kind": "source",
                "title_base": _clean_source_stem(src_ref, cfg),
                "cluster_label": "",
                "priority": _triage_priority_score(rel, item["mirror_path"]),
                "sort_key": src_ref,
                "items": [item],
            })
        return units

    depth2_counts: dict[str, int] = {}
    enriched: list[dict[str, Any]] = []
    for item in uncompiled_items:
        src_ref = item["source"]
        rel = cfg.strip_source_prefix(src_ref).replace("\\", "/").lstrip("/")
        parts = PurePosixPath(rel).parts
        score = _triage_priority_score(rel, item["mirror_path"])
        if len(parts) == 2:
            d2_key = _clean_cluster_seg(parts[0]) or parts[0]
            depth2_counts[d2_key] = depth2_counts.get(d2_key, 0) + 1
        enriched.append({"item": item, "rel": rel, "parts": parts, "score": score})

    grouped: dict[str, dict[str, Any]] = {}
    standalone_units: list[dict[str, Any]] = []

    for row in enriched:
        item = row["item"]
        src_ref = item["source"]
        parts: tuple[str, ...] = row["parts"]
        score: int = row["score"]

        cluster_key: str | None = None
        cluster_label = ""
        if len(parts) == 2:
            d2_key = _clean_cluster_seg(parts[0]) or parts[0]
            if depth2_counts.get(d2_key, 0) >= 2:
                cluster_key = f"d2/{d2_key}"
                cluster_label = d2_key
        elif len(parts) >= 3:
            top = parts[0]
            if top in {"svn", "钉钉群文件"}:
                lbl = _clean_cluster_seg(parts[1]) or _clean_cluster_seg(top)
                cluster_key = f"{top}/{lbl}"
                cluster_label = lbl
            elif len(parts) == 3:
                s0 = _clean_cluster_seg(parts[0])
                s1 = _clean_cluster_seg(parts[1])
                cluster_label = f"{s0}-{s1}" if (s0 and s1) else (s1 or s0)
                cluster_key = f"{parts[0]}/{s1}"
            else:
                s1 = _clean_cluster_seg(parts[1])
                s2 = _clean_cluster_seg(parts[2])
                cluster_label = f"{s1}-{s2}" if (s1 and s2) else (s2 or s1 or _clean_cluster_seg(parts[0]))
                cluster_key = f"{parts[0]}/{s1}/{s2}"

        if cluster_key is None:
            standalone_units.append({
                "kind": "source",
                "title_base": _clean_source_stem(src_ref, cfg),
                "cluster_label": "",
                "priority": score,
                "sort_key": src_ref,
                "items": [item],
            })
        else:
            grp = grouped.setdefault(cluster_key, {"label": cluster_label, "rows": []})
            grp["rows"].append(row)

    units: list[dict[str, Any]] = list(standalone_units)
    cap = max(1, int(max_sources_per_cluster))
    rollup_cap = max(cap, int(max_rollup_sources))
    suffix_word = "综述" if is_zh else "Overview"
    rollup_suffix = "项目总览" if is_zh else "Project-Overview"

    for ckey, grp in sorted(grouped.items()):
        rows = sorted(grp["rows"], key=lambda r: (-r["score"], r["item"]["source"]))
        if len(rows) == 1:
            only = rows[0]
            units.append({
                "kind": "source",
                "title_base": _clean_source_stem(only["item"]["source"], cfg),
                "cluster_label": "",
                "priority": only["score"],
                "sort_key": only["item"]["source"],
                "items": [only["item"]],
            })
            continue
        if project_rollup and ckey.startswith("svn/") and len(rows) > cap:
            max_p = max(r["score"] for r in rows)
            prompt_rows = rows[:rollup_cap]
            units.append({
                "kind": "synthesis",
                "title_base": _slugify_title(f"{grp['label']}-{rollup_suffix}"),
                "cluster_label": grp["label"],
                "priority": max_p,
                "sort_key": f"{ckey}#rollup",
                "items": [r["item"] for r in rows],
                "prompt_items": [r["item"] for r in prompt_rows],
            })
            continue
        for idx in range(0, len(rows), cap):
            chunk = rows[idx: idx + cap]
            if len(chunk) == 1:
                only = chunk[0]
                units.append({
                    "kind": "source",
                    "title_base": _clean_source_stem(only["item"]["source"], cfg),
                    "cluster_label": "",
                    "priority": only["score"],
                    "sort_key": only["item"]["source"],
                    "items": [only["item"]],
                })
                continue
            max_p = max(r["score"] for r in chunk)
            units.append({
                "kind": "synthesis",
                "title_base": _slugify_title(f"{grp['label']}-{suffix_word}"),
                "cluster_label": grp["label"],
                "priority": max_p,
                "sort_key": f"{ckey}#{idx // cap:04d}",
                "items": [r["item"] for r in chunk],
            })

    units.sort(key=lambda u: (-u["priority"], u["sort_key"]))
    return units


def _extract_body_source_refs(body: str, cfg: VaultConfig) -> list[str]:
    """Extract primary source references listed under ``## 来源`` or ``## Sources``."""
    refs: list[str] = []
    in_sources_section = False
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not in_sources_section:
            if _BODY_SOURCES_HEADING_RE.match(stripped):
                in_sources_section = True
            continue
        if stripped.startswith("## ") or stripped.startswith("# "):
            break
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            item = stripped[2:].strip().strip("`").strip()
            clean = clean_source_ref(item)
            if cfg.is_source_ref(clean):
                refs.append(clean)
    return refs


def _compile_skip_reason(
    src_rel: str,
    cfg: VaultConfig,
    *,
    include_images: bool = False,
    skip_csv: bool | None = None,
) -> str | None:
    """Return a skip reason when an indexed mirror should not auto-compile into a standalone Wiki page."""
    rel = src_rel.replace("\\", "/").lstrip("/")
    p = PurePosixPath(rel)
    src_ref = f"{cfg.source_prefix}{rel}"
    if not include_images and p.suffix.lower() in IMAGE_EXTS:
        return "image-mirror-excluded-from-compile"
    if cfg.exclude_patterns and is_excluded_source(src_ref, cfg.exclude_patterns):
        return "vault-exclude-pattern"
    if cfg.compile_exclude_patterns and is_excluded_source(src_ref, cfg.compile_exclude_patterns):
        return "compile-exclude-pattern"
    if cfg.compile_include_patterns and not is_excluded_source(src_ref, cfg.compile_include_patterns):
        return "compile-not-in-include-patterns"
    eff_skip_csv = cfg.compile_skip_csv if skip_csv is None else bool(skip_csv)
    if eff_skip_csv and p.suffix.lower() in {".csv", ".tsv"}:
        return "csv-indexed-only-excluded-from-compile"
    for part in p.parts[:-1]:
        low_part = part.lower()
        if any(kw in low_part for kw in _OPERATIONAL_DIR_KEYWORDS) or part.startswith("DetectionRecognitionDemo"):
            return "non-knowledge-operational-doc"
    name_low = p.name.lower()
    if name_low.startswith(_OPERATIONAL_FILE_PREFIXES) or any(kw in name_low for kw in _OPERATIONAL_FILE_KEYWORDS):
        return "non-knowledge-operational-doc"
    return None


def _enforce_draft_status(md: str) -> str:
    """Prevent machine-compiled pages from self-marking ``status: reviewed``."""
    fm, body = split_frontmatter(md)
    if not fm:
        return md
    changed = False
    out_lines: list[str] = []
    for line in fm.splitlines(keepends=True):
        m = re.match(
            r'^(\s*(?:status|状态)\s*:\s*)(["\']?)reviewed\2(\s*(?:#.*)?)$',
            line.rstrip("\r\n"),
            re.IGNORECASE,
        )
        if m:
            nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            out_lines.append(f"{m.group(1)}{m.group(2)}draft{m.group(2)}{m.group(3)}{nl}")
            changed = True
        else:
            out_lines.append(line)
    if not changed:
        return md
    return "---\n" + "".join(out_lines) + "---\n" + body


def _compress_mirror_for_llm(body: str) -> str:
    """Strip repeated per-page watermark/footer noise and excess layout indentation before prompting."""
    if not body:
        return ""
    page_blocks = re.split(r"(?m)^(?=## Page \d+\b)", body)
    if len(page_blocks) >= 6:
        # First element before `## Page 1` may be preamble
        pages = [b for b in page_blocks if b.lstrip().startswith("## Page ")]
        n_pages = len(pages)
        if n_pages >= 5:
            doc_freq: dict[str, int] = {}
            for pb in pages:
                seen: set[str] = set()
                for ln in pb.splitlines()[1:]:
                    norm = re.sub(r"\s+", " ", ln.strip())
                    if not norm or norm.startswith("#"):
                        continue
                    seen.add(norm)
                for norm in seen:
                    doc_freq[norm] = doc_freq.get(norm, 0) + 1
            short_th = max(4, int(n_pages * 0.30))
            line_th = max(4, int(n_pages * 0.40))
            noise = {
                norm for norm, df in doc_freq.items()
                if not re.match(r"^\|?[\s:\-|]+\|?$", norm)
                and ((len(norm) <= 2 and df >= short_th) or (len(norm) <= 60 and df >= line_th))
            }
            if noise:
                kept_lines: list[str] = []
                for ln in body.splitlines():
                    norm = re.sub(r"\s+", " ", ln.strip())
                    if norm and not norm.startswith("## Page ") and norm in noise:
                        continue
                    kept_lines.append(ln)
                body = "\n".join(kept_lines)

    out_lines: list[str] = []
    blank_run = 0
    for ln in body.splitlines():
        stripped = ln.strip()
        if not stripped:
            blank_run += 1
            if blank_run <= 2:
                out_lines.append("")
            continue
        blank_run = 0
        if stripped.startswith("|") or stripped.startswith("```"):
            out_lines.append(ln.rstrip())
        else:
            compact = re.sub(r"^[ \t]{4,}", "", ln).rstrip()
            compact = re.sub(r"[ \t]{4,}", "  ", compact)
            out_lines.append(compact)
    return "\n".join(out_lines).strip()


def _load_mirror_visual_hint(mirror_path: Path, src_ref: str, is_zh: bool) -> tuple[int | None, str]:
    """Read sibling `.json` sidecar to expose total page count & visual assets to the compiler prompt."""
    sidecar = mirror_path.with_suffix(".json")
    if not sidecar.is_file():
        return None, ""
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    if not isinstance(meta, dict):
        return None, ""
    page_count = meta.get("page_count")
    pcount = int(page_count) if isinstance(page_count, int) and page_count > 0 else None
    vpages = meta.get("visual_pages") if isinstance(meta.get("visual_pages"), list) else []
    assets = meta.get("assets") if isinstance(meta.get("assets"), list) else []
    asset_ids = [
        str(a.get("asset_id") or a.get("name") or "").strip()
        for a in assets[:8]
        if isinstance(a, dict) and (a.get("asset_id") or a.get("name"))
    ]
    parts: list[str] = []
    if pcount is not None:
        parts.append(f"共 {pcount} 页" if is_zh else f"{pcount} pages total")
    if vpages:
        vp_str = ", ".join(str(p) for p in vpages[:12])
        parts.append(f"稀疏/图表页：Page {vp_str}" if is_zh else f"sparse/visual pages: {vp_str}")
    if asset_ids:
        a_str = ", ".join(asset_ids)
        if is_zh:
            parts.append(
                f"已提取视觉附件（{len(assets)} 张，示例：{a_str}；涉及图示可提示使用 `wiki_inspect_visual(source=\"{src_ref}\", asset=\"...\")` 查阅）"
            )
        else:
            parts.append(
                f"extracted visual assets ({len(assets)}, e.g. {a_str}; reference `wiki_inspect_visual(source=\"{src_ref}\", asset=\"...\")` for diagrams)"
            )
    return pcount, ("；".join(parts) if is_zh else "; ".join(parts))


def _split_long_body_into_chunks(text: str, *, max_chunk_chars: int = 16000) -> list[str]:
    """Split long markdown cleanly on page/section/paragraph boundaries."""
    if len(text) <= max_chunk_chars:
        return [text]
    blocks = re.split(r"\n(?=(?:## |### |\n))", text)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for blk in blocks:
        if not blk:
            continue
        if len(blk) > max_chunk_chars:
            if cur:
                chunks.append("\n".join(cur).strip())
                cur, cur_len = [], 0
            for start in range(0, len(blk), max_chunk_chars):
                chunks.append(blk[start: start + max_chunk_chars].strip())
            continue
        if cur_len + len(blk) + 1 > max_chunk_chars and cur:
            chunks.append("\n".join(cur).strip())
            cur, cur_len = [blk], len(blk)
        else:
            cur.append(blk)
            cur_len += len(blk) + 1
    if cur:
        chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c]


def _prepare_single_source_excerpt(
    cleaned_body: str,
    *,
    caller: LLMCallable,
    is_zh: bool,
    max_direct_chars: int = 24000,
    max_chunk_chars: int = 16000,
) -> tuple[str, bool]:
    """Return full cleaned text up to `max_direct_chars`, or Map-Reduce full-coverage digest for long docs."""
    if len(cleaned_body) <= max_direct_chars:
        return cleaned_body, False

    all_chunks = _split_long_body_into_chunks(cleaned_body, max_chunk_chars=max_chunk_chars)
    if len(all_chunks) > 4:
        # Always preserve head (0) and tail (-1), plus 2 evenly spaced interior chunks
        m1 = len(all_chunks) // 3
        m2 = (2 * len(all_chunks)) // 3
        picked_indices = sorted({0, m1, m2, len(all_chunks) - 1})
        chunks = [(idx + 1, all_chunks[idx]) for idx in picked_indices]
    else:
        chunks = [(idx + 1, ch) for idx, ch in enumerate(all_chunks)]

    total_chunks = len(all_chunks)
    map_sys = (
        "你是严谨的技术资料提炼员。请从给定分段中提取核心概念、精确数值、型号、标准号、规则与实操结论，"
        "输出紧凑 Markdown 要点（不超过 450 字），不寒暄、不抄流水账、不编造事实。"
        if is_zh
        else "You are an exacting technical extractor. Distill key concepts, exact figures, codes, rules, and conclusions "
        "from this section into concise Markdown bullets (<=350 words). Never invent facts."
    )
    digests: list[str] = []
    for part_no, ch_text in chunks:
        map_usr = (
            f"这是长文档的第 {part_no}/{total_chunks} 段（全文共 {len(cleaned_body)} 字符）。请提炼关键信息：\n\n{ch_text}"
            if is_zh
            else f"Part {part_no}/{total_chunks} of a long document ({len(cleaned_body)} chars total). Extract key facts:\n\n{ch_text}"
        )
        part_summary = _strip_outer_markdown_fence(caller(map_sys, map_usr)).strip()
        digests.append(f"### {'分段' if is_zh else 'Part'} {part_no}/{total_chunks}\n{part_summary}")

    if len(chunks) < total_chunks:
        header = (
            f"[长文档采样分段摘要：原文共 {len(cleaned_body)} 字符（{total_chunks} 段），已均匀抽取首尾及中段共 {len(chunks)} 段提炼，请综合编排]"
            if is_zh
            else f"[Sampled long-document digest: {len(cleaned_body)} chars across {total_chunks} sections; {len(chunks)} representative sections covering head, middle, and tail extracted]"
        )
    else:
        header = (
            f"[全篇分段提炼摘要：原文共 {len(cleaned_body)} 字符，已按全部 {total_chunks} 段完整抽取，请综合编排，切勿误判为截断原文]"
            if is_zh
            else f"[Full-document chunked digest: {len(cleaned_body)} chars total across all {total_chunks} sections covering head-to-tail]"
        )
    return header + "\n\n" + "\n\n".join(digests), True


def _extract_catalog_summary(md: str, *, fallback: str, max_chars: int = 68) -> str:
    """Extract a clean one-line topic summary from `## 概览` / `## Overview` for catalog entries."""
    _, body = split_frontmatter(md)
    lines = body.splitlines()
    in_overview = False
    candidates: list[str] = []
    for raw_ln in lines:
        s = raw_ln.strip()
        if re.match(r"^##\s+(?:概览|概述|摘要|overview|summary)\b", s, re.I):
            in_overview = True
            continue
        if in_overview and re.match(r"^#{1,3}\s+", s):
            break
        if not in_overview or not s:
            continue
        if s.startswith("|") or s.startswith("```") or s.startswith("<!--"):
            continue
        cleaned = re.sub(r"^(?:[-*+]|\d+[.)、])\s*", "", s)
        cleaned = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: (m.group(2) or m.group(1)).strip(), cleaned)
        cleaned = re.sub(r"[*_`]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ：:;；-—")
        if len(cleaned) < 6:
            continue
        # Skip boilerplate meta-only opening lines like "一手史料镜像：史料/..."
        if re.match(r"^(?:一手史料镜像|来源文件|primary source|source mirror)[:：]", cleaned, re.I):
            continue
        candidates.append(cleaned)
        if len(candidates) >= 2:
            break

    if not candidates:
        return fallback
    summary = candidates[0]
    if len(summary) > max_chars:
        cut = summary[:max_chars]
        for sep in ("。", "；", "，", ". ", "; ", ", "):
            pos = cut.rfind(sep)
            if pos >= max_chars // 2:
                cut = cut[:pos]
                break
        summary = cut.rstrip(" ，,；;。") + "…"
    return summary


def plan_compilation(
    vault: Path | str,
    *,
    include_images: bool = False,
    skip_csv: bool | None = None,
) -> dict:
    """Inspect vault and return deduplicated ``uncompiled_sources`` + ``stale_wiki_pages`` without calling an LLM."""
    cfg = VaultConfig.load(vault)
    covered_sources: set[str] = set()
    if cfg.wiki_root.is_dir():
        for wp in sorted(cfg.wiki_root.glob("*.md")):
            raw = wp.read_text(encoding="utf-8", errors="replace")
            fm, body = split_frontmatter(raw)
            meta = parse_wiki_frontmatter(fm)
            for s in [*meta["sources"], *_extract_body_source_refs(body, cfg)]:
                clean = clean_source_ref(s)
                if cfg.is_source_ref(clean):
                    covered_sources.add(cfg.normalize_source_ref(clean))

    manifest_p = cfg.mirrors_root / ".manifest.json"
    manifest: dict = {}
    if manifest_p.is_file():
        try:
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}

    covered_hashes: set[str] = set()
    for src_rel, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        src_ref = f"{cfg.source_prefix}{src_rel.lstrip('/')}"
        h = str(entry.get("hash") or "").strip()
        if h and src_ref in covered_sources:
            covered_hashes.add(h)

    raw_candidates: list[dict] = []
    for src_rel, entry in sorted(manifest.items()):
        if not isinstance(entry, dict):
            continue
        m_rel = str(entry.get("mirror") or f"{src_rel}.md").lstrip("/")
        m_path = cfg.mirrors_root / m_rel
        if not m_path.is_file() or mirror_skip_reason(m_path, m_rel, cfg=cfg, manifest_entry=entry) is not None:
            continue
        if _compile_skip_reason(src_rel, cfg, include_images=include_images, skip_csv=skip_csv) is not None:
            continue
        src_ref = f"{cfg.source_prefix}{src_rel.lstrip('/')}"
        if src_ref not in covered_sources:
            raw_candidates.append({
                "source": src_ref,
                "rel": src_rel.replace("\\", "/").lstrip("/"),
                "mirror_path": m_path,
                "hash": str(entry.get("hash") or "").strip(),
            })

    # Content-hash deduplication: keep highest-priority copy per identical SHA-256 hash
    uncompiled: list[dict] = []
    duplicate_skipped = 0
    best_by_hash: dict[str, tuple[tuple[int, int, str], dict]] = {}
    no_hash_items: list[dict] = []
    for item in raw_candidates:
        h = item["hash"]
        if not h:
            no_hash_items.append({"source": item["source"], "mirror_path": item["mirror_path"]})
            continue
        if h in covered_hashes:
            duplicate_skipped += 1
            continue
        rank = (_triage_priority_score(item["rel"], item["mirror_path"]), -len(item["rel"]), item["source"])
        prev = best_by_hash.get(h)
        if prev is None:
            best_by_hash[h] = (rank, {"source": item["source"], "mirror_path": item["mirror_path"]})
        else:
            duplicate_skipped += 1
            if rank[0] > prev[0][0] or (rank[0] == prev[0][0] and rank[1] > prev[0][1]):
                best_by_hash[h] = (rank, {"source": item["source"], "mirror_path": item["mirror_path"]})

    uncompiled = sorted(
        [*no_hash_items, *(v[1] for v in best_by_hash.values())],
        key=lambda x: x["source"],
    )

    stale_pages = check_vault_freshness(cfg.root, only_stale=True)
    mem_store = MemoryStore(cfg.root)
    active_notes = mem_store.list_notes(status="active")
    return {
        "vault": cfg.name,
        "uncompiled_sources": [u["source"] for u in uncompiled],
        "stale_pages": [s["page"] for s in stale_pages],
        "active_notes": [n.note_id for n in active_notes],
        "duplicate_sources_skipped": duplicate_skipped,
        "_uncompiled_items": uncompiled,
        "_stale_items": stale_pages,
        "_active_notes": active_notes,
    }


def compile_vault(
    vault: Path | str,
    *,
    llm_fn: LLMCallable | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    protocol: str = "openai",
    max_items: int = 10,
    dry_run: bool = False,
    include_images: bool = False,
    cluster_topics: bool = True,
    max_sources_per_cluster: int = 8,
    workers: int | None = None,
    max_tokens: int | None = None,
    disable_thinking: bool | None = None,
    overwrite_reviewed: bool = False,
    project_rollup: bool | None = None,
    skip_csv: bool | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Compile unindexed primary mirrors (with auto-triage, clustering & concurrency) and refresh stale Wiki pages."""
    cfg = VaultConfig.load(vault)
    eff_skip_csv = cfg.compile_skip_csv if skip_csv is None else bool(skip_csv)
    eff_rollup = cfg.compile_project_rollup if project_rollup is None else bool(project_rollup)
    eff_max_tokens = cfg.compile_max_tokens if max_tokens is None else max(256, min(8192, int(max_tokens)))
    eff_disable_thinking = cfg.compile_disable_thinking if disable_thinking is None else bool(disable_thinking)
    eff_workers = (
        max(1, min(16, int(workers)))
        if workers is not None
        else (1 if llm_fn is not None else max(1, cfg.compile_workers))
    )

    plan = plan_compilation(cfg.root, include_images=include_images, skip_csv=eff_skip_csv)
    is_zh = cfg.layout == "zh"
    units_all = _group_uncompiled_into_clusters(
        plan["_uncompiled_items"],
        cfg,
        cluster_topics=cluster_topics,
        max_sources_per_cluster=max_sources_per_cluster,
        project_rollup=eff_rollup,
        is_zh=is_zh,
    )
    if dry_run:
        return {
            "vault": cfg.name,
            "dry_run": True,
            "uncompiled_sources": plan["uncompiled_sources"],
            "stale_pages": plan["stale_pages"],
            "active_notes": plan["active_notes"],
            "duplicate_sources_skipped": plan.get("duplicate_sources_skipped", 0),
            "cluster_count": len(units_all),
            "top_clusters": [
                {
                    "title": u["title_base"],
                    "kind": u["kind"],
                    "priority": u["priority"],
                    "source_count": len(u["items"]),
                    "sources": [it["source"] for it in u["items"]],
                }
                for u in units_all[:max_items]
            ],
        }

    # Filter stale pages to protect human-verified `status: reviewed` pages unless explicitly overridden
    eligible_stale_all: list[dict] = []
    skipped_reviewed_pages: list[str] = []
    for s_item in plan["_stale_items"]:
        wp_check = cfg.wiki_root / s_item["page"]
        if not overwrite_reviewed and wp_check.is_file():
            fm_s, _ = split_frontmatter(wp_check.read_text(encoding="utf-8", errors="replace"))
            st_val = str(parse_wiki_frontmatter(fm_s).get("status") or "").strip().lower()
            if st_val == "reviewed":
                skipped_reviewed_pages.append(s_item["page"])
                continue
        eligible_stale_all.append(s_item)

    compile_units = units_all[:max_items]
    remaining_budget = max(0, max_items - len(compile_units))
    stale_items = eligible_stale_all[:remaining_budget]
    active_notes = plan["_active_notes"]
    pending_total = (
        len(plan["_uncompiled_items"])
        + len(eligible_stale_all)
        + (1 if active_notes else 0)
    )
    covered_by_batch = sum(len(u["items"]) for u in compile_units)
    remaining_uncompiled = max(0, len(plan["_uncompiled_items"]) - covered_by_batch)
    remaining_stale = max(0, len(eligible_stale_all) - len(stale_items))

    if not compile_units and not stale_items and not active_notes:
        return {
            "vault": cfg.name,
            "compiled_pages": [],
            "refreshed_pages": [],
            "consolidated_notes": [],
            "skipped_reviewed_pages": skipped_reviewed_pages,
            "failed_units": [],
            "duplicate_sources_skipped": plan.get("duplicate_sources_skipped", 0),
            "remaining_uncompiled": 0,
            "remaining_stale": 0,
            "batch_limit": max_items,
            "noop": True,
        }

    caller = llm_fn or build_http_llm_caller(
        model=model,
        base_url=base_url,
        api_key=api_key,
        protocol=protocol,
        max_tokens=eff_max_tokens,
        disable_thinking=eff_disable_thinking,
    )
    cfg.wiki_root.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    now_hm = dt.datetime.now().strftime("%H:%M")

    overview_link = "[[知识库总览]]" if is_zh else "[[Overview]]"
    existing_stems_display: list[str] = []
    valid_wikilink_targets: set[str] = {
        "overview",
        "知识库总览",
        Path(cfg.catalog_file).stem.lower(),
        Path(cfg.log_file).stem.lower(),
        "agents",
    }
    existing_titles_lower: set[str] = set()
    if cfg.wiki_root.is_dir():
        for wp_ex in sorted(cfg.wiki_root.glob("*.md")):
            if not wp_ex.is_symlink():
                existing_stems_display.append(wp_ex.stem)
                valid_wikilink_targets.add(wp_ex.stem.lower())
                fm_ex, _ = split_frontmatter(wp_ex.read_text(encoding="utf-8", errors="replace"))
                t_ex = (parse_wiki_frontmatter(fm_ex)["title"] or wp_ex.stem).strip()
                existing_titles_lower.add(t_ex.lower())

    sys_prompt = (
        "You are an exacting technical librarian compiling a high-density, concise LLM Wiki guide from primary sources.\n"
        "Rules:\n"
        "1. Target 800-1500 Chinese characters (or 500-900 English words): synthesize by concept, rule, threshold, and "
        "conclusion — NEVER recite slide-by-slide or page-by-page transcripts.\n"
        "2. Preserve exact numbers, model codes, standard IDs, formulas, and table facts. Never invent facts.\n"
        "3. Output ONLY the complete Markdown document starting with `---` YAML frontmatter (no outer code fences).\n"
        "4. Include sections for Overview, Key Facts, Caveats/Pending Verification, Related Pages, and Sources. "
        "Keep Caveats brief (max 3 bullets) and ONLY note genuine data gaps, conflicts, or diagram-only rules — "
        "NEVER list repetitive legal disclaimers, watermark noise, or speaker filler.\n"
        "5. Only use `[[PageName]]` wikilinks for existing pages explicitly listed in the prompt; "
        "write all other entities, companies, concepts, or uncompiled document names in plain text or bold without `[[...]]`.\n"
    )

    state_lock = threading.Lock()
    compiled_pages: list[str] = []
    refreshed_pages: list[str] = []
    catalog_additions: list[tuple[str, str]] = []
    failed_units: list[dict[str, Any]] = []
    total_steps = len(compile_units) + len(stale_items) + (1 if active_notes else 0)
    done_steps = 0

    def _emit(phase: str, current: str = "") -> None:
        if on_progress is not None:
            try:
                with state_lock:
                    snap_prog = {
                        "phase": phase,
                        "done": done_steps,
                        "total": total_steps,
                        "pending_total": pending_total,
                        "batch_limit": max_items,
                        "current": current,
                        "compiled_count": len(compiled_pages),
                        "refreshed_count": len(refreshed_pages),
                    }
                on_progress(snap_prog)
            except Exception:
                pass

    # Pre-allocate deterministic unique filenames & stems on the main thread before parallel execution
    reserved_stems_lower: set[str] = set(existing_titles_lower) | set(valid_wikilink_targets)
    prepared_units: list[dict[str, Any]] = []
    for unit in compile_units:
        base_title = unit["title_base"]
        page_filename = f"{base_title}.md"
        target_wp = cfg.wiki_root / page_filename
        idx_suffix = 2
        while target_wp.exists() or target_wp.stem.lower() in reserved_stems_lower:
            page_filename = f"{base_title}-{idx_suffix}.md"
            target_wp = cfg.wiki_root / page_filename
            idx_suffix += 1
        stem_title = target_wp.stem
        reserved_stems_lower.add(stem_title.lower())
        prepared_units.append({
            "unit": unit,
            "page_filename": page_filename,
            "target_wp": target_wp,
            "stem_title": stem_title,
            "base_title": base_title,
        })

    # Add all pre-allocated stems to allowed targets so sibling pages compiled in parallel can link cleanly
    all_planned_stems = [*existing_stems_display, *(p["stem_title"] for p in prepared_units)]
    for p_info in prepared_units:
        valid_wikilink_targets.add(p_info["stem_title"].lower())

    def _compile_one_unit(p_info: dict[str, Any]) -> dict[str, Any]:
        unit = p_info["unit"]
        unit_items: list[dict] = unit["items"]
        prompt_items: list[dict] = unit.get("prompt_items") or unit_items
        unit_srcs = [it["source"] for it in unit_items]
        page_filename: str = p_info["page_filename"]
        target_wp: Path = p_info["target_wp"]
        stem_title: str = p_info["stem_title"]
        base_title: str = p_info["base_title"]

        progress_label = f"{unit['cluster_label']} ({len(unit_srcs)} 篇)" if len(unit_srcs) > 1 else unit_srcs[0]
        _emit("compiling_new", progress_label)

        allowed_pool = [s for s in all_planned_stems if s != stem_title][:25]
        allowed_links = ", ".join(f"[[{s}]]" for s in (allowed_pool or [overview_link.strip("[]")]))
        srcs_yaml_block = "\n".join(f"  - {s}" for s in unit_srcs)
        srcs_md_list = "\n".join(f"- {s}" for s in unit_srcs[:30])
        if len(unit_srcs) > 30:
            srcs_md_list += f"\n- *（其余 {len(unit_srcs) - 30} 份同项目文件已收录进全文索引）*" if is_zh else f"\n- *(plus {len(unit_srcs) - 30} more project sources indexed in FTS5)*"

        if len(unit_items) == 1:
            src_ref = unit_srcs[0]
            raw_m = unit_items[0]["mirror_path"].read_text(encoding="utf-8", errors="replace")
            _, m_body = split_frontmatter(raw_m)
            cleaned_body = _compress_mirror_for_llm(m_body)
            _pcount, visual_hint = _load_mirror_visual_hint(unit_items[0]["mirror_path"], src_ref, is_zh)
            excerpt, _chunked = _prepare_single_source_excerpt(cleaned_body, caller=caller, is_zh=is_zh)
            meta_line = (
                (f"文档元信息：{visual_hint}\n" if visual_hint else "")
                if is_zh
                else (f"Document metadata: {visual_hint}\n" if visual_hint else "")
            )
            if is_zh:
                usr_prompt = (
                    f"请将以下一手史料编译为一篇高密度结构化 Wiki 导读页（标题：{stem_title}）。\n"
                    f"必须使用如下 YAML frontmatter 开头：\n"
                    f"---\ntitle: \"{stem_title}\"\ntype: source\nstatus: draft\nupdated: {today}\n"
                    f"sources:\n{srcs_yaml_block}\n---\n\n"
                    f"{meta_line}"
                    f"写作要求：总篇幅控制在 800–1500 字，按核心主题/概念/参数/实操规则组织（严禁逐页复述流水账）；\n"
                    f"正文包含：`# {stem_title}`、`## 概览`（首句直接点明核心主题与适用范围）、`## 关键信息`、"
                    f"`## 待核验与口径提示`（最多 3 条，仅记录实质缺口/冲突/纯图示页，勿列重复免责声明或口头禅）、"
                    f"`## 关联页面`（仅可链接已存在页面：{allowed_links}；禁止为不存在的实体或概念加 `[[...]]`）、"
                    f"`## 来源`（列出 `- {src_ref}`）。\n\n"
                    f"=== 一手史料镜像原文 ({src_ref}) ===\n{excerpt}"
                )
            else:
                usr_prompt = (
                    f"Compile the following primary source into a concise, high-density Wiki guide titled '{stem_title}'.\n"
                    f"Start with exact YAML frontmatter:\n"
                    f"---\ntitle: \"{stem_title}\"\ntype: source\nstatus: draft\nupdated: {today}\n"
                    f"sources:\n{srcs_yaml_block}\n---\n\n"
                    f"{meta_line}"
                    f"Requirements: keep to 500-900 words organized by topic/rules/metrics (no page-by-page transcript). "
                    f"Include `# {stem_title}`, `## Overview` (lead sentence states scope directly), `## Key Facts`, "
                    f"`## Caveats` (max 3 concise items; real gaps/visual-only rules only, no boilerplate disclaimers), "
                    f"`## Related Pages` (ONLY link to existing pages: {allowed_links}; do NOT wrap other terms in `[[...]]`), "
                    f"and `## Sources` (`- {src_ref}`).\n\n"
                    f"=== Primary Mirror ({src_ref}) ===\n{excerpt}"
                )
        else:
            total_budget = 24000 if len(prompt_items) <= 10 else 28000
            per_src_budget = max(1200, total_budget // max(1, len(prompt_items)))
            src_sections: list[str] = []
            for idx_s, it in enumerate(prompt_items, start=1):
                raw_m = it["mirror_path"].read_text(encoding="utf-8", errors="replace")
                _, m_body = split_frontmatter(raw_m)
                cleaned_m = _compress_mirror_for_llm(m_body)
                _pc, vhint = _load_mirror_visual_hint(it["mirror_path"], it["source"], is_zh)
                hdr_extra = f" [{vhint}]" if vhint else ""
                src_sections.append(f"=== 来源 {idx_s}: {it['source']}{hdr_extra} ===\n{cleaned_m[:per_src_budget]}")
            if len(unit_items) > len(prompt_items):
                extra_names = [it["source"] for it in unit_items[len(prompt_items):][:25]]
                src_sections.append(
                    ("=== 其余同项目文件清单（已入全文索引，无需逐篇复述） ===\n" if is_zh else "=== Additional Project Sources (indexed in FTS5) ===\n")
                    + "\n".join(f"- {n}" for n in extra_names)
                )
            combined_excerpt = "\n\n".join(src_sections)
            if is_zh:
                usr_prompt = (
                    f"请将同一主题/项目组（{unit['cluster_label']}）下的 {len(unit_items)} 份一手资料"
                    f"综合编译为一篇高密度主题综述 Wiki 页面（标题：{stem_title}）。\n"
                    f"必须使用如下 YAML frontmatter 开头（保留 `sources`）：\n"
                    f"---\ntitle: \"{stem_title}\"\ntype: synthesis\nstatus: draft\nupdated: {today}\n"
                    f"sources:\n{srcs_yaml_block}\n---\n\n"
                    f"写作要求：总篇幅控制在 900–1600 字，按子主题提炼核心架构、参数指标、关键约束与结论（严禁逐文件流水账）；\n"
                    f"正文包含：`# {stem_title}`、`## 概览`（首句直接概括项目/主题定位与范围）、`## 关键信息`、"
                    f"`## 待核验与口径提示`（最多 3 条实质缺口或版本冲突）、"
                    f"`## 关联页面`（仅可链接已存在页面：{allowed_links}；禁止为不存在的实体或文档加 `[[...]]`）、"
                    f"`## 来源`（列出主要来源）：\n{srcs_md_list}\n\n"
                    f"{combined_excerpt}"
                )
            else:
                usr_prompt = (
                    f"Synthesize the following {len(unit_items)} primary sources under topic '{unit['cluster_label']}' "
                    f"into one cohesive, concise Wiki synthesis page titled '{stem_title}'.\n"
                    f"Start with exact YAML frontmatter:\n"
                    f"---\ntitle: \"{stem_title}\"\ntype: synthesis\nstatus: draft\nupdated: {today}\n"
                    f"sources:\n{srcs_yaml_block}\n---\n\n"
                    f"Requirements: keep to 600-1000 words organized by sub-topic with exact figures/clauses (no file-by-file dump). "
                    f"Include `# {stem_title}`, `## Overview`, `## Key Facts`, `## Caveats` (max 3 genuine items), "
                    f"`## Related Pages` (ONLY link to existing pages: {allowed_links}; do NOT invent `[[...]]` links), "
                    f"and `## Sources` listing:\n{srcs_md_list}\n\n"
                    f"{combined_excerpt}"
                )

        generated = _strip_outer_markdown_fence(caller(sys_prompt, usr_prompt))
        fm_gen, _ = split_frontmatter(generated)
        gen_title_low = (parse_wiki_frontmatter(fm_gen)["title"] or "").strip().lower()
        with state_lock:
            force_unique_title = (stem_title != base_title) or (gen_title_low in existing_titles_lower)
            targets_snap = set(valid_wikilink_targets)
        generated = _ensure_frontmatter_integrity(
            generated,
            required_sources=unit_srcs,
            title=stem_title,
            doc_type=unit["kind"],
            today=today,
            force_title=force_unique_title,
        )
        if overview_link not in generated:
            generated = generated.rstrip() + f"\n\n## {'关联页面' if is_zh else 'Related Pages'}\n\n{overview_link}\n"

        generated = _sanitize_dangling_wikilinks(generated, targets_snap)
        # Write immediately so large batch runs are resumable if interrupted
        target_wp.write_text(generated.rstrip() + "\n", encoding="utf-8")

        fallback_desc = (
            (f"综合 {len(unit_srcs)} 份资料（{unit['cluster_label']}）" if is_zh else f"Synthesized {len(unit_srcs)} sources (`{unit['cluster_label']}`)")
            if len(unit_srcs) > 1
            else (f"提炼自 `{unit_srcs[0]}`" if is_zh else f"Compiled from `{unit_srcs[0]}`")
        )
        cat_desc = _extract_catalog_summary(generated, fallback=fallback_desc)
        return {
            "page_filename": page_filename,
            "stem": target_wp.stem,
            "cat_desc": cat_desc,
            "progress_label": progress_label,
        }

    # 1) Compile newly added uncompiled sources (parallel when eff_workers > 1, deterministic order when == 1)
    if eff_workers <= 1 or len(prepared_units) <= 1:
        for p_info in prepared_units:
            try:
                res_u = _compile_one_unit(p_info)
                with state_lock:
                    existing_titles_lower.add(res_u["stem"].lower())
                    compiled_pages.append(res_u["page_filename"])
                    catalog_additions.append((res_u["stem"], res_u["cat_desc"]))
                    done_steps += 1
                _emit("compiling_new", res_u["progress_label"])
            except Exception as exc:
                with state_lock:
                    failed_units.append({
                        "title": p_info["stem_title"],
                        "sources": [it["source"] for it in p_info["unit"]["items"]],
                        "error": str(exc),
                    })
                    done_steps += 1
                _emit("compiling_new", p_info["stem_title"])
    else:
        ordered_results: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(eff_workers, len(prepared_units))) as pool:
            fut_map = {pool.submit(_compile_one_unit, p_info): idx for idx, p_info in enumerate(prepared_units)}
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                p_info = prepared_units[idx]
                try:
                    res_u = fut.result()
                    ordered_results[idx] = res_u
                    with state_lock:
                        existing_titles_lower.add(res_u["stem"].lower())
                        done_steps += 1
                    _emit("compiling_new", res_u["progress_label"])
                except Exception as exc:
                    with state_lock:
                        failed_units.append({
                            "title": p_info["stem_title"],
                            "sources": [it["source"] for it in p_info["unit"]["items"]],
                            "error": str(exc),
                        })
                        done_steps += 1
                    _emit("compiling_new", p_info["stem_title"])
        for idx in sorted(ordered_results):
            res_u = ordered_results[idx]
            compiled_pages.append(res_u["page_filename"])
            catalog_additions.append((res_u["stem"], res_u["cat_desc"]))

    # If any pre-allocated sibling failed, strip links to uncreated pages so lint stays 0-error
    if failed_units and compiled_pages:
        actual_targets = {
            "overview",
            "知识库总览",
            Path(cfg.catalog_file).stem.lower(),
            Path(cfg.log_file).stem.lower(),
            "agents",
        }
        for wp_now in cfg.wiki_root.glob("*.md"):
            if not wp_now.is_symlink():
                actual_targets.add(wp_now.stem.lower())
        valid_wikilink_targets = actual_targets
        for pname in compiled_pages:
            p_path = cfg.wiki_root / pname
            if p_path.is_file():
                raw_txt = p_path.read_text(encoding="utf-8", errors="replace")
                fixed = _sanitize_dangling_wikilinks(raw_txt, actual_targets)
                if fixed != raw_txt:
                    p_path.write_text(fixed, encoding="utf-8")

    # 2) Refresh stale Wiki pages (concurrent when eff_workers > 1)
    def _refresh_one_stale(s_item: dict[str, Any]) -> str | None:
        wp = cfg.wiki_root / s_item["page"]
        _emit("refreshing_stale", s_item["page"])
        if not wp.is_file():
            return None
        old_raw = wp.read_text(encoding="utf-8", errors="replace")
        src_blobs: list[str] = []
        for s_ref in s_item["sources"]:
            rel = cfg.strip_source_prefix(clean_source_ref(s_ref))
            mp = cfg.mirrors_root / f"{rel}.md"
            if mp.is_file():
                _, mb = split_frontmatter(mp.read_text(encoding="utf-8", errors="replace"))
                cleaned_mb = _compress_mirror_for_llm(mb)
                src_blobs.append(f"--- SOURCE: {s_ref} ---\n{cleaned_mb[:12000]}")
        if not src_blobs:
            return None

        usr_prompt = (
            f"Update this stale Wiki page ({wp.name}) to reflect the updated primary source(s) below. "
            f"Keep it concise (800-1500 chars), set `updated: {today}`, and preserve all valid `sources:` entries and wikilinks.\n\n"
            f"=== Existing Wiki Page ===\n{old_raw[:8000]}\n\n"
            + "\n\n".join(src_blobs)
        )
        updated_md = _strip_outer_markdown_fence(caller(sys_prompt, usr_prompt))
        if not updated_md.strip():
            raise RuntimeError(f"LLM returned empty content while refreshing stale page {wp.name}")
        old_fm, _ = split_frontmatter(old_raw)
        old_meta = parse_wiki_frontmatter(old_fm)
        req_srcs = [cfg.normalize_source_ref(clean_source_ref(s)) for s in s_item.get("sources") or [] if clean_source_ref(s)]
        updated_md = _ensure_frontmatter_integrity(
            updated_md,
            required_sources=req_srcs,
            title=old_meta.get("title") or wp.stem,
            doc_type=old_meta.get("type") or "topic",
            today=today,
            force_title=False,
        )
        updated_md = _sanitize_dangling_wikilinks(updated_md, valid_wikilink_targets)
        wp.write_text(updated_md.rstrip() + "\n", encoding="utf-8")
        return wp.name

    if stale_items:
        if eff_workers <= 1 or len(stale_items) <= 1:
            for s_item in stale_items:
                try:
                    ref_name = _refresh_one_stale(s_item)
                    with state_lock:
                        if ref_name:
                            refreshed_pages.append(ref_name)
                        done_steps += 1
                    _emit("refreshing_stale", s_item["page"])
                except Exception as exc:
                    with state_lock:
                        failed_units.append({"title": s_item["page"], "sources": s_item.get("sources", []), "error": str(exc)})
                        done_steps += 1
                    _emit("refreshing_stale", s_item["page"])
        else:
            ref_ordered: dict[int, str] = {}
            with ThreadPoolExecutor(max_workers=min(eff_workers, len(stale_items))) as pool:
                fut_map = {pool.submit(_refresh_one_stale, s_item): idx for idx, s_item in enumerate(stale_items)}
                for fut in as_completed(fut_map):
                    idx = fut_map[fut]
                    s_item = stale_items[idx]
                    try:
                        ref_name = fut.result()
                        if ref_name:
                            ref_ordered[idx] = ref_name
                        with state_lock:
                            done_steps += 1
                        _emit("refreshing_stale", s_item["page"])
                    except Exception as exc:
                        with state_lock:
                            failed_units.append({"title": s_item["page"], "sources": s_item.get("sources", []), "error": str(exc)})
                            done_steps += 1
                        _emit("refreshing_stale", s_item["page"])
            for idx in sorted(ref_ordered):
                refreshed_pages.append(ref_ordered[idx])

    # 2.5) Consolidate Layer-4 Operational Memory (`notes/` / `记忆/`) into Compiled Wiki
    consolidated_notes: list[str] = []
    if active_notes:
        mem_store = MemoryStore(cfg.root)
        notes_title = "实战经验与勘误汇总" if is_zh else "Operational-Notes-and-Errata"
        notes_wp = cfg.wiki_root / f"{notes_title}.md"
        existing_notes_wiki = notes_wp.read_text(encoding="utf-8", errors="replace") if notes_wp.is_file() else ""
        all_note_srcs: list[str] = []
        note_blobs: list[str] = []
        for n in active_notes[:20]:
            for rs in n.related_sources:
                if rs not in all_note_srcs:
                    all_note_srcs.append(rs)
            note_blobs.append(f"- [{n.note_id} | {n.kind} | author={n.author} | refs={list(n.related_sources)}]: {n.text}")

        srcs_yaml = ("\n" + "\n".join(f"  - {s}" for s in all_note_srcs)) if all_note_srcs else " []"
        note_prompt = (
            f"Consolidate the following verified operational field notes and errata into a clean Wiki synthesis page "
            f"titled `{notes_title}`.\n"
            f"Start with YAML frontmatter:\n---\ntitle: \"{notes_title}\"\ntype: synthesis\nstatus: draft\n"
            f"updated: {today}\nsources:{srcs_yaml}\n---\n\n"
            f"Include `{overview_link}` under Related Pages.\n\n"
            + (f"=== Previous Page ===\n{existing_notes_wiki[:4000]}\n\n" if existing_notes_wiki else "")
            + "=== Pending Operational Notes ===\n"
            + "\n".join(note_blobs)
        )
        try:
            gen_notes_md = _strip_outer_markdown_fence(caller(sys_prompt, note_prompt))
            if not gen_notes_md.startswith("---"):
                gen_notes_md = (
                    f'---\ntitle: "{notes_title}"\ntype: synthesis\nstatus: draft\nupdated: {today}\n'
                    f"sources:{srcs_yaml}\n---\n\n{gen_notes_md}\n"
                )
            gen_notes_md = _enforce_draft_status(gen_notes_md)
            if overview_link not in gen_notes_md:
                gen_notes_md = gen_notes_md.rstrip() + f"\n\n## {'关联页面' if is_zh else 'Related Pages'}\n\n{overview_link}\n"
            valid_wikilink_targets.add(notes_title.lower())
            gen_notes_md = _sanitize_dangling_wikilinks(gen_notes_md, valid_wikilink_targets)
            was_existing = notes_wp.is_file()
            notes_wp.write_text(gen_notes_md.rstrip() + "\n", encoding="utf-8")
            if was_existing:
                if notes_wp.name not in refreshed_pages:
                    refreshed_pages.append(notes_wp.name)
            else:
                compiled_pages.append(notes_wp.name)
                notes_desc = _extract_catalog_summary(
                    gen_notes_md,
                    fallback=("实战经验与原件勘误汇总" if is_zh else "Consolidated operational notes & field errata"),
                )
                catalog_additions.append((notes_wp.stem, notes_desc))

            for n in active_notes[:20]:
                if n.kind != "errata":
                    # Keep `errata` active so 1-hop Errata Pins remain pinned onto read-only primary chunks!
                    mem_store.update_status(n.note_id, "consolidated", consolidated_into=f"{cfg.wiki_prefix}{notes_wp.name}")
                consolidated_notes.append(n.note_id)
        except Exception as exc:
            failed_units.append({"title": notes_title, "sources": all_note_srcs, "error": str(exc)})

    # If the entire batch failed with zero completed pages, surface the root error immediately
    if failed_units and not compiled_pages and not refreshed_pages and not consolidated_notes:
        raise RuntimeError(failed_units[0]["error"])

    # 3) Update catalog (`index.md` / `目录.md`) and append `log.md` / `日志.md`
    if catalog_additions and cfg.catalog_path.is_file():
        cat_txt = cfg.catalog_path.read_text(encoding="utf-8", errors="replace").rstrip()
        new_lines = [f"- [[{stem}]] — {desc}" for stem, desc in catalog_additions if f"[[{stem}]]" not in cat_txt]
        if new_lines:
            cfg.catalog_path.write_text(cat_txt + "\n" + "\n".join(new_lines) + "\n", encoding="utf-8")

    if cfg.log_path.is_file() and (compiled_pages or refreshed_pages):
        log_txt = cfg.log_path.read_text(encoding="utf-8", errors="replace").rstrip()
        header = f"## {today}"
        touched = ", ".join([*compiled_pages, *refreshed_pages])
        row = f"| {now_hm} | compile | {touched} | Compiled {len(compiled_pages)} new, refreshed {len(refreshed_pages)} stale |"
        if header in log_txt:
            cfg.log_path.write_text(log_txt + "\n" + row + "\n", encoding="utf-8")
        else:
            tbl_hdr = (
                "| 时间 | 动作 | 涉及页面/资料 | 说明 |\n| --- | --- | --- | --- |"
                if is_zh
                else "| Time | Action | Targets | Notes |\n| --- | --- | --- | --- |"
            )
            cfg.log_path.write_text(f"{log_txt}\n\n{header}\n{tbl_hdr}\n{row}\n", encoding="utf-8")

    # 4) Snapshot verified hashes, rebuild FTS5 index, then lint
    _emit("rebuilding_index", cfg.name)
    for pname in (*compiled_pages, *refreshed_pages):
        snapshot_wiki_hashes(cfg.root, page=pname)

    idx_summary = rebuild_index(cfg.root)
    lint_report = lint_vault(cfg.root)

    return {
        "vault": cfg.name,
        "compiled_pages": compiled_pages,
        "refreshed_pages": refreshed_pages,
        "consolidated_notes": consolidated_notes,
        "skipped_reviewed_pages": skipped_reviewed_pages,
        "failed_units": failed_units,
        "duplicate_sources_skipped": plan.get("duplicate_sources_skipped", 0),
        "remaining_uncompiled": remaining_uncompiled,
        "remaining_stale": remaining_stale,
        "batch_limit": max_items,
        "lint": lint_report,
        "index": idx_summary,
    }
