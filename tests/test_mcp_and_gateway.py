"""Tests for Multi-Vault Streamable HTTP Gateway, Token hot-reload, and concurrency."""

from __future__ import annotations

import base64
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

from wiki_context.context import PrefixACLPolicy, Principal  # noqa: E402
from wiki_context.gateway import MultiVaultGateway, TokenRegistry, create_http_server, selftest_gateway  # noqa: E402
from wiki_context.mcp_server import KBService, handle_jsonrpc_message, selftest_mcp  # noqa: E402
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
                # 1. Query specific vault via `/mcp/hr` Streamable HTTP route (`wiki_context` + legacy `kb_context` alias)
                mcp_call = {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "wiki_context", "arguments": {"question": "入职满一年有几天年假？"}},
                }
                req = urllib.request.Request(f"{base}/mcp/hr", data=json.dumps(mcp_call).encode(), headers=hdrs, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    self.assertIn("10 天", data["result"]["content"][0]["text"])
                    self.assertIn("<wiki_context", data["result"]["content"][0]["text"])

                legacy_call = {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "kb_context", "arguments": {"question": "入职满一年有几天年假？"}},
                }
                req_leg = urllib.request.Request(f"{base}/mcp/hr", data=json.dumps(legacy_call).encode(), headers=hdrs, method="POST")
                with urllib.request.urlopen(req_leg, timeout=5) as resp_leg:
                    self.assertIn("10 天", json.loads(resp_leg.read())["result"]["content"][0]["text"])

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

    def test_initialize_instructions_and_on_demand_pdf_visual(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "pdf_vault"
            init_vault(v, name="PolicyKB", layout="zh")
            (v / "史料" / "公开").mkdir(parents=True)
            (v / "史料" / "机密").mkdir(parents=True)

            minimal_pdf = (
                b"%PDF-1.4\n"
                b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 120]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
                b"4 0 obj<</Length 44>>stream\nBT /F1 14 Tf 20 60 Td (Policy 95518) Tj ET\nendstream\nendobj\n"
                b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
                b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n"
                b"0000000101 00000 n \n0000000212 00000 n \n0000000305 00000 n \n"
                b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n375\n%%EOF\n"
            )
            (v / "史料" / "公开" / "保单.pdf").write_bytes(minimal_pdf)
            (v / "史料" / "机密" / "内部保单.pdf").write_bytes(minimal_pdf)
            sync_vault(v)

            acl = PrefixACLPolicy({"staff": {"allow": ["史料/公开/**"], "deny": ["史料/机密/**"]}})
            svc = KBService(v, policy=acl)
            try:
                init_resp = handle_jsonrpc_message(
                    svc,
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                )
                assert init_resp is not None
                instr = init_resp["result"].get("instructions", "")
                self.assertIn("wiki_context", instr)
                self.assertIn("PolicyKB", instr)

                staff = Principal(uid="bob", groups=("staff",))
                deny_vis = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "wiki_inspect_visual", "arguments": {"source": "史料/机密/内部保单.pdf"}},
                    },
                    principal=staff,
                )
                assert deny_vis is not None
                self.assertTrue(deny_vis["result"]["isError"])

                ok_vis = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "wiki_inspect_visual", "arguments": {"source": "史料/公开/保单.pdf"}},
                    },
                    principal=staff,
                )
                assert ok_vis is not None
                from wiki_context.state_lock import resolve_external_tool

                if resolve_external_tool("pdftoppm") or (sys.platform == "darwin" and Path("/usr/bin/sips").is_file()):
                    self.assertFalse(ok_vis["result"]["isError"], ok_vis)
                    meta = json.loads(ok_vis["result"]["content"][0]["text"])
                    self.assertEqual(meta["asset"], "page-001.png")
                    self.assertTrue(meta["rendered_on_demand"])
            finally:
                svc.close()

    def test_all_nine_mcp_tools_annotations_and_jsonrpc_calls(self):
        """Verify MCP tool annotations and JSON-RPC `tools/call` across all 9 tools."""
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "all_tools_vault"
            init_vault(v, name="AllToolsKB", layout="en")
            (v / "sources" / "rfc-042.md").write_text(
                "# RFC-042 Rate Limiter\n\nDefault burst limit is 2500 RPS.\nTimeout is 150ms.\n",
                encoding="utf-8",
            )
            (v / "sources" / "diagram.png").write_bytes(tiny_png)
            sync_vault(v)

            svc = KBService(v)
            try:
                # 1. Verify tools/list returns MCP annotations for all 9 tools (full + slim_remote)
                list_resp = handle_jsonrpc_message(svc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert list_resp is not None
                tools = {t["name"]: t for t in list_resp["result"]["tools"]}
                expected_names = {
                    "wiki_context",
                    "wiki_search",
                    "wiki_read",
                    "wiki_route",
                    "wiki_list",
                    "wiki_inspect_visual",
                    "wiki_remember",
                    "wiki_forget",
                    "wiki_status",
                }
                self.assertEqual(set(tools.keys()), expected_names)

                read_only_tools = expected_names - {"wiki_remember", "wiki_forget"}
                for name in read_only_tools:
                    ann = tools[name].get("annotations")
                    self.assertIsInstance(ann, dict, f"missing annotations on {name}")
                    self.assertTrue(ann["title"])
                    self.assertTrue(ann["readOnlyHint"], name)
                    self.assertFalse(ann["destructiveHint"], name)
                    self.assertTrue(ann["idempotentHint"], name)
                    self.assertFalse(ann["openWorldHint"], name)

                rem_ann = tools["wiki_remember"]["annotations"]
                self.assertFalse(rem_ann["readOnlyHint"])
                self.assertFalse(rem_ann["destructiveHint"])
                self.assertFalse(rem_ann["idempotentHint"])
                self.assertFalse(rem_ann["openWorldHint"])

                fgt_ann = tools["wiki_forget"]["annotations"]
                self.assertFalse(fgt_ann["readOnlyHint"])
                self.assertTrue(fgt_ann["destructiveHint"])
                self.assertTrue(fgt_ann["idempotentHint"])
                self.assertFalse(fgt_ann["openWorldHint"])

                slim_resp = handle_jsonrpc_message(
                    svc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, slim_remote=True
                )
                assert slim_resp is not None
                for t in slim_resp["result"]["tools"]:
                    self.assertIn("annotations", t)
                    self.assertIn("readOnlyHint", t["annotations"])

                # 2. wiki_context
                r_ctx = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "wiki_context", "arguments": {"question": "What is the RFC-042 burst limit?"}},
                    },
                )
                assert r_ctx is not None
                self.assertFalse(r_ctx["result"]["isError"])
                self.assertIn("2500 RPS", r_ctx["result"]["content"][0]["text"])

                # 3. wiki_search
                r_search = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {"name": "wiki_search", "arguments": {"query": "RFC-042 burst limit", "k": 5}},
                    },
                )
                assert r_search is not None
                self.assertFalse(r_search["result"]["isError"])
                search_body = json.loads(r_search["result"]["content"][0]["text"])
                self.assertGreaterEqual(len(search_body["hits"]), 1)

                # 4. wiki_read (valid line range + path traversal blocked)
                r_read = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 12,
                        "method": "tools/call",
                        "params": {"name": "wiki_read", "arguments": {"source": "sources/rfc-042.md", "start": 1, "end": 10}},
                    },
                )
                assert r_read is not None
                self.assertFalse(r_read["result"]["isError"])
                read_body = json.loads(r_read["result"]["content"][0]["text"])
                self.assertIn("2500 RPS", read_body["text"])

                r_read_bad = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 13,
                        "method": "tools/call",
                        "params": {"name": "wiki_read", "arguments": {"source": "sources/../../etc/passwd"}},
                    },
                )
                assert r_read_bad is not None
                self.assertTrue(r_read_bad["result"]["isError"])

                # 5. wiki_route (fact vs complex)
                r_route_fact = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 14,
                        "method": "tools/call",
                        "params": {"name": "wiki_route", "arguments": {"question": "What is the RFC-042 timeout?"}},
                    },
                )
                assert r_route_fact is not None
                self.assertFalse(r_route_fact["result"]["isError"])
                route_fact = json.loads(r_route_fact["result"]["content"][0]["text"])
                self.assertEqual(route_fact["mode"], "fact")

                r_route_complex = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 15,
                        "method": "tools/call",
                        "params": {
                            "name": "wiki_route",
                            "arguments": {"question": "Compare RFC-042 and RFC-012 architecture evolution and trade-offs"},
                        },
                    },
                )
                assert r_route_complex is not None
                self.assertFalse(r_route_complex["result"]["isError"])
                route_complex = json.loads(r_route_complex["result"]["content"][0]["text"])
                self.assertEqual(route_complex["mode"], "complex")

                # 6. wiki_list
                r_list = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 16,
                        "method": "tools/call",
                        "params": {"name": "wiki_list", "arguments": {"filter": "rfc-042"}},
                    },
                )
                assert r_list is not None
                self.assertFalse(r_list["result"]["isError"])
                list_body = json.loads(r_list["result"]["content"][0]["text"])
                self.assertTrue(any("rfc-042.md" in d["source"] for d in list_body["docs"]))

                # 7. wiki_inspect_visual
                r_vis = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 17,
                        "method": "tools/call",
                        "params": {"name": "wiki_inspect_visual", "arguments": {"source": "sources/diagram.png"}},
                    },
                )
                assert r_vis is not None
                self.assertFalse(r_vis["result"]["isError"])
                self.assertTrue(any(b.get("type") == "image" for b in r_vis["result"]["content"]))

                # 8. wiki_remember -> 9. wiki_forget round-trip
                r_rem = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 18,
                        "method": "tools/call",
                        "params": {
                            "name": "wiki_remember",
                            "arguments": {
                                "text": "RFC-042 burst limit was raised to 3000 RPS in Q4.",
                                "kind": "errata",
                                "related_sources": ["sources/rfc-042.md"],
                                "tags": ["rate-limit"],
                            },
                        },
                    },
                )
                assert r_rem is not None
                self.assertFalse(r_rem["result"]["isError"])
                rem_body = json.loads(r_rem["result"]["content"][0]["text"])
                note_id = rem_body["note_id"]
                self.assertTrue(note_id.startswith("mem-"))

                r_fgt = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 19,
                        "method": "tools/call",
                        "params": {"name": "wiki_forget", "arguments": {"note_id": note_id}},
                    },
                )
                assert r_fgt is not None
                self.assertFalse(r_fgt["result"]["isError"])
                fgt_body = json.loads(r_fgt["result"]["content"][0]["text"])
                self.assertTrue(fgt_body["archived"])
                self.assertEqual(fgt_body["note_id"], note_id)

                # 10. wiki_status
                r_stat = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 20,
                        "method": "tools/call",
                        "params": {"name": "wiki_status", "arguments": {}},
                    },
                )
                assert r_stat is not None
                self.assertFalse(r_stat["result"]["isError"])
                stat_body = json.loads(r_stat["result"]["content"][0]["text"])
                self.assertEqual(stat_body["vault"], "AllToolsKB")
                self.assertGreaterEqual(stat_body["docs"], 1)
            finally:
                svc.close()


if __name__ == "__main__":
    unittest.main()
