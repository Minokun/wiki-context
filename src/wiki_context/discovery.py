"""Zero-config network discovery, Tailscale peer scanning & single-use OTP pairing (`wctx1_`).

Security model:
- Pairing codes NEVER embed long-lived Bearer tokens. They carry a 5-minute single-use
  `(otp_id, otp_secret)` ticket plus `(instance_id, fp)` TOFU fingerprint and local/Tailscale IPs.
- Connecting side strictly validates pairing URLs against an SSRF allowlist (RFC1918 private
  IPv4 or Tailscale CGNAT `100.64.0.0/10`, port >= 1024; blocks loopback and `169.254.0.0/16`
  unless `allow_loopback=True` is passed explicitly in tests).
- Connecting side verifies `/healthz` `instance_id` + `fp` BEFORE sending `POST /v1/pair/exchange`
  to the single fastest verified endpoint to mint a device-specific, revocable Bearer token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__
from .state_lock import resolve_external_tool

UDP_DISCOVERY_PORT = 48765
UDP_DISCOVERY_MAGIC = b"WCTX_DISCOVER_V1"
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_PAIRING_TTL_SEC = 300


def compute_host_fingerprint(instance_id: str, host_secret: str) -> str:
    digest = hashlib.sha256(f"{instance_id}:{host_secret}".encode("utf-8")).hexdigest()
    return f"sha256:{digest[:24]}"


def validate_pairing_url(raw_url: str, *, allow_loopback: bool = False) -> str:
    """Validate a candidate pairing/discovery URL against SSRF rules."""
    s = (raw_url or "").strip().rstrip("/")
    parsed = urlparse(s)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme in pairing target: {raw_url}")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URL authority are forbidden")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError(f"Missing host in URL: {raw_url}")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port < 1024 or port > 65535:
        raise ValueError(f"Pairing port must be >= 1024 (got {port})")

    if host.lower() == "localhost":
        if not allow_loopback:
            raise ValueError("Loopback pairing target blocked by SSRF guard")
        return f"{parsed.scheme}://{host}:{port}"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"Pairing URL host must be a direct IP address (got '{host}')") from exc

    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError(f"Disallowed link-local/special IP in pairing URL: {host}")
    if ip.is_loopback:
        if not allow_loopback:
            raise ValueError(f"Loopback IP blocked in pairing URL: {host}")
        return f"{parsed.scheme}://{host}:{port}"

    if ip.version == 4 and (ip.is_private or ip in TAILSCALE_CGNAT):
        return f"{parsed.scheme}://{host}:{port}"

    raise ValueError(f"Pairing IP '{host}' is outside RFC1918 private LAN and Tailscale 100.64/10 ranges")


def validate_pairing_urls(urls: list[str], *, allow_loopback: bool = False) -> list[str]:
    clean: list[str] = []
    for u in urls:
        norm = validate_pairing_url(u, allow_loopback=allow_loopback)
        if norm not in clean:
            clean.append(norm)
    if not clean:
        raise ValueError("Pairing code contains no valid reachable private/Tailscale URLs")
    return clean


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def decode_pairing_code(code: str, *, allow_loopback: bool = False) -> dict[str, Any]:
    s = (code or "").strip()
    if not s.startswith("wctx1_"):
        raise ValueError("无效的配对码格式（必须以 wctx1_ 开头）")
    try:
        payload = json.loads(_b64u_decode(s[len("wctx1_"):]).decode("utf-8"))
    except Exception as exc:
        raise ValueError("配对码解析失败或已损坏") from exc
    if not isinstance(payload, dict) or int(payload.get("v") or 0) != 1:
        raise ValueError("不支持的配对码版本")
    exp = int(payload.get("exp") or 0)
    if exp <= int(time.time()):
        raise ValueError("配对码已过期（有效期 5 分钟），请在提供端重新生成")
    instance_id = str(payload.get("instance_id") or "").strip()
    fp = str(payload.get("fp") or "").strip()
    otp_id = str(payload.get("otp_id") or "").strip()
    otp_secret = str(payload.get("otp_secret") or "").strip()
    raw_urls = payload.get("urls") or []
    if not instance_id or not fp or not otp_id or not otp_secret or not isinstance(raw_urls, list):
        raise ValueError("配对码缺少必要的身份指纹或一次性票据字段")
    valid_urls = validate_pairing_urls([str(u) for u in raw_urls], allow_loopback=allow_loopback)
    return {
        "v": 1,
        "host": str(payload.get("host") or instance_id),
        "instance_id": instance_id,
        "fp": fp,
        "urls": valid_urls,
        "otp_id": otp_id,
        "otp_secret": otp_secret,
        "exp": exp,
    }


class PairingManager:
    """In-memory single-use OTP pairing ticket registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, dict[str, Any]] = {}

    def _purge_expired_locked(self, now: float | None = None) -> None:
        cur = now if now is not None else time.time()
        dead = [k for k, v in self._tickets.items() if float(v.get("exp", 0)) <= cur]
        for k in dead:
            self._tickets.pop(k, None)

    def create_pairing_code(
        self,
        *,
        host_label: str,
        instance_id: str,
        fp: str,
        urls: list[str],
        allowed_vaults: list[str],
        groups: list[str],
        ttl_sec: int = DEFAULT_PAIRING_TTL_SEC,
        allow_loopback: bool = False,
    ) -> dict[str, Any]:
        valid_urls = validate_pairing_urls(urls, allow_loopback=allow_loopback)
        ttl = max(30, min(int(ttl_sec), 900))
        otp_id = f"otp_{secrets.token_hex(4)}"
        otp_secret = secrets.token_urlsafe(18)
        exp = int(time.time()) + ttl

        with self._lock:
            self._purge_expired_locked()
            self._tickets[otp_id] = {
                "otp_secret": otp_secret,
                "exp": exp,
                "allowed_vaults": list(allowed_vaults) or ["*"],
                "groups": list(groups) or ["reader"],
                "attempts": 0,
            }

        payload = {
            "v": 1,
            "host": host_label,
            "instance_id": instance_id,
            "fp": fp,
            "urls": valid_urls,
            "otp_id": otp_id,
            "otp_secret": otp_secret,
            "exp": exp,
        }
        code = "wctx1_" + _b64u_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return {
            "code": code,
            "otp_id": otp_id,
            "expires_at": exp,
            "ttl_sec": ttl,
            "urls": valid_urls,
            "instance_id": instance_id,
            "fp": fp,
        }

    def redeem_otp(self, otp_id: str, otp_secret: str) -> dict[str, Any]:
        clean_id = (otp_id or "").strip()
        clean_sec = (otp_secret or "").strip()
        if not clean_id or not clean_sec:
            raise ValueError("Missing otp_id or otp_secret")

        with self._lock:
            self._purge_expired_locked()
            rec = self._tickets.get(clean_id)
            if rec is None:
                raise ValueError("Pairing ticket invalid, already used, or expired")
            if float(rec.get("exp", 0)) <= time.time():
                self._tickets.pop(clean_id, None)
                raise ValueError("Pairing ticket expired")
            if not hmac.compare_digest(clean_sec, str(rec.get("otp_secret") or "")):
                rec["attempts"] = int(rec.get("attempts") or 0) + 1
                if rec["attempts"] >= 3:
                    self._tickets.pop(clean_id, None)
                raise ValueError("Invalid pairing secret")
            # Single-use: burn ticket immediately upon match
            self._tickets.pop(clean_id, None)
            return {
                "allowed_vaults": list(rec["allowed_vaults"]),
                "groups": list(rec["groups"]),
            }


