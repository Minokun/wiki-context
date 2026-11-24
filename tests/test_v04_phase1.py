"""Tests for v0.4.0 Phase 1: locks, Windows hardening, telemetry, ACL explain, Tauri CORS/SSE, OTP pairing & Playground."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.console import ConsoleState, create_console_server  # noqa: E402
from wiki_context.context import PrefixACLPolicy, Principal  # noqa: E402
from wiki_context.discovery import PairingManager, decode_pairing_code, validate_pairing_url  # noqa: E402
from wiki_context.indexer import rebuild_index  # noqa: E402
from wiki_context.mcp_server import KBService, run_stdio_server  # noqa: E402
from wiki_context.remote_stdio import run_bridge  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.state_lock import atomic_write_bytes, ensure_schema_version, file_lock  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402
from wiki_context.telemetry import TelemetryStore, scrub_telemetry_text  # noqa: E402
from wiki_context.watcher import VaultWatcherManager  # noqa: E402


class TestV04Phase1(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_lock_atomic_0600_and_schema_migration(self):
        lock_file = self.root / "test.lock"
        with file_lock(lock_file, blocking=False):
            with self.assertRaises(RuntimeError):
                with file_lock(lock_file, blocking=False):
                    pass

        secret_file = self.root / "secret.json"
        atomic_write_bytes(secret_file, b'{"k": "v"}\n', private_mode=True)
        if os.name != "nt":
            mode = stat.S_IMODE(secret_file.stat().st_mode)
            self.assertEqual(mode, 0o600)

        migrated, changed = ensure_schema_version(
            {"version": 1, "vaults": []},
            target_version=2,
            migrator=lambda d, _v: {**d, "instance_id": "inst_test"},
            source_path=secret_file,
        )
        self.assertTrue(changed)
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["instance_id"], "inst_test")
        self.assertTrue((self.root / "secret.json.v1.bak").is_file())

    def test_deferred_trash_gc_for_windows_winerror32(self):
        vdir = self.root / "win-trash-vault"
        init_vault(vdir, name="WinTrash", layout="en")
        (vdir / "sources" / "a.md").write_text("# Doc A\nHello world 1\n", encoding="utf-8")
        sync_vault(vdir, rebuild=True)

        # Force 3 more generations while simulating Windows [WinError 32] lock on old gen-* dirs
        real_rmtree = shutil.rmtree

        def locked_rmtree(path, *args, **kwargs):
            if "gen-" in str(path):
                raise PermissionError(32, "The process cannot access the file because it is being used")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch("wiki_context.indexer.shutil.rmtree", side_effect=locked_rmtree):
            for i in range(3):
                (vdir / "sources" / "a.md").write_text(f"# Doc A\nHello world v{i+2}\n", encoding="utf-8")
                sync_vault(vdir, rebuild=True)

        trash_file = vdir / "mirrors" / ".index" / ".trash.json"
        self.assertTrue(trash_file.is_file())
        trashed = json.loads(trash_file.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(trashed), 1)

        # Next rebuild with unlocked handles sweeps `.trash.json` cleanly
        (vdir / "sources" / "a.md").write_text("# Doc A\nHello world final\n", encoding="utf-8")
        sync_vault(vdir, rebuild=True)
        self.assertFalse(trash_file.is_file())
        for old_gen in trashed:
            self.assertFalse((vdir / "mirrors" / ".index" / old_gen).exists())

    def test_stdio_utf8_binary_under_gbk_cp936(self):
        vdir = self.root / "gbk-vault"
        init_vault(vdir, name="GBKVault", layout="zh")
        (vdir / "史料" / "spec.md").write_text("# 限流规范\n默认限流阈值 📌 3500 RPS ⚠️ 图片 🖼️\n", encoding="utf-8")
        sync_vault(vdir, rebuild=True)

        req_payload = (
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "wiki_context", "arguments": {"question": "限流阈值"}},
            }, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")

        in_buf = io.BytesIO(req_payload)
        out_buf = io.BytesIO()
        fake_stdin = io.TextIOWrapper(in_buf, encoding="gbk")
        fake_stdout = io.TextIOWrapper(out_buf, encoding="gbk")  # strict GBK text wrapper!

        with mock.patch.object(sys, "stdin", fake_stdin), mock.patch.object(sys, "stdout", fake_stdout):
            with mock.patch.dict(os.environ, {"WCTX_TELEMETRY": "0"}):
                rc = run_stdio_server(vdir)

        self.assertEqual(rc, 0)
        raw_out = out_buf.getvalue().decode("utf-8")
        resp = json.loads(raw_out.strip())
        self.assertIn("3500 RPS", resp["result"]["content"][0]["text"])
        self.assertIn("⚠️", resp["result"]["content"][0]["text"])

    def test_acl_decision_and_telemetry_store(self):
        pol = PrefixACLPolicy({
            "partner": {"allow": ["sources/public/**"], "deny": ["sources/public/secret/**"]},
        })
        p = Principal(uid="ext", groups=("partner",))
        self.assertEqual(pol.explain_source_access(p, "sources/public/doc.md").reason_code, "allowed")
        d_deny = pol.explain_source_access(p, "sources/public/secret/a.md")
        self.assertFalse(d_deny.allowed)
        self.assertEqual(d_deny.reason_code, "deny_prefix")
        d_out = pol.explain_source_access(p, "sources/hr/salary.md")
        self.assertEqual(d_out.reason_code, "outside_allowlist")
        d_wiki = pol.explain_wiki_access(p, "wiki/Summary.md", ["sources/public/secret/a.md"])
        self.assertFalse(d_wiki.allowed)
        self.assertEqual(d_wiki.reason_code, "wiki_provenance_blocked")

        self.assertNotIn("wctx_abc123456789", scrub_telemetry_text("tok=wctx_abc123456789 code=wctx1_abcdef123456"))

        db_path = self.root / "telemetry.sqlite"
        tel = TelemetryStore(db_path=db_path, async_writer=True)
        try:
            tel.record({
                "client": "cursor",
                "mode": "stdio",
                "vault_id": "wiki",
                "tool": "wiki_context",
                "query": "with bearer wctx_secret_token_xyz123",
                "elapsed_ms": 12.5,
                "wiki_count": 0,
                "primary_count": 0,
                "acl_filtered_count": 2,
                "missing_concepts": ["未收录概念X"],
                "hit_sources": ["sources/public/doc.md"],
            })
            tel.flush()
            listed = tel.list_invocations(limit=10)
            self.assertEqual(len(listed["items"]), 1)
            row = listed["items"][0]
            self.assertEqual(row["client"], "cursor")
            self.assertEqual(row["acl_filtered_count"], 2)
            self.assertNotIn("wctx_secret_token_xyz123", row["query"])
            st = tel.stats(hours=24)
            self.assertEqual(st["total_calls"], 1)
            self.assertEqual(st["top_missing_concepts"][0]["term"], "未收录概念X")
        finally:
            tel.close()

    def test_cors_dual_auth_playground_sse_async_sync_and_otp_pairing(self):
        # Set up Computer A and Computer B consoles
        cfg_a = self.root / "cfg-a"
        cfg_b = self.root / "cfg-b"
        home_a = self.root / "home-a"
        home_b = self.root / "home-b"
        home_a.mkdir()
        home_b.mkdir()

        vault_a = self.root / "vault-a"
        (vault_a / "public").mkdir(parents=True)
        (vault_a / "secret").mkdir(parents=True)
        (vault_a / "public" / "gateway.md").write_text("# 网关规范\n默认限流阈值为 4200 RPS。\n", encoding="utf-8")
        (vault_a / "secret" / "budget.md").write_text("# 财务预算\n研发年度预算 880 万元。\n", encoding="utf-8")

        admin_tok = "adm_secret_test_token_12345"
        stop_evt_a = threading.Event()
        state_a = ConsoleState(config_dir=cfg_a, client_home=home_a, enable_watcher=False)
        srv_a = create_console_server(state_a, host="127.0.0.1", port=0, admin_token=admin_tok, shutdown_event=stop_evt_a)
        port_a = srv_a.server_address[1]
        base_a = f"http://127.0.0.1:{port_a}"
        threading.Thread(target=srv_a.serve_forever, daemon=True).start()

        state_b = ConsoleState(config_dir=cfg_b, client_home=home_b, enable_watcher=False)
        srv_b = create_console_server(state_b, host="127.0.0.1", port=0)
        port_b = srv_b.server_address[1]
        base_b = f"http://127.0.0.1:{port_b}"
        threading.Thread(target=srv_b.serve_forever, daemon=True).start()

        try:
            def api_a(path: str, body: dict | None = None, *, hdrs: dict[str, str] | None = None) -> dict:
                h = {"X-WCTX-Admin-Token": admin_tok, **(hdrs or {})}
                data = None
                method = "GET"
                if body is not None:
                    method = "POST"
                    h["Content-Type"] = "application/json"
                    data = json.dumps(body).encode("utf-8")
                with urllib.request.urlopen(urllib.request.Request(f"{base_a}{path}", data=data, headers=h, method=method), timeout=10) as r:
                    return json.loads(r.read().decode("utf-8"))

            # 1. Tauri CORS preflight & Origin enforcement
            req_opt = urllib.request.Request(
                f"{base_a}/api/overview",
                headers={"Origin": "tauri://localhost"},
                method="OPTIONS",
            )
            with urllib.request.urlopen(req_opt, timeout=5) as r_opt:
                self.assertEqual(r_opt.status, 204)
                self.assertEqual(r_opt.headers.get("Access-Control-Allow-Origin"), "tauri://localhost")

            with self.assertRaises(urllib.error.HTTPError) as evil_err:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base_a}/api/overview",
                        headers={"Origin": "https://evil.example.com", "X-WCTX-Admin-Token": admin_tok},
                    ),
                    timeout=5,
                )
            evil_err.exception.close()
            self.assertEqual(evil_err.exception.code, 403)

            # 2. /api/bootstrap & onboard + async sync job + watch + synonyms
            boot = api_a("/api/bootstrap")
            self.assertEqual(boot["mode"], "desktop")
            self.assertEqual(boot["instanceId"], state_a.instance_id)

            api_a("/api/vaults/onboard", {"path": str(vault_a), "vault_id": "wiki-a", "layout": "zh"})
            job = api_a("/api/vaults/sync", {"vault_id": "wiki-a", "async": True})
            self.assertEqual(job["status"], "running")
            for _ in range(40):
                jres = api_a(f"/api/jobs/{job['job_id']}")
                if jres["status"] == "done":
                    break
                time.sleep(0.05)
            self.assertEqual(jres["status"], "done")

            w_res = api_a("/api/vaults/watch", {"vault_id": "wiki-a", "enabled": True})
            self.assertTrue(w_res["watching"])

            syn_res = api_a("/api/vaults/synonyms", {
                "vault_id": "wiki-a",
                "groups": [{"canonical": ["限流阈值"], "aliases": ["QPS上限"]}],
            })
            self.assertEqual(syn_res["count"], 1)

            # 3. Playground X-Ray + structured ACL Shadow Diff
            api_a("/api/acl/save", {"acl": {"partner": {"allow": ["史料/public/**"], "deny": ["史料/secret/**"]}}})
            pg = api_a("/api/playground/inspect", {
                "vault_id": "wiki-a",
                "question": "年度预算多少万元",
                "mode": "wiki_context",
                "principal_spec": {"type": "group", "groups": ["partner"]},
            })
            self.assertEqual(pg["kind"], "local")
            self.assertTrue(pg["capabilities"]["can_shadow_diff"])
            self.assertTrue(any(b["reason_code"] in {"deny_prefix", "outside_allowlist"} for b in pg["shadow_blocked"]))

            # 4. Telemetry list + SSE /api/logs/stream + graceful shutdown
            state_a.telemetry.flush()
            logs = api_a("/api/logs/list?limit=10")
            self.assertGreaterEqual(len(logs["items"]), 1)

            sse_req = urllib.request.Request(
                f"{base_a}/api/logs/stream",
                headers={"X-WCTX-Admin-Token": admin_tok, "Origin": "tauri://localhost"},
            )
            with urllib.request.urlopen(sse_req, timeout=5) as sse_resp:
                first_chunk = sse_resp.readline().decode("utf-8")
                self.assertIn("retry:", first_chunk)
                stop_evt_a.set()

            # 5. OTP Pairing + SSRF Guard + Token Rotation
            with self.assertRaises(ValueError):
                validate_pairing_url("http://169.254.169.254:8090/latest/meta-data")
            with self.assertRaises(ValueError):
                validate_pairing_url("http://8.8.8.8:8090")

            state_a.start_lan_gateway(host="127.0.0.1", port=0, persist=False)
            ticket = state_a.create_pairing_ticket(
                allowed_vaults=["wiki-a"],
                groups=["partner"],
                allow_loopback=True,
            )
            code = ticket["code"]
            decoded = decode_pairing_code(code, allow_loopback=True)
            self.assertNotIn("token", decoded)

            # Connect Computer B using one-time pairing code
            paired = state_b.connect_with_pairing_code(
                code=code,
                client_label="HomeMac",
                allow_loopback=True,
            )
            self.assertEqual(paired["instance_id"], state_a.instance_id)
            self.assertEqual(paired["connected_vaults"][0]["id"], "wiki-a")

            # Reusing the same pairing code must fail (single-use burned!)
            with self.assertRaises(ValueError):
                state_b.connect_with_pairing_code(code=code, client_label="Attacker", allow_loopback=True)

            # Remote Playground mode respects capabilities.can_xray == False
            pg_remote = state_b.inspect_playground(vault_id="wiki-a", question="默认限流阈值", mode="wiki_search")
            self.assertEqual(pg_remote["kind"], "remote")
            self.assertTrue(pg_remote["unsupported"])

            # Token rotation invalidates old secret immediately
            toks = state_a.list_tokens_public()
            paired_tok = next(t for t in toks if t["uid"].startswith("peer-"))
            rot = state_a.rotate_token(paired_tok["id"])
            self.assertTrue(rot["token"].startswith("wctx_"))
        finally:
            stop_evt_a.set()
            srv_a.shutdown()
            srv_a.server_close()
            state_a.close()
            srv_b.shutdown()
            srv_b.server_close()
            state_b.close()

    def test_06_builtin_llm_compiler_agent_pipeline(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _MockLLMHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                raw = json.loads(self.rfile.read(length).decode("utf-8"))
                usr_msg = str((raw.get("messages") or [{}])[-1].get("content") or "")
                if "Reply with the single word OK" in usr_msg:
                    reply_text = "OK"
                else:
                    reply_text = (
                        '---\ntitle: "服务限流规范"\ntype: source\nstatus: reviewed\nupdated: 2026-11-24\n'
                        'sources:\n  - 史料/spec.md\n---\n\n# 服务限流规范\n\n## 概览\n\n网关单实例最大 QPS 为 3200。\n\n'
                        '## 关联页面\n\n[[知识库总览]]\n\n## 来源\n\n- 史料/spec.md\n'
                    )
                out = json.dumps({"choices": [{"message": {"content": reply_text}}]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        mock_srv = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        mock_port = mock_srv.server_address[1]
        threading.Thread(target=mock_srv.serve_forever, daemon=True).start()

        state = ConsoleState(config_dir=self.root / "llm-home", client_home=self.root / "fake-home", enable_watcher=False)
        try:
            saved = state.save_llm_compiler_config({
                "provider": "custom",
                "protocol": "openai",
                "base_url": f"http://127.0.0.1:{mock_port}/v1",
                "model": "mock-librarian",
                "api_key": "sk-test-secret-123456",
                "auto_compile_on_sync": True,
                "max_items_per_run": 10,
            })
            self.assertTrue(saved["ready"])
            self.assertTrue(saved["has_api_key"])
            self.assertIn("****", saved["api_key_masked"])

            ping = state.test_llm_compiler()
            self.assertTrue(ping["ok"])

            # Onboard a fresh folder with a loose markdown document -> should auto-trigger LLM compiler agent!
            raw_folder = self.root / "new-docs"
            raw_folder.mkdir()
            (raw_folder / "spec.md").write_text("# 服务限流规范\n网关单实例最大 QPS 为 3200。\n", encoding="utf-8")

            ob = state.onboard_and_register(path=str(raw_folder), vault_id="wiki-auto", name="自动整理库", layout="zh")
            self.assertTrue(ob["llm_ready"])
            cjob = ob["compile_job"]
            self.assertIsNotNone(cjob)
            jid = cjob["job_id"]

            for _ in range(50):
                st = state.get_job(jid)
                if st["status"] in {"done", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(st["status"], "done")
            self.assertGreaterEqual(len(st["result"]["compiled_pages"]), 1)

            # Verify L3 Wiki is indexed in FTS5 and uncompiled_count dropped to 0
            detailed = state.list_vaults_detailed()
            vinfo = next(v for v in detailed if v["id"] == "wiki-auto")
            self.assertGreaterEqual(vinfo["wiki_docs"], 2)
            self.assertEqual(vinfo["uncompiled_count"], 0)
        finally:
            state.close()
            mock_srv.shutdown()
            mock_srv.server_close()

    def test_07_telemetry_opt_out_sensitive_scrub_watcher_and_bridge(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        # 1. Default auto_compile_on_sync is False, and WCTX_TELEMETRY=0 suppresses telemetry.sqlite in Console & stdio
        fake_ak = "LT" + "AI" + "0000TestDummyKey1234"
        scrubbed = scrub_telemetry_text(f"用 {fake_ak} 登录，密码：MySecret99!")
        self.assertNotIn(fake_ak, scrubbed)
        self.assertNotIn("MySecret99!", scrubbed)
        self.assertIn("[REDACTED_ACCESS_KEY_ID]", scrubbed)
        self.assertIn("[REDACTED_SECRET]", scrubbed)

        tel_cfg = self.root / "no-tel-home"
        v_dir = self.root / "vault-priv"
        init_vault(v_dir, name="PrivKB", layout="en")
        (v_dir / "sources" / "note.md").write_text("# Note\n\nPrivate fact alpha-42.\n", encoding="utf-8")
        sync_vault(v_dir)

        with mock.patch.dict(os.environ, {"WCTX_TELEMETRY": "0", "WCTX_TELEMETRY_DB": str(tel_cfg / "stdio_tel.sqlite")}):
            adm_tok = "adm_test_notel_tok"
            st = ConsoleState(config_dir=tel_cfg, client_home=self.root / "fake-home", enable_watcher=False)
            self.assertFalse(st.get_llm_compiler_status()["auto_compile_on_sync"])
            srv = create_console_server(st, host="127.0.0.1", port=0, admin_token=adm_tok)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                self.assertFalse((tel_cfg / "telemetry.sqlite").exists())
                req_list = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/logs/list",
                    headers={"X-WCTX-Admin-Token": adm_tok},
                )
                with urllib.request.urlopen(req_list, timeout=3) as r:
                    logs_data = json.loads(r.read().decode("utf-8"))
                self.assertEqual(logs_data["items"], [])
                req_stats = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/logs/stats?hours=24",
                    headers={"X-WCTX-Admin-Token": adm_tok},
                )
                with urllib.request.urlopen(req_stats, timeout=3) as r:
                    stats_data = json.loads(r.read().decode("utf-8"))
                self.assertEqual(stats_data["total_calls"], 0)
                self.assertEqual(len(stats_data["hourly"]), 24)
            finally:
                srv.shutdown()
                srv.server_close()
                st.close()
            self.assertFalse((tel_cfg / "telemetry.sqlite").exists())

            fake_stdin = type("FakeStd", (), {"buffer": io.BytesIO(b"")})()
            with mock.patch.object(sys, "stdin", fake_stdin):
                self.assertEqual(run_stdio_server(v_dir), 0)
            self.assertFalse((tel_cfg / "stdio_tel.sqlite").exists())

        # 2. Sensitive vault & WCTX_TELEMETRY_LOG_QUERIES=0 omit query text while preserving numeric counts
        (v_dir / "vault.json").write_text(json.dumps({"name": "PrivKB", "layout": "en", "sensitive": True}), encoding="utf-8")
        sens_db = self.root / "sens_tel.sqlite"
        tel = TelemetryStore(db_path=sens_db, async_writer=False, enabled=True)
        try:
            with mock.patch.dict(os.environ, {"WCTX_USER": "alice", "WCTX_INTERNAL_MODEL": "1"}):
                svc = KBService(v_dir, telemetry=tel, telemetry_vault_id="priv")
                try:
                    _, is_err = svc.dispatch_tool("wiki_context", {"question": "What is private fact alpha-42?"})
                    self.assertFalse(is_err)
                finally:
                    svc.close()
            rows = tel.list_invocations(limit=10)["items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["query"], "")

            with mock.patch.dict(os.environ, {"WCTX_TELEMETRY_LOG_QUERIES": "0"}):
                tel.record({"client": "cursor", "mode": "stdio", "vault_id": "pub", "query": "secret prompt"})
            rows2 = tel.list_invocations(limit=10)["items"]
            self.assertEqual(len(rows2), 2)
            self.assertEqual(rows2[0]["query"], "")
        finally:
            tel.close()

        # 3. VaultWatcherManager detects source edits and triggers debounced callback
        w_vault = self.root / "watch-vault"
        init_vault(w_vault, name="WatchKB", layout="en")
        (w_vault / "sources" / "doc.md").write_text("# Doc\nv1\n", encoding="utf-8")
        fired = threading.Event()
        seen_vids: list[str] = []
        wm = VaultWatcherManager(
            on_change=lambda vid: (seen_vids.append(vid), fired.set()),
            poll_interval_sec=1.0,
            debounce_sec=0.5,
        )
        wm.set_vaults({"watch-kb": w_vault})
        wm.start()
        try:
            (w_vault / "sources" / "doc2.md").write_text("# Doc2\nnew file\n", encoding="utf-8")
            self.assertTrue(fired.wait(timeout=5.0), "VaultWatcherManager did not trigger on_change")
            self.assertEqual(seen_vids[0], "watch-kb")
        finally:
            wm.stop()

        # 4. remote_stdio.run_bridge forwards JSON-RPC over HTTP with Bearer token and handles errors
        class _MockBridgeHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != "Bearer wctx_bridge_tok":
                    self.send_response(401)
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length") or "0")
                msg = json.loads(self.rfile.read(n).decode("utf-8"))
                if msg.get("id") == 2:
                    self.send_response(502)
                    self.end_headers()
                    return
                resp = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"msg": "中文响应"}}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        b_srv = HTTPServer(("127.0.0.1", 0), _MockBridgeHandler)
        b_port = b_srv.server_address[1]
        threading.Thread(target=b_srv.serve_forever, daemon=True).start()
        try:
            in_buf = io.BytesIO(
                b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
                b'{"jsonrpc":"2.0","id":2,"method":"tools/call"}\n'
            )
            out_buf = io.BytesIO()
            fake_in = type("FakeIn", (), {"buffer": in_buf})()
            fake_out = type("FakeOut", (), {"buffer": out_buf})()
            with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out):
                self.assertEqual(run_bridge(url=f"http://127.0.0.1:{b_port}/mcp", token="wctx_bridge_tok"), 0)
            lines = [json.loads(line) for line in out_buf.getvalue().decode("utf-8").splitlines() if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["result"]["msg"], "中文响应")
            self.assertEqual(lines[1]["error"]["code"], -32000)
            self.assertIn("502", lines[1]["error"]["message"])
        finally:
            b_srv.shutdown()
            b_srv.server_close()

    def test_console_unlimited_migration_and_cancel_job(self) -> None:
        cfg_dir = self.root / "legacy-llm-cfg"
        cfg_dir.mkdir()
        # Simulate legacy console.json where old UI implicitly saved max_items_per_run=15
        (cfg_dir / "console.json").write_text(
            json.dumps({
                "vaults": {},
                "llm_compiler": {
                    "provider": "deepseek",
                    "protocol": "openai",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4.1-flash",
                    "max_items_per_run": 15,
                },
            }),
            encoding="utf-8",
        )
        state = ConsoleState(config_dir=cfg_dir, client_home=self.root / "fake-home-mig", enable_watcher=False)
        try:
            st = state.get_llm_compiler_status()
            self.assertEqual(st["max_items_per_run"], 0)
            self.assertEqual(st["max_tokens"], 4096)

            # Explicitly choosing 15 persists max_items_explicit and honors 15
            saved15 = state.save_llm_compiler_config({"max_items_per_run": 15})
            self.assertEqual(saved15["max_items_per_run"], 15)

            # Explicitly switching back to 0 (all pending) honors 0 and max_tokens up to 16384
            saved0 = state.save_llm_compiler_config({"max_items_per_run": 0, "max_tokens": 16384})
            self.assertEqual(saved0["max_items_per_run"], 0)
            self.assertEqual(saved0["max_tokens"], 16384)
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
