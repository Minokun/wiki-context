"""Host-independent TLS trust-store resolution for stdlib HTTPS clients.

Packaged desktop builds ship their own Python/OpenSSL (PyInstaller bundle). That
OpenSSL keeps a *compiled-in* CA file that belongs to a python.org framework
install::

    /Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/cert.pem

When that framework version is not installed on the user's machine the bundle
starts with an empty trust store, so every outgoing HTTPS request fails with::

    <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
     unable to get local issuer certificate (_ssl.c:1085)>

The console then reports it verbatim as "「某库」文档整理失败：<urlopen error ...>",
which tells the user nothing actionable. This module resolves a trust store that
does not depend on the compiled-in path (env override -> bundled certifi ->
platform bundle -> OS store) and exposes it as a ready ``ssl.SSLContext``.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Explicit user overrides, highest priority first. ``SSL_CERT_FILE`` is honoured
#: because it is the OpenSSL-native way to point at a bundle.
ENV_VARS: tuple[str, ...] = (
    "WCTX_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

#: Well-known CA bundles that exist without any Python package being installed.
PLATFORM_BUNDLES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/etc/ssl/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
        "/usr/local/etc/openssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/openssl/cert.pem",
    ),
    "linux": (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/ca-bundle.pem",
        "/etc/ssl/cert.pem",
    ),
    "win32": (),
}

_FIX_HINT = (
    "请任选一种方式修复后重试："
    "① 设置环境变量 WCTX_CA_BUNDLE=/etc/ssl/cert.pem（macOS/Linux 通用根证书包）；"
    "② 安装证书包 pip install certifi；"
    "③ 重新安装桌面版（新版本内置根证书，不再依赖宿主机 Python）。"
)


class TLSUnavailable(RuntimeError):
    """No usable CA bundle could be loaded, so HTTPS cannot be verified."""


@dataclass(frozen=True)
class TrustStore:
    """The CA bundle that outgoing HTTPS requests actually trust."""

    context: ssl.SSLContext
    source: str
    cafile: str
    ca_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "source": self.source,
            "cafile": self.cafile,
            "ca_count": self.ca_count,
            "openssl": ssl.OPENSSL_VERSION,
        }


def _ca_count(ctx: ssl.SSLContext) -> int:
    try:
        return int(ctx.cert_store_stats().get("x509_ca") or 0)
    except (AttributeError, ssl.SSLError, ValueError):
        return 0


def _build_context(cafile: str | None) -> ssl.SSLContext | None:
    """Return a verifying context with at least one CA, else ``None``."""
    try:
        if cafile:
            ctx = ssl.create_default_context(cafile=cafile)
        else:
            ctx = ssl.create_default_context()
            ctx.load_default_certs()
    except (OSError, ValueError, ssl.SSLError):
        return None
    return ctx if _ca_count(ctx) > 0 else None


def _platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform.startswith("win"):
        return "win32"
    return ""


def _certifi_bundle() -> str:
    """Bundled ``certifi`` CA file when the build shipped one."""
    try:
        import certifi  # optional dependency, present in packaged engines
    except Exception:  # pragma: no cover - depends on the interpreter
        return ""
    try:
        path = Path(certifi.where())
    except Exception:  # pragma: no cover - defensive
        return ""
    return str(path) if path.is_file() else ""


def candidate_bundles() -> list[tuple[str, str]]:
    """Ordered ``(source, cafile)`` candidates that exist on disk."""
    found: list[tuple[str, str]] = []
    for var in ENV_VARS:
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
        except (OSError, ValueError):
            continue
        if path.is_file():
            found.append((f"env:{var}", str(path)))
    certifi_bundle = _certifi_bundle()
    if certifi_bundle:
        found.append(("certifi", certifi_bundle))
    for path_str in PLATFORM_BUNDLES.get(_platform_key(), ()):
        path = Path(path_str)
        if path.is_file():
            found.append(("platform", str(path)))
    return found


def _resolve() -> TrustStore:
    for source, cafile in candidate_bundles():
        ctx = _build_context(cafile)
        if ctx is not None:
            return TrustStore(context=ctx, source=source, cafile=cafile, ca_count=_ca_count(ctx))
    ctx = _build_context(None)  # Windows cert store / distro-installed OpenSSL defaults
    if ctx is not None:
        paths = ssl.get_default_verify_paths()
        return TrustStore(
            context=ctx,
            source="system",
            cafile=paths.cafile or paths.openssl_cafile or "",
            ca_count=_ca_count(ctx),
        )
    paths = ssl.get_default_verify_paths()
    missing = paths.openssl_cafile or paths.cafile or "未知路径"
    raise TLSUnavailable(
        "未找到可用的 TLS 根证书，HTTPS 请求无法完成。"
        f"当前 OpenSSL 默认证书位置为 {missing}（该文件不存在或为空）。"
        f"{_FIX_HINT}"
    )


@lru_cache(maxsize=1)
def trust_store() -> TrustStore:
    """Resolve (once per process) the CA bundle used for outgoing HTTPS."""
    return _resolve()


def ssl_context() -> ssl.SSLContext:
    """Ready-to-use verifying context for ``urllib.request.urlopen(..., context=...)``."""
    return trust_store().context


def describe_trust_store() -> dict[str, Any]:
    """Diagnostic view for ``wctx doctor`` / the console settings page."""
    try:
        return trust_store().as_dict()
    except TLSUnavailable as exc:
        return {"ok": False, "error": str(exc), "openssl": ssl.OPENSSL_VERSION}


def probe_tls(host: str, *, port: int = 443, timeout: float = 8.0, attempts: int = 2) -> dict[str, Any]:
    """Complete a real TLS handshake against ``host`` using the resolved trust store.

    Network hiccups are retried once; certificate failures are returned at once
    because they are deterministic (a retry cannot change the verdict).
    """
    result: dict[str, Any] = {"host": host, "port": port, "ok": False, "error": ""}
    try:
        ctx = ssl_context()
    except TLSUnavailable as exc:
        result["error"] = str(exc)
        return result
    started = time.perf_counter()
    for attempt in range(max(1, attempts)):
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    result["tls_version"] = tls.version() or ""
                    cert = tls.getpeercert() or {}
                    result["subject"] = str(cert.get("subject", ""))
                    result["issuer"] = str(cert.get("issuer", ""))
                    result["not_after"] = str(cert.get("notAfter", ""))
            result["ok"] = True
            result["error"] = ""
            break
        except ssl.SSLCertVerificationError as exc:
            result["error"] = format_network_error(exc, target=host)
            break
        except (OSError, ssl.SSLError, ValueError) as exc:
            result["error"] = f"无法与 {host}:{port} 建立 TLS 连接：{exc}"
            if attempt + 1 >= max(1, attempts):
                break
            time.sleep(0.6)
    result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return result


def _error_text(exc: BaseException) -> str:
    """Readable failure text without urllib's ``<urlopen error ...>`` wrapper."""
    text = str(exc).strip()
    if text.startswith("<urlopen error "):
        text = text[len("<urlopen error ") :]
        if text.endswith(">"):
            text = text[:-1]
    if not text:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, BaseException):
            text = str(reason)
    return text.strip()