def probe_verified_pairing_endpoint(
    urls: list[str],
    *,
    expected_instance_id: str,
    expected_fp: str,
    timeout: float = 2.0,
) -> tuple[str, dict[str, Any]]:
    """Concurrently probe candidate URLs and return the fastest endpoint matching TOFU fingerprint."""

    def _probe(u: str) -> tuple[str, float, dict[str, Any]]:
        t0 = time.perf_counter()
        req = urllib.request.Request(f"{u.rstrip('/')}/healthz", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            hz = json.loads(resp.read().decode("utf-8"))
        latency_ms = (time.perf_counter() - t0) * 1000.0
        if str(hz.get("instance_id") or "") != expected_instance_id:
            raise ValueError(f"instance_id mismatch from {u}")
        if str(hz.get("fp") or "") != expected_fp:
            raise ValueError(f"TOFU fingerprint mismatch from {u}")
        return u, latency_ms, hz

    errors: list[str] = []
    hits: list[tuple[float, str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(urls)))) as pool:
        futs = [pool.submit(_probe, u) for u in urls]
        for fut in as_completed(futs):
            try:
                u, lat, hz = fut.result()
                hits.append((lat, u, hz))
            except Exception as exc:
                errors.append(str(exc))

    if not hits:
        raise ValueError(f"无法连通并验证配对码中的任何地址 ({'; '.join(errors[:3])})")
    hits.sort(key=lambda x: x[0])
    best_lat, best_url, best_hz = hits[0]
    return best_url, {"latency_ms": round(best_lat, 2), "healthz": best_hz}


# -- Tailscale & LAN UDP Discovery ------------------------------------------ #

_TS_CACHE_LOCK = threading.Lock()
_TS_CACHE: tuple[float, dict[str, Any]] | None = None
TAILSCALE_CACHE_TTL_SEC = 10.0


