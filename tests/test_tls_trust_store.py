"""Tests for host-independent TLS trust-store resolution (`wiki_context.net`).

Regression: the packaged desktop engine (PyInstaller) carries an OpenSSL whose
compiled CA file points at a python.org framework path that need not exist on
the host, e.g. /Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/cert.pem.
Every HTTPS call then failed with "unable to get local issuer certificate" and the
console surfaced it verbatim as "「某库」文档整理失败：<urlopen error ...>".
"""

from __future__ import annotations

import ssl
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context import net  # noqa: E402
from wiki_context.compiler import build_http_llm_caller, test_llm_connection  # noqa: E402


def _reset() -> None:
    net.trust_store.cache_clear()


def _sample_pem_text() -> str:
    """Return a real PEM certificate bundle usable across macOS, Linux, and Windows CI."""
    for _src, cafile in net.candidate_bundles():
        try:
            txt = Path(cafile).read_text(encoding="utf-8", errors="ignore")
            if "BEGIN CERTIFICATE" in txt:
                return txt
        except OSError:
            continue
    enum_certs = getattr(ssl, "enum_certificates", None)
    if callable(enum_certs):
        pems: list[str] = []
        for der, enc, _trust in enum_certs("ROOT"):
            if enc == "x509_asn":
                pems.append(ssl.DER_cert_to_PEM_cert(der))
                if len(pems) >= 3:
                    break
        if pems:
            return "\n".join(pems) + "\n"
    raise unittest.SkipTest("No host PEM or Windows ROOT certificates available to construct test bundle")


class TestTrustStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict("os.environ", {}, clear=False)
        self._env.start()
        for var in net.ENV_VARS:
            import os

            os.environ.pop(var, None)
        _reset()

    def tearDown(self):
        _reset()
        self._env.stop()
        self._tmp.cleanup()

    def test_resolves_a_usable_bundle(self):
        store = net.trust_store()
        self.assertGreater(store.ca_count, 0, "a trust store with zero CAs cannot verify anything")
        self.assertTrue(store.source)
        info = net.describe_trust_store()
        self.assertTrue(info["ok"])
        self.assertEqual(info["ca_count"], store.ca_count)

    def test_explicit_env_bundle_wins(self):
        bundle = self.tmp / "ca.pem"
        bundle.write_text(_sample_pem_text(), encoding="utf-8")
        import os

        os.environ["WCTX_CA_BUNDLE"] = str(bundle)
        _reset()
        store = net.trust_store()
        self.assertEqual(store.source, "env:WCTX_CA_BUNDLE")
        self.assertEqual(store.cafile, str(bundle))
        self.assertGreater(store.ca_count, 0)

    def test_missing_env_bundle_falls_through(self):
        import os

        os.environ["WCTX_CA_BUNDLE"] = str(self.tmp / "does-not-exist.pem")
        _reset()
        store = net.trust_store()
        self.assertNotEqual(store.source, "env:WCTX_CA_BUNDLE")
        self.assertGreater(store.ca_count, 0)

    def test_default_store_without_cas_falls_back(self):
        """Simulate the frozen engine: default context trusts nothing, bundled/platform CA takes over."""
        original = net._build_context

        def fake_build(cafile):
            if cafile is None:
                return None
            return original(cafile)

        real_candidates = net.candidate_bundles()
        if real_candidates:
            cands = real_candidates
        else:
            # Bare Windows CI has no /etc/ssl PEM and may not have certifi installed; simulate bundled certifi
            bundle = self.tmp / "bundled-certifi.pem"
            bundle.write_text(_sample_pem_text(), encoding="utf-8")
            cands = [("certifi", str(bundle))]

        with mock.patch.object(net, "_build_context", side_effect=fake_build):
            with mock.patch.object(net, "candidate_bundles", return_value=cands):
                store = net._resolve()
        self.assertGreater(store.ca_count, 0)
        self.assertIn(store.source, {"certifi", "platform", "env:SSL_CERT_FILE"})

    def test_no_bundle_raises_actionable_error(self):
        with mock.patch.object(net, "candidate_bundles", return_value=[]):
            with mock.patch.object(net, "_build_context", return_value=None):
                with self.assertRaises(net.TLSUnavailable) as ctx:
                    net._resolve()
        message = str(ctx.exception)
        self.assertIn("WCTX_CA_BUNDLE", message)
        self.assertIn("根证书", message)


class TestNetworkErrorMessages(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_cert_failure_without_trust_store_is_actionable(self):
        cert_exc = ssl.SSLCertVerificationError(
            1,
            "certificate verify failed: unable to get local issuer certificate (_ssl.c:1085)",
        )
        wrapped = urllib.error.URLError(cert_exc)
        with mock.patch.object(
            net,
            "describe_trust_store",
            return_value={"ok": False, "error": "no CA"},
        ):
            message = net.format_network_error(wrapped, target="https://example.test/v1")
        self.assertIn("TLS 证书校验失败", message)
        self.assertIn("WCTX_CA_BUNDLE", message)
        self.assertNotIn("<urlopen error", message)

    def test_cert_failure_with_trust_store_points_at_endpoint(self):
        cert_exc = ssl.SSLCertVerificationError(1, "certificate verify failed")
        with mock.patch.object(
            net,
            "describe_trust_store",
            return_value={"ok": True, "ca_count": 140, "cafile": "/etc/ssl/cert.pem", "source": "platform"},
        ):
            message = net.format_network_error(urllib.error.URLError(cert_exc), target="https://x.test")
        self.assertIn("140 个根证书", message)
        self.assertIn("代理", message)

    def test_timeout_and_dns_are_explained(self):
        self.assertIn("超时", net.format_network_error(TimeoutError("timed out")))
        self.assertIn("域名", net.format_network_error(OSError("nodename nor servname provided")))

    def test_probe_reports_handshake(self):
        probe = net.probe_tls("127.0.0.1", port=1, timeout=1.0)
        self.assertFalse(probe["ok"])
        self.assertTrue(probe["error"])


class TestLLMCallerTLS(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_caller_passes_explicit_context(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices": [{"message": {"content": "OK"}}]}'

        def fake_urlopen(req, timeout=None, context=None):
            captured["context"] = context
            captured["url"] = req.full_url
            return FakeResponse()

        caller = build_http_llm_caller(
            model="m", base_url="https://llm.test/v1", api_key="k", protocol="openai"
        )
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(caller("sys", "usr"), "OK")
        self.assertIsInstance(captured["context"], ssl.SSLContext)
        self.assertEqual(captured["url"], "https://llm.test/v1/chat/completions")

    def test_compile_failure_message_is_readable(self):
        cert_exc = ssl.SSLCertVerificationError(
            1, "certificate verify failed: unable to get local issuer certificate (_ssl.c:1085)"
        )
        caller = build_http_llm_caller(
            model="m", base_url="https://llm.test/v1", api_key="k", protocol="openai"
        )
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(cert_exc)):
            with self.assertRaises(RuntimeError) as ctx:
                caller("sys", "usr")
        message = str(ctx.exception)
        self.assertIn("TLS 证书校验失败", message)
        self.assertNotIn("<urlopen error", message)

    def test_test_llm_connection_reports_trust_store(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                test_llm_connection(model="m", base_url="https://llm.test/v1", api_key="k")
        self.assertIn("TLS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
