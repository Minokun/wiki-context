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
        "catalog_file": "index.md",
        "log_file": "log.md",
    },
    "zh": {
        "sources_dir": "史料",
        "mirrors_dir": "史料_md",
        "wiki_dir": "知识库",
        "catalog_file": "目录.md",
        "log_file": "日志.md",
    },
}

DEFAULT_MAX_DOC_BYTES = 2 * 1024 * 1024  # 2 MiB


@dataclass(frozen=True)
class VaultConfig:
    root: Path
    name: str
    layout: str = "en"
    sources_dir: str = "sources"
    mirrors_dir: str = "mirrors"
    wiki_dir: str = "wiki"
    catalog_file: str = "index.md"
    log_file: str = "log.md"
    index_subdir: str = ".index"
    max_doc_bytes: int = DEFAULT_MAX_DOC_BYTES
    sensitive: bool = False
    exclude_patterns: tuple[str, ...] = ()
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
        catalog_file = str(raw_cfg.get("catalog_file") or preset["catalog_file"]).strip("/")
        log_file = str(raw_cfg.get("log_file") or preset["log_file"]).strip("/")

        name = str(raw_cfg.get("name") or root.name or "vault").strip()
        max_bytes = int(raw_cfg.get("max_doc_bytes") or DEFAULT_MAX_DOC_BYTES)
        sensitive = bool(raw_cfg.get("sensitive") is True)

        excl_raw = raw_cfg.get("exclude_patterns") or ()
        exclude_patterns = tuple(
            str(x).strip() for x in excl_raw if isinstance(x, str) and str(x).strip()
        )

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

        return cls(
            root=root,
            name=name,
            layout=layout,
            sources_dir=sources_dir,
            mirrors_dir=mirrors_dir,
            wiki_dir=wiki_dir,
            catalog_file=catalog_file,
            log_file=log_file,
            max_doc_bytes=max_bytes,
            sensitive=sensitive,
            exclude_patterns=exclude_patterns,
            ocr_image_dirs=ocr_image_dirs,
            anchor_terms=anchor_terms,
            acl_rules=acl_rules,
        )

    @property
    def sources_root(self) -> Path:
        return self.root / self.sources_dir

    @property
    def mirrors_root(self) -> Path:
        return self.root / self.mirrors_dir

    @property
    def wiki_root(self) -> Path:
        return self.root / self.wiki_dir

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
            "exclude_patterns": list(self.exclude_patterns),
            "ocr_image_dirs": sorted(self.ocr_image_dirs),
        }