def _looks_like_cert_failure(exc: BaseException, text: str) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text


def format_network_error(exc: BaseException, *, target: str = "", timeout_sec: float | None = None) -> str:
    """Translate ``urllib``/``ssl``/socket failures into one actionable Chinese line."""
    where = f"（{target}）" if target else ""
    text = _error_text(exc)
    reason = getattr(exc, "reason", None)
    cert_probe = reason if isinstance(reason, BaseException) else exc

    if _looks_like_cert_failure(cert_probe, text):
        info = describe_trust_store()
        if info.get("ok"):
            return (
                f"TLS 证书校验失败{where}：系统已加载 {info.get('ca_count')} 个根证书"
                f"（来自 {info.get('cafile') or info.get('source')}），但目标站点证书不在其中。"
                "常见原因：公司代理/安全软件做了 HTTPS 中间人拦截、证书已过期，或域名配置有误。"
                "如属代理拦截，请把代理根证书加入 PEM 文件后用 WCTX_CA_BUNDLE 指向它。"
            )
        return (
            f"TLS 证书校验失败{where}：本机没有可用的根证书信任库，"
            f"因此无法验证任何 HTTPS 站点（原始报错：{text}）。{_FIX_HINT}"
        )

    if isinstance(exc, TimeoutError) or "timed out" in text.lower():
        suffix = f"（超过 {timeout_sec:g} 秒）" if timeout_sec else ""
        return f"连接{where}超时{suffix}，请检查网络或代理设置"

    if "Name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
        return f"无法解析{where}的域名，请检查网络连通性或 DNS 设置"

    if "Connection refused" in text:
        return f"目标拒绝连接{where}，请确认服务地址与端口是否正确"

    return f"无法连接{where}：{text}"
