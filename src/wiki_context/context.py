"""Deterministic 5-Step Context Pack Engine & Pluggable Access Control.

Transforms a natural-language question into a single citation-anchored Markdown
evidence bundle (`<wiki_context>`) combining:
1. Dynamic complexity routing (`fact` 2.6k vs `complex` 4.8k tokens) + hot-reloaded
   canonical synonym convergence (`synonyms.json`);
2. Parallel Layer-3 (Compiled Wiki) + Layer-2 (Primary Source) FTS5/BM25 retrieval
   with multi-facet entity diversification;
3. Controlled 1-hop Wiki provenance boosting (verified against distinctive core IDF);
4. Bounded adjacent chunk merging (`<=95` lines) + distinct-term IDF sliding window
   with automatic Markdown `table_header` pinning;
5. Zero-hit missing-concept anti-hallucination warnings + unmirrored boundary notes.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .config import VaultConfig
from .memory import MemoryNote, MemoryStore
from .search import (
    ACLFilter,
    BM25Index,
    CJK_RUN_RE,
    EN_STOPWORDS,
    LATIN_NUM_RE,
    MAX_ACL_INDEX_DEPTH,
    NUM_MEASURE_RE,
    _MODAL_PARTICLE_SUFFIX_RE,
    _STOP_QUERY_PHRASE_RE,
    encode_acl_prefix_token,
    is_excluded_source,
    route,
    tokenize,
    tokenize_query,
)


# -- Access Control Policy -------------------------------------------------- #


@dataclass(frozen=True)
class Principal:
    uid: str = "anonymous"
    groups: tuple[str, ...] = ("all",)
    allowed_vaults: tuple[str, ...] = ("*",)

    def can_access_vault(self, vault_id: str) -> bool:
        if "*" in self.allowed_vaults or "admin" in self.groups:
            return True
        return vault_id in self.allowed_vaults


@dataclass(frozen=True)
class ACLDecision:
    allowed: bool
    reason_code: str = "allowed"  # "allowed" | "admin_bypass" | "deny_prefix" | "outside_allowlist" | "wiki_provenance_blocked" | "excluded"
    matched_rule: str = ""
    blocked_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "matched_rule": self.matched_rule,
            "blocked_refs": list(self.blocked_refs),
        }


class AccessPolicy:
    def can_access_source(self, principal: Principal, source: str) -> bool:
        raise NotImplementedError

    def can_access_wiki(
        self, principal: Principal, wiki_source: str, refs: list[str] | tuple[str, ...]
    ) -> bool:
        raise NotImplementedError

    def explain_source_access(self, principal: Principal, source: str) -> ACLDecision:
        ok = self.can_access_source(principal, source)
        return ACLDecision(allowed=ok, reason_code="allowed" if ok else "outside_allowlist")

    def explain_wiki_access(
        self, principal: Principal, wiki_source: str, refs: list[str] | tuple[str, ...]
    ) -> ACLDecision:
        ok = self.can_access_wiki(principal, wiki_source, refs)
        return ACLDecision(allowed=ok, reason_code="allowed" if ok else "wiki_provenance_blocked")


class AllowAllPolicy(AccessPolicy):
    def can_access_source(self, principal: Principal, source: str) -> bool:
        return True

    def can_access_wiki(
        self, principal: Principal, wiki_source: str, refs: list[str] | tuple[str, ...]
    ) -> bool:
        return True

    def explain_source_access(self, principal: Principal, source: str) -> ACLDecision:
        return ACLDecision(allowed=True, reason_code="allowed")

    def explain_wiki_access(
        self, principal: Principal, wiki_source: str, refs: list[str] | tuple[str, ...]
    ) -> ACLDecision:
        return ACLDecision(allowed=True, reason_code="allowed")


class PrefixACLPolicy(AccessPolicy):
    """Directory-prefix ACL (`deny` overrides `allow`) with automatic Wiki provenance inheritance."""

    def __init__(
        self,
        group_rules: dict[str, dict[str, list[str]]] | Callable[[], dict[str, dict[str, list[str]]]],
        *,
        public_wiki_without_sources: bool = True,
        allow_when_unconfigured: bool = False,
    ):
        self._group_rules = group_rules
        self.public_wiki_without_sources = public_wiki_without_sources
        self.allow_when_unconfigured = bool(allow_when_unconfigured)

    @property
    def group_rules(self) -> dict[str, dict[str, list[str]]]:
        return self._group_rules() if callable(self._group_rules) else self._group_rules

    @staticmethod
    def _match_prefix(path: str, prefix: str) -> bool:
        clean_p = prefix.strip().rstrip("*").rstrip("/")
        if not clean_p and "*" in prefix:
            return True
        if not clean_p:
            return False
        return path == clean_p or path.startswith(clean_p + "/")

    def explain_source_access(self, principal: Principal, source: str) -> ACLDecision:
        if "admin" in principal.groups or "all" in principal.groups:
            return ACLDecision(allowed=True, reason_code="admin_bypass", matched_rule="group:admin|all")
        rules = self.group_rules
        if not rules and self.allow_when_unconfigured:
            return ACLDecision(allowed=True, reason_code="allowed", matched_rule="unconfigured_acl_allow_all")
        denies: list[tuple[str, str]] = []
        allows: list[tuple[str, str]] = []
        for g in principal.groups:
            rule = rules.get(g) or {}
            for d in rule.get("deny") or ():
                denies.append((g, str(d)))
            for a in rule.get("allow") or ():
                allows.append((g, str(a)))
        for grp, pat in denies:
            if self._match_prefix(source, pat):
                return ACLDecision(allowed=False, reason_code="deny_prefix", matched_rule=f"{grp}:deny:{pat}")
        for grp, pat in allows:
            if self._match_prefix(source, pat):
                return ACLDecision(allowed=True, reason_code="allowed", matched_rule=f"{grp}:allow:{pat}")
        return ACLDecision(allowed=False, reason_code="outside_allowlist", matched_rule="")

    def can_access_source(self, principal: Principal, source: str) -> bool:
        return self.explain_source_access(principal, source).allowed

    def explain_wiki_access(
        self, principal: Principal, wiki_source: str, refs: list[str] | tuple[str, ...]
    ) -> ACLDecision:
        if "admin" in principal.groups or "all" in principal.groups:
            return ACLDecision(allowed=True, reason_code="admin_bypass", matched_rule="group:admin|all")
        rules = self.group_rules
        if not rules and self.allow_when_unconfigured:
            return ACLDecision(allowed=True, reason_code="allowed", matched_rule="unconfigured_acl_allow_all")
        valid_refs = [
            r for r in refs
            if isinstance(r, str) and (r.startswith("sources/") or r.startswith("史料/") or "/" in r)
        ]
        if not valid_refs:
            if self.public_wiki_without_sources:
                return ACLDecision(allowed=True, reason_code="allowed", matched_rule="public_wiki_no_refs")
            return ACLDecision(allowed=False, reason_code="outside_allowlist", matched_rule="no_public_wiki")
        blocked_refs: list[str] = []
        first_rule = ""
        for ref in valid_refs:
            dec = self.explain_source_access(principal, ref)
            if not dec.allowed:
                blocked_refs.append(ref)
                if not first_rule:
                    first_rule = dec.matched_rule or dec.reason_code
        if blocked_refs:
            return ACLDecision(
                allowed=False,
                reason_code="wiki_provenance_blocked",
                matched_rule=first_rule,
                blocked_refs=tuple(blocked_refs),
            )
        return ACLDecision(allowed=True, reason_code="allowed", matched_rule="wiki_refs_ok")

    def can_access_wiki(
        self, principal: Principal, wiki_source: str, refs: list[str] | tuple[str, ...]
    ) -> bool:
        return self.explain_wiki_access(principal, wiki_source, refs).allowed


def policy_for_vault(cfg: VaultConfig) -> AccessPolicy:
    if cfg.acl_rules:
        return PrefixACLPolicy(cfg.acl_rules)
    return AllowAllPolicy()


def compile_acl_filter(policy: AccessPolicy, principal: Principal) -> ACLFilter:
    """Compile runtime hot-reloaded `AccessPolicy` + `Principal` into a C-level FTS5 `ACLFilter`."""
    if isinstance(policy, AllowAllPolicy):
        return ACLFilter(allow_all=True)
    if isinstance(policy, PrefixACLPolicy):
        if "admin" in principal.groups or "all" in principal.groups:
            return ACLFilter(allow_all=True)
        rules = policy.group_rules
        if not rules and policy.allow_when_unconfigured:
            return ACLFilter(allow_all=True)
        allow_toks: list[str] = []
        deny_toks: list[str] = []
        exact_fts = True
        wild_allow = False
        pred = lambda src: policy.can_access_source(principal, src)  # noqa: E731

        for g in principal.groups:
            rule = rules.get(g) or {}
            for d in rule.get("deny") or ():
                d_str = str(d).strip()
                clean_d = d_str.rstrip("*").rstrip("/").replace("\\", "/").strip("/")
                if not clean_d and "*" in d_str:
                    return ACLFilter(deny_all=True, predicate=pred)
                if not clean_d:
                    continue
                if len([p for p in clean_d.split("/") if p and p != "."]) > MAX_ACL_INDEX_DEPTH:
                    exact_fts = False
                tok_d = encode_acl_prefix_token(clean_d)
                if tok_d not in deny_toks:
                    deny_toks.append(tok_d)

            for a in rule.get("allow") or ():
                a_str = str(a).strip()
                clean_a = a_str.rstrip("*").rstrip("/").replace("\\", "/").strip("/")
                if not clean_a and "*" in a_str:
                    wild_allow = True
                    continue
                if not clean_a:
                    continue
                if len([p for p in clean_a.split("/") if p and p != "."]) > MAX_ACL_INDEX_DEPTH:
                    exact_fts = False
                tok_a = encode_acl_prefix_token(clean_a)
                if tok_a not in allow_toks:
                    allow_toks.append(tok_a)

        if wild_allow:
            allow_toks = ["acl_all"]
        if not allow_toks:
            # Empty allow-set under PrefixACLPolicy means all-deny (`outside_allowlist`)
            return ACLFilter(deny_all=True, predicate=pred)
        return ACLFilter(
            allow_all=False,
            deny_all=False,
            allow_tokens=tuple(allow_toks),
            deny_tokens=tuple(deny_toks),
            exact_fts=exact_fts,
            predicate=pred,
        )

    return ACLFilter(
        allow_all=False,
        deny_all=False,
        exact_fts=False,
        predicate=lambda src: policy.can_access_source(principal, src),
    )


# -- Hot-Reloading Synonym Book --------------------------------------------- #


class SynonymBook:
    """mtime-watched one-way canonical synonym dictionary (`canonical <- aliases`)."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._mtime_ns: int | None = None
        self._entries: list[tuple[list[str], list[str]]] = []

    def _reload_if_needed(self) -> None:
        if self.path is None or not self.path.is_file():
            self._entries = []
            self._mtime_ns = None
            return
        try:
            st = self.path.stat()
        except OSError:
            return
        if st.st_mtime_ns == self._mtime_ns:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        entries: list[tuple[list[str], list[str]]] = []
        raw_groups = data.get("groups") if isinstance(data, dict) else data
        if isinstance(raw_groups, list):
            for item in raw_groups:
                if isinstance(item, dict):
                    cans = [str(x).strip() for x in (item.get("canonical") or ()) if str(x).strip()]
                    als = [str(x).strip() for x in (item.get("aliases") or ()) if str(x).strip()]
                    triggers = list(dict.fromkeys([*cans, *als]))
                    if cans and len(triggers) >= 2:
                        entries.append((cans, triggers))
                elif isinstance(item, list):
                    clean = [str(x).strip() for x in item if str(x).strip()]
                    if len(clean) >= 2:
                        entries.append(([clean[0]], clean))
        self._entries = entries
        self._mtime_ns = st.st_mtime_ns

    @property
    def groups(self) -> list[list[str]]:
        self._reload_if_needed()
        return [triggers for _, triggers in self._entries]

    def anchor_terms(self) -> tuple[str, ...]:
        self._reload_if_needed()
        terms: list[str] = []
        for cans, trigs in self._entries:
            for t in (*cans, *trigs):
                if 2 <= len(t) <= 6 and CJK_RUN_RE.fullmatch(t) and t not in terms:
                    terms.append(t)
        return tuple(sorted(terms, key=len, reverse=True))

    def expand_structured(self, text: str) -> list[tuple[list[str], list[str]]]:
        low = (text or "").lower()
        if not low:
            return []
        self._reload_if_needed()
        matched: list[tuple[list[str], list[str]]] = []
        seen: set[str] = set()
        for cans, triggers in self._entries:
            if any(t.lower() in low for t in triggers):
                added: list[str] = []
                for c in cans:
                    cl = c.lower()
                    if cl not in low and cl not in seen:
                        seen.add(cl)
                        added.append(c)
                if added:
                    matched.append((added, triggers))
        return matched

    def expand(self, text: str) -> list[str]:
        out: list[str] = []
        for added, _ in self.expand_structured(text):
            out.extend(added)
        return out