def discover_tailscale_peers(*, force_refresh: bool = False) -> dict[str, Any]:
    """Query local `tailscale status --json` outside any console lock with a 10s cache."""
    global _TS_CACHE
    now = time.monotonic()
    if not force_refresh:
        with _TS_CACHE_LOCK:
            if _TS_CACHE is not None and (now - _TS_CACHE[0]) < TAILSCALE_CACHE_TTL_SEC:
                return _TS_CACHE[1]

    ts_bin = resolve_external_tool("tailscale")
    if not ts_bin:
        res = {"available": False, "tool_path": "", "self_ips": [], "peers": [], "note": "未检测到 Tailscale CLI"}
        with _TS_CACHE_LOCK:
            _TS_CACHE = (now, res)
        return res

    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            res = {
                "available": False,
                "tool_path": ts_bin,
                "self_ips": [],
                "peers": [],
                "note": (proc.stderr or "Tailscale 未登录或守护进程未运行").strip()[:160],
            }
        else:
            raw = json.loads(proc.stdout)
            self_node = raw.get("Self") if isinstance(raw.get("Self"), dict) else {}
            self_ips = [
                str(ip) for ip in (self_node.get("TailscaleIPs") or ())
                if isinstance(ip, str) and "." in ip
            ]
            peers_out: list[dict[str, Any]] = []
            raw_peers = raw.get("Peer") if isinstance(raw.get("Peer"), dict) else {}
            for pdata in raw_peers.values():
                if not isinstance(pdata, dict):
                    continue
                ips = [str(ip) for ip in (pdata.get("TailscaleIPs") or ()) if isinstance(ip, str) and "." in ip]
                if not ips:
                    continue
                peers_out.append({
                    "hostname": str(pdata.get("HostName") or pdata.get("DNSName") or ips[0]).rstrip("."),
                    "dns_name": str(pdata.get("DNSName") or "").rstrip("."),
                    "tailscale_ips": ips,
                    "os": str(pdata.get("OS") or ""),
                    "online": bool(pdata.get("Online", False)),
                })
            peers_out.sort(key=lambda x: (not x["online"], x["hostname"].lower()))
            res = {
                "available": True,
                "tool_path": ts_bin,
                "self_ips": self_ips,
                "peers": peers_out,
                "note": "",
            }
    except Exception as exc:
        res = {"available": False, "tool_path": ts_bin, "self_ips": [], "peers": [], "note": str(exc)[:160]}

    with _TS_CACHE_LOCK:
        _TS_CACHE = (time.monotonic(), res)
    return res


class LanDiscoveryResponder:
    """Best-effort UDP responder on port 48765 when LAN sharing gateway is active."""

    def __init__(
        self,
        *,
        instance_id: str,
        fp: str,
        host_label: str,
        get_gateway_port: Callable[[], int | None],
    ):
        self.instance_id = instance_id
        self.fp = fp
        self.host_label = host_label
        self.get_gateway_port = get_gateway_port
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)
            sock.bind(("0.0.0.0", UDP_DISCOVERY_PORT))
            self._sock = sock
        except OSError:
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wctx-udp-discovery", daemon=True)
        self._thread.start()
        return True

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != UDP_DISCOVERY_MAGIC:
                continue
            gw_port = self.get_gateway_port()
            if not gw_port:
                continue
            reply = json.dumps({
                "v": 1,
                "instance_id": self.instance_id,
                "fp": self.fp,
                "host": self.host_label,
                "gateway_port": int(gw_port),
                "version": __version__,
            }, ensure_ascii=False).encode("utf-8")
            try:
                self._sock.sendto(reply, addr)
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


def probe_lan_udp(*, timeout: float = 0.65, self_instance_id: str = "") -> list[dict[str, Any]]:
    """Broadcast a best-effort UDP discovery probe on port 48765."""
    found: dict[str, dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.15)
        try:
            sock.sendto(UDP_DISCOVERY_MAGIC, ("255.255.255.255", UDP_DISCOVERY_PORT))
        except OSError:
            return []

        deadline = time.monotonic() + max(0.2, timeout)
        while time.monotonic() < deadline:
            try:
                raw, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                obj = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(obj, dict) or not obj.get("instance_id"):
                continue
            iid = str(obj["instance_id"])
            if self_instance_id and iid == self_instance_id:
                continue
            port = int(obj.get("gateway_port") or 8090)
            ip = addr[0]
            if iid not in found:
                found[iid] = {
                    "instance_id": iid,
                    "fp": str(obj.get("fp") or ""),
                    "host": str(obj.get("host") or ip),
                    "ip": ip,
                    "gateway_port": port,
                    "url": f"http://{ip}:{port}",
                    "version": str(obj.get("version") or ""),
                    "channel": "lan-udp",
                }
    finally:
        sock.close()
    return list(found.values())
