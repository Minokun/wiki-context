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
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import VaultConfig, format_domain_profile
from .memory import MemoryNote, MemoryStore
from .synonym_miner import _valid_term as _valid_synonym_term
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
    r"简要说明|简述|项目背景|项目基本情况|基本情况|交付范围|设备构成|"
    r"最近进展|最新进展|最新动态|当前进展|进展情况|主要区别|技术项点要求|各项技术要求|编写要求|"
    r"(?:那你|请你|麻烦你|麻烦|帮我|给我|替我|请)?(?:大致|大概|简单|简要|详细|具体|全面|系统地?)?"
    r"(?:讲解|介绍|讲讲|说说|聊聊|梳理|总结|概括|解释|列出|指出|给出|看一下|看看|查一下|查查|找一下|找找)(?:一下|下)?|"
    r"那你给我|请你帮我|麻烦帮我|给我输出出来|输出出来|"
    r"\b(?:what\s+is\s+the|what\s+are\s+the|can\s+you\s+explain|please\s+explain|"
    r"walk\s+me\s+through|tell\s+me\s+about|give\s+me\s+an?\s+overview\s+of|"
    r"allowed\s+range|possible\s+causes?|root\s+cause|how\s+should\s+we)\b)",
    re.I,
)

_TECH_CODE_RE = re.compile(r"^(?:[a-z]{1,6}\d{1,5}|\d{1,4}[a-z]{1,4}|rfc|iso|iec|ieee|gb|tb|uic|en|din|dl)$", re.I)

_STRUCTURED_STD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:GB(?:/T)?|TB(?:/T)?|DL(?:/T)?|JB(?:/T)?|NB(?:/T)?|JT(?:/T)?|CJ(?:/T)?|"
    r"Q/[A-Z0-9]+|ISO(?:/IEC)?|IEC|IEEE|EN|DIN|UIC|JJG|JJF|RFC)\s*[-–—]?\s*\d{2,6}(?:[.\-–—]\d{1,5})*(?![A-Za-z0-9])",
    re.I,
)

_CJK_INTERROGATIVE_RE = re.compile(
    r"(?:哪个|哪几|哪两|哪三|哪家|哪天|哪年|哪月|哪日|哪一|哪类|哪些|哪儿|哪里|"
    r"什么|多少|几个|几部分|几类|几种|几位|几项|几套|几台|是否|能否|可否|"
    r"如何|怎么|怎样|为什么|为何|有没有|是不是|请问|请列出|告诉我)"
)
_SPURIOUS_TRAILING_CJK_RE = re.compile(r"[的了着过在用做以和与及向从对把被是有为里中上下项]$")

_EXAM_HEADING_RE = re.compile(
    r"(?:复习(?:思考)?题|思考题|简答题|问答题|选择题|填空题|判断题|计算题|习题|课后练习|练习题|自测题|模拟题|考试题|试题|课堂训练|巩固练习|测评|思考与练习|单元练习|课后题|自测)"
)
_QUERY_WANTS_EXAM_RE = re.compile(r"(?:复习题|思考题|简答题|习题|考题|试题|练习题|自测|选择题|填空题|判断题)")

_GENERIC_CORE_WORDS = frozenset({
    "检测", "监测", "系统", "方案", "技术", "参数", "标准", "规范", "要求", "报告", "数据",
    "异常", "问题", "故障", "分析", "判断", "控制", "管理", "状态", "运行", "设备", "检查",
    "排查", "影响", "区别", "差异", "关联", "关系", "规则", "指标", "情况", "方法", "原因",
    "严重", "合格", "正常", "超标", "超限", "背景", "需求", "核心", "内容", "目标", "功能",
    "定位", "概述", "简介", "结论", "结果", "信息", "具体", "详情", "详细", "基本",
    "简要", "简明", "大致", "整体", "总体", "进展", "动态", "方面", "相关", "主要",
    "简称", "全称", "名称", "标准名称", "主题", "编号", "物料", "物料编号", "项目编号", "合同编号",
    "时间", "日期", "签订时间", "签订日期", "交货期", "完成时限", "同时", "两家", "三家", "参会", "我方", "对方",
    "改动", "变化", "修改", "字母", "开头", "字母开头", "组成", "部分", "分别", "各自",
    "验收要求", "编写要求", "技术要求", "技术项点要求", "各项技术要求", "文档编写规范", "模板章节要求",
    "基本情况", "项目背景", "交付范围", "设备构成", "最近进展", "最新进展", "最新动态", "项目基本情况",
    "system", "service", "config", "configuration", "architecture", "design", "spec",
    "specification", "standard", "requirement", "requirements", "background", "overview",
    "summary", "status", "issue", "problem", "error", "method", "value", "values", "limit",
    "limits", "threshold", "range", "default", "policy", "rule", "rules", "process", "data",
    "article", "articles", "report", "reports", "reported", "reporting", "news",
    "individual", "individuals", "figure", "person", "company", "companies",
    "suggest", "suggests", "suggested", "according", "regarding", "considering",
    "discussing", "discussed", "mentioned", "detailing", "published", "portrayal",
    "compared", "comparing", "consistent", "consistency", "indicate", "indicates",
    "indicated", "claim", "claims", "claimed", "event", "events", "action", "actions",
    "nature", "information", "associated", "related", "alleged", "accused",
    "implicated", "similar", "different", "earlier", "later", "first", "single",
    "letter", "character", "recently", "practices", "personal", "story", "stories",
})

_EN_PROPER_PHRASE_RE = re.compile(
    r"(?<![A-Za-z0-9\-])[A-Z][a-z][A-Za-z0-9'\-]*"
    r"(?:(?:\s+(?:of|the|and|in|on|for))?\s+[A-Z][a-z][A-Za-z0-9'\-]*){1,3}(?![A-Za-z0-9\-])"
)
_EN_PHRASE_LEAD_STOP = frozenset({
    "who", "which", "what", "where", "when", "why", "how", "does", "did", "do",
    "is", "are", "was", "were", "has", "have", "had", "can", "could", "would",
    "should", "considering", "according", "between", "both", "while", "after",
    "before", "during", "the", "a", "an", "in", "on", "at", "for", "from",
    "to", "with", "by", "as", "if", "of",
})
_EN_PHRASE_TRAIL_GENERIC = frozenset({"article", "articles", "report", "reports", "story", "stories"})

