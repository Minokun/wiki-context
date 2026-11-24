"""Lightweight shallow directory-mtime watcher for CLI `wctx ui` mode.

In Tauri Desktop mode (`WCTX_DESKTOP_WATCHER=1`), Rust's native `notify`
crate (`FSEvents` / `ReadDirectoryChangesW`) drives `/api/vaults/sync`
with zero idle CPU; this module provides the pure-Python fallback when
running `wctx ui` directly from a terminal.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from .config import VaultConfig


class VaultWatcherManager:
    """Debounced shallow directory-stat watcher triggering async incremental vault sync."""

    def __init__(
        self,
        *,
        on_change: Callable[[str], None],
        poll_interval_sec: float = 2.0,
        debounce_sec: float = 2.0,
    ):
        self.on_change = on_change
        self.poll_interval_sec = max(1.0, poll_interval_sec)
        self.debounce_sec = max(0.5, debounce_sec)
        self._lock = threading.Lock()
        self._watched: dict[str, Path] = {}
        self._last_sigs: dict[str, tuple[int, int]] = {}
        self._pending_since: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _shallow_sig(vault_path: Path) -> tuple[int, int]:
        try:
            cfg = VaultConfig.load(vault_path)
            roots = tuple(p for _, p in cfg.iter_source_roots()) + (cfg.wiki_root,)
        except Exception:
            roots = (vault_path,)

        max_ns = 0
        dir_count = 0
        for root in roots:
            if not root.is_dir():
                continue
            try:
                st = root.stat()
                max_ns = max(max_ns, st.st_mtime_ns)
                dir_count += 1
                with os.scandir(root) as it:
                    for entry in it:
                        try:
                            est = entry.stat(follow_symlinks=False)
                            max_ns = max(max_ns, est.st_mtime_ns)
                            dir_count += 1
                            if entry.is_dir(follow_symlinks=False):
                                with os.scandir(entry.path) as sub_it:
                                    for sub in sub_it:
                                        try:
                                            sst = sub.stat(follow_symlinks=False)
                                            max_ns = max(max_ns, sst.st_mtime_ns)
                                            dir_count += 1
                                        except OSError:
                                            pass
                        except OSError:
                            pass
            except OSError:
                pass
        return (max_ns, dir_count)

    def set_vaults(self, vault_paths: dict[str, Path]) -> None:
        with self._lock:
            self._watched = {k: Path(v) for k, v in vault_paths.items()}
            for k in list(self._last_sigs):
                if k not in self._watched:
                    self._last_sigs.pop(k, None)
                    self._pending_since.pop(k, None)
            for k, p in self._watched.items():
                if k not in self._last_sigs:
                    self._last_sigs[k] = self._shallow_sig(p)

    def start(self) -> None:
        if os.environ.get("WCTX_DESKTOP_WATCHER") == "1":
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wctx-vault-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval_sec):
            with self._lock:
                items = list(self._watched.items())
            if not items:
                continue
            now = time.monotonic()
            triggered: list[str] = []
            for vid, vpath in items:
                sig = self._shallow_sig(vpath)
                with self._lock:
                    prev = self._last_sigs.get(vid)
                    if prev is None:
                        self._last_sigs[vid] = sig
                    elif sig != prev:
                        self._last_sigs[vid] = sig
                        self._pending_since[vid] = now
                    elif vid in self._pending_since and (now - self._pending_since[vid]) >= self.debounce_sec:
                        self._pending_since.pop(vid, None)
                        triggered.append(vid)
            for vid in triggered:
                try:
                    self.on_change(vid)
                except Exception:
                    pass