# -- Query Plan & Concept Extraction ---------------------------------------- #

CHARS_PER_TOKEN = 1.6
DEFAULT_FACT_TOKENS = 2600
DEFAULT_COMPLEX_TOKENS = 4800
MIN_TOKENS = 1000
MAX_TOKENS = 8000

_META_INQUIRY_RE = re.compile(
    r"(?:取值范围|测量范围|允许范围|标准范围|可能原因|主要原因|什么原因|根本原因|"
    r"结果不一致|不一致|是否有关联|有关联|是否有关系|检测手段|优先排查|重点检查|"
    r"需要预警|需要报警|注意事项|哪些方面|怎么处理|如何处理|如何确认|如何验证|什么时候|"
    r"具体信息|详细信息|基本信息|具体内容|什么来着|来着|"
    r"(?:那你|请你|麻烦你|麻烦|帮我|给我|替我|请)?(?:大致|大概|简单|简要|详细|具体|全面|系统地?)?"
    r"(?:讲解|介绍|讲讲|说说|聊聊|梳理|总结|概括|解释|看一下|看看|查一下|查查|找一下|找找)(?:一下|下)?|"
    r"那你给我|请你帮我|麻烦帮我|给我输出出来|输出出来|"
    r"\b(?:what\s+is\s+the|what\s+are\s+the|can\s+you\s+explain|please\s+explain|"
    r"walk\s+me\s+through|tell\s+me\s+about|give\s+me\s+an?\s+overview\s+of|"
    r"allowed\s+range|possible\s+causes?|root\s+cause|how\s+should\s+we)\b)",
    re.I,
)

_TECH_CODE_RE = re.compile(r"^(?:[a-z]{1,6}\d{1,5}|\d{1,4}[a-z]{1,4}|rfc|iso|iec|ieee|gb|tb|uic)$", re.I)

_GENERIC_CORE_WORDS = frozenset({
    "检测", "监测", "系统", "方案", "技术", "参数", "标准", "规范", "要求", "报告", "数据",
    "异常", "问题", "故障", "分析", "判断", "控制", "管理", "状态", "运行", "设备", "检查",
    "排查", "影响", "区别", "差异", "关联", "关系", "规则", "指标", "情况", "方法", "原因",
    "严重", "合格", "正常", "超标", "超限", "背景", "需求", "核心", "内容", "目标", "功能",
    "定位", "概述", "简介", "结论", "结果", "信息", "具体", "详情", "详细", "基本",
    "system", "service", "config", "configuration", "architecture", "design", "spec",
    "specification", "standard", "requirement", "requirements", "background", "overview",
    "summary", "status", "issue", "problem", "error", "method", "value", "values", "limit",
    "limits", "threshold", "range", "default", "policy", "rule", "rules", "process", "data",
})

_COMPOUND_HEAD_ANCHORS = frozenset({
    "报告", "保单", "服务器", "数据库", "说明书", "手册", "合同", "清单", "配置", "账号",
})
_PAREN_ENUM_RE = re.compile(r"[（(]([^（）()]{4,160})[）)]")


@dataclass
class QueryPlan:
    question: str
    route_mode: str
    route_reason: list[str]
    token_budget: int
    char_budget: int
    weighted_tokens: dict[str, float]
    groups: list[set[str]]
    core_weighted_tokens: dict[str, float]
    core_concepts: list[tuple[str, set[str]]]
    facets: list[tuple[str, set[str]]]
    auto_synonyms: list[str]


def _split_cjk_concept(run: str, anchors: tuple[str, ...]) -> list[str]:
    if len(run) <= 6 or not anchors:
        return [run]
    out: list[str] = []
    rem = run
    while rem:
        if len(rem) <= 6:
            if len(rem) >= 2:
                out.append(rem)
            break
        best_anchor = None
        best_pos = len(rem) + 1
        for a in anchors:
            pos = rem.find(a)
            if 0 <= pos < best_pos:
                best_pos = pos
                best_anchor = a
        if best_anchor is None:
            if len(rem) >= 2:
                out.append(rem)
            break
        if 0 < best_pos <= 4 and best_anchor in _COMPOUND_HEAD_ANCHORS:
            out.append(rem[: best_pos + len(best_anchor)])
            rem = rem[best_pos + len(best_anchor):]
            continue
        if best_pos > 0:
            prefix = rem[:best_pos]
            if len(prefix) >= 2:
                out.append(prefix)
        out.append(best_anchor)
        rem = rem[best_pos + len(best_anchor):]

    expanded: list[str] = []
    for seg in (out or [run]):
        if len(seg) <= 8:
            expanded.append(seg)
        else:
            expanded.append(seg[:4])
            expanded.append(seg[-4:])
    return expanded


def _strip_paren_field_lists(q: str) -> tuple[str, list[str]]:
    attr_chunks: list[str] = []

    def _repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        sep_cnt = sum(inner.count(ch) for ch in ("、", ",", "，", ";", "；"))
        if sep_cnt >= 2:
            attr_chunks.append(inner)
            return " "
        return m.group(0)

    stripped = _PAREN_ENUM_RE.sub(_repl, q).strip()
    return stripped, attr_chunks


