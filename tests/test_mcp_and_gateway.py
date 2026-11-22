"""Tests for Multi-Vault Streamable HTTP Gateway, Token hot-reload, and concurrency."""

from __future__ import annotations

import concurrent.futures
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.gateway import MultiVaultGateway, TokenRegistry, create_http_server, selftest_gateway  # noqa: E402
from wiki_context.mcp_server import selftest_mcp  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


class TestMCPAndGateway(unittest.TestCase):
    def test_multi_vault_gateway_acl_hot_reload_and_concurrency(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            v_eng = root / "eng"
            v_hr = root / "hr"
            init_vault(v_eng, name="EngineeringKB", layout="en")
            init_vault(v_hr, name="PeopleOpsKB", layout="zh")

            (v_eng / "sources" / "grpc.md").write_text("# gRPC Keepalive\nPing interval is 30s.\n", encoding="utf-8")
            (v_hr / "史料" / "假期制度.md").write_text("# 年假规定\n入职满一年享有 10 天带薪年假。\n", encoding="utf-8")
            sync_vault(v_eng)
            sync_vault(v_hr)

            self.assertEqual(selftest_mcp(v_eng), 0)
            self.assertEqual(selftest_gateway({"eng": v_eng, "hr": v_hr}), 0)

            tok_file = root / "tokens.json"
            tok_file.write_text(
                json.dumps({
                    "tokens": [
                        {"uid": "alice", "token": "tok_alice_secret_1", "groups": ["all"]},
                    ]
                }),
                encoding="utf-8",
            )

            reg = TokenRegistry(token_file=tok_file)
            gw = MultiVaultGateway({"eng": v_eng, "hr": v_hr}, tokens=reg)
            srv = create_http_server(gw, host="127.0.0.1", port=0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            hdrs = {"Authorization": "Bearer tok_alice_secret_1", "Content-Type": "application/json"}

            try:
                # 1. Query specific vault via `/mcp/hr` Streamable HTTP route
                mcp_call = {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "kb_context", "arguments": {"question": "入职满一年有几天年假？"}},
                }
                req = urllib.request.Request(f"{base}/mcp/hr", data=json.dumps(mcp_call).encode(), headers=hdrs, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    self.assertIn("10 天", data["result"]["content"][0]["text"])

                # 2. Query default vault (`eng`) via REST `/v1/context` (spoofed `user` ignored)
                rest_req = urllib.request.Request(
                    f"{base}/v1/context",
                    data=json.dumps({"question": "What is the gRPC ping interval?", "user": "root"}).encode(),
                    headers=hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(rest_req, timeout=5) as resp:
                    rdata = json.loads(resp.read())
                    self.assertIn("30s", rdata["context_markdown"])

                # 3. 24-worker concurrent queries while rebuilding `eng` index in parallel
                def _worker(i: int) -> bool:
                    target_v = "eng" if i % 2 == 0 else "hr"
                    q = "gRPC ping interval" if target_v == "eng" else "带薪年假几天"
                    r = urllib.request.Request(
                        f"{base}/v1/context",
                        data=json.dumps({"vault": target_v, "question": q}).encode(),
                        headers=hdrs,
                        method="POST",
                    )
                    with urllib.request.urlopen(r, timeout=5) as res:
                        body = json.loads(res.read())
                        return "error" not in body and body["primary_count"] >= 1

                (v_eng / "sources" / "extra.md").write_text("# TLS\nMinimum TLS 1.3.\n", encoding="utf-8")
                with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                    futs = [pool.submit(_worker, i) for i in range(24)]
                    sync_vault(v_eng)  # Live atomic hot-reload mid-flight!
                    results = [f.result() for f in futs]
                self.assertTrue(all(results))

                # 4. Token revocation hot-reload
                time.sleep(0.02)
                tok_file.write_text(
                    json.dumps({"tokens": [{"uid": "bob", "token": "tok_bob_2", "groups": ["all"]}]}),
                    encoding="utf-8",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                    urllib.request.urlopen(urllib.request.Request(f"{base}/v1/whoami", headers=hdrs))
                ctx_err.exception.close()
                self.assertEqual(ctx_err.exception.code, 401)
            finally:
                srv.shutdown()
                srv.server_close()
                gw.close()


if __name__ == "__main__":
    unittest.main()
