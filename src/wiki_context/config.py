"""Vault configuration & bilingual directory layout auto-detection.

Supports both English (`sources/ mirrors/ wiki/ index.md log.md`) and
Chinese (`史料/ 史料_md/ 知识库/ 目录.md 日志.md`) three-layer LLM Wiki vaults
out of the box, with optional overrides via ``vault.json`` or ``.vault.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

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
    compile_skip_csv: bool = False
    compile_project_rollup: bool = False
    compile_workers: int = 4
    compile_max_tokens: int = 1800
    compile_disable_thinking: bool = False
    ocr_image_dirs: frozenset[str] = field(default_factory=frozenset)
    anchor_terms: tuple[str, ...] = ()
    acl_rules: dict[str, dict[str, list[str]]] = field(default_factory=dict)

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
        compile_skip_csv = bool(raw_cfg.get("compile_skip_csv") is True)
        compile_project_rollup = bool(raw_cfg.get("compile_project_rollup") is True)
        compile_workers = max(1, min(16, int(raw_cfg.get("compile_workers") or 4)))
        compile_max_tokens = max(256, min(8192, int(raw_cfg.get("compile_max_tokens") or 1800)))
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
            compile_workers=compile_workers,
            compile_max_tokens=compile_max_tokens,
            compile_disable_thinking=compile_disable_thinking,
            ocr_image_dirs=ocr_image_dirs,
            anchor_terms=anchor_terms,
            acl_rules=acl_rules,
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