_COMPOUND_HEAD_ANCHORS = frozenset({
    "报告", "保单", "服务器", "数据库", "说明书", "手册", "合同", "清单", "配置", "账号",
    "高铁", "地铁", "铁路", "城轨", "线路",
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
    focus_terms: list[str] = field(default_factory=list)
    expanded_terms: list[str] = field(default_factory=list)


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

    return [seg for seg in (out or [run]) if 2 <= len(seg) <= 8]


def _is_spurious_cjk_concept(sp: str, *, anchor_set: frozenset[str] | set[str]) -> bool:
    """Reject CJK interrogative shells and torn cross-word fragments from missing-concept guardrails."""
    if not sp or len(sp) < 2 or len(sp) > 8:
        return True
    if sp in _GENERIC_CORE_WORDS:
        return True
    if _CJK_INTERROGATIVE_RE.search(sp):
        return True
    if _SPURIOUS_TRAILING_CJK_RE.search(sp):
        return True
    if sp.endswith(("分别", "各自", "哪些", "哪几", "提出", "参加", "参会", "包括", "包含", "属于", "称为")):
        return True
    if sp.startswith(("哪些", "哪个", "哪几", "哪两", "哪家", "哪天", "请问", "请列", "列出", "各项", "是否", "能否")):
        return True
    if len(sp) == 4 and sp[:2] in _GENERIC_CORE_WORDS and sp[2:] in _GENERIC_CORE_WORDS:
        return True
    return False


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
    focus_terms: list[str] | None = None,
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

    raw_tokens, raw_groups = tokenize_query(q)
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

    anchors = tuple(dict.fromkeys([
        *extra_anchors,
        *(synonym_book.anchor_terms() if synonym_book is not None else ()),
        *_COMPOUND_HEAD_ANCHORS,
        "标准", "规范", "规则", "接口", "协议", "阈值", "范围", "架构",
        "接触网", "检测", "监测", "技术", "项目", "装置", "系统", "工程", "设备", "原理",
    ]))
    anchor_set = frozenset(anchors)
    clause_filler_re = re.compile(
        r"(?:有一台|有一个|有一份|有一张|有一|有个|我的|你的|他的|她的|我方|对方|咱们|怎么样|怎样|"
        r"(?:第[一二三四五六七八九十百\d]{1,4}|[这那每各]?[几两三四五六七八九十]+|哪(?:几|两|三|一)?)[次期批轮节条项步类种部分个套台名位]|"
        r"哪个|哪几|哪两|哪三|哪家|哪天|哪年|哪月|哪日|哪一|哪类|哪些|哪儿|哪里|谁|多少|几个|几部分|几类|几种|"
        r"请列出|列出|指出|给出|请问|告诉我|分别|各自|同时|两家|三家|"
        r"与|和|及|或|或者|对|在|从|将|把|被|向|由|跟|的|了|着|过|但|而|中|内|上|下|有|是|"
        r"是否|能否|可不可以|有没有|是不是|为什么|怎么|如何|什么|"
        r"简称|全称|主题|名称|开头|结尾|属于|包含|包括|组成|提出|负责|参与|参会|改动|"
        r"连续|超过|低于|高于|大于|小于|达到|通过|判断|发生|出现|存在|进行|使用|采用|导致|引起|说明|需要|应该|可以)+"
    )

    def _extract_concepts_from(text_src: str) -> list[tuple[str, set[str]]]:
        found: list[tuple[str, set[str]]] = []
        local_seen: set[str] = set()
        covered_spans: list[tuple[int, int]] = []

        def _overlaps(s_pos: int, e_pos: int) -> bool:
            return any(not (e_pos <= cs or s_pos >= ce) for cs, ce in covered_spans)

        for m in _STRUCTURED_STD_RE.finditer(text_src):
            raw_std = " ".join(m.group(0).split())
            low_std = raw_std.lower()
            if low_std in local_seen:
                continue
            toks, _ = tokenize_query(low_std)
            if toks:
                local_seen.add(low_std)
                covered_spans.append((m.start(), m.end()))
                found.append((raw_std, set(toks)))

        for m in _EN_PROPER_PHRASE_RE.finditer(text_src):
            if _overlaps(m.start(), m.end()):
                continue
            words = m.group(0).split()
            while words and words[0].lower() in _EN_PHRASE_LEAD_STOP:
                words.pop(0)
            while words and words[-1].lower() in _EN_PHRASE_TRAIL_GENERIC:
                words.pop()
            cap_words = [w for w in words if w and w[0].isupper()]
            if len(words) < 2 or len(cap_words) < 2:
                continue
            phrase = " ".join(words)
            low_p = phrase.lower()
            if low_p in local_seen or low_p in EN_STOPWORDS or low_p in _GENERIC_CORE_WORDS:
                continue
            toks, _ = tokenize_query(low_p)
            sig_toks = {t for t in toks if t not in EN_STOPWORDS and t not in _GENERIC_CORE_WORDS}
            if len(sig_toks) >= 2:
                local_seen.add(low_p)
                covered_spans.append((m.start(), m.end()))
                found.append((phrase, sig_toks))

        for m in LATIN_NUM_RE.finditer(text_src):
            if _overlaps(m.start(), m.end()):
                continue
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
                covered_spans.append((m.start(), m.end()))
                found.append((raw_lbl, set(toks)))

        for m in NUM_MEASURE_RE.finditer(text_src):
            if _overlaps(m.start(), m.end()):
                continue
            if m.start() > 0 and re.match(r"[A-Za-z0-9\-_/]", text_src[m.start() - 1]):
                continue
            comp = (m.group(1) + m.group(2)).lower()
            covered_spans.append((m.start(), m.end()))
            if comp not in local_seen:
                local_seen.add(comp)
                found.append((comp, {comp}))

        cjk_src_chars = list(text_src)
        for cs, ce in covered_spans:
            for ci in range(max(0, cs), min(len(cjk_src_chars), ce)):
                cjk_src_chars[ci] = " "
        cjk_src = "".join(cjk_src_chars)

        for seg in clause_filler_re.split(cjk_src):
            for run in CJK_RUN_RE.findall(seg):
                if len(run) < 2 or run in _GENERIC_CORE_WORDS:
                    continue
                for sp in _split_cjk_concept(run, anchors):
                    if sp in local_seen or _is_spurious_cjk_concept(sp, anchor_set=anchor_set):
                        continue
                    sp_toks = set(tokenize(sp))
                    if sp_toks:
                        local_seen.add(sp)
                        found.append((sp, sp_toks))
        return found

    raw_focus = [str(t).strip() for t in (focus_terms or ()) if str(t).strip()][:6]
    sanitized_focus: list[str] = []
    seen_focus_low: set[str] = set()
    for item in raw_focus:
        clean_item = _STOP_QUERY_PHRASE_RE.sub(" ", _META_INQUIRY_RE.sub(" ", item)).strip(" ：:，,。.;；?!？！")
        if not clean_item:
            continue
        if len(clean_item) > 14 or re.search(r"[，。；？！?!]", clean_item):
            for sub_lbl, _ in _extract_concepts_from(clean_item):
                low_s = sub_lbl.lower()
                if low_s not in seen_focus_low:
                    seen_focus_low.add(low_s)
                    sanitized_focus.append(sub_lbl)
                    if len(sanitized_focus) >= 8:
                        break
        else:
            low_i = clean_item.lower()
            if low_i not in seen_focus_low and low_i not in _GENERIC_CORE_WORDS and low_i not in EN_STOPWORDS:
                seen_focus_low.add(low_i)
                sanitized_focus.append(clean_item)
        if len(sanitized_focus) >= 8:
            break

    focus_tok_set: set[str] = set()
    focus_groups: list[set[str]] = []
    seen_group_keys: set[tuple[str, ...]] = set()
    focus_surface_toks: set[str] = set()
    for ft in sanitized_focus:
        ft_toks, ft_grps = tokenize_query(ft)
        focus_tok_set.update(ft_toks)
        for m in LATIN_NUM_RE.finditer(ft.lower()):
            focus_surface_toks.add(m.group(0))
        grp_union: set[str] = set(ft_toks)
        for g in ft_grps:
            grp_union.update(g)
        gkey = tuple(sorted(grp_union))
        if grp_union and gkey not in seen_group_keys:
            seen_group_keys.add(gkey)
            focus_groups.append(grp_union)

    primary_surface_toks = {m.group(0) for m in LATIN_NUM_RE.finditer(q.lower())} | focus_surface_toks
    q_code_toks: set[str] = set()
    for m in LATIN_NUM_RE.finditer(q):
        q_code_toks.update(tokenize(m.group(0)))

    weights: dict[str, float] = {}
    core_weights: dict[str, float] = {}
    seen_tok_counts: dict[str, int] = {}
    for tok in raw_tokens:
        if tok in core_token_set:
            base_w = 1.85 if _TECH_CODE_RE.match(tok) else 1.0
            if sanitized_focus:
                if tok not in focus_tok_set and tok not in q_code_toks:
                    base_w *= 0.35
            elif tok not in main_token_set:
                base_w *= 0.42
            # Derived sub-variants of a compound code shouldn't stack 3x-5x weight over other query concepts
            is_derived_sub = (
                bool(re.search(r"[a-z0-9]", tok))
                and tok not in primary_surface_toks
                and any(tok in g and len(g) > 1 for g in raw_groups)
            )
            w = base_w * 0.48 if is_derived_sub else base_w
            occ = seen_tok_counts.get(tok, 0)
            seen_tok_counts[tok] = occ + 1
            inc = w if occ == 0 else w * 0.25
            cap = w * 1.60
            weights[tok] = min(cap, weights.get(tok, 0.0) + inc)
            core_weights[tok] = min(cap, core_weights.get(tok, 0.0) + inc)
        else:
            weights[tok] = max(weights.get(tok, 0.0), 0.20 if sanitized_focus else 0.30)

    for ft in sanitized_focus:
        ft_toks, ft_grps = tokenize_query(ft)
        for tok in ft_toks:
            base_fw = 1.45 if (_TECH_CODE_RE.match(tok) or LATIN_NUM_RE.fullmatch(tok)) else 1.15
            is_derived_ft = (
                bool(re.search(r"[a-z0-9]", tok))
                and tok not in primary_surface_toks
                and any(tok in g and len(g) > 1 for g in ft_grps)
            )
            fw = base_fw * 0.48 if is_derived_ft else base_fw
            weights[tok] = max(weights.get(tok, 0.0), fw)
            core_weights[tok] = max(core_weights.get(tok, 0.0), fw)

    groups: list[set[str]] = list(focus_groups) if sanitized_focus else list(raw_groups)
    if sanitized_focus:
        for m in LATIN_NUM_RE.finditer(core_text_clean):
            mtoks, _ = tokenize_query(m.group(0))
            mset = set(mtoks)
            if mset and not (mset & focus_tok_set):
                gkey = tuple(sorted(mset))
                if gkey not in seen_group_keys:
                    seen_group_keys.add(gkey)
                    groups.append(mset)

    syn_input = q if not sanitized_focus else f"{q} {' '.join(sanitized_focus)}"
    auto_syns: list[str] = []
    if synonym_book is not None:
        for added_cans, all_triggers in synonym_book.expand_structured(syn_input):
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

    ext_terms = [str(t).strip() for t in (expanded_terms or ()) if str(t).strip()][:6]
    if ext_terms:
        for phrase in ext_terms:
            etoks, _ = tokenize_query(phrase)
            if etoks:
                for et in etoks:
                    weights[et] = max(weights.get(et, 0.0), 0.65)
                groups.append(set(etoks))

    if sanitized_focus:
        core_concepts: list[tuple[str, set[str]]] = []
        seen_c_low: set[str] = set()
        for ft in sanitized_focus:
            ext_sub = _extract_concepts_from(ft)
            if ext_sub:
                for sl, st in ext_sub:
                    if sl.lower() not in seen_c_low:
                        seen_c_low.add(sl.lower())
                        core_concepts.append((sl, st))
            else:
                ft_set = set(tokenize(ft))
                if ft_set and ft.lower() not in seen_c_low:
                    seen_c_low.add(ft.lower())
                    core_concepts.append((ft, ft_set))
        # Anti-hallucination guardrail: keep omitted tech codes / proper nouns from `question`
        for qlbl, qtset in _extract_concepts_from(core_text_clean):
            if (LATIN_NUM_RE.fullmatch(qlbl) or NUM_MEASURE_RE.fullmatch(qlbl.lower())) and qlbl.lower() not in seen_c_low:
                if not any(qtset & ct for _, ct in core_concepts):
                    seen_c_low.add(qlbl.lower())
                    core_concepts.append((qlbl, qtset))
        facet_source_concepts = list(core_concepts)
    else:
        core_concepts = _extract_concepts_from(core_text_clean)
        facet_source_concepts = _extract_concepts_from(main_core) if has_distinct_main else core_concepts
        if not facet_source_concepts:
            facet_source_concepts = core_concepts

    cjk_q_count = sum(1 for ch in q if "\u4e00" <= ch <= "\u9fff")
    ordered_facet_cands = list(facet_source_concepts)
    if cjk_q_count < 4 and len(ordered_facet_cands) > 1:
        def _en_facet_rank(item: tuple[str, set[str]]) -> tuple[int, int, str]:
            lbl, _ = item
            if _STRUCTURED_STD_RE.fullmatch(lbl) or _TECH_CODE_RE.match(lbl) or any(ch.isdigit() for ch in lbl):
                tier = 0
            elif " " in lbl:
                tier = 1
            elif lbl and lbl[0].isupper():
                is_special = any(ch.isupper() for ch in lbl[1:]) or bool(
                    re.search(rf"(?<!^)\b{re.escape(lbl)}\b", core_text_clean)
                )
                tier = 2 if is_special else 3
            else:
                tier = 4
            return (tier, -len(lbl), lbl.lower())

        ordered_facet_cands.sort(key=_en_facet_rank)

    facets: list[tuple[str, set[str]]] = []
    for lbl, tset in ordered_facet_cands:
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
        focus_terms=sanitized_focus,
        expanded_terms=ext_terms,
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
        skip_titles = {
            "log.md", "index.md", "overview", "日志.md", "目录.md", "知识库总览", "史料清单",
            "页面名", "专题", "title", "page", "pages", "notes", "说明",
        }
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("|") and ("---" in s or "页面名" in s or "专题" in s or re.search(r"\|\s*(?:Page|Title)\s*\|", s, re.I)):
                continue
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
                if not grp or not _valid_synonym_term(grp[0], is_alias=False):
                    continue
                valid_als = [a for a in grp[1:] if _valid_synonym_term(a, is_alias=True)]
                if not valid_als:
                    continue
                if is_local or any(term.lower() in raw_low for term in (grp[0], *valid_als)):
                    syn_pairs.append(f"{grp[0]}({'/'.join(valid_als[:2])})")
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
    ADJACENT_MERGE_GAP = 42
    MAX_MERGED_SPAN_LINES = 115

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
        self._vault_cfg_mtime_ns: int | None = None
        self._cached_domain_profile: dict[str, Any] = dict(self.cfg.domain_profile)

    def _reload_domain_profile_if_needed(self) -> dict[str, Any]:
        cfg_file = self.vault / "vault.json"
        if not cfg_file.is_file() and (self.vault / ".vault.json").is_file():
            cfg_file = self.vault / ".vault.json"
        try:
            st = cfg_file.stat() if cfg_file.is_file() else None
        except OSError:
            st = None
        mtime = st.st_mtime_ns if st is not None else 0
        if self._vault_cfg_mtime_ns != mtime:
            self._vault_cfg_mtime_ns = mtime
            try:
                self._cached_domain_profile = dict(VaultConfig.load(self.vault).domain_profile)
            except Exception:
                self._cached_domain_profile = {}
        return self._cached_domain_profile

    def domain_headline(self) -> str:
        dp = self._reload_domain_profile_if_needed()
        return str(dp.get("domain") or "").strip()[:80]

    def domain_profile_summary(self) -> str:
        dp = self._reload_domain_profile_if_needed()
        formatted = format_domain_profile(dp, layout=self.cfg.layout)
        if formatted:
            return formatted
        return self.catalog.cheatsheet(self.synonym_book)

    def domain_cheatsheet(self) -> str:
        return self.domain_profile_summary()

    def build(
        self,
        snap,
        question: str,
        *,
        focus_terms: list[str] | None = None,
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
            focus_terms=focus_terms,
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
        if plan.token_budget <= 1500:
            wiki_max_k = 1
            target_l2_k = 3
        elif plan.token_budget <= 2200:
            wiki_max_k = 2 if len(plan.facets) >= 2 else 1
            target_l2_k = 4
        else:
            wiki_max_k = 3 if plan.route_mode == "complex" or len(plan.facets) >= 2 else 2
            target_l2_k = 6 if plan.route_mode == "complex" else 4

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

        # P-04: Demote textbook review/exam-question sections unless the user explicitly asks for exam questions
        if l2_candidates and not _QUERY_WANTS_EXAM_RE.search(q):
            for h in l2_candidates:
                hd = str(h.get("heading") or "")
                hd_compact = re.sub(r"\s+", "", hd)
                tx = str(h.get("text") or "")
                non_empty = [ln.strip() for ln in tx.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                q_line_ratio = (
                    sum(1 for ln in non_empty if ln.endswith(("?", "？"))) / len(non_empty)
                    if len(non_empty) >= 3
                    else 0.0
                )
                if _EXAM_HEADING_RE.search(hd_compact):
                    h["score"] = round(float(h.get("score") or 0.0) * 0.22, 4)
                elif hd.rstrip().endswith(("?", "？")) or q_line_ratio >= 0.60:
                    h["score"] = round(float(h.get("score") or 0.0) * 0.28, 4)
            l2_candidates.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)

        if include_debug and not acl_filter.allow_all:
            # Populate Playground X-Ray Shadow Diff for hits pruned inside SQLite FTS5 `acl_pfx`
            for sh_hit in index.search_weighted(plan.weighted_tokens, plan.groups, k=10, max_per_source=3, query_text=q):
                allowed_l2(sh_hit.get("source") or "", hit=sh_hit)

        active_facets: list[tuple[str, set[str]]] = list(plan.facets)
        concept_df_map: dict[str, int] = {}
        has_cjk_q = any("\u4e00" <= ch <= "\u9fff" for ch in q)
        if not has_cjk_q and len(plan.core_concepts) >= 2:
            all_c_toks = [t for _, tset in plan.core_concepts for t in tset]
            concept_df_map = index.tokens_df(all_c_toks)
            n_src_est = (getattr(index, "_n_sources_cached", 0) or getattr(index, "n_sources", 0) or 1)
            scored_f: list[tuple[int, float, int, str, set[str]]] = []
            for lbl, tset in plan.core_concepts:
                in_vault_idfs = [
                    math.log(1 + (n_src_est - concept_df_map[t] + 0.5) / (concept_df_map[t] + 0.5))
                    for t in tset
                    if concept_df_map.get(t, 0) > 0
                ]
                if not in_vault_idfs:
                    continue
                is_prop = int(bool(
                    _STRUCTURED_STD_RE.fullmatch(lbl)
                    or _TECH_CODE_RE.match(lbl)
                    or any(ch.isdigit() for ch in lbl)
                    or " " in lbl
                    or (lbl and lbl[0].isupper())
                ))
                scored_f.append((is_prop, max(in_vault_idfs), len(lbl), lbl, tset))
            if scored_f:
                scored_f.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3].lower()))
                idf_facets: list[tuple[str, set[str]]] = []
                for _, _, _, lbl, tset in scored_f:
                    if not any(tset & prev_set for _, prev_set in idf_facets):
                        idf_facets.append((lbl, tset))
                        if len(idf_facets) >= 4:
                            break
                if idf_facets:
                    active_facets = idf_facets

        if len(active_facets) >= 2:
            wiki_candidates, l2_candidates = self._ensure_facet_candidates(
                index=index,
                plan=plan,
                wiki_candidates=wiki_candidates,
                l2_candidates=l2_candidates,
                allowed_wiki=allowed_wiki,
                allowed_l2=allowed_l2,
                acl_filter=acl_filter,
                facets=active_facets,
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
        selected_l2 = self._select_l2_with_elastic_cap(
            merged_pool,
            active_facets,
            max_k=target_l2_k,
            route_mode=plan.route_mode,
            q_idf=q_idf_sample,
        )

        # P-07: Account for global header/footer + per-block framing overhead in total budget
        est_blocks = len(selected_wiki) + max(len(selected_l2), 1)
        overhead_reserve = 220 + est_blocks * 140
        usable_chars = max(720, plan.char_budget - overhead_reserve)
        wiki_char_budget = int(usable_chars * 0.24) if selected_wiki else 0
        l2_char_budget = max(480, usable_chars - wiki_char_budget)

        formatted_wiki = self._format_wiki_blocks(selected_wiki, plan, wiki_char_budget)
        actual_wiki_chars = sum(len(w["content"]) + 90 for w in formatted_wiki)
        l2_char_budget = max(min(l2_char_budget, max(420, usable_chars - actual_wiki_chars)), l2_char_budget if not formatted_wiki else 420)

        formatted_l2 = self._format_l2_blocks(
            selected_l2, plan, l2_char_budget, snap=snap, resolve_mirror_fn=resolve_mirror_fn
        )

        # P-02: Index-backed exact-match rescue BEFORE claiming any concept is missing from vault
        def _compose_combined_text(w_blocks: list[dict], s_blocks: list[dict], pinned: list[str], mems: list[dict]) -> tuple[str, float]:
            top_l2_v = max((float(s.get("score") or 0.0) for s in s_blocks), default=0.0)
            top_w_v = max((float(w.get("score") or 0.0) for w in w_blocks), default=0.0)
            sc_l2 = (
                [s for s in s_blocks if float(s.get("score") or 0.0) >= top_l2_v * 0.35]
                if top_l2_v >= 15.0
                else s_blocks
            )
            sc_w = (
                [w for w in w_blocks if float(w.get("score") or 0.0) >= top_w_v * 0.50]
                if top_w_v >= 15.0
                else w_blocks
            )
            txt = "\n".join(
                [
                    *(w["content"] + " " + w["heading"] for w in sc_w),
                    *(s["content"] + " " + s["heading"] + " " + s["source"] for s in sc_l2),
                    *pinned,
                    *(m["text"] for m in mems),
                ]
            ).lower()
            return txt, top_l2_v

        pre_combined, _ = _compose_combined_text(formatted_wiki, formatted_l2, [], [])
        pre_missing = self._detect_missing_concepts(plan, pre_combined)
        if pre_missing:
            concept_map = {lbl: tset for lbl, tset in plan.core_concepts}
            seen_sel_keys = {(h.get("source"), h.get("line_start"), h.get("page_num")) for h in selected_l2}
            rescued_any = False
            for mc in pre_missing[:2]:
                mc_toks = concept_map.get(mc) or set(tokenize(mc))
                if not mc_toks:
                    continue
                probe_weights = {t: 2.0 for t in mc_toks}
                for kt, kw in plan.core_weighted_tokens.items():
                    if kt not in probe_weights:
                        probe_weights[kt] = kw * 0.2
                probe_hits = index.search_weighted(
                    probe_weights,
                    [set(mc_toks)],
                    k=6,
                    max_per_source=2,
                    acl_filter=acl_filter,
                    query_text=mc,
                )
                for ph in probe_hits:
                    psrc = ph.get("source") or ""
                    pkey = (psrc, ph.get("line_start"), ph.get("page_num"))
                    if pkey in seen_sel_keys or not allowed_l2(psrc, hit=ph):
                        continue
                    pblob = f"{psrc} {ph.get('heading', '')} {ph.get('text', '')}".lower()
                    pcompact = re.sub(r"[.\-/+_\s]+", "", pblob)
                    if not self._concept_present_in_text(
                        mc, mc_toks, pblob, pcompact, synonym_book=self.synonym_book
                    ):
                        continue
                    ph = dict(ph)
                    ph["facet_rescued"] = mc
                    ph["score"] = max(float(ph.get("score") or 0.0), 18.0)
                    if len(selected_l2) >= target_l2_k and len(selected_l2) >= 2:
                        rep_idx = next(
                            (i for i in range(len(selected_l2) - 1, 0, -1) if not selected_l2[i].get("facet_rescued")),
                            len(selected_l2) - 1,
                        )
                        selected_l2[rep_idx] = ph
                    else:
                        selected_l2.append(ph)
                    seen_sel_keys.add(pkey)
                    rescued_any = True
                    break
            if rescued_any:
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
        combined_text, top_l2_sc = _compose_combined_text(
            formatted_wiki, formatted_l2, pinned_errata_texts, memory_blocks
        )
        uncovered_facets: list[str] = []
        if plan.route_mode == "complex" and len(active_facets) >= 2:
            l2_cov_blob = "\n".join(
                f"{s.get('source', '')} {s.get('heading', '')} {s.get('content', '')}"
                for s in formatted_l2
            ).lower()
            if not concept_df_map and active_facets:
                concept_df_map = index.tokens_df(t for _, tset in active_facets for t in tset)
            for flbl, ftoks in active_facets:
                if any(concept_df_map.get(t, 0) > 0 for t in ftoks) and not self._facet_covered_in_blob(
                    flbl, ftoks, l2_cov_blob, q_idf=q_idf_sample
                ):
                    uncovered_facets.append(flbl)

        missing_concepts = self._detect_missing_concepts(
            plan,
            combined_text,
            index=index,
            vault_gap_only_en=True,
            top_l2_score=top_l2_sc,
        )
        boundary_notes = self._collect_boundary_notes(
            snap,
            formatted_l2,
            plan=plan,
            missing_concepts=missing_concepts,
            top_l2_score=top_l2_sc,
            allowed_l2=allowed_l2,
            uncovered_facets=uncovered_facets,
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
            "focus_terms": plan.focus_terms,
            "expanded_terms": plan.expanded_terms,
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

    @staticmethod
    def _facet_covered_in_blob(
        lbl: str,
        f_toks: set[str],
        blob: str,
        q_idf: dict[str, float] | None = None,
    ) -> bool:
        low_lbl = lbl.lower().strip()
        if not low_lbl:
            return True
        if CJK_RUN_RE.search(lbl):
            return (low_lbl in blob) or any(t in blob for t in f_toks if len(t) >= 2)
        if re.fullmatch(r"[a-z0-9._\-/+ ]+", low_lbl) and " " in low_lbl:
            if re.search(rf"(?<![a-z0-9]){re.escape(low_lbl)}(?![a-z0-9])", blob):
                return True
        sig = [t for t in f_toks if len(t) >= 2 and t not in EN_STOPWORDS and t not in _GENERIC_CORE_WORDS]
        if not sig:
            return True
        hits = [
            t
            for t in sig
            if (
                re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob)
                if re.fullmatch(r"[a-z0-9._\-/+]+", t)
                else (t in blob)
            )
        ]
        if len(sig) == 1:
            return len(hits) == 1
        if q_idf:
            top_sig = max(sig, key=lambda t: q_idf.get(t, 0.0))
            return top_sig in hits and len(hits) >= max(2, math.ceil(len(sig) * 0.6))
        if len(sig) <= 3:
            return len(hits) == len(sig)
        return len(hits) >= max(2, len(sig) - 1)

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
        facets: list[tuple[str, set[str]]] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        use_facets = facets if facets is not None else plan.facets
        top_l2_text = " ".join(
            f"{h.get('source', '')} {h.get('heading', '')} {h.get('text', '')[:500]}"
            for h in l2_candidates[:6]
        ).lower()
        top_wiki_text = " ".join(
            f"{w.get('source', '')} {w.get('title', '')} {w.get('heading', '')} {w.get('text', '')[:400]}"
            for w in wiki_candidates[:3]
        ).lower()
        q_idf_sample = (l2_candidates[0].get("_q_idf") if l2_candidates else None) or {}

        seen_l2 = {(h.get("source"), h.get("line_start"), h.get("page_num")) for h in l2_candidates}
        seen_w = {(w.get("source"), w.get("line_start")) for w in wiki_candidates}
        main_top_l2_sc = max((float(h.get("score") or 0.0) for h in l2_candidates), default=0.0)
        main_top_wiki_sc = max((float(w.get("score") or 0.0) for w in wiki_candidates), default=0.0)

        for lbl, f_toks in use_facets:
            sub_weights = {t: 1.5 for t in f_toks}
            for k_tok, v_w in plan.core_weighted_tokens.items():
                if k_tok not in sub_weights:
                    sub_weights[k_tok] = v_w * 0.25
            sub_groups = [set(f_toks)]

            if not self._facet_covered_in_blob(lbl, f_toks, top_l2_text, q_idf=q_idf_sample):
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
                        if main_top_l2_sc > 0.0:
                            sh["score"] = round(min(float(sh.get("score") or 0.0), main_top_l2_sc * 0.55), 4)
                        sh["facet_rescued"] = lbl
                        l2_candidates.append(sh)
                        seen_l2.add(key)

            if not self._facet_covered_in_blob(lbl, f_toks, top_wiki_text, q_idf=q_idf_sample):
                for sw in index.search_wiki_weighted(sub_weights, sub_groups, k=1, max_per_source=1):
                    wkey = (sw.get("source"), sw.get("line_start"))
                    if wkey not in seen_w and allowed_wiki(sw):
                        if main_top_wiki_sc > 0.0:
                            sw["score"] = round(min(float(sw.get("score") or 0.0), main_top_wiki_sc * 0.65), 4)
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
    def _fold_into_picked(
        cand: dict,
        picked: list[dict],
        picked_sets: list[set[str]],
        *,
        cand_toks: set[str] | None = None,
    ) -> bool:
        """Return True if ``cand`` is a cross-source near-duplicate of an already-picked chunk (recording provenance)."""
        cand_text = cand.get("text") or ""
        cand_src = cand.get("source") or ""
        if not cand_text or not picked:
            return False
        ctoks = cand_toks if cand_toks is not None else set(tokenize(cand_text[:1200]))
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
        cand_tok_cache: dict[int, set[str]] = {}

        def _cand_toks(c: dict) -> set[str]:
            cid = id(c)
            toks = cand_tok_cache.get(cid)
            if toks is None:
                toks = set(tokenize((c.get("text") or "")[:1200]))
                cand_tok_cache[cid] = toks
            return toks

        picked: list[dict] = []
        picked_sets: list[set[str]] = []
        per_src: dict[str, int] = {}
        is_multi_facet_complex = route_mode == "complex" and len(facets) >= 2 and max_k >= 4
        reserve = min(len(facets) - 1, 2) if is_multi_facet_complex else (1 if len(facets) >= 2 and max_k >= 3 else 0)
        main_cap = max(1, max_k - reserve)

        for c in filtered:
            src = c.get("source") or ""
            cnt = per_src.get(src, 0)
            if is_multi_facet_complex:
                limit = 2 if (src == top_src and float(c.get("score") or 0.0) >= other_best * 1.8) else 1
            elif src == top_src:
                limit = 4 if float(c.get("score") or 0.0) >= other_best * 1.4 else 3
            else:
                limit = 1 if dist_subj_toks else 3
            ctoks = _cand_toks(c)
            if cnt >= limit or self._fold_into_picked(c, picked, picked_sets, cand_toks=ctoks):
                continue
            picked.append(c)
            picked_sets.append(ctoks)
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
                if self._facet_covered_in_blob(lbl, f_toks, cov_blob, q_idf=q_idf):
                    continue
                chosen = None
                chosen_blob = ""
                for require_unseen in (True, False):
                    for c in filtered:
                        if c in picked:
                            continue
                        src = c.get("source") or ""
                        if require_unseen and per_src.get(src, 0) > 0:
                            continue
                        if not require_unseen and per_src.get(src, 0) >= 2:
                            continue
                        ctoks = _cand_toks(c)
                        if self._fold_into_picked(c, picked, picked_sets, cand_toks=ctoks):
                            continue
                        c_blob = f"{c.get('source', '')} {c.get('heading', '')} {c.get('text', '')[:800]}".lower()
                        if self._facet_covered_in_blob(lbl, f_toks, c_blob, q_idf=q_idf):
                            chosen = c
                            chosen_blob = c_blob
                            break
                    if chosen is not None:
                        break
                if chosen is not None:
                    picked.append(chosen)
                    picked_sets.append(_cand_toks(chosen))
                    src = chosen.get("source") or ""
                    per_src[src] = per_src.get(src, 0) + 1
                    cov_blob += " " + chosen_blob

        for c in filtered:
            if len(picked) >= max_k:
                break
            if c in picked:
                continue
            src = c.get("source") or ""
            sec_limit = 1 if (dist_subj_toks and src != top_src) else (2 if is_multi_facet_complex else 3)
            ctoks = _cand_toks(c)
            if per_src.get(src, 0) >= sec_limit or self._fold_into_picked(c, picked, picked_sets, cand_toks=ctoks):
                continue
            picked.append(c)
            picked_sets.append(ctoks)
            per_src[src] = per_src.get(src, 0) + 1
        return picked

    @staticmethod
    def _prepare_virtual_lines(raw_lines: list[str]) -> tuple[list[str], list[int], str]:
        """Split single-line HTML table rows into virtual sub-lines preserving 0-based original offsets."""
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
        return lines, line_offsets, html_tbl_hdr

    @staticmethod
    def _build_window_tok_vals(
        plan: QueryPlan,
        q_idf: dict[str, float] | None,
        hdr_low: str,
    ) -> dict[str, float]:
        tok_val: dict[str, float] = {}
        for tok, qw in plan.weighted_tokens.items():
            idf = (q_idf or {}).get(tok, 1.0)
            base_v = qw * (max(0.25, idf) ** 1.35) if q_idf else (qw * max(0.25, idf))
            if re.search(r"\d", tok):
                base_v *= 1.25
            if hdr_low and tok in hdr_low:
                base_v *= 0.08
            tok_val[tok] = base_v
        return tok_val

    @staticmethod
    def _score_document_lines(lines: list[str], tok_val: dict[str, float]) -> tuple[list[set[str]], list[int]]:
        line_tok_sets: list[set[str]] = []
        line_lens: list[int] = []
        tok_keys = set(tok_val)
        for ln in lines:
            low_ln = ln.lower()
            matched_t = {t for t in tok_val if t in low_ln}
            if not matched_t:
                matched_t = set(tokenize(low_ln)) & tok_keys
            line_tok_sets.append(matched_t)
            line_lens.append(len(ln) + 1)
        return line_tok_sets, line_lens

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

        lines, line_offsets, html_tbl_hdr = ContextPackEngine._prepare_virtual_lines(raw_lines)
        hdr = (table_header or "").strip() or html_tbl_hdr
        hdr_low = hdr.lower()
        hdr_cost = (len(hdr) + 2) if hdr else 0
        body_budget = max(min(220, max_chars), max_chars - hdr_cost)

        tok_val = ContextPackEngine._build_window_tok_vals(plan, q_idf, hdr_low)
        line_tok_sets, line_lens = ContextPackEngine._score_document_lines(lines, tok_val)

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

    def _find_other_matches(
        self,
        text: str,
        *,
        plan: QueryPlan,
        q_idf: dict[str, float] | None,
        table_header: str,
        base_line_start: int | None,
        shown_start: int | None,
        shown_end: int | None,
        tot_lines: int | None,
        max_matches: int = 3,
    ) -> list[dict]:
        """Locate up to ``max_matches`` non-overlapping residual spans outside ``[shown_start..shown_end]``."""
        if not text or base_line_start is None or shown_start is None or shown_end is None:
            return []
        raw_lines = text.splitlines()
        if len(raw_lines) < 6:
            return []

        lines, line_offsets, html_tbl_hdr = self._prepare_virtual_lines(raw_lines)
        hdr = (table_header or "").strip() or html_tbl_hdr
        tok_val = self._build_window_tok_vals(plan, q_idf, hdr.lower())
        if not tok_val:
            return []
        line_tok_sets, line_lens = self._score_document_lines(lines, tok_val)

        # Residual downweighting: tokens already displayed in the primary window get 0.20x weight
        covered_in_shown: set[str] = set()
        for off, tset in zip(line_offsets, line_tok_sets):
            abs_ln = base_line_start + off
            if shown_start <= abs_ln <= shown_end:
                covered_in_shown.update(tset)
        res_tok_val = {
            t: (w * 0.20 if t in covered_in_shown else w)
            for t, w in tok_val.items()
        }

        disjoint_indices = [
            idx
            for idx, off in enumerate(line_offsets)
            if (base_line_start + off) < shown_start or (base_line_start + off) > shown_end
        ]
        if len(disjoint_indices) < 2:
            return []

        span_char_cap = 420
        span_line_cap = 10
        candidates: list[tuple[float, int, int, set[str]]] = []
        i = 0
        while i < len(lines):
            abs_i = base_line_start + line_offsets[i]
            if shown_start <= abs_i <= shown_end or not line_tok_sets[i]:
                i += 1
                continue
            cur_c = 0
            unique_t: set[str] = set()
            r = i
            while r < len(lines) and (r - i) < span_line_cap:
                abs_r = base_line_start + line_offsets[r]
                if shown_start <= abs_r <= shown_end:
                    break
                if r > i and cur_c + line_lens[r] > span_char_cap:
                    break
                cur_c += line_lens[r]
                unique_t.update(line_tok_sets[r])
                r += 1
            end_idx = r - 1
            if unique_t and end_idx >= i:
                sc = sum(res_tok_val.get(t, 0.0) for t in unique_t)
                candidates.append((sc, i, end_idx, unique_t))
            i = max(i + 1, end_idx + 1)

        if not candidates:
            return []
        candidates.sort(key=lambda item: (-item[0], item[1]))
        floor_sc = max(0.8, candidates[0][0] * 0.25)

        picked_ranges: list[tuple[int, int]] = []
        out: list[dict] = []
        for sc, l_idx, r_idx, utoks in candidates:
            if sc < floor_sc:
                break
            m_s = base_line_start + line_offsets[l_idx]
            m_e = base_line_start + line_offsets[r_idx]
            if tot_lines is not None and m_e > tot_lines:
                m_e = tot_lines
            if m_e < m_s:
                continue
            if any(not (m_e < ps or m_s > pe) for ps, pe in picked_ranges):
                continue
            picked_ranges.append((m_s, m_e))
            ranked_toks = sorted(utoks, key=lambda t: (-res_tok_val.get(t, 0.0), t))[:5]
            raw_snip = " ".join(ln.strip() for ln in lines[l_idx : r_idx + 1] if ln.strip())
            clean_snip = self._scrub(raw_snip)[:180]
            out.append({
                "start": m_s,
                "end": m_e,
                "matched": ranked_toks,
                "snippet": clean_snip,
            })
            if len(out) >= max_matches:
                break
        out.sort(key=lambda item: item["start"])
        return out

    def _expand_heading_only_wiki_hit(self, w: dict, *, max_chars: int) -> tuple[str, int | None, int | None]:
        """P-09: When a Wiki hit contains only an H1/title line, append its opening `## 概览` / body lines."""
        raw_text = w.get("text") or ""
        ls = w.get("line_start")
        le = w.get("line_end")
        non_heading = [ln.strip() for ln in raw_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if non_heading or not isinstance(ls, int):
            return raw_text, ls, le
        src = str(w.get("source") or "")
        parts = PurePosixPath(src).parts
        if len(parts) < 2:
            return raw_text, ls, le
        try:
            wiki_root_res = self.cfg.wiki_root.resolve()
            wfile = (wiki_root_res / PurePosixPath(*parts[1:])).resolve()
            if wiki_root_res not in wfile.parents or not wfile.is_file():
                return raw_text, ls, le
            all_lines = wfile.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return raw_text, ls, le

        start_idx = max(0, ls - 1)
        picked_lines: list[str] = []
        total_c = 0
        seen_subheadings = 0
        new_end = ls
        for idx in range(start_idx, min(len(all_lines), start_idx + 28)):
            ln = all_lines[idx]
            stripped = ln.strip()
            if idx > start_idx and re.match(r"^##\s+", stripped):
                seen_subheadings += 1
                if seen_subheadings >= 2 or (seen_subheadings == 1 and any(x.strip() and not x.strip().startswith("#") for x in picked_lines)):
                    break
            next_c = total_c + len(ln) + 1
            if picked_lines and next_c > min(680, max(360, max_chars)):
                break
            picked_lines.append(ln)
            total_c = next_c
            new_end = idx + 1
        while picked_lines and not picked_lines[-1].strip():
            picked_lines.pop()
            new_end -= 1
        if len(picked_lines) <= len(raw_text.splitlines()):
            return raw_text, ls, le
        return "\n".join(picked_lines), ls, max(ls, new_end)

    def _format_wiki_blocks(self, wiki_hits: list[dict], plan: QueryPlan, total_budget: int) -> list[dict]:
        if not wiki_hits or total_budget <= 0:
            return []
        per_budget = max(min(550, total_budget // len(wiki_hits)), total_budget // len(wiki_hits))
        per_budget = max(240, per_budget)
        out: list[dict] = []
        for idx, w in enumerate(wiki_hits, 1):
            exp_text, exp_s, exp_e = self._expand_heading_only_wiki_hit(w, max_chars=per_budget)
            content, shown_s, shown_e, windowed = self._window_block_safely(
                exp_text,
                plan=plan,
                q_idf=w.get("_q_idf"),
                table_header=w.get("table_header") or "",
                max_chars=per_budget,
                base_line_start=exp_s,
            )
            out.append({
                "id": f"W{idx}",
                "source": w["source"],
                "title": w.get("title") or "",
                "heading": w.get("heading") or "",
                "line_start": shown_s if shown_s is not None else exp_s,
                "line_end": shown_e if shown_e is not None else exp_e,
                "freshness": w.get("freshness") or "unverified",
                "score": w.get("score"),
                "windowed": windowed,
                "content": content,
            })
        return out

    @staticmethod
    def _align_section_line_start(text: str, sec_s: int | None, sec_e: int | None, mirror_lines: list[str] | None) -> int | None:
        """P-05(a): Re-align `line_start` when indexed chunk text had leading blank lines stripped."""
        if not isinstance(sec_s, int) or not text or not mirror_lines:
            return sec_s
        first_line = text.splitlines()[0].strip()
        if not first_line:
            return sec_s
        lo = max(0, sec_s - 1)
        hi = min(len(mirror_lines), (sec_e if isinstance(sec_e, int) else sec_s + 5))
        if lo < len(mirror_lines) and mirror_lines[lo].strip() == first_line:
            return sec_s
        for idx in range(lo, min(len(mirror_lines), lo + 6)):
            if mirror_lines[idx].strip() == first_line:
                return idx + 1
        return sec_s

    @staticmethod
    def _text_end_line(text: str, sec_s: int | None, mirror_lines: list[str] | None) -> int | None:
        """1-based file line of ``text``'s last line, or ``None`` when ``text`` is not a verbatim contiguous
        run of mirror lines starting at ``sec_s`` (and at least one following line exists).

        Indexed section text is stored ``strip()``ped, so with a legacy index its ``line_end`` can sit past the
        text's real last line (trailing blank lines). Anything appended "after the section" must therefore
        start right after the *text*, not after ``line_end``: otherwise the lines in between vanish from the
        display while ``L<a>-L<b>`` labels are still derived from line counts. Checking the whole chunk against
        the mirror also keeps a stale index from appending lines at a guessed position.
        """
        if not isinstance(sec_s, int) or sec_s < 1 or not mirror_lines or not text:
            return None
        text_lines = text.splitlines()
        end_line = sec_s + text.count("\n")  # same derivation as the displayed `L<a>-L<b>` label
        if end_line >= len(mirror_lines):
            return None
        # `text` spans file lines sec_s..end_line; a trailing newline stands for a final blank line
        # (merged chunks are read verbatim from the mirror and keep it).
        for offset, mirror_line in enumerate(mirror_lines[sec_s - 1:end_line]):
            text_line = text_lines[offset] if offset < len(text_lines) else ""
            if mirror_line.strip() != text_line.strip():
                return None
        return end_line

    @staticmethod
    def _expand_enumeration_tail(
        text: str,
        *,
        sec_e: int | None,
        mirror_lines: list[str] | None,
        question: str,
        max_chars: int,
        sec_s: int | None,
    ) -> tuple[str, int | None]:
        """P-06: Pull sibling enumeration sub-headings/items immediately after a truncated list header.

        ``sec_s`` is the aligned file line of ``text``'s first line (``None``: unknown, nothing is appended).
        Appended lines are the mirror's own lines directly following ``text``, so the result stays one
        contiguous file slice and the ``L<a>-L<b>`` label derived from its line count stays exact (windowed or
        not).
        """
        if not isinstance(sec_e, int) or not mirror_lines or len(text) >= int(max_chars * 0.70):
            return text, sec_e
        has_enum_intent = bool(re.search(r"(?:哪些|哪几|分别|组成|包含|包括|几个|六个|四个|五个|三个|步骤|子系统|分类|清单)", question))
        has_list_cue = bool(re.search(r"(?:组成|包括|包含|分为|如下|以下|步骤)", text))
        if not (has_enum_intent or has_list_cue):
            return text, sec_e

        text_end = ContextPackEngine._text_end_line(text, sec_s, mirror_lines)
        if text_end is None:
            return text, sec_e
        start_idx = text_end  # 0-based index of the line right after `text`
        if start_idx >= len(mirror_lines):
            return text, sec_e

        first_non_blank = ""
        for j in range(start_idx, min(len(mirror_lines), max(start_idx, sec_e) + 6)):
            if mirror_lines[j].strip():
                first_non_blank = mirror_lines[j].strip()
                break
        sibling_re = re.compile(
            r"^(?:#{1,4}\s+(?:\d+[A-Za-z]\b|[一二三四五六七八九十]+、|\d+[.、\s]|步骤|[（(]\d+[)）])|"
            r"[-*•]\s+|\d+[.、)）]\s+)"
        )
        if not sibling_re.match(first_non_blank):
            return text, sec_e

        extra: list[str] = []
        used = len(text) + 16
        new_e = sec_e
        for j in range(start_idx, min(len(mirror_lines), max(start_idx, sec_e) + 65)):
            ln = mirror_lines[j]
            st = ln.strip()
            if j > start_idx and re.match(r"^#{1,2}\s+", st) and not sibling_re.match(st):
                break
            if used + len(ln) + 1 > max_chars:
                break
            extra.append(ln)
            used += len(ln) + 1
            new_e = j + 1
        while extra and not extra[-1].strip():
            extra.pop()
            new_e -= 1
        if not extra:
            return text, sec_e
        return f"{text}\n" + "\n".join(extra), max(sec_e, new_e)

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

        # P-07: Proportional per-block floor so tight `max_tokens` budgets stay respected
        per_floor = min(420, max(160, total_budget // max(n, 1)))
        keep_floor = min(240, max(120, per_floor))
        allocs = [max(per_floor, int(total_budget * (wt / w_sum))) for wt in weights]
        text_lens = [len(h.get("text") or "") + len(h.get("table_header") or "") + 16 for h in l2_hits]
        surplus = 0
        needy_indices: list[int] = []
        for i in range(n):
            if text_lens[i] < allocs[i]:
                freed = allocs[i] - max(keep_floor, text_lens[i])
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
        mirror_lines_cache: dict[str, list[str] | None] = {}

        def _get_mirror_lines(src_name: str) -> list[str] | None:
            if src_name in mirror_lines_cache:
                return mirror_lines_cache[src_name]
            if resolve_mirror_fn is not None and self.cfg.is_source_ref(src_name):
                rel_p = self.cfg.strip_source_prefix(src_name)
                mp = resolve_mirror_fn(src_name, rel_p)
                if mp is not None and mp.is_file():
                    try:
                        ml = mp.read_text(encoding="utf-8", errors="replace").splitlines()
                        mirror_lines_cache[src_name] = ml
                        return ml
                    except OSError:
                        pass
            mirror_lines_cache[src_name] = None
            return None

        def _doc_total_lines(src_name: str) -> int | None:
            d_row = docs_by_src.get(src_name) or {}
            if isinstance(d_row.get("lines"), int) and d_row["lines"] > 0:
                return d_row["lines"]
            ml = _get_mirror_lines(src_name)
            return len(ml) if ml is not None else None

        out: list[dict] = []

        for idx, (h, alloc) in enumerate(zip(l2_hits, allocs), 1):
            src = h["source"]
            raw_text = h.get("text") or ""
            sec_s = h.get("line_start")
            sec_e = h.get("line_end")
            ml = _get_mirror_lines(src) if resolve_mirror_fn is not None else None
            eff_sec_s = self._align_section_line_start(raw_text, sec_s, sec_e, ml)
            if idx <= 2 and (h.get("kind") or "section") == "section" and ml is not None:
                raw_text, sec_e = self._expand_enumeration_tail(
                    raw_text,
                    sec_s=eff_sec_s,
                    sec_e=sec_e,
                    mirror_lines=ml,
                    question=plan.question,
                    max_chars=alloc,
                )

            content, shown_s, shown_e, windowed = self._window_block_safely(
                raw_text,
                plan=plan,
                q_idf=h.get("_q_idf"),
                table_header=h.get("table_header") or "",
                max_chars=alloc,
                base_line_start=eff_sec_s,
            )
            tot_l = _doc_total_lines(src)
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
                "line_start": shown_s if shown_s is not None else eff_sec_s,
                "line_end": shown_e if shown_e is not None else sec_e,
                "section_line_start": eff_sec_s,
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
                    and shown_s <= (eff_sec_s or 1)
                    and shown_e >= tot_l
                )
                if not already_covers_all and rm_end >= rm_start:
                    entry["read_more"] = {
                        "source": src,
                        "start": rm_start,
                        "end": rm_end,
                    }
                other_m = self._find_other_matches(
                    raw_text,
                    plan=plan,
                    q_idf=h.get("_q_idf"),
                    table_header=h.get("table_header") or "",
                    base_line_start=eff_sec_s,
                    shown_start=shown_s,
                    shown_end=shown_e,
                    tot_lines=tot_l,
                )
                if other_m:
                    entry["other_matches"] = other_m
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

    @staticmethod
    def _concept_present_in_text(
        lbl: str,
        tset: set[str],
        text_lower: str,
        compact_text: str,
        *,
        synonym_book: SynonymBook | None = None,
    ) -> bool:
        low_lbl = lbl.lower().strip()
        if not low_lbl:
            return True
        syns = synonym_book.expand(lbl) if synonym_book is not None else []

        # P-03: Structured standard identifiers (`GB/T 49999`, `TB/T 3590`, `IEC 61373`) must match intact with digit boundary
        if _STRUCTURED_STD_RE.fullmatch(lbl):
            m_std = re.match(r"^([a-z/]+)\s*[-–—]?\s*(\d[\d.\-–—]*)$", low_lbl)
            if m_std:
                pfx = re.escape(m_std.group(1))
                num_body = re.sub(r"\s+", "", m_std.group(2))
                std_pat = rf"(?<![a-z0-9]){pfx}\s*[-–—]?\s*{re.escape(num_body)}(?![0-9])"
                if re.search(std_pat, text_lower):
                    return True
                # Only accept synonyms that preserve the standard's numeric code (never bare prefix synonyms like `GB/T` -> `中国国家标准`)
                return any(num_body in s.lower() and s.lower() in text_lower for s in syns)

        # P-03: Numeric/alphanumeric codes require token boundaries so `49999` never matches `400001` and `6C` never matches `16C`
        if re.search(r"\d", low_lbl) and re.fullmatch(r"[a-z0-9._\-/+]+", low_lbl):
            code_pat = rf"(?<![a-z0-9]){re.escape(low_lbl)}(?![a-z0-9])"
            if re.search(code_pat, text_lower):
                return True
            parts = [re.escape(p) for p in re.split(r"[.\-/+_\s]+", low_lbl) if p]
            if len(parts) > 1:
                sep_pat = r"[.\-/+\s_]*".join(parts)
                if re.search(rf"(?<![a-z0-9]){sep_pat}(?![a-z0-9])", text_lower):
                    return True
            return any(
                re.search(rf"(?<![a-z0-9]){re.escape(s.lower())}(?![a-z0-9])", text_lower)
                if re.fullmatch(r"[a-z0-9._\-/+]+", s.lower())
                else (s.lower() in text_lower)
                for s in syns
            )

        if low_lbl in text_lower:
            return True
        compact_lbl = re.sub(r"[.\-/+_\s]+", "", low_lbl)
        if len(compact_lbl) >= 3 and compact_lbl in compact_text:
            return True
        if any(s.lower() in text_lower for s in syns):
            return True
        if len(low_lbl) >= 3 and len(tset) >= 2 and all(t in text_lower for t in tset):
            return True
        if re.fullmatch(r"[a-z]{5,}", low_lbl):
            stem = low_lbl
            if low_lbl.endswith("ies") and len(low_lbl) >= 6:
                stem = low_lbl[:-3] + "y"
            elif low_lbl.endswith(("ches", "shes", "sses", "xes", "zes")) and len(low_lbl) >= 6:
                stem = low_lbl[:-2]
            elif low_lbl.endswith("ied") and len(low_lbl) >= 6:
                stem = low_lbl[:-3] + "y"
            elif low_lbl.endswith("ing") and len(low_lbl) >= 7:
                stem = low_lbl[:-3]
            elif low_lbl.endswith("ed") and len(low_lbl) >= 6:
                stem = low_lbl[:-2]
            elif low_lbl.endswith("s") and not low_lbl.endswith("ss"):
                stem = low_lbl[:-1]
            if len(stem) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(stem)}[a-z]{{0,4}}(?![a-z0-9])", text_lower):
                return True
        if " " in low_lbl and len(tset) >= 2 and re.fullmatch(r"[a-z0-9.\-/+ '\-]+", low_lbl):
            if all(
                re.search(rf"(?<![a-z0-9]){re.escape(t[:max(4, len(t) - 2)])}[a-z]{{0,4}}(?![a-z0-9])", text_lower)
                if (re.fullmatch(r"[a-z]+", t) and len(t) >= 5)
                else bool(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text_lower))
                for t in tset
            ):
                return True
            hyphen_toks = [w for w in low_lbl.split() if "-" in w and len(w) >= 7]
            if hyphen_toks and any(hw in text_lower for hw in hyphen_toks):
                return True
        if 2 <= len(low_lbl) <= 3 and CJK_RUN_RE.fullmatch(low_lbl):
            if re.search(rf"{re.escape(low_lbl[0])}[\u4e00-\u9fff]{re.escape(low_lbl[1:])}", text_lower):
                return True
        if len(low_lbl) >= 4 and CJK_RUN_RE.fullmatch(low_lbl):
            bgs = [low_lbl[i:i + 2] for i in range(len(low_lbl) - 1)]
            hits = sum(1 for bg in bgs if bg in text_lower)
            p1, p2 = re.escape(low_lbl[:2]), re.escape(low_lbl[2:])
            near_4 = (
                len(low_lbl) == 4
                and bool(re.search(rf"{p1}[^\n]{{0,16}}{p2}|{p2}[^\n]{{0,16}}{p1}", text_lower))
            )
            if near_4 or (len(bgs) >= 4 and hits >= len(bgs) - 1):
                return True
        return False

    def _detect_missing_concepts(
        self,
        plan: QueryPlan,
        combined_text_lower: str,
        *,
        index: BM25Index | None = None,
        vault_gap_only_en: bool = False,
        top_l2_score: float = 0.0,
    ) -> list[str]:
        if not plan.core_concepts:
            return []
        compact_combined = re.sub(r"[.\-/+_\s]+", "", combined_text_lower)
        syn_book = getattr(self, "synonym_book", None)
        df_map: dict[str, int] = {}
        is_en_q = not CJK_RUN_RE.search(plan.question)
        if vault_gap_only_en and index is not None and is_en_q:
            df_map = index.tokens_df(t for _, tset in plan.core_concepts for t in tset)

        missing: list[str] = []
        for lbl, tset in plan.core_concepts:
            low_lbl = lbl.lower()
            if low_lbl in _GENERIC_CORE_WORDS or low_lbl in EN_STOPWORDS:
                continue
            if ContextPackEngine._concept_present_in_text(
                lbl, tset, combined_text_lower, compact_combined, synonym_book=syn_book
            ):
                continue
            if (
                vault_gap_only_en
                and index is not None
                and is_en_q
                and not CJK_RUN_RE.search(lbl)
                and not _STRUCTURED_STD_RE.fullmatch(lbl)
                and not _TECH_CODE_RE.match(lbl)
                and not re.search(r"\d", lbl)
            ):
                sig_toks = [t for t in tset if t not in EN_STOPWORDS and t not in _GENERIC_CORE_WORDS]
                if not sig_toks:
                    continue
                if lbl.islower() and lbl.isalpha() and " " not in lbl:
                    all_zero = all(df_map.get(t, 0) == 0 for t in sig_toks)
                    if top_l2_score >= 3.0 or not all_zero:
                        continue
                else:
                    if not any(df_map.get(t, 0) == 0 for t in sig_toks):
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
        uncovered_facets: list[str] | None = None,
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

        if uncovered_facets and len(notes) < 4:
            shown_f = ", ".join(uncovered_facets[:3])
            notes.append(
                f"多面问题中关于「{shown_f}」的证据未完整进槽，建议对其单独调用 wiki_search / wiki_read"
                if is_zh
                else f"In-vault facet(s) [{shown_f}] were not fully covered in this top-K pack; run wiki_search / wiki_read for those terms"
            )

        if failed_srcs and len(notes) < 4:
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
                if s.get("other_matches") and plan.token_budget > 1500:
                    om_parts = [
                        f"L{m['start']}-L{m['end']} ({', '.join(m['matched'])})"
                        for m in s["other_matches"][:3]
                    ]
                    om_lbl = "本文其他命中位置" if is_zh else "Other matching spans"
                    lines.append(f"> 📍 {om_lbl}: {'; '.join(om_parts)}")
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
