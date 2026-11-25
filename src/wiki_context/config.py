"""Vault configuration & bilingual directory layout auto-detection.

Supports both English (`sources/ mirrors/ wiki/ index.md log.md`) and
Chinese (`史料/ 史料_md/ 知识库/ 目录.md 日志.md`) three-layer LLM Wiki vaults
out of the box, with optional overrides via ``vault.json`` or ``.vault.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state_lock import file_lock, replace_with_retry

LAYOUT_PRESETS: dict[str, dict[str, str]] = {
    "en": {
        "sources_dir": "sources",
        "mirrors_dir": "mirrors",
        "wiki_dir": "wiki",
        "notes_dir": "notes",
        "catalog_file": "index.md",
        "log_file": "log.md",
    },
    "zh": {
        "sources_dir": "史料",
        "mirrors_dir": "史料_md",
        "wiki_dir": "知识库",
        "notes_dir": "记忆",
        "catalog_file": "目录.md",
        "log_file": "日志.md",
    },
}

DEFAULT_MAX_DOC_BYTES = 2 * 1024 * 1024  # 2 MiB (plain text & derived Markdown mirrors)
DEFAULT_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MiB (rich binary docs: PDF/PPTX/DOCX/XLSX)


def validate_external_source_path(vault_root: Path, source_path: Path) -> None:
    """Ensure an external read-only source root never equals, contains, or nests inside the vault workspace."""
    v = vault_root.expanduser().resolve()
    s = source_path.expanduser().resolve()
    if s == v or v in s.parents or s in v.parents:
        raise ValueError(
            f"External source directory '{s}' must not equal, contain, or be inside vault workspace '{v}'"
        )


@dataclass(frozen=True)
class VaultConfig:
    root: Path
    name: str
    layout: str = "en"
    sources_dir: str = "sources"
    sources_path: Path | None = None
    source_mounts: tuple[tuple[str, Path], ...] = ()
    invalid_mounts: tuple[tuple[str, str, str], ...] = ()
    mirrors_dir: str = "mirrors"
    wiki_dir: str = "wiki"
    notes_dir: str = "notes"
    catalog_file: str = "index.md"
    log_file: str = "log.md"
    index_subdir: str = ".index"
    max_doc_bytes: int = DEFAULT_MAX_DOC_BYTES
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    sensitive: bool = False
    exclude_patterns: tuple[str, ...] = ()
    compile_exclude_patterns: tuple[str, ...] = ()
    compile_include_patterns: tuple[str, ...] = ()
    compile_skip_csv: bool = True
    compile_project_rollup: bool = True
    compile_skip_csv_explicit: bool = False
    compile_project_rollup_explicit: bool = False
    compile_workers: int = 4
    compile_max_tokens: int = 4096
    compile_disable_thinking: bool = False
    ocr_image_dirs: frozenset[str] = field(default_factory=frozenset)
    anchor_terms: tuple[str, ...] = ()
    acl_rules: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    domain_profile: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, vault_path: Path | str) -> VaultConfig:
        root = Path(vault_path).expanduser().resolve()
        raw_cfg: dict = {}
        for cfg_name in ("vault.json", ".vault.json"):
            p = root / cfg_name
            if p.is_file():
                try:
                    loaded = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        raw_cfg = loaded
                        break
                except (OSError, ValueError):
                    pass

        declared_layout = str(raw_cfg.get("layout") or "").strip().lower()
        if declared_layout in LAYOUT_PRESETS:
            layout = declared_layout
        elif (root / "史料").is_dir() or (root / "史料_md").is_dir() or (root / "知识库").is_dir():
            layout = "zh"
        else:
            layout = "en"

        preset = LAYOUT_PRESETS[layout]
        sources_dir = str(raw_cfg.get("sources_dir") or preset["sources_dir"]).strip("/")
        mirrors_dir = str(raw_cfg.get("mirrors_dir") or preset["mirrors_dir"]).strip("/")
        wiki_dir = str(raw_cfg.get("wiki_dir") or preset["wiki_dir"]).strip("/")
        notes_dir = str(raw_cfg.get("notes_dir") or preset["notes_dir"]).strip("/")
        catalog_file = str(raw_cfg.get("catalog_file") or preset["catalog_file"]).strip("/")
        log_file = str(raw_cfg.get("log_file") or preset["log_file"]).strip("/")

        name = str(raw_cfg.get("name") or root.name or "vault").strip()
        max_bytes = int(raw_cfg.get("max_doc_bytes") or DEFAULT_MAX_DOC_BYTES)
        raw_max_src = raw_cfg.get("max_source_bytes")
        max_src_bytes = int(raw_max_src) if raw_max_src else max(DEFAULT_MAX_SOURCE_BYTES, max_bytes)
        sensitive = bool(raw_cfg.get("sensitive") is True)

        excl_raw = raw_cfg.get("exclude_patterns") or ()
        exclude_patterns = tuple(
            str(x).strip() for x in excl_raw if isinstance(x, str) and str(x).strip()
        )

        cmp_excl_raw = raw_cfg.get("compile_exclude_patterns") or ()
        compile_exclude_patterns = tuple(
            str(x).strip() for x in cmp_excl_raw if isinstance(x, str) and str(x).strip()
        )

        cmp_incl_raw = raw_cfg.get("compile_include_patterns") or ()
        compile_include_patterns = tuple(
            str(x).strip() for x in cmp_incl_raw if isinstance(x, str) and str(x).strip()
        )
        raw_skip_csv = raw_cfg.get("compile_skip_csv")
        compile_skip_csv_explicit = isinstance(raw_skip_csv, bool)
        compile_skip_csv = raw_skip_csv if compile_skip_csv_explicit else True

        raw_project_rollup = raw_cfg.get("compile_project_rollup")
        compile_project_rollup_explicit = isinstance(raw_project_rollup, bool)
        compile_project_rollup = raw_project_rollup if compile_project_rollup_explicit else True
        compile_workers = max(1, min(16, int(raw_cfg.get("compile_workers") or 4)))
        compile_max_tokens = max(256, min(16384, int(raw_cfg.get("compile_max_tokens") or 4096)))
        compile_disable_thinking = bool(raw_cfg.get("compile_disable_thinking") is True)

        ocr_dirs_raw = raw_cfg.get("ocr_image_dirs") or ()
        ocr_image_dirs = frozenset(
            str(x).strip().strip("/") for x in ocr_dirs_raw if isinstance(x, str) and str(x).strip()
        )

        anchors_raw = raw_cfg.get("anchor_terms") or ()
        anchor_terms = tuple(
            str(x).strip() for x in anchors_raw if isinstance(x, str) and len(str(x).strip()) >= 2
        )

        acl_raw = raw_cfg.get("acl")
        acl_rules = dict(acl_raw) if isinstance(acl_raw, dict) else {}

        raw_dp = raw_cfg.get("domain_profile")
        domain_profile = normalize_domain_profile(raw_dp) if isinstance(raw_dp, dict) else {}

        raw_sources_path = str(raw_cfg.get("sources_path") or "").strip()
        sources_path: Path | None = None
        if raw_sources_path:
            p_sp = Path(raw_sources_path).expanduser()
            sources_path = (p_sp if p_sp.is_absolute() else (root / p_sp)).resolve()
            validate_external_source_path(root, sources_path)

        raw_mounts = raw_cfg.get("source_mounts")
        parsed_mounts: list[tuple[str, Path]] = []
        invalid_mounts_list: list[tuple[str, str, str]] = []
        seen_mount_keys: set[str] = set()
        if isinstance(raw_mounts, dict):
            for mk, mv in sorted(raw_mounts.items(), key=lambda kv: (str(kv[0]).strip().strip("/").lower(), str(kv[0]))):
                mname = str(mk).strip().strip("/").replace("\\", "/")
                mval = str(mv or "").strip()
                if not mname or not mval or "/" in mname or mname in {".", ".."} or ".." in mname:
                    invalid_mounts_list.append((str(mk), mval, "invalid mount name or empty path"))
                    continue
                norm_key = mname.lower()
                if norm_key in seen_mount_keys:
                    invalid_mounts_list.append((str(mk), mval, f"duplicate normalized mount name '{mname}'"))
                    continue
                p_mv = Path(mval).expanduser()
                mpath = (p_mv if p_mv.is_absolute() else (root / p_mv)).resolve()
                try:
                    validate_external_source_path(root, mpath)
                except ValueError as exc:
                    invalid_mounts_list.append((mname, mval, str(exc)))
                    continue
                seen_mount_keys.add(norm_key)
                parsed_mounts.append((mname, mpath))
        source_mounts = tuple(parsed_mounts)
        invalid_mounts = tuple(invalid_mounts_list)

        return cls(
            root=root,
            name=name,
            layout=layout,
            sources_dir=sources_dir,
            sources_path=sources_path,
            source_mounts=source_mounts,
            invalid_mounts=invalid_mounts,
            mirrors_dir=mirrors_dir,
            wiki_dir=wiki_dir,
            notes_dir=notes_dir,
            catalog_file=catalog_file,
            log_file=log_file,
            max_doc_bytes=max_bytes,
            max_source_bytes=max_src_bytes,
            sensitive=sensitive,
            exclude_patterns=exclude_patterns,
            compile_exclude_patterns=compile_exclude_patterns,
            compile_include_patterns=compile_include_patterns,
            compile_skip_csv=compile_skip_csv,
            compile_project_rollup=compile_project_rollup,
            compile_skip_csv_explicit=compile_skip_csv_explicit,
            compile_project_rollup_explicit=compile_project_rollup_explicit,
            compile_workers=compile_workers,
            compile_max_tokens=compile_max_tokens,
            compile_disable_thinking=compile_disable_thinking,
            ocr_image_dirs=ocr_image_dirs,
            anchor_terms=anchor_terms,
            acl_rules=acl_rules,
            domain_profile=domain_profile,
        )

    @property
    def is_external_sources(self) -> bool:
        return self.sources_path is not None or bool(self.source_mounts)

    @property
    def storage_mode(self) -> str:
        return "managed" if self.sources_path is not None else "in_place"

    @property
    def sources_root(self) -> Path:
        if self.sources_path is not None:
            return self.sources_path
        return self.root / self.sources_dir

    def iter_source_roots(self) -> list[tuple[str, Path]]:
        """Yield ``(mount_prefix, directory)`` pairs across the primary source root and all extra mounts."""
        roots: list[tuple[str, Path]] = [("", self.sources_root)]
        for mname, mpath in self.source_mounts:
            roots.append((mname, mpath))
        return roots

    def resolve_source_rel(self, rel: str) -> tuple[Path, Path] | None:
        """Resolve an already-prefix-stripped source relative path safely to ``(file_path, jail_root)``."""
        clean_rel = (rel or "").strip().replace("\\", "/").lstrip("/")
        if not clean_rel or ".." in clean_rel.split("/"):
            return None
        for mname, mpath in self.source_mounts:
            pfx = f"{mname}/"
            if clean_rel.startswith(pfx):
                sub = clean_rel[len(pfx):]
                if not sub:
                    return None
                jail = mpath.resolve()
                unres = jail / sub
                if unres.is_symlink():
                    return None
                cand = unres.resolve()
                if jail in cand.parents:
                    return (cand, jail)
                return None
        jail = self.sources_root.resolve()
        unres = jail / clean_rel
        if unres.is_symlink():
            return None
        cand = unres.resolve()
        if jail in cand.parents:
            return (cand, jail)
        return None

    def resolve_source_file(self, rel_or_ref: str) -> tuple[Path, Path] | None:
        """Resolve a logical source ref (prefixed with ``sources/`` or ``史料/``) safely to ``(file_path, jail_root)``."""
        return self.resolve_source_rel(self.strip_source_prefix(rel_or_ref))

    @property
    def mirrors_root(self) -> Path:
        return self.root / self.mirrors_dir

    @property
    def wiki_root(self) -> Path:
        return self.root / self.wiki_dir

    @property
    def notes_root(self) -> Path:
        return self.root / self.notes_dir

    @property
    def index_root(self) -> Path:
        return self.mirrors_root / self.index_subdir

    @property
    def catalog_path(self) -> Path:
        return self.root / self.catalog_file

    @property
    def log_path(self) -> Path:
        return self.root / self.log_file

    @property
    def synonyms_path(self) -> Path:
        return self.root / "synonyms.json"

    @property
    def source_prefix(self) -> str:
        return f"{self.sources_dir}/"

    @property
    def mirror_prefix(self) -> str:
        return f"{self.mirrors_dir}/"

    @property
    def wiki_prefix(self) -> str:
        return f"{self.wiki_dir}/"

    def is_source_ref(self, ref: str) -> bool:
        if not isinstance(ref, str):
            return False
        return ref.startswith(self.source_prefix) or ref.startswith("sources/") or ref.startswith("史料/")

    def is_wiki_ref(self, ref: str) -> bool:
        if not isinstance(ref, str):
            return False
        s = ref.strip().replace("\\", "/")
        return s.startswith(self.wiki_prefix) or s.startswith("wiki/") or s.startswith("知识库/")

    def normalize_memory_ref(self, ref: str) -> str:
        """Normalize a Layer-4 memory anchor: Layer-3 wiki pages stay wiki refs, all else is a source ref."""
        s = ref.strip().replace("\\", "/")
        if self.is_wiki_ref(s):
            for pfx in (self.wiki_prefix, "wiki/", "知识库/"):
                if s.startswith(pfx):
                    return self.wiki_prefix + s[len(pfx):]
        return self.normalize_source_ref(s)

    def normalize_source_ref(self, ref: str) -> str:
        s = ref.strip().replace("\\", "/")
        for pfx in (self.source_prefix, "sources/", "史料/"):
            if s.startswith(pfx):
                return self.source_prefix + s[len(pfx):]
        return self.source_prefix + s.lstrip("/")

    def strip_source_prefix(self, ref: str) -> str:
        s = ref.strip().replace("\\", "/")
        for pfx in (self.source_prefix, "sources/", "史料/"):
            if s.startswith(pfx):
                return s[len(pfx):]
        return s.lstrip("/")

    def strip_mirror_prefix(self, ref: str) -> str:
        s = ref.strip().replace("\\", "/")
        for pfx in (self.mirror_prefix, "mirrors/", "史料_md/"):
            if s.startswith(pfx):
                return s[len(pfx):]
        return s.lstrip("/")

    def config_signature_dict(self) -> dict:
        return {
            "layout": self.layout,
            "sources_dir": self.sources_dir,
            "mirrors_dir": self.mirrors_dir,
            "wiki_dir": self.wiki_dir,
            "max_doc_bytes": self.max_doc_bytes,
            "max_source_bytes": self.max_source_bytes,
            "exclude_patterns": list(self.exclude_patterns),
            "ocr_image_dirs": sorted(self.ocr_image_dirs),
        }


def normalize_domain_profile(raw: Any, *, preserve_meta: bool = True) -> dict[str, Any]:
    """Normalize and length-clamp a Vault domain profile dict."""
    if not isinstance(raw, dict):
        return {}
    domain = " ".join(str(raw.get("domain") or "").split()).strip()[:80]
    hints_raw = raw.get("entity_hints") or ()
    entity_hints: list[str] = []
    if isinstance(hints_raw, (list, tuple)):
        seen_h: set[str] = set()
        for item in hints_raw:
            clean_h = " ".join(str(item or "").split()).strip()[:36]
            if clean_h and clean_h.lower() not in seen_h:
                seen_h.add(clean_h.lower())
                entity_hints.append(clean_h)
            if len(entity_hints) >= 6:
                break
    query_tips = " ".join(str(raw.get("query_tips") or "").split()).strip()[:80]
    if not domain and not entity_hints and not query_tips:
        return {}

    out: dict[str, Any] = {
        "domain": domain,
        "entity_hints": entity_hints,
        "query_tips": query_tips,
    }
    if preserve_meta:
        src = str(raw.get("source") or "").strip().lower()
        if src in {"auto", "manual"}:
            out["source"] = src
        if "locked" in raw:
            out["locked"] = bool(raw.get("locked") is True)
        if raw.get("fingerprint"):
            out["fingerprint"] = str(raw["fingerprint"])[:64]
        top_dirs = raw.get("top_dirs_at_gen")
        if isinstance(top_dirs, (list, tuple)):
            out["top_dirs_at_gen"] = [str(x).strip() for x in top_dirs if str(x).strip()][:50]
        if "doc_count_at_gen" in raw:
            try:
                out["doc_count_at_gen"] = max(0, int(raw["doc_count_at_gen"]))
            except (TypeError, ValueError):
                pass
        if "wiki_count_at_gen" in raw:
            try:
                out["wiki_count_at_gen"] = max(0, int(raw["wiki_count_at_gen"]))
            except (TypeError, ValueError):
                pass
        if raw.get("updated_at"):
            out["updated_at"] = str(raw["updated_at"])[:40]
    return out


def load_domain_profile(vault_path: Path | str) -> dict[str, Any]:
    return dict(VaultConfig.load(vault_path).domain_profile)


def save_domain_profile(vault_path: Path | str, profile: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist `domain_profile` into `vault.json` (or `.vault.json`) without dropping keys."""
    root = Path(vault_path).expanduser().resolve()
    norm = normalize_domain_profile(profile)
    with file_lock(root / ".vault.lock", blocking=True):
        cfg_path = root / "vault.json"
        if not cfg_path.is_file() and (root / ".vault.json").is_file():
            cfg_path = root / ".vault.json"
        raw_cfg: dict[str, Any] = {}
        if cfg_path.is_file():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw_cfg = loaded
            except (OSError, ValueError):
                raw_cfg = {}
        if norm:
            raw_cfg["domain_profile"] = norm
        else:
            raw_cfg.pop("domain_profile", None)
        tmp = cfg_path.with_name(f".{cfg_path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(raw_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replace_with_retry(tmp, cfg_path)
    return norm


def format_domain_profile(profile: dict[str, Any], *, layout: str = "zh") -> str:
    """Format a compact, ACL-safe domain profile summary for MCP instructions / tool descriptions."""
    norm = normalize_domain_profile(profile, preserve_meta=False)
    domain = str(norm.get("domain") or "").strip()
    if not domain:
        return ""
    hints = [str(x).strip() for x in (norm.get("entity_hints") or ()) if str(x).strip()][:5]
    tips = str(norm.get("query_tips") or "").strip()
    if layout == "zh":
        parts = [f"行业领域：{domain}"]
        if hints:
            parts.append(f"核心实体：{' / '.join(hints)}")
        if tips:
            parts.append(f"提词建议：{tips}")
        text = "；".join(parts)
        if len(text) > 180:
            text = text[:179].rstrip("；、 ") + "…"
        return text
    parts = [f"Domain: {domain}"]
    if hints:
        parts.append(f"Entities: {' / '.join(hints)}")
    if tips:
        parts.append(f"Query tips: {tips}")
    text = "; ".join(parts)
    if len(text) > 320:
        text = text[:319].rstrip("; ") + "…"
    return text
