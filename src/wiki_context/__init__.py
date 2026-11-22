"""wiki-context: Deterministic Context Engine for AI Agents."""

from __future__ import annotations

__version__ = "0.1.0"

from .config import VaultConfig
from .context import AccessPolicy, AllowAllPolicy, ContextPackEngine, PrefixACLPolicy, Principal, SynonymBook
from .indexer import check_index, rebuild_index
from .mcp_server import KBService
from .search import BM25Index, IndexUnavailableError, route, tokenize, tokenize_query

__all__ = [
    "__version__",
    "AccessPolicy",
    "AllowAllPolicy",
    "BM25Index",
    "ContextPackEngine",
    "IndexUnavailableError",
    "KBService",
    "PrefixACLPolicy",
    "Principal",
    "SynonymBook",
    "VaultConfig",
    "check_index",
    "rebuild_index",
    "route",
    "tokenize",
    "tokenize_query",
]