def build_query_plan(
    question: str,
    *,
    expanded_terms: list[str] | None = None,
    max_tokens: int | None = None,
    synonym_book: SynonymBook | None = None,
    extra_anchors: tuple[str, ...] = (),
) -> QueryPlan:
    q = (question or "").strip()
    decision = route(q)
    mode = decision["mode"]
    if max_tokens is not None:
        budget = max(MIN_TOKENS, min(int(max_tokens), MAX_TOKENS))
    else:
        budget = DEFAULT_COMPLEX_TOKENS if mode == "complex" else DEFAULT_FACT_TOKENS
    char_budget = int(budget * CHARS_PER_TOKEN)

    raw_tokens, groups = tokenize_query(q)
    main_q_cand, attr_chunks = _strip_paren_field_lists(q)
    main_core = _STOP_QUERY_PHRASE_RE.sub(" ", _META_INQUIRY_RE.sub(" ", _MODAL_PARTICLE_SUFFIX_RE.sub(" ", main_q_cand)))
    main_core_toks, _ = tokenize_query(main_core)
    has_distinct_main = bool(attr_chunks and len(set(main_core_toks)) >= 2)

    core_text = _MODAL_PARTICLE_SUFFIX_RE.sub(" ", q)
    core_text = _META_INQUIRY_RE.sub(" ", core_text)
    core_text_clean = _STOP_QUERY_PHRASE_RE.sub(" ", core_text)
    core_tokens_raw, _ = tokenize_query(core_text_clean)
    core_token_set = set(core_tokens_raw)
    main_token_set = set(main_core_toks) if has_distinct_main else core_token_set

    primary_surface_toks = {m.group(0) for m in LATIN_NUM_RE.finditer(q.lower())}
    weights: dict[str, float] = {}
    core_weights: dict[str, float] = {}
    seen_tok_counts: dict[str, int] = {}
    for tok in raw_tokens:
        if tok in core_token_set:
            base_w = 1.45 if _TECH_CODE_RE.match(tok) else 1.0
            if tok not in main_token_set:
                base_w *= 0.42
            # Derived sub-variants of a compound code shouldn't stack 3x-5x weight over other query concepts
            is_derived_sub = (
                bool(re.search(r"[a-z0-9]", tok))
                and tok not in primary_surface_toks
                and any(tok in g and len(g) > 1 for g in groups)
            )
            w = base_w * 0.48 if is_derived_sub else base_w
            occ = seen_tok_counts.get(tok, 0)
            seen_tok_counts[tok] = occ + 1
            inc = w if occ == 0 else w * 0.25
            cap = w * 1.60
            weights[tok] = min(cap, weights.get(tok, 0.0) + inc)
            core_weights[tok] = min(cap, core_weights.get(tok, 0.0) + inc)
        else:
            weights[tok] = max(weights.get(tok, 0.0), 0.30)

    auto_syns: list[str] = []
    if synonym_book is not None:
        for added_cans, all_triggers in synonym_book.expand_structured(q):
            auto_syns.extend(added_cans)
            trig_toks: set[str] = set()
            for tr in all_triggers:
                tt, _ = tokenize_query(tr)
                trig_toks.update(tt)
            syn_toks: list[str] = []
            for ac in added_cans:
                at, _ = tokenize_query(ac)
                syn_toks.extend(at)
            for st_tok in syn_toks:
                weights[st_tok] = max(weights.get(st_tok, 0.0), 0.58)
            merged = False
            for grp in groups:
                if grp & trig_toks:
                    grp.update(syn_toks)
                    merged = True
            if not merged and syn_toks:
                groups.append(set(syn_toks))

    ext_terms = [str(t).strip() for t in (expanded_terms or ()) if str(t).strip()][:8]
    if ext_terms:
        ext_union: set[str] = set()
        for phrase in ext_terms:
            etoks, _ = tokenize_query(phrase)
            for et in etoks:
                weights[et] = max(weights.get(et, 0.0), 0.60)
                ext_union.add(et)
        if ext_union:
            groups.append(ext_union)

    anchors = tuple(dict.fromkeys([
        *extra_anchors,
        *(synonym_book.anchor_terms() if synonym_book is not None else ()),
        *_COMPOUND_HEAD_ANCHORS,
        "标准", "规范", "规则", "接口", "协议", "阈值", "范围", "架构",
    ]))
    clause_filler_re = re.compile(
        r"(?:有一台|有一个|有一份|有一张|有一|有个|我的|你的|他的|她的|咱们|怎么样|怎样|"
        r"与|和|及|或|或者|对|在|从|将|把|被|向|由|跟|同|的|了|着|过|但|而|中|内|上|下|有|是|"
        r"是否|能否|可不可以|有没有|为什么|怎么|如何|哪些|多少|什么|"
        r"连续|超过|低于|高于|大于|小于|达到|通过|判断|发生|出现|存在|进行|使用|采用|导致|引起|说明|需要|应该|可以)+"
    )

    def _extract_concepts_from(text_src: str) -> list[tuple[str, set[str]]]:
        found: list[tuple[str, set[str]]] = []
        local_seen: set[str] = set()
        for m in LATIN_NUM_RE.finditer(text_src):
            raw_lbl = m.group(0)
            low_lbl = raw_lbl.lower()
            if low_lbl in local_seen or low_lbl in EN_STOPWORDS or low_lbl in _GENERIC_CORE_WORDS:
                continue
            if low_lbl.isdigit() and len(low_lbl) <= 2:
                continue
            if len(low_lbl) < 2:
                continue
            toks, _ = tokenize_query(low_lbl)
            if toks:
                local_seen.add(low_lbl)
                found.append((raw_lbl, set(toks)))

        for m in NUM_MEASURE_RE.finditer(text_src.lower()):
            comp = m.group(1) + m.group(2)
            if comp not in local_seen:
                local_seen.add(comp)
                found.append((comp, {comp}))

        for seg in clause_filler_re.split(text_src):
            for run in CJK_RUN_RE.findall(seg):
                if len(run) < 2 or run in _GENERIC_CORE_WORDS:
                    continue
                for sp in _split_cjk_concept(run, anchors):
                    if len(sp) < 2 or len(sp) > 8 or sp in _GENERIC_CORE_WORDS or sp in local_seen:
                        continue
                    sp_toks = set(tokenize(sp))
                    if sp_toks:
                        local_seen.add(sp)
                        found.append((sp, sp_toks))
        return found

    core_concepts = _extract_concepts_from(core_text_clean)
    facet_source_concepts = _extract_concepts_from(main_core) if has_distinct_main else core_concepts
    if not facet_source_concepts:
        facet_source_concepts = core_concepts

    facets: list[tuple[str, set[str]]] = []
    for lbl, tset in facet_source_concepts:
        if not any(tset & prev_set for _, prev_set in facets):
            facets.append((lbl, tset))

    if not core_weights:
        core_weights = dict(weights)

    return QueryPlan(
        question=q,
        route_mode=mode,
        route_reason=decision["reason"],
        token_budget=budget,
        char_budget=char_budget,
        weighted_tokens=weights,
        groups=groups,
        core_weighted_tokens=core_weights,
        core_concepts=core_concepts,
        facets=facets[:4],
        auto_synonyms=auto_syns,
    )


# -- Catalog Cache & Dynamic Cheatsheet ------------------------------------- #


class CatalogCache:
    def __init__(self, cfg: VaultConfig):
        self.cfg = cfg
        self.path = cfg.catalog_path
        self._mtime_ns: int | None = None
        self._cheatsheet: str = ""

    def cheatsheet(self, synonym_book: SynonymBook | None = None) -> str:
        if not self.path.is_file():
            return ""
        try:
            st = self.path.stat()
        except OSError:
            return ""
        if st.st_mtime_ns == self._mtime_ns and self._cheatsheet:
            return self._cheatsheet
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

        topics: list[str] = []
        skip_titles = {"log.md", "index.md", "overview", "日志.md", "目录.md", "知识库总览", "史料清单"}
        for line in raw.splitlines():
            s = line.strip()
            m1 = re.search(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", s)
            if m1:
                t = m1.group(1).strip()
                if t.lower() not in skip_titles and not t.startswith("Clippings/"):
                    topics.append(t)
                continue
            m2 = re.search(r"\[([^\]]+)\]\(([^)]+)\)", s)
            if m2:
                t = m2.group(1).strip()
                if t.lower() not in skip_titles:
                    topics.append(t)

        syn_pairs: list[str] = []
        if synonym_book is not None:
            is_local = (
                synonym_book.path is not None
                and synonym_book.path.parent.resolve() == self.cfg.root.resolve()
            )
            raw_low = raw.lower()
            for grp in synonym_book.groups:
                if is_local or any(term.lower() in raw_low for term in grp):
                    syn_pairs.append(f"{grp[0]}({'/'.join(grp[1:3])})")
                if len(syn_pairs) >= 8:
                    break

        parts: list[str] = []
        if self.cfg.layout == "zh":
            if topics:
                parts.append("库内核心专题：" + "、".join(topics[:14]))
            if syn_pairs:
                parts.append("领域标准术语对照：" + "；".join(syn_pairs[:8]))
            self._cheatsheet = "。".join(parts)[:340]
        else:
            if topics:
                parts.append("Core topics: " + ", ".join(topics[:14]))
            if syn_pairs:
                parts.append("Domain terms: " + "; ".join(syn_pairs[:8]))
            self._cheatsheet = ". ".join(parts)[:360]

        self._mtime_ns = st.st_mtime_ns
        return self._cheatsheet


# -- ContextPackEngine ------------------------------------------------------ #


class ContextPackEngine:
    PROVENANCE_BOOST = 1.25
    ADJACENT_MERGE_GAP = 20
    MAX_MERGED_SPAN_LINES = 95

    def __init__(
        self,
        vault: Path | str,
        *,
        synonym_book: SynonymBook | None = None,
        policy: AccessPolicy | None = None,
        scrub_fn: Callable[[str], str] | None = None,
    ):
        self.cfg = VaultConfig.load(vault)
        self.vault = self.cfg.root
        self.synonym_book = synonym_book or SynonymBook(
            self.cfg.synonyms_path if self.cfg.synonyms_path.is_file() else None
        )
        self.policy = policy or policy_for_vault(self.cfg)
        self.catalog = CatalogCache(self.cfg)
        self._scrub = scrub_fn or (lambda s: s)
        self.memory_store = MemoryStore(self.vault, scrub_fn=self._scrub)
        self._inline_src_re = re.compile(
            rf"(?:{re.escape(self.cfg.sources_dir)}|sources|史料)/[^\s`\"'<>)\]）】；，,]+"
        )

    def domain_cheatsheet(self) -> str:
        return self.catalog.cheatsheet(self.synonym_book)

    def build(
        self,
        snap,
        question: str,
        *,
        expanded_terms: list[str] | None = None,
        max_tokens: int | None = None,
        principal: Principal | None = None,
        resolve_mirror_fn: Callable[[str, str], Path | None] | None = None,
        exclude_patterns: tuple[str, ...] = (),
        include_debug: bool = False,
    ) -> dict:
        t0 = time.perf_counter()
        q = (question or "").strip()
        if not q:
            return {"error": "question must not be empty"}

        user = principal or Principal()
        plan = build_query_plan(
            q,
            expanded_terms=expanded_terms,
            max_tokens=max_tokens,
            synonym_book=self.synonym_book,
            extra_anchors=self.cfg.anchor_terms,
        )
        index: BM25Index = snap.index

        acl_blocked_l2_sources: set[str] = set()
        acl_blocked_wiki_sources: set[str] = set()
        acl_shadow_blocked: list[dict] = []
        seen_shadow_keys: set[tuple[str, str]] = set()

        def allowed_l2(src: str, *, hit: dict | None = None) -> bool:
            if not src or is_excluded_source(src, exclude_patterns):
                return False
            dec = self.policy.explain_source_access(user, src)
            if dec.allowed:
                return True
            acl_blocked_l2_sources.add(src)
            if include_debug and ("l2", src) not in seen_shadow_keys:
                seen_shadow_keys.add(("l2", src))
                acl_shadow_blocked.append({
                    "layer": "primary",
                    "source": src,
                    "heading": str((hit or {}).get("heading") or ""),
                    "score": round(float((hit or {}).get("score") or 0.0), 4),
                    **dec.to_dict(),
                })
            return False

        def allowed_wiki(w: dict) -> bool:
            wsrc = w.get("source") or ""
            refs = w.get("sources") or []
            if not wsrc or is_excluded_source(wsrc, exclude_patterns):
                return False
            if any(isinstance(r, str) and is_excluded_source(r, exclude_patterns) for r in refs):
                return False
            dec = self.policy.explain_wiki_access(user, wsrc, refs)
            if dec.allowed:
                return True
            acl_blocked_wiki_sources.add(wsrc)
            if include_debug and ("wiki", wsrc) not in seen_shadow_keys:
                seen_shadow_keys.add(("wiki", wsrc))
                acl_shadow_blocked.append({
                    "layer": "wiki",
                    "source": wsrc,
                    "heading": str(w.get("heading") or ""),
                    "score": round(float(w.get("score") or 0.0), 4),
                    **dec.to_dict(),
                })
            return False

        # Step 2: Parallel coarse retrieval (with pre-LIMIT C-level FTS5 ACL filtering)
        acl_filter = compile_acl_filter(self.policy, user)
        wiki_max_k = 3 if plan.route_mode == "complex" or len(plan.facets) >= 2 else 2
        raw_wiki = index.search_wiki_weighted(plan.weighted_tokens, plan.groups, k=8, max_per_source=3)
        wiki_candidates = [w for w in raw_wiki if allowed_wiki(w)]

        raw_l2 = index.search_weighted(
            plan.weighted_tokens,
            plan.groups,
            k=28,
            max_per_source=6,
            acl_filter=acl_filter,
            query_text=q,
        )
        # Defense-in-depth assert: re-verify returned rows through Python AccessPolicy
        l2_candidates = [h for h in raw_l2 if allowed_l2(h.get("source") or "", hit=h)]

        if include_debug and not acl_filter.allow_all:
            # Populate Playground X-Ray Shadow Diff for hits pruned inside SQLite FTS5 `acl_pfx`
            for sh_hit in index.search_weighted(plan.weighted_tokens, plan.groups, k=10, max_per_source=3, query_text=q):
                allowed_l2(sh_hit.get("source") or "", hit=sh_hit)

        if len(plan.facets) >= 2:
            wiki_candidates, l2_candidates = self._ensure_facet_candidates(
                index=index,
                plan=plan,
                wiki_candidates=wiki_candidates,
                l2_candidates=l2_candidates,
                allowed_wiki=allowed_wiki,
                allowed_l2=allowed_l2,
                acl_filter=acl_filter,
            )

        # Step 3: Controlled 1-hop Wiki provenance
        wiki_per_source = 1
        if plan.route_mode == "complex" and len(plan.facets) < 2 and wiki_candidates:
            top_w_src = wiki_candidates[0].get("source") or ""
            top_w_sc = float(wiki_candidates[0].get("score") or 0.0)
            other_w_best = max(
                (float(w.get("score") or 0.0) for w in wiki_candidates if w.get("source") != top_w_src),
                default=0.0,
            )
            if top_w_sc >= other_w_best * 1.35:
                wiki_per_source = 2

        selected_wiki = self._select_balanced_hits(
            wiki_candidates, plan.facets, max_k=wiki_max_k, max_per_source=wiki_per_source
        )
        if len(selected_wiki) > 1:
            top_w_best = float(selected_wiki[0].get("score") or 0.0)
            selected_wiki = [
                selected_wiki[0],
                *[
                    w for w in selected_wiki[1:]
                    if float(w.get("score") or 0.0) >= top_w_best * 0.28 or w.get("facet_rescued")
                ],
            ]
        core_tok_set = set(plan.core_weighted_tokens)
        q_idf_sample = (l2_candidates[0].get("_q_idf") if l2_candidates else None) or {}
        core_idfs = [q_idf_sample[t] for t in core_tok_set if t in q_idf_sample]
        if core_idfs:
            idf_floor = max(core_idfs) * 0.45
            dist_core_toks = {t for t in core_tok_set if q_idf_sample.get(t, 0.0) >= idf_floor} or core_tok_set
        else:
            dist_core_toks = core_tok_set

        raw_l2_top_sc = float(l2_candidates[0].get("score") or 0.0) if l2_candidates else 0.0
        if selected_wiki and raw_l2_top_sc >= 20.0:
            top_l2_srcs = {h.get("source") for h in l2_candidates[:3] if h.get("source")}
            high_idf_toks = (
                {t for t in core_tok_set if q_idf_sample.get(t, 0.0) >= max(core_idfs) * 0.78}
                if core_idfs
                else set()
            )
            kept_wiki: list[dict] = []
            for w in selected_wiki:
                w_sc = float(w.get("score") or 0.0)
                if w_sc >= raw_l2_top_sc * 0.22:
                    kept_wiki.append(w)
                    continue
                w_refs = {self.cfg.normalize_source_ref(r) for r in (w.get("sources") or ()) if isinstance(r, str)}
                if w_refs & top_l2_srcs:
                    kept_wiki.append(w)
                    continue
                if high_idf_toks and (set(w.get("matched") or ()) & high_idf_toks):
                    kept_wiki.append(w)
            selected_wiki = kept_wiki

        provenance_sources = self._collect_scoped_provenance(selected_wiki, allowed_l2)

        existing_l2_sources: set[str] = set()
        for h in l2_candidates:
            src = h.get("source") or ""
            existing_l2_sources.add(src)
            if src in provenance_sources:
                matched_core = set(h.get("matched") or ()) & dist_core_toks
                if not matched_core:
                    htoks = set(tokenize(f"{h.get('heading', '')} {h.get('text', '')[:800]}"))
                    matched_core = htoks & dist_core_toks
                if matched_core:
                    h["score"] = round(float(h["score"]) * self.PROVENANCE_BOOST, 4)
                    h["via_wiki"] = True

        probe_groups = [tset for _, tset in plan.core_concepts] or plan.groups
        for p_src in provenance_sources:
            if p_src in existing_l2_sources:
                continue
            for rh in index.score_source_units(p_src, plan.core_weighted_tokens, probe_groups, top_n=1):
                if float(rh.get("score") or 0.0) >= 2.5:
                    rh["via_wiki"] = True
                    l2_candidates.append(rh)
                    existing_l2_sources.add(p_src)

        l2_candidates.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)

        # Step 4: Bounded merge -> elastic selection -> table-header-pinned windowing
        merged_pool = self._merge_adjacent_candidates(l2_candidates, resolve_mirror_fn=resolve_mirror_fn)
        target_l2_k = 6 if plan.route_mode == "complex" else 4
        selected_l2 = self._select_l2_with_elastic_cap(
            merged_pool,
            plan.facets,
            max_k=target_l2_k,
            route_mode=plan.route_mode,
            q_idf=q_idf_sample,
        )

        usable_chars = max(1200, plan.char_budget - 260)
        wiki_char_budget = int(usable_chars * 0.24) if selected_wiki else 0
        l2_char_budget = usable_chars - wiki_char_budget

        formatted_wiki = self._format_wiki_blocks(selected_wiki, plan, wiki_char_budget)
        actual_wiki_chars = sum(len(w["content"]) + 80 for w in formatted_wiki)
        l2_char_budget = max(l2_char_budget, usable_chars - actual_wiki_chars)

        formatted_l2 = self._format_l2_blocks(
            selected_l2, plan, l2_char_budget, snap=snap, resolve_mirror_fn=resolve_mirror_fn
        )

        # Layer-4 Operational Memory & 1-hop Source-Anchored Errata Pins
        retrieved_src_set: set[str] = set()
        for w_blk in formatted_wiki:
            if w_blk.get("source"):
                retrieved_src_set.add(w_blk["source"])  # wiki-anchored notes (e.g. 知识库/x.md)
        for s_blk in formatted_l2:
            retrieved_src_set.add(s_blk["source"])
            for twin in (s_blk.get("also_at") or ()):
                retrieved_src_set.add(twin)

        errata_by_src, standalone_notes = self.memory_store.match_for_context(
            weighted_tokens=plan.weighted_tokens,
            retrieved_sources=retrieved_src_set,
            policy=self.policy,
            principal=user,
        )
        pinned_errata_texts: list[str] = []
        for s_blk in formatted_l2:
            cand_srcs = [s_blk["source"], *(s_blk.get("also_at") or ())]
            pins: list[dict] = []
            seen_pin_ids: set[str] = set()
            for cs in cand_srcs:
                for enote in errata_by_src.get(cs, ()):
                    if enote.note_id not in seen_pin_ids:
                        seen_pin_ids.add(enote.note_id)
                        pins.append({
                            "id": enote.note_id,
                            "author": enote.author,
                            "text": enote.text,
                        })
                        pinned_errata_texts.append(enote.text)
            if pins:
                s_blk["errata_pins"] = pins

        memory_blocks = [
            {
                "id": n.note_id,
                "kind": n.kind,
                "author": n.author,
                "created_at": n.created_at,
                "related_sources": list(n.related_sources),
                "text": n.text,
            }
            for n in standalone_notes
        ]

        # Step 5: Anti-hallucination missing-concept & unmirrored boundary check
        top_l2_sc = max((float(s.get("score") or 0.0) for s in formatted_l2), default=0.0)
        top_w_sc_fmt = max((float(w.get("score") or 0.0) for w in formatted_wiki), default=0.0)
        scoped_l2 = (
            [s for s in formatted_l2 if float(s.get("score") or 0.0) >= top_l2_sc * 0.35]
            if top_l2_sc >= 15.0
            else formatted_l2
        )
        scoped_wiki = (
            [w for w in formatted_wiki if float(w.get("score") or 0.0) >= top_w_sc_fmt * 0.50]
            if top_w_sc_fmt >= 15.0
            else formatted_wiki
        )
        combined_text = "\n".join(
            [
                *(w["content"] + " " + w["heading"] for w in scoped_wiki),
                *(s["content"] + " " + s["heading"] + " " + s["source"] for s in scoped_l2),
                *pinned_errata_texts,
                *(m["text"] for m in memory_blocks),
            ]
        ).lower()
        missing_concepts = self._detect_missing_concepts(plan, combined_text)
        boundary_notes = self._collect_boundary_notes(
            snap,
            formatted_l2,
            plan=plan,
            missing_concepts=missing_concepts,
            top_l2_score=top_l2_sc,
            allowed_l2=allowed_l2,
        )

        context_md = self._render_markdown(
            generation_id=snap.generation_id or "unknown",
            plan=plan,
            wiki_blocks=formatted_wiki,
            l2_blocks=formatted_l2,
            memory_blocks=memory_blocks,
            missing_concepts=missing_concepts,
            boundary_notes=boundary_notes,
        )

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        acl_filtered_count = len(acl_blocked_l2_sources) + len(acl_blocked_wiki_sources)
        out = {
            "vault": self.cfg.name,
            "generation_id": snap.generation_id,
            "route": plan.route_mode,
            "route_reason": plan.route_reason,
            "token_budget": plan.token_budget,
            "auto_synonyms": plan.auto_synonyms,
            "missing_concepts": missing_concepts,
            "boundary_notes": boundary_notes,
            "wiki_count": len(formatted_wiki),
            "primary_count": len(formatted_l2),
            "memory_count": len(memory_blocks) + len(pinned_errata_texts),
            "acl_filtered_count": acl_filtered_count,
            "elapsed_ms": elapsed_ms,
            "wiki_hits": formatted_wiki,
            "primary_hits": formatted_l2,
            "memory_hits": memory_blocks,
            "context_markdown": context_md,
        }
        if include_debug:
            out["plan_debug"] = {
                "route_mode": plan.route_mode,
                "route_reason": list(plan.route_reason),
                "token_budget": plan.token_budget,
                "char_budget": plan.char_budget,
                "weighted_tokens": {k: round(float(v), 3) for k, v in sorted(plan.weighted_tokens.items(), key=lambda kv: -kv[1])[:40]},
                "core_concepts": [[lbl, sorted(toks)] for lbl, toks in plan.core_concepts],
                "facets": [[lbl, sorted(toks)] for lbl, toks in plan.facets],
                "auto_synonyms": list(plan.auto_synonyms),
            }
            out["acl_shadow_blocked"] = acl_shadow_blocked
        return out

    # -- Pipeline Helpers --------------------------------------------------- #

    def _ensure_facet_candidates(
        self,
        *,
        index: BM25Index,
        plan: QueryPlan,
        wiki_candidates: list[dict],
        l2_candidates: list[dict],
        allowed_wiki: Callable[[dict], bool],
        allowed_l2: Callable[[str], bool],
        acl_filter: ACLFilter | None = None,
    ) -> tuple[list[dict], list[dict]]:
        top_l2_text = " ".join(
            f"{h.get('source', '')} {h.get('heading', '')} {h.get('text', '')[:500]}"
            for h in l2_candidates[:6]
        ).lower()
        top_wiki_text = " ".join(
            f"{w.get('source', '')} {w.get('title', '')} {w.get('heading', '')} {w.get('text', '')[:400]}"
            for w in wiki_candidates[:3]
        ).lower()

        seen_l2 = {(h.get("source"), h.get("line_start"), h.get("page_num")) for h in l2_candidates}
        seen_w = {(w.get("source"), w.get("line_start")) for w in wiki_candidates}

        for lbl, f_toks in plan.facets:
            sub_weights = {t: 1.5 for t in f_toks}
            for k_tok, v_w in plan.core_weighted_tokens.items():
                if k_tok not in sub_weights:
                    sub_weights[k_tok] = v_w * 0.25
            sub_groups = [set(f_toks)]

            if lbl.lower() not in top_l2_text and not any(t in top_l2_text for t in f_toks if len(t) >= 2):
                for sh in index.search_weighted(
                    sub_weights,
                    sub_groups,
                    k=3,
                    max_per_source=2,
                    acl_filter=acl_filter,
                    query_text=lbl,
                ):
                    key = (sh.get("source"), sh.get("line_start"), sh.get("page_num"))
                    if key not in seen_l2 and allowed_l2(sh.get("source") or ""):
                        sh["facet_rescued"] = lbl
                        l2_candidates.append(sh)
                        seen_l2.add(key)

            if lbl.lower() not in top_wiki_text and not any(t in top_wiki_text for t in f_toks if len(t) >= 2):
                for sw in index.search_wiki_weighted(sub_weights, sub_groups, k=1, max_per_source=1):
                    wkey = (sw.get("source"), sw.get("line_start"))
                    if wkey not in seen_w and allowed_wiki(sw):
                        sw["facet_rescued"] = lbl
                        wiki_candidates.append(sw)
                        seen_w.add(wkey)

        return wiki_candidates, l2_candidates

    def _collect_scoped_provenance(
        self, selected_wiki: list[dict], allowed_l2: Callable[[str], bool]
    ) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()

        def add_ref(r: str) -> None:
            clean = r.strip().rstrip(".,;，。；)）]")
            if self.cfg.is_source_ref(clean):
                norm = self.cfg.normalize_source_ref(clean)
                if norm not in seen and allowed_l2(norm):
                    seen.add(norm)
                    refs.append(norm)

        top_sc = max((float(w.get("score") or 0.0) for w in selected_wiki), default=0.0)
        for w in selected_wiki:
            sc = float(w.get("score") or 0.0)
            if sc < 3.0:
                continue
            for ih in self._inline_src_re.findall(w.get("text") or ""):
                add_ref(ih)
            if (sc >= 6.0 and sc >= top_sc * 0.65) or w.get("facet_rescued"):
                for s in (w.get("sources") or ())[:12]:
                    if isinstance(s, str):
                        add_ref(s)
        return refs

    def _merge_adjacent_candidates(
        self,
        candidates: list[dict],
        *,
        resolve_mirror_fn: Callable[[str, str], Path | None] | None,
    ) -> list[dict]:
        by_src: dict[str, list[dict]] = {}
        passthrough: list[dict] = []
        for h in candidates:
            if h.get("kind") == "section" and isinstance(h.get("line_start"), int) and isinstance(h.get("line_end"), int):
                by_src.setdefault(h["source"], []).append(dict(h))
            else:
                passthrough.append(dict(h))

        merged_all: list[dict] = list(passthrough)
        for src, chunks in by_src.items():
            chunks.sort(key=lambda x: (x["line_start"], x["line_end"]))
            cur = chunks[0]
            grouped: list[dict] = []
            for nxt in chunks[1:]:
                span = max(cur["line_end"], nxt["line_end"]) - min(cur["line_start"], nxt["line_start"])
                if nxt["line_start"] <= cur["line_end"] + self.ADJACENT_MERGE_GAP and span <= self.MAX_MERGED_SPAN_LINES:
                    new_s = min(cur["line_start"], nxt["line_start"])
                    new_e = max(cur["line_end"], nxt["line_end"])
                    best = cur if float(cur["score"]) >= float(nxt["score"]) else nxt
                    combined = self._read_merged_lines(src, new_s, new_e, resolve_mirror_fn)
                    if not combined:
                        combined = cur.get("text", "") + "\n\n" + nxt.get("text", "")
                    cur = {
                        **best,
                        "line_start": new_s,
                        "line_end": new_e,
                        "score": round(max(float(cur["score"]), float(nxt["score"])) * 1.04, 4),
                        "text": combined,
                        "table_header": cur.get("table_header") or nxt.get("table_header") or "",
                        "via_wiki": bool(cur.get("via_wiki") or nxt.get("via_wiki")),
                    }
                else:
                    grouped.append(cur)
                    cur = nxt
            grouped.append(cur)
            merged_all.extend(grouped)

        merged_all.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return merged_all

    def _read_merged_lines(
        self,
        source: str,
        start: int,
        end: int,
        resolve_mirror_fn: Callable[[str, str], Path | None] | None,
    ) -> str:
        if resolve_mirror_fn is None or not self.cfg.is_source_ref(source) or end - start > 400:
            return ""
        rel = self.cfg.strip_source_prefix(source)
        mirror = resolve_mirror_fn(source, rel)
        if mirror is None or not mirror.is_file():
            return ""
        try:
            lines = mirror.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[max(0, start - 1): min(len(lines), end)])
        except OSError:
            return ""

    def _select_balanced_hits(
        self,
        candidates: list[dict],
        facets: list[tuple[str, set[str]]],
        *,
        max_k: int,
        max_per_source: int,
    ) -> list[dict]:
        if not candidates or max_k <= 0:
            return []
        picked: list[dict] = []
        per_src: dict[str, int] = {}
        reserve = 1 if len(facets) >= 2 and max_k >= 2 else 0
        init_cap = max(1, max_k - reserve)

        for c in candidates:
            src = c.get("source") or ""
            if per_src.get(src, 0) >= max_per_source:
                continue
            picked.append(c)
            per_src[src] = per_src.get(src, 0) + 1
            if len(picked) >= init_cap:
                break

        if len(facets) >= 2 and len(picked) < max_k:
            cov_blob = " ".join(
                f"{p.get('source', '')} {p.get('title', '')} {p.get('heading', '')} {p.get('text', '')[:600]}"
                for p in picked
            ).lower()
            for lbl, f_toks in facets:
                if len(picked) >= max_k:
                    break
                if lbl.lower() in cov_blob or any(t in cov_blob for t in f_toks if len(t) >= 2):
                    continue
                for c in candidates:
                    if c in picked:
                        continue
                    src = c.get("source") or ""
                    if per_src.get(src, 0) >= max_per_source + 1:
                        continue
                    c_blob = f"{c.get('source', '')} {c.get('title', '')} {c.get('heading', '')} {c.get('text', '')[:600]}".lower()
                    if lbl.lower() in c_blob or any(t in c_blob for t in f_toks):
                        picked.append(c)
                        per_src[src] = per_src.get(src, 0) + 1
                        cov_blob += " " + c_blob
                        break

        for c in candidates:
            if len(picked) >= max_k:
                break
            if c in picked:
                continue
            src = c.get("source") or ""
            if per_src.get(src, 0) >= max_per_source:
                continue
            picked.append(c)
            per_src[src] = per_src.get(src, 0) + 1
        return picked

    @staticmethod
    def _fold_into_picked(cand: dict, picked: list[dict], picked_sets: list[set[str]]) -> bool:
        """Return True if ``cand`` is a cross-source near-duplicate of an already-picked chunk (recording provenance)."""
        cand_text = cand.get("text") or ""
        cand_src = cand.get("source") or ""
        if not cand_text or not picked:
            return False
        ctoks = set(tokenize(cand_text[:1200]))
        if len(ctoks) < 10:
            return False
        for p, pset in zip(picked, picked_sets):
            p_src = p.get("source") or ""
            if p_src == cand_src or not pset:
                continue
            overlap = len(ctoks & pset) / min(len(ctoks), len(pset))
            if overlap >= 0.75:
                folded = p.setdefault("_folded_sources", [])
                if cand_src and cand_src not in folded:
                    folded.append(cand_src)
                return True
        return False

    def _select_l2_with_elastic_cap(
        self,
        candidates: list[dict],
        facets: list[tuple[str, set[str]]],
        *,
        max_k: int,
        route_mode: str = "fact",
        q_idf: dict[str, float] | None = None,
    ) -> list[dict]:
        if not candidates or max_k <= 0:
            return []
        top_cand = candidates[0]
        top_src = top_cand.get("source") or ""
        top_sc = float(top_cand.get("score") or 0.0)
        min_ratio = 0.15 if route_mode == "complex" else 0.25

        # Single-subject fact guard: when one clear subject facet exists and top_cand matches its
        # distinctive tokens, prevent sibling documents that miss the subject from diluting budget.
        dist_subj_toks: set[str] = set()
        if route_mode == "fact" and len(facets) == 1 and top_sc >= 20.0:
            f_toks = facets[0][1]
            idf_map = q_idf or {}
            f_idfs = [idf_map[t] for t in f_toks if t in idf_map]
            if f_idfs:
                floor_f = max(f_idfs) * 0.72
                dist_subj_toks = {t for t in f_toks if idf_map.get(t, 0.0) >= floor_f} or set(f_toks)
            else:
                dist_subj_toks = set(f_toks)
            top_blob = f"{top_src} {top_cand.get('heading', '')} {top_cand.get('text', '')[:1200]}".lower()
            if not any(t in top_blob for t in dist_subj_toks):
                dist_subj_toks = set()

        def _eligible_secondary(c: dict) -> bool:
            if c is top_cand:
                return True
            sc = float(c.get("score") or 0.0)
            if top_sc > 0 and sc < top_sc * min_ratio and not c.get("facet_rescued"):
                return False
            c_src = c.get("source") or ""
            if dist_subj_toks and c_src != top_src:
                c_blob = f"{c_src} {c.get('heading', '')} {c.get('text', '')[:1200]}".lower()
                if not any(t in c_blob for t in dist_subj_toks):
                    return False
                if sc < top_sc * 0.60:
                    return False
            return True

        filtered = [c for c in candidates if _eligible_secondary(c)] or [top_cand]
        other_best = max(
            (float(c.get("score") or 0.0) for c in filtered if c.get("source") != top_src),
            default=0.0,
        )
        picked: list[dict] = []
        picked_sets: list[set[str]] = []
        per_src: dict[str, int] = {}
        reserve = 1 if len(facets) >= 2 and max_k >= 3 else 0
        main_cap = max(1, max_k - reserve)

        for c in filtered:
            src = c.get("source") or ""
            cnt = per_src.get(src, 0)
            if src == top_src:
                limit = 4 if float(c.get("score") or 0.0) >= other_best * 1.4 else 3
            else:
                limit = 1 if dist_subj_toks else 3
            if cnt >= limit or self._fold_into_picked(c, picked, picked_sets):
                continue
            picked.append(c)
            picked_sets.append(set(tokenize((c.get("text") or "")[:1200])))
            per_src[src] = cnt + 1
            if len(picked) >= main_cap:
                break

        if len(facets) >= 2 and len(picked) < max_k:
            cov_blob = " ".join(
                f"{p.get('source', '')} {p.get('heading', '')} {p.get('text', '')[:800]}"
                for p in picked
            ).lower()
            for lbl, f_toks in facets:
                if len(picked) >= max_k:
                    break
                if lbl.lower() in cov_blob or any(t in cov_blob for t in f_toks if len(t) >= 2):
                    continue
                for c in filtered:
                    if c in picked or self._fold_into_picked(c, picked, picked_sets):
                        continue
                    c_blob = f"{c.get('source', '')} {c.get('heading', '')} {c.get('text', '')[:800]}".lower()
                    if lbl.lower() in c_blob or any(t in c_blob for t in f_toks):
                        picked.append(c)
                        picked_sets.append(set(tokenize((c.get("text") or "")[:1200])))
                        src = c.get("source") or ""
                        per_src[src] = per_src.get(src, 0) + 1
                        cov_blob += " " + c_blob
                        break

        for c in filtered:
            if len(picked) >= max_k:
                break
            if c in picked:
                continue
            src = c.get("source") or ""
            sec_limit = 1 if (dist_subj_toks and src != top_src) else 3
            if per_src.get(src, 0) >= sec_limit or self._fold_into_picked(c, picked, picked_sets):
                continue
            picked.append(c)
            picked_sets.append(set(tokenize((c.get("text") or "")[:1200])))
            per_src[src] = per_src.get(src, 0) + 1
        return picked

    def _window_block_safely(
        self,
        text: str,
        *,
        plan: QueryPlan,
        q_idf: dict[str, float] | None,
        table_header: str,
        max_chars: int,
        base_line_start: int | None,
    ) -> tuple[str, int | None, int | None, bool]:
        if not text:
            return "", base_line_start, base_line_start, False
        if len(text) <= max_chars:
            out = self._scrub(text)
            end_l = (base_line_start + text.count("\n")) if base_line_start is not None else None
            return out, base_line_start, end_l, False

        raw_lines = text.splitlines()
        if not raw_lines:
            return "", base_line_start, base_line_start, False

        # Split compressed single-line HTML `<table><tr>...</tr></table>` rows into virtual sub-lines
        # while preserving exact 1-based file line offsets for `wiki_read` alignment.
        lines: list[str] = []
        line_offsets: list[int] = []
        html_tbl_hdr = ""
        for orig_idx, raw_ln in enumerate(raw_lines):
            if len(raw_ln) > 300 and "</tr>" in raw_ln.lower() and "<tr" in raw_ln.lower():
                parts = [p for p in re.split(r"(?i)(?<=</tr>)\s*(?=<tr\b)", raw_ln) if p]
                if len(parts) >= 2:
                    if not html_tbl_hdr and len(parts[0]) <= 320:
                        html_tbl_hdr = parts[0]
                    for pt in parts:
                        lines.append(pt)
                        line_offsets.append(orig_idx)
                    continue
            lines.append(raw_ln)
            line_offsets.append(orig_idx)

        hdr = (table_header or "").strip() or html_tbl_hdr
        hdr_low = hdr.lower()
        hdr_cost = (len(hdr) + 2) if hdr else 0
        body_budget = max(240, max_chars - hdr_cost)

        tok_val: dict[str, float] = {}
        for tok, qw in plan.weighted_tokens.items():
            idf = (q_idf or {}).get(tok, 1.0)
            base_v = qw * max(0.25, idf)
            # Because `table_header` is automatically pinned above any window slice,
            # column-header tokens already present in `hdr` must not anchor the window
            # at row 0 at the expense of row-specific entity keys deeper in the table.
            if hdr_low and tok in hdr_low:
                base_v *= 0.08
            tok_val[tok] = base_v

        line_tok_sets: list[set[str]] = []
        line_lens: list[int] = []
        for ln in lines:
            low_ln = ln.lower()
            matched_t = {t for t in tok_val if t in low_ln}
            if not matched_t:
                matched_t = set(tokenize(low_ln)) & set(tok_val)
            line_tok_sets.append(matched_t)
            line_lens.append(len(ln) + 1)

        best_l, best_r = 0, 0
        best_unique_sc = -1.0
        best_total_hits = -1
        cur_chars = 0
        active_counts: dict[str, int] = {}
        unique_sc = 0.0
        total_hits = 0
        left = 0

        for right in range(len(lines)):
            cur_chars += line_lens[right]
            for tok in line_tok_sets[right]:
                prev = active_counts.get(tok, 0)
                if prev == 0:
                    unique_sc += tok_val[tok]
                active_counts[tok] = prev + 1
                total_hits += 1
            while left < right and cur_chars > body_budget:
                cur_chars -= line_lens[left]
                for tok in line_tok_sets[left]:
                    active_counts[tok] -= 1
                    total_hits -= 1
                    if active_counts[tok] == 0:
                        unique_sc -= tok_val[tok]
                left += 1
            if (unique_sc > best_unique_sc + 1e-9) or (
                abs(unique_sc - best_unique_sc) <= 1e-9 and total_hits > best_total_hits
            ):
                best_unique_sc = unique_sc
                best_total_hits = total_hits
                best_l, best_r = left, right

        # Expand window within `body_budget` if the winning span is very short
        used_chars = sum(line_lens[best_l: best_r + 1])
        while best_r + 1 < len(lines) and used_chars + line_lens[best_r + 1] <= body_budget:
            best_r += 1
            used_chars += line_lens[best_r]
        while best_l > 0 and used_chars + line_lens[best_l - 1] <= body_budget:
            best_l -= 1
            used_chars += line_lens[best_l]

        selected_slice = "\n".join(lines[best_l: best_r + 1])
        if len(selected_slice) > body_budget + 200:
            low_s = selected_slice.lower()
            top_toks = sorted(tok_val, key=lambda t: tok_val[t], reverse=True)
            pos = next((low_s.find(t) for t in top_toks if low_s.find(t) >= 0), 0)
            s_idx = max(0, min(pos - body_budget // 3, len(selected_slice) - body_budget))
            selected_slice = selected_slice[s_idx: s_idx + body_budget]

        is_full_span = (best_l == 0 and best_r == len(lines) - 1 and len(selected_slice) >= len(text) - 2)
        prefix = f"{hdr}\n" if (hdr and best_l > 0 and not selected_slice.lstrip().startswith(hdr[:40])) else ""
        out_text = self._scrub(prefix + selected_slice)
        shown_s = (base_line_start + line_offsets[best_l]) if base_line_start is not None else None
        shown_e = (base_line_start + line_offsets[best_r]) if base_line_start is not None else None
        return out_text, shown_s, shown_e, (not is_full_span)

    def _format_wiki_blocks(self, wiki_hits: list[dict], plan: QueryPlan, total_budget: int) -> list[dict]:
        if not wiki_hits or total_budget <= 0:
            return []
        per_budget = max(550, total_budget // len(wiki_hits))
        out: list[dict] = []
        for idx, w in enumerate(wiki_hits, 1):
            content, shown_s, shown_e, windowed = self._window_block_safely(
                w.get("text") or "",
                plan=plan,
                q_idf=w.get("_q_idf"),
                table_header=w.get("table_header") or "",
                max_chars=per_budget,
                base_line_start=w.get("line_start"),
            )
            out.append({
                "id": f"W{idx}",
                "source": w["source"],
                "title": w.get("title") or "",
                "heading": w.get("heading") or "",
                "line_start": shown_s if shown_s is not None else w.get("line_start"),
                "line_end": shown_e if shown_e is not None else w.get("line_end"),
                "freshness": w.get("freshness") or "unverified",
                "score": w.get("score"),
                "windowed": windowed,
                "content": content,
            })
        return out

    def _format_l2_blocks(
        self,
        l2_hits: list[dict],
        plan: QueryPlan,
        total_budget: int,
        *,
        snap,
        resolve_mirror_fn: Callable[[str, str], Path | None] | None = None,
    ) -> list[dict]:
        if not l2_hits or total_budget <= 0:
            return []
        n = len(l2_hits)
        s0 = float(l2_hits[0].get("score") or 0.0)
        s1 = float(l2_hits[1].get("score") or 0.0) if n > 1 else 0.0
        lead_w = 1.85 if (n > 1 and s1 > 0 and s0 >= s1 * 1.55) else 1.40
        weights = [lead_w if i == 0 else (1.05 if i == 1 else 0.85) for i in range(n)]
        w_sum = sum(weights) or 1.0

        # Water-filling budget allocation: recycle unused chars from compact blocks to longer top hits
        allocs = [max(480, int(total_budget * (wt / w_sum))) for wt in weights]
        text_lens = [len(h.get("text") or "") + len(h.get("table_header") or "") + 16 for h in l2_hits]
        surplus = 0
        needy_indices: list[int] = []
        for i in range(n):
            if text_lens[i] < allocs[i]:
                freed = allocs[i] - max(240, text_lens[i])
                if freed > 0:
                    allocs[i] -= freed
                    surplus += freed
            elif text_lens[i] > allocs[i]:
                needy_indices.append(i)
        if surplus > 0 and needy_indices:
            needy_w_sum = sum(weights[i] for i in needy_indices) or 1.0
            for i in needy_indices:
                allocs[i] += int(surplus * (weights[i] / needy_w_sum))

        dup_map = getattr(snap, "duplicates_by_source", {}) or {}
        assets_map = getattr(snap, "assets_by_source", {}) or {}
        docs_by_src = {r.get("source"): r for r in (getattr(snap, "docs", ()) or ()) if isinstance(r, dict)}
        mirror_lines_cache: dict[str, int] = {}

        def _doc_total_lines(src_name: str) -> int | None:
            if src_name in mirror_lines_cache:
                return mirror_lines_cache[src_name]
            d_row = docs_by_src.get(src_name) or {}
            if isinstance(d_row.get("lines"), int) and d_row["lines"] > 0:
                mirror_lines_cache[src_name] = d_row["lines"]
                return d_row["lines"]
            if resolve_mirror_fn is not None and self.cfg.is_source_ref(src_name):
                rel_p = self.cfg.strip_source_prefix(src_name)
                mp = resolve_mirror_fn(src_name, rel_p)
                if mp is not None and mp.is_file():
                    try:
                        cnt = len(mp.read_text(encoding="utf-8", errors="replace").splitlines())
                        mirror_lines_cache[src_name] = cnt
                        return cnt
                    except OSError:
                        pass
            return None

        out: list[dict] = []

        for idx, (h, alloc) in enumerate(zip(l2_hits, allocs), 1):
            content, shown_s, shown_e, windowed = self._window_block_safely(
                h.get("text") or "",
                plan=plan,
                q_idf=h.get("_q_idf"),
                table_header=h.get("table_header") or "",
                max_chars=alloc,
                base_line_start=h.get("line_start"),
            )
            src = h["source"]
            tot_l = _doc_total_lines(src)
            sec_s = h.get("line_start")
            sec_e = h.get("line_end")
            if tot_l is not None:
                if isinstance(shown_e, int) and shown_e > tot_l:
                    shown_e = tot_l
                if isinstance(sec_e, int) and sec_e > tot_l:
                    sec_e = tot_l

            entry: dict = {
                "id": f"S{idx}",
                "source": src,
                "kind": h.get("kind") or "section",
                "heading": h.get("heading") or "",
                "line_start": shown_s if shown_s is not None else sec_s,
                "line_end": shown_e if shown_e is not None else sec_e,
                "section_line_start": sec_s,
                "section_line_end": sec_e,
                "page_num": h.get("page_num"),
                "score": h.get("score"),
                "via_wiki": bool(h.get("via_wiki")),
                "windowed": windowed,
                "content": content,
            }
            if windowed and isinstance(shown_s, int) and isinstance(shown_e, int):
                rm_start = max(1, shown_s - 40)
                rm_end = min(shown_e + 80, tot_l) if tot_l is not None else (shown_e + 80)
                already_covers_all = (
                    tot_l is not None
                    and shown_s <= (sec_s or 1)
                    and shown_e >= tot_l
                )
                if not already_covers_all and rm_end >= rm_start:
                    entry["read_more"] = {
                        "source": src,
                        "start": rm_start,
                        "end": rm_end,
                    }
            elif h.get("kind") == "page" and h.get("page_num") is not None and windowed:
                entry["read_more"] = {"source": src, "page_num": h.get("page_num")}

            dups = list(dup_map.get(src) or ())
            for fs in (h.get("_folded_sources") or ()):
                if fs not in dups and fs != src:
                    dups.append(fs)
            if dups:
                entry["also_at"] = dups[:4]
            v_assets = assets_map.get(src)
            if v_assets:
                entry["visual_assets"] = [str(a.get("asset_id") or "") for a in v_assets[:4] if a.get("asset_id")]
            out.append(entry)
        return out

    def _detect_missing_concepts(self, plan: QueryPlan, combined_text_lower: str) -> list[str]:
        if not plan.core_concepts:
            return []
        compact_combined = re.sub(r"[.\-/+_\s]+", "", combined_text_lower)
        missing: list[str] = []
        for lbl, tset in plan.core_concepts:
            low_lbl = lbl.lower()
            if low_lbl in _GENERIC_CORE_WORDS or low_lbl in EN_STOPWORDS:
                continue
            if low_lbl in combined_text_lower:
                continue
            compact_lbl = re.sub(r"[.\-/+_\s]+", "", low_lbl)
            if len(compact_lbl) >= 3 and compact_lbl in compact_combined:
                continue
            syns = self.synonym_book.expand(lbl)
            if any(s.lower() in combined_text_lower for s in syns):
                continue
            if len(low_lbl) >= 3 and len(tset) >= 2 and all(t in combined_text_lower for t in tset):
                continue
            if 2 <= len(low_lbl) <= 3 and CJK_RUN_RE.fullmatch(low_lbl):
                if re.search(rf"{re.escape(low_lbl[0])}[\u4e00-\u9fff]{re.escape(low_lbl[1:])}", combined_text_lower):
                    continue
            if len(low_lbl) >= 4 and CJK_RUN_RE.fullmatch(low_lbl):
                bgs = [low_lbl[i:i + 2] for i in range(len(low_lbl) - 1)]
                hits = sum(1 for bg in bgs if bg in combined_text_lower)
                p1, p2 = re.escape(low_lbl[:2]), re.escape(low_lbl[2:])
                near_4 = (
                    len(low_lbl) == 4
                    and bool(re.search(rf"{p1}[^\n]{{0,16}}{p2}|{p2}[^\n]{{0,16}}{p1}", combined_text_lower))
                )
                if near_4 or (len(bgs) >= 4 and hits >= len(bgs) - 1):
                    continue
            missing.append(lbl)
        return missing

    def _collect_boundary_notes(
        self,
        snap,
        formatted_l2: list[dict],
        *,
        plan: QueryPlan | None = None,
        missing_concepts: list[str] | None = None,
        top_l2_score: float = 0.0,
        allowed_l2: Callable[[str], bool] | None = None,
    ) -> list[str]:
        notes: list[str] = []
        summary = getattr(snap, "summary", {}) or {}
        skipped_srcs = summary.get("policy_skipped_sources") or []
        skipped_img_dirs: dict[str, int] = summary.get("skipped_image_dirs") or {}
        failed_srcs = summary.get("failed_sources") or []
        is_zh = self.cfg.layout == "zh"

        hit_dirs: set[str] = set()
        hit_sources: set[str] = set()
        for b in formatted_l2:
            src = b["source"]
            hit_sources.add(src)
            parent = str(PurePosixPath(src).parent)
            if parent and parent != ".":
                hit_dirs.add(parent)

        reported_img_dirs: set[str] = set()
        for d in sorted(hit_dirs):
            cnt = sum(1 for s in skipped_srcs if str(PurePosixPath(s).parent) == d) + int(skipped_img_dirs.get(d) or 0)
            if cnt > 0:
                if d in skipped_img_dirs:
                    reported_img_dirs.add(d)
                notes.append(
                    f"目录「{d}」下另有 {cnt} 个图片/非文档文件未做全文收录"
                    if is_zh
                    else f"Directory '{d}' has {cnt} non-indexed image/binary file(s)"
                )

        # Surface unindexed image directories when query concepts are missing or top hits are weak
        if skipped_img_dirs and (missing_concepts or top_l2_score < 15.0):
            q_toks = set(plan.weighted_tokens) if plan is not None else set()
            ranked_dirs: list[tuple[int, int, str]] = []
            for img_d, img_cnt in skipped_img_dirs.items():
                if img_d in reported_img_dirs or int(img_cnt or 0) <= 0:
                    continue
                if allowed_l2 is not None and not allowed_l2(f"{img_d}/_"):
                    continue
                d_toks = set(tokenize(img_d.lower()))
                overlap = len(q_toks & d_toks)
                if overlap > 0 or missing_concepts or top_l2_score < 8.0:
                    ranked_dirs.append((overlap, int(img_cnt), img_d))
            ranked_dirs.sort(key=lambda item: (-item[0], -item[1], item[2]))
            for overlap, img_cnt, img_d in ranked_dirs[:2]:
                if overlap == 0 and len(notes) >= 2:
                    break
                notes.append(
                    f"源目录「{img_d}」下有 {img_cnt} 张图片默认未做文字入库（可按路径调 wiki_inspect_visual 查看，或在 vault.json 的 ocr_image_dirs 中开启入库）"
                    if is_zh
                    else f"Source directory '{img_d}' has {img_cnt} unindexed image(s) (view via wiki_inspect_visual or enable in vault.json ocr_image_dirs)"
                )

        docs_by_src = {r.get("source"): r for r in (getattr(snap, "docs", ()) or ())}
        for src in sorted(hit_sources):
            doc_row = docs_by_src.get(src) or {}
            vpages = doc_row.get("visual_pages") or []
            if vpages:
                notes.append(
                    f"「{src}」含 {len(vpages)} 页图表/视觉版式，文字镜像可能不含图中细节"
                    if is_zh
                    else f"'{src}' contains {len(vpages)} visual/diagram page(s); plain-text mirror may omit figure details"
                )

        if failed_srcs and len(notes) < 3:
            notes.append(
                f"全库另有 {len(failed_srcs)} 份史料转换失败未入索引"
                if is_zh
                else f"Vault has {len(failed_srcs)} failed source extraction(s) excluded from index"
            )
        return notes[:4]

    def _render_markdown(
        self,
        *,
        generation_id: str,
        plan: QueryPlan,
        wiki_blocks: list[dict],
        l2_blocks: list[dict],
        memory_blocks: list[dict] | None = None,
        missing_concepts: list[str],
        boundary_notes: list[str],
    ) -> str:
        is_zh = self.cfg.layout == "zh"
        lines: list[str] = [
            f'<wiki_context vault="{self.cfg.name}" route="{plan.route_mode}" generation="{generation_id}">'
        ]
        if plan.auto_synonyms:
            label = "术语自动对齐" if is_zh else "Auto-aligned synonyms"
            lines.append(f"<!-- {label}: {', '.join(plan.auto_synonyms)} -->")

        if wiki_blocks:
            lines.append(
                "## 一、 知识库编译导读（二级综述，具体数值与条款以下方一手史料为准）"
                if is_zh
                else "## 1. Compiled Wiki Synthesis (Verify exact figures & clauses against Primary Sources below)"
            )
            for w in wiki_blocks:
                loc = f"L{w['line_start']}-L{w['line_end']}" if w.get("line_start") else ""
                f_state = w["freshness"]
                if f_state in {"stale", "missing"}:
                    badge = f"⚠️ {f_state}:源史料已变更，须核对下方原件" if is_zh else f"⚠️ {f_state}: source changed, verify primary"
                else:
                    badge = f"freshness:{f_state}"
                lines.append(f"### [{w['id']}] {w['source']} § {w['heading']} ({loc}) [{badge}]")
                lines.append(w["content"].strip())
                lines.append("")

        lines.append(
            "## 二、 一手史料原文证据（权威事实源）"
            if is_zh
            else "## 2. Primary Source Evidence (Authoritative Ground Truth)"
        )
        if not l2_blocks:
            lines.append(
                "*(未命中直接相关的一手史料片段)*\n" if is_zh else "*(No directly matching primary source chunks found)*\n"
            )
        else:
            for s in l2_blocks:
                meta_parts = [s["source"]]
                if s.get("heading"):
                    meta_parts.append(s["heading"])
                if s.get("line_start") is not None and s.get("line_end") is not None:
                    loc_s = f"L{s['line_start']}-L{s['line_end']}"
                    if s.get("windowed") and s.get("section_line_end"):
                        sec_lbl = "本节" if is_zh else "section"
                        loc_s += f" ({sec_lbl} L{s['section_line_start']}-L{s['section_line_end']})"
                    meta_parts.append(loc_s)
                elif s.get("page_num") is not None:
                    meta_parts.append(f"Page {int(s['page_num']) + 1}")

                tag_s = (" [Wiki关联溯源]" if is_zh else " [via Wiki provenance]") if s.get("via_wiki") else ""
                rm = s.get("read_more")
                more_s = ""
                if rm:
                    lbl = "继续深读" if is_zh else "Read more"
                    if "start" in rm:
                        more_s = f' | {lbl}: wiki_read(source="{rm["source"]}", start={rm["start"]}, end={rm["end"]})'
                    elif "page_num" in rm:
                        more_s = f' | {lbl}: wiki_read(source="{rm["source"]}", page_num={rm["page_num"]})'

                lines.append(f"### [{s['id']}] {' | '.join(meta_parts)}{tag_s}{more_s}")
                if s.get("also_at"):
                    dup_lbl = "同内容副本亦见" if is_zh else "Identical/near-duplicate copies"
                    lines.append(f"> {dup_lbl}: {', '.join(s['also_at'])}")
                if s.get("visual_assets"):
                    va_list = ", ".join(s["visual_assets"])
                    if is_zh:
                        lines.append(f'> 🖼️ 关联高清附图: {va_list}（可按需调用 wiki_inspect_visual(source="{s["source"]}") 查看原图）')
                    else:
                        lines.append(f'> 🖼️ Visual figures available: {va_list} (call wiki_inspect_visual(source="{s["source"]}") to view image)')
                for ep in (s.get("errata_pins") or ()):
                    if is_zh:
                        lines.append(f"> 📌 [原件勘误/变更通知 {ep['id']} by {ep['author']}]: {ep['text']}")
                    else:
                        lines.append(f"> 📌 [Errata / Field Override {ep['id']} by {ep['author']}]: {ep['text']}")
                lines.append(s["content"].strip())
                lines.append("")

        if memory_blocks:
            lines.append(
                "## 附加、 领域实战经验与工作记忆（待沉淀笔记，请结合一手史料核验）"
                if is_zh
                else "## Operational Memory & Field Notes (Uncompiled Session/Team Notes — Verify Against Primary Sources)"
            )
            for m in memory_blocks:
                refs_s = f" (refs: {', '.join(m['related_sources'])})" if m.get("related_sources") else ""
                lines.append(f"- **[{m['id']} | {m['kind']} by {m['author']}]**{refs_s}: {m['text']}")
            lines.append("")

        lines.append(
            "## 三、 缺口诊断与作答硬约束（必读）"
            if is_zh
            else "## 3. Gap Diagnostics & Grounding Guardrails (Mandatory)"
        )
        if missing_concepts:
            if is_zh:
                quoted = "、".join(f"「{c}」" for c in missing_concepts)
                lines.append(
                    f"- ⚠️ 缺词预警：提问中的关键概念 {quoted} 在本次召回的全部原文中均未出现（0 命中）！"
                    "极可能属于库内资料空白，请严格核对，切勿用外部常识脑补数值或条款。"
                )
            else:
                quoted = ", ".join(f"'{c}'" for c in missing_concepts)
                lines.append(
                    f"- ⚠️ Missing-Concept Alert: Key query concept(s) {quoted} had 0 hits across all retrieved chunks! "
                    "This likely indicates a knowledge gap in the vault — do NOT hallucinate figures or clauses from pretraining."
                )
        for bn in boundary_notes:
            if is_zh:
                lines.append(f"- 覆盖边界：{bn}（索引未提及 ≠ 原件不存在）。")
            else:
                lines.append(f"- Coverage Boundary: {bn} (unindexed != absent in raw file).")

        if is_zh:
            lines.append(
                "- 可答性铁律：请逐条核对你的论断是否被上方 [S1]~[Sn] 直接支撑并标注出处；"
                "凡原文未明确记载的数值、条款或结论，必须直说「库内现有资料不足以确认」，严禁使用预训练常识补全。"
            )
        else:
            lines.append(
                "- Grounding Rule: Every claim must be directly supported by [S1]~[Sn] above with explicit source citations; "
                "if a figure, clause, or conclusion is not stated in the retrieved text, state clearly that the vault lacks sufficient evidence."
            )
        lines.append("</wiki_context>")
        return "\n".join(lines)
