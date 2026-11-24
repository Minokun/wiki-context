"""End-to-end tests for Control Console (`wctx ui`), Folder Onboarding, Vault/Prefix ACL,
Multi-Client MCP Config Sync (DSH, Codex, Claude Code, WorkBuddy, Cursor), and Layer-4 Memory CRUD.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context import __version__  # noqa: E402
from wiki_context.cli import main as cli_main  # noqa: E402
from wiki_context.console import ConsoleState, create_console_server, load_console_vault_map  # noqa: E402
from wiki_context.ops import install_service, render_service_definition, run_doctor, uninstall_service  # noqa: E402


class TestConsoleAndClients(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg_dir = self.root / "wctx-home"
        self.fake_home = self.root / "fake-user-home"
        self.fake_home.mkdir(parents=True)

        # Seed realistic pre-existing client configs to test lossless merging
        dsh_dir = self.fake_home / ".dsh" / "profiles" / "desktop"
        dsh_dir.mkdir(parents=True)
        (dsh_dir / "cordis.patch.yml").write_text(
            "disabled: !!js '!ctx.get(''profileContext'')'\n"
            "- insert:\n"
            "    - id: mcp-mt\n"
            "      name: '@deepseek-ai/dsh-mcp-client'\n"
            "      config:\n"
            "        serverName: mt\n"
            "        transport: stdio\n"
            "        command: node\n"
            "- id: preset-ptc\n"
            "  name: '@deepseek-ai/dsh-agent-preset'\n",
            encoding="utf-8",
        )

        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            '[projects."/srv/app"]\ntrust_level = "trusted"\n\n'
            '[mcp_servers.magpie]\ncommand = "/bin/magpie"\nargs = ["mcp"]\n',
            encoding="utf-8",
        )

        (self.fake_home / ".claude.json").write_text(
            json.dumps({
                "lastReleaseNotesSeen": "2.1.0",
                "mcpServers": {
                    "magpie-memory": {"type": "stdio", "command": "/bin/magpie", "args": ["mcp"]},
                },
            }),
            encoding="utf-8",
        )
        (self.fake_home / ".workbuddy").mkdir(parents=True)
        (self.fake_home / ".cursor").mkdir(parents=True)
        (self.fake_home / ".gemini" / "config").mkdir(parents=True)
        magpie_dir = self.fake_home / ".config" / "magpie"
        magpie_dir.mkdir(parents=True)
        (magpie_dir / "library.json").write_text(
            json.dumps({
                "mcp": [
                    {
                        "name": "kb",
                        "transport": "stdio",
                        "command": "/usr/bin/python3",
                        "args": ["/legacy/kb_mcp.py", "--vault", "/legacy/vault"],
                        "agents": ["agy", "claude", "codex", "cursor"],
                        "local": True,
                    },
                    {"name": "magpie-memory", "transport": "stdio", "command": "/bin/magpie", "args": ["mcp"]},
                ],
                "applied": {
                    "agy": {"mcp": ["kb", "magpie-memory"]},
                    "dsh": {"mcp": ["magpie-memory"]},
                },
            }),
            encoding="utf-8",
        )

        self.state = ConsoleState(config_dir=self.cfg_dir, client_home=self.fake_home)
        self.srv = create_console_server(self.state, host="127.0.0.1", port=0)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

        # Fetch index.html to extract the dynamic CSRF token
        with urllib.request.urlopen(f"{self.base}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        m = re.search(r'"csrfToken":\s*"([^"]+)"', html)
        assert m is not None
        self.csrf = m.group(1)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.state.close()
        self._tmp.cleanup()

    def _api(self, path: str, body: dict | None = None, *, csrf: str | None = "DEFAULT") -> dict:
        hdrs: dict[str, str] = {}
        if csrf == "DEFAULT":
            hdrs["X-WCTX-CSRF"] = self.csrf
        elif csrf is not None:
            hdrs["X-WCTX-CSRF"] = csrf
        data = None
        method = "GET"
        if body is not None:
            method = "POST"
            hdrs["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=hdrs, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_security_guards_csrf_and_non_loopback_bind(self):
        # 1. Refuses non-loopback bind without allow_remote_admin
        with self.assertRaises(ValueError):
            create_console_server(self.state, host="0.0.0.0", port=0)

        # 2. Rejects /api/overview without CSRF header
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._api("/api/overview", csrf=None)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)

    def test_full_console_workflow_onboard_tokens_acl_clients_and_memory(self):
        # 1. Prepare a raw non-empty document folder and onboard it in-place (zh layout)
        raw_folder = self.root / "研发规范资料"
        (raw_folder / "公开").mkdir(parents=True)
        (raw_folder / "机密").mkdir(parents=True)
        (raw_folder / "公开" / "网关限流规范.md").write_text(
            "# 网关限流规范\n默认单租户限流阈值为 3500 RPS。\n", encoding="utf-8"
        )
        (raw_folder / "机密" / "底价核算.md").write_text(
            "# 内部底价\n年度保底授权单价为 68 万元。\n", encoding="utf-8"
        )

        insp = self._api("/api/fs/inspect", {"path": str(raw_folder)})
        self.assertEqual(insp["kind"], "raw_documents_folder")
        self.assertEqual(insp["detected_layout"], "zh")
        self.assertTrue(insp["suggested_id"].startswith("wiki"))

        ob = self._api("/api/vaults/onboard", {
            "path": str(raw_folder),
            "vault_id": "wiki-rd",
            "name": "研发规范库",
            "layout": "zh",
        })
        self.assertEqual(ob["vault_id"], "wiki-rd")
        self.assertTrue((raw_folder / "史料" / "公开" / "网关限流规范.md").is_file())
        self.assertTrue((raw_folder / "史料_md" / ".index" / "current.json").is_file())

        # Add a confidential Wiki page referencing 史料/机密/底价核算.md to verify provenance ACL
        (raw_folder / "知识库" / "商务底价综述.md").write_text(
            "---\ntitle: 商务底价综述\nsources:\n  - 史料/机密/底价核算.md\n---\n\n# 商务底价综述\n年度保底 68 万。\n",
            encoding="utf-8",
        )
        self._api("/api/vaults/sync", {"vault_id": "wiki-rd"})

        # 2. Onboard a second empty vault (`wiki-hr`) + import an external folder into `wiki-rd`
        hr_folder = self.root / "hr-vault"
        self._api("/api/vaults/onboard", {"path": str(hr_folder), "vault_id": "wiki-hr", "name": "HR库", "layout": "en"})

        ext_folder = self.root / "外部交付材料"
        ext_folder.mkdir()
        (ext_folder / "sla.md").write_text("# 外部交付 SLA\n故障响应时间 <= 15 分钟。\n", encoding="utf-8")
        imp_res = self._api("/api/vaults/import-folder", {
            "vault_id": "wiki-rd",
            "external_folder": str(ext_folder),
            "target_subfolder": "交付标准",
        })
        self.assertEqual(imp_res["copied_count"], 1)

        # 3. Create restricted Bearer token (allowed ONLY `wiki-rd`, group `partner`)
        tok_res = self._api("/api/tokens/create", {
            "uid": "partner-bot",
            "label": "外部合作方只读令牌",
            "groups": ["partner"],
            "allowed_vaults": ["wiki-rd"],
        })
        raw_token = tok_res["token"]
        tok_id = tok_res["record"]["id"]
        self.assertTrue(raw_token.startswith("wctx_"))
        # Plaintext token must NEVER exist on disk
        self.assertNotIn(raw_token, (self.cfg_dir / "tokens.json").read_text(encoding="utf-8"))

        # Configure Role Prefix ACL (`allow: ["**"]`, `deny: ["史料/机密/**"]`)
        self._api("/api/acl/save", {
            "acl": {"partner": {"allow": ["**"], "deny": ["史料/机密/**"]}},
        })

        # Verify ACL Simulator
        sim_rd = self._api("/api/acl/simulate", {"token_id": tok_id, "vault_id": "wiki-rd"})
        self.assertTrue(sim_rd["vault_allowed"])
        self.assertEqual(sim_rd["denied_sources_count"], 1)
        self.assertIn("知识库/商务底价综述.md", sim_rd["denied_wiki"])

        sim_hr = self._api("/api/acl/simulate", {"token_id": tok_id, "vault_id": "wiki-hr"})
        self.assertFalse(sim_hr["vault_allowed"])

        # Verify live Gateway HTTP enforcement (`403` on unauthorized `wiki-hr`, redacted confidential on `wiki-rd`)
        gw_hdrs = {"Authorization": f"Bearer {raw_token}", "Content-Type": "application/json"}
        with urllib.request.urlopen(urllib.request.Request(f"{self.base}/v1/vaults", headers=gw_hdrs), timeout=5) as r:
            vlist = json.loads(r.read())
            self.assertEqual([v["id"] for v in vlist["vaults"]], ["wiki-rd"])

        with self.assertRaises(urllib.error.HTTPError) as hr_err:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.base}/mcp/wiki-hr",
                    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                    headers=gw_hdrs,
                    method="POST",
                ),
                timeout=5,
            )
        hr_err.exception.close()
        self.assertEqual(hr_err.exception.code, 403)

        # Public doc allowed vs Confidential doc blocked on `/v1/context`
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{self.base}/v1/context",
                data=json.dumps({"vault": "wiki-rd", "question": "默认单租户限流阈值是多少？"}).encode(),
                headers=gw_hdrs,
                method="POST",
            ),
            timeout=5,
        ) as r_pub:
            self.assertIn("3500 RPS", json.loads(r_pub.read())["context_markdown"])

        with urllib.request.urlopen(
            urllib.request.Request(
                f"{self.base}/v1/context",
                data=json.dumps({"vault": "wiki-rd", "question": "年度保底授权单价是多少？"}).encode(),
                headers=gw_hdrs,
                method="POST",
            ),
            timeout=5,
        ) as r_sec:
            self.assertNotIn("68 万", json.loads(r_sec.read())["context_markdown"])

        # 4. Sync MCP Configs across ALL 6 AI clients (DSH, Codex, Claude Code, WorkBuddy, Cursor, Antigravity)
        sync_mcp = self._api("/api/clients/sync", {
            "client_ids": ["dsh", "codex", "claude", "workbuddy", "cursor", "agy"],
            "mode": "stdio",
            "vault_ids": ["wiki-rd", "wiki-hr"],
        })
        self.assertEqual(len(sync_mcp["results"]), 6)

        # Verify DSH preserved `!!js` & `mcp-mt` and bumped version on second sync
        dsh_text = (self.fake_home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("!!js '!ctx.get(''profileContext'')'", dsh_text)
        self.assertIn("id: mcp-mt", dsh_text)
        self.assertIn("serverName: 'wiki-rd'", dsh_text)
        self.assertIn("WCTX_MCP_VERSION: '1'", dsh_text)

        self._api("/api/clients/sync", {
            "client_ids": ["dsh"],
            "mode": "stdio",
            "vault_ids": ["wiki-rd"],
        })
        dsh_text_2 = (self.fake_home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("WCTX_MCP_VERSION: '2'", dsh_text_2)
        self.assertNotIn("serverName: 'wiki-hr'", dsh_text_2)

        # Verify Antigravity (`mcp_config.json`) & Magpie (`library.json`) also replaced legacy `kb` cleanly
        agy_obj = json.loads((self.fake_home / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8"))
        self.assertIn("wiki-rd", agy_obj["mcpServers"])
        magpie_obj = json.loads((self.fake_home / ".config" / "magpie" / "library.json").read_text(encoding="utf-8"))
        magpie_names = [m["name"] for m in magpie_obj["mcp"]]
        self.assertIn("wiki-rd", magpie_names)
        self.assertNotIn("kb", magpie_names)
        self.assertEqual(magpie_obj["applied"]["agy"]["mcp"], ["wiki-rd", "magpie-memory"])
        self.assertEqual(magpie_obj["applied"]["dsh"]["mcp"], ["magpie-memory"])

        # Verify Codex preserved `[projects.*]` and `magpie`
        codex_text = (self.fake_home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('[projects."/srv/app"]', codex_text)
        self.assertIn("[mcp_servers.magpie]", codex_text)
        self.assertIn("[mcp_servers.wiki-rd]", codex_text)

        # Verify Claude Code preserved `magpie-memory` & `lastReleaseNotesSeen`
        claude_obj = json.loads((self.fake_home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude_obj["lastReleaseNotesSeen"], "2.1.0")
        self.assertIn("magpie-memory", claude_obj["mcpServers"])
        self.assertEqual(claude_obj["mcpServers"]["wiki-rd"]["type"], "stdio")

        # Verify WorkBuddy both stdio and streamable-http gateway modes
        wb_obj = json.loads((self.fake_home / ".workbuddy" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(wb_obj["mcpServers"]["wiki-rd"]["type"], "stdio")

        prev_wb_http = self._api("/api/clients/preview", {
            "client_id": "workbuddy",
            "mode": "http",
            "vault_ids": ["wiki-rd"],
            "gateway_url": self.base,
            "token": raw_token,
        })
        wb_http_obj = json.loads(prev_wb_http["snippet"])
        self.assertEqual(wb_http_obj["mcpServers"]["wiki-rd"]["type"], "streamable-http")
        self.assertIn("Bearer ", wb_http_obj["mcpServers"]["wiki-rd"]["headers"]["Authorization"])

        # 5. Layer-4 Memory & Errata CRUD + 1-hop Errata Pin verification
        mem = self._api("/api/memory/create", {
            "vault_id": "wiki-rd",
            "kind": "errata",
            "text": "2026Q4 修订：默认单租户限流阈值已上调至 5000 RPS（原 3500 RPS 作废）",
            "related_sources": ["史料/公开/网关限流规范.md"],
            "tags": ["限流", "勘误"],
        })
        nid = mem["note_id"]

        q_out = self._api("/api/vaults/query", {"vault_id": "wiki-rd", "question": "默认单租户限流阈值是多少？"})
        self.assertIn("5000 RPS", q_out["context_markdown"])
        self.assertIn("📌", q_out["context_markdown"])

        # Edit memory
        upd = self._api("/api/memory/update", {
            "vault_id": "wiki-rd",
            "note_id": nid,
            "text": "2026Q4 最终修订：默认单租户限流阈值调整为 6000 RPS",
        })
        self.assertIn("6000 RPS", upd["text"])

        # Archive (forget) -> no longer injected
        self._api("/api/memory/update", {"vault_id": "wiki-rd", "note_id": nid, "status": "archived"})
        q_after_forget = self._api("/api/vaults/query", {"vault_id": "wiki-rd", "question": "默认单租户限流阈值是多少？"})
        self.assertNotIn("6000 RPS", q_after_forget["context_markdown"])

        # Delete permanently
        del_res = self._api("/api/memory/delete", {"vault_id": "wiki-rd", "note_id": nid})
        self.assertTrue(del_res["deleted"])

        # 6. Rename vault ID (`wiki-rd` -> `wiki`) -> updates token `allowed_vaults` + syncs all 6 AI clients
        ren = self._api("/api/vaults/rename", {"old_id": "wiki-rd", "new_id": "wiki", "sync_clients": True})
        self.assertEqual(ren["new_id"], "wiki")
        self.assertEqual(len(ren["client_sync"]), 6)
        with urllib.request.urlopen(urllib.request.Request(f"{self.base}/v1/vaults", headers=gw_hdrs), timeout=5) as r_ren:
            self.assertEqual([v["id"] for v in json.loads(r_ren.read())["vaults"]], ["wiki"])
        wb_after_ren = json.loads((self.fake_home / ".workbuddy" / "mcp.json").read_text(encoding="utf-8"))
        self.assertIn("wiki", wb_after_ren["mcpServers"])
        self.assertNotIn("wiki-rd", wb_after_ren["mcpServers"])

    def test_build_backend_version_doctor_and_service_helpers(self):
        # 1. Build backend version matches package __version__
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root))
        try:
            import _build_backend

            self.assertEqual(_build_backend.VERSION, __version__)
            self.assertEqual(_build_backend.REQUIRES_PYTHON, ">=3.11")
            self.assertIn("Requires-Python: >=3.11\n", _build_backend._metadata_bytes().decode("utf-8"))
        finally:
            if str(repo_root) in sys.path:
                sys.path.remove(str(repo_root))

        # 2. Console /api/doctor endpoint & CLI doctor
        doc = self._api("/api/doctor")
        self.assertEqual(doc["version"], __version__)
        self.assertTrue(doc["python"]["ok"])
        py_check = next(c for c in doc["checks"] if c["id"] == "python")
        self.assertIn(">= 3.11", py_check["label"])
        self.assertTrue(any(c["id"] == "pdftotext" for c in doc["checks"]))
        self.assertIn("security_boundary", doc)

        cli_doc = run_doctor(config_dir=self.cfg_dir, client_home=self.fake_home)
        self.assertEqual(cli_doc["console"]["config_dir"], str(self.cfg_dir.resolve()))
        self.assertEqual(cli_main(["doctor", "--config-dir", str(self.cfg_dir), "--json"]), 0)

        # 3. Service definition rendering & security guardrails (launchd + systemd)
        with self.assertRaises(ValueError):
            render_service_definition("ui", host="0.0.0.0", port=8765, platform="darwin", home=self.fake_home)

        plist = render_service_definition(
            "gateway",
            host="127.0.0.1",
            port=8090,
            config_dir=self.cfg_dir,
            platform="darwin",
            home=self.fake_home,
        )
        self.assertEqual(plist["platform"], "launchd")
        self.assertIn("<string>io.wikicontext.gateway</string>", plist["content"])
        self.assertIn("--token-file", plist["content"])

        systemd = render_service_definition(
            "ui",
            host="127.0.0.1",
            port=8765,
            config_dir=self.cfg_dir,
            platform="linux",
            home=self.fake_home,
        )
        self.assertEqual(systemd["platform"], "systemd")
        self.assertIn("--no-browser", systemd["content"])

        inst = install_service(
            "gateway",
            config_dir=self.cfg_dir,
            platform="linux",
            home=self.fake_home,
            activate=False,
        )
        self.assertTrue(inst["installed"])
        self.assertTrue(Path(inst["service_path"]).is_file())
        uninst = uninstall_service("gateway", platform="linux", home=self.fake_home, activate=False)
        self.assertTrue(uninst["removed"])
        self.assertFalse(Path(inst["service_path"]).is_file())

        # 4. Console vault map loader for gateway reuse
        v_dir = self.root / "gw-reused-vault"
        v_dir.mkdir(parents=True)
        self._api("/api/vaults/onboard", {"path": str(v_dir), "vault_id": "wiki-gw", "layout": "en"})
        vmap, tok_file = load_console_vault_map(self.cfg_dir)
        self.assertIn("wiki-gw", vmap)
        self.assertEqual(tok_file, (self.cfg_dir / "tokens.json").resolve())

    def test_computer_a_to_computer_b_remote_connect_and_hybrid_auto_sync(self):
        # --- Computer A: Turn ON LAN sharing BEFORE creating any tokens (0 tokens -> 0 vaults exposed!) ---
        team_dir = self.root / "comp-a-team"
        (team_dir / "公开" / "运维手册").mkdir(parents=True)
        (team_dir / "机密").mkdir(parents=True)
        (team_dir / "公开" / "运维手册" / "服务器台账.md").write_text("# 服务器台账\n\n生产集群共 18 台 GPU 推理节点。\n", encoding="utf-8")
        (team_dir / "机密" / "薪酬预算.md").write_text("# 预算\n\n核心研发年终总包 900 万。\n", encoding="utf-8")

        self._api("/api/vaults/onboard", {"path": str(team_dir), "vault_id": "wiki-team", "name": "团队公共库", "layout": "zh"})

        # Verify 2-level graphical folder_tree extraction for visual ACL editor
        ov_a = self._api("/api/overview")
        team_v = next(v for v in ov_a["vaults"] if v["id"] == "wiki-team")
        tree_by_name = {n["name"]: n for n in team_v["folder_tree"]}
        self.assertIn("公开", tree_by_name)
        self.assertIn("机密", tree_by_name)
        self.assertEqual([c["prefix"] for c in tree_by_name["公开"]["children"]], ["史料/公开/运维手册/**"])

        # Start LAN gateway with ZERO tokens configured -> succeeds, exposes NO vaults to unauthenticated callers
        lan_status = self._api("/api/lan-gateway/start", {"host": "127.0.0.1", "port": 0})
        self.assertTrue(lan_status["running"])
        self.assertEqual(lan_status["active_token_count"], 0)
        comp_a_lan_url = f"127.0.0.1:{lan_status['port']}"  # Intentionally omit http:// to test normalization

        with urllib.request.urlopen(f"http://{comp_a_lan_url}/healthz", timeout=5) as r_unauth_hz:
            hz_unauth = json.loads(r_unauth_hz.read())
            self.assertEqual(hz_unauth["status"], "ok")
            self.assertEqual(hz_unauth["vaults"], {})

        # Now configure ACL & issue token on Computer A (hot-reloaded immediately without restarting LAN gateway)
        self._api("/api/acl/save", {"acl": {"teammate": {"allow": ["史料/公开/**"], "deny": ["史料/机密/**"]}}})
        tok_resp = self._api("/api/tokens/create", {
            "uid": "bob-macbook",
            "label": "B电脑同事令牌",
            "groups": ["teammate"],
            "allowed_vaults": ["wiki-team"],
        })
        bob_token = tok_resp["token"]

        # --- Computer B: Separate home & config_dir, has 1 local personal vault + connects to Computer A ---
        home_b = self.root / "fake-home-b"
        for sub_dir in (".dsh/profiles/desktop", ".codex", ".workbuddy", ".cursor", ".gemini/config"):
            (home_b / sub_dir).mkdir(parents=True, exist_ok=True)
        (home_b / ".claude.json").write_text("{}", encoding="utf-8")

        cfg_b = self.root / "wctx-home-b"
        state_b = ConsoleState(config_dir=cfg_b, client_home=home_b)
        srv_b = create_console_server(state_b, host="127.0.0.1", port=0)
        t_b = threading.Thread(target=srv_b.serve_forever, daemon=True)
        t_b.start()
        base_b = f"http://127.0.0.1:{srv_b.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base_b}/", timeout=5) as r_html:
                m_csrf = re.search(r'"csrfToken":\s*"([^"]+)"', r_html.read().decode("utf-8"))
                assert m_csrf is not None
                csrf_b = m_csrf.group(1)

            def api_b(path: str, payload: dict | None = None) -> dict:
                hdrs = {"X-WCTX-CSRF": csrf_b, "Accept": "application/json"}
                data = None
                method = "GET"
                if payload is not None:
                    method = "POST"
                    hdrs["Content-Type"] = "application/json"
                    data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(f"{base_b}{path}", data=data, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            # B also has its own local personal vault
            my_notes = self.root / "comp-b-personal"
            my_notes.mkdir(parents=True)
            (my_notes / "个人备忘.md").write_text("# 个人备忘\n\n周四下午牙医预约。\n", encoding="utf-8")
            api_b("/api/vaults/onboard", {"path": str(my_notes), "vault_id": "wiki-personal", "name": "个人笔记", "layout": "zh"})

            # B probes A's gateway with just address + token
            probed = api_b("/api/remotes/probe", {"gateway_url": comp_a_lan_url, "token": bob_token})
            self.assertEqual(probed["uid"], "bob-macbook")
            self.assertEqual([v["remote_vault_id"] for v in probed["vaults"]], ["wiki-team"])

            # B clicks "立即连接并自动配置" (sync_clients=True) — ZERO config snippet copy-paste!
            conn = api_b("/api/remotes/connect", {
                "gateway_url": comp_a_lan_url,
                "token": bob_token,
                "sync_clients": True,
            })
            self.assertEqual(len(conn["connected_vaults"]), 1)
            self.assertEqual(len(conn["client_sync"]), 6)

            # Overview on B shows both local + remote vaults, all 6 installed clients "synced", and never leaks raw token
            ov_b = api_b("/api/overview")
            ov_raw_str = json.dumps(ov_b, ensure_ascii=False)
            self.assertNotIn(bob_token, ov_raw_str)
            by_id = {v["id"]: v for v in ov_b["vaults"]}
            self.assertEqual(by_id["wiki-personal"]["kind"], "local")
            self.assertEqual(by_id["wiki-team"]["kind"], "remote")
            self.assertTrue(by_id["wiki-team"]["ready"])
            installed_clients = [c for c in ov_b["clients"] if c["installed"]]
            self.assertEqual(len(installed_clients), 6)
            for c in installed_clients:
                self.assertEqual(c["sync_status"], "synced", f"Client {c['id']} not synced: {c}")
                self.assertEqual(c["matched_count"], 2)

            # Verify hybrid configs written on Computer B:
            # WorkBuddy: wiki-personal -> stdio, wiki-team -> streamable-http with Bearer token
            wb_b = json.loads((home_b / ".workbuddy" / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(wb_b["mcpServers"]["wiki-personal"]["type"], "stdio")
            self.assertEqual(wb_b["mcpServers"]["wiki-team"]["type"], "streamable-http")
            self.assertEqual(wb_b["mcpServers"]["wiki-team"]["url"], f"http://127.0.0.1:{lan_status['port']}/mcp/wiki-team")

            # DSH: wiki-personal -> serve, wiki-team -> bridge with WCTX_TOKEN in env (not leaked in args)
            dsh_b = (home_b / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
            self.assertIn("serverName: 'wiki-personal'", dsh_b)
            self.assertIn("serverName: 'wiki-team'", dsh_b)
            self.assertIn("WCTX_TOKEN:", dsh_b)

            # B tests querying A's remote vault right inside B's UI playground (ACL enforced by A!)
            q_pub = api_b("/api/vaults/query", {"vault_id": "wiki-team", "question": "生产集群有多少台 GPU 推理节点？"})
            self.assertIn("18 台", q_pub["context_markdown"])
            q_sec = api_b("/api/vaults/query", {"vault_id": "wiki-team", "question": "核心研发年终总包是多少？"})
            self.assertNotIn("900 万", q_sec["context_markdown"])

            # Local gateway map on B only includes B's own local vault, never re-exporting A's remote vault
            vmap_b, _ = load_console_vault_map(cfg_b)
            self.assertEqual(list(vmap_b.keys()), ["wiki-personal"])

            # CLI `wctx remote list` works cleanly on B
            self.assertEqual(cli_main(["remote", "list", "--config-dir", str(cfg_b)]), 0)
        finally:
            srv_b.shutdown()
            srv_b.server_close()
            state_b.close()
            self._api("/api/lan-gateway/stop", {})

    def test_rename_vault_id_updates_tokens_and_clients(self) -> None:
        raw_dir = self.root / "研发规范"
        raw_dir.mkdir(parents=True)
        (raw_dir / "架构.md").write_text("# 架构\n\n采用无状态边缘网关。\n", encoding="utf-8")

        # Auto-slugify Chinese folder name -> defaults to "wiki"
        onb = self._api("/api/vaults/onboard", {"path": str(raw_dir), "name": "研发规范", "layout": "zh"})
        self.assertEqual(onb["vault_id"], "wiki")

        # Suppose a legacy vault had id "kb-legacy"; rename it to "wiki-eng" and verify token + client sync
        legacy_dir = self.root / "legacy"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "doc.md").write_text("# Legacy\n\nV1 spec.\n", encoding="utf-8")
        self._api("/api/vaults/onboard", {"path": str(legacy_dir), "vault_id": "kb-legacy", "name": "Legacy", "layout": "en"})
        self._api("/api/tokens/create", {"uid": "alice", "groups": ["all"], "allowed_vaults": ["kb-legacy"]})
        self._api("/api/clients/sync", {"client_ids": ["claude", "dsh"]})

        ren = self._api("/api/vaults/rename", {"old_id": "kb-legacy", "new_id": "wiki-eng", "sync_clients": True})
        self.assertEqual(ren["new_id"], "wiki-eng")

        tokens_after = self._api("/api/overview")["tokens"]
        alice_tok = next(t for t in tokens_after if t["uid"] == "alice")
        self.assertEqual(alice_tok["allowed_vaults"], ["wiki-eng"])

        claude_cfg = json.loads((self.fake_home / ".claude.json").read_text(encoding="utf-8"))
        self.assertIn("wiki-eng", claude_cfg["mcpServers"])
        self.assertNotIn("kb-legacy", claude_cfg["mcpServers"])

    def test_mainstream_agent_discovery_formats_and_disconnect(self) -> None:
        # 1. Seed diverse mainstream agent configs inside self.fake_home (including JSONC comments + trailing comma)
        if sys.platform == "darwin":
            app_sup = self.fake_home / "Library" / "Application Support"
        elif sys.platform.startswith("win"):
            app_sup = self.fake_home / "AppData" / "Roaming"
        else:
            app_sup = self.fake_home / ".config"

        # VS Code (`servers` root key + JSONC // comment + trailing comma)
        vsc_dir = app_sup / "Code" / "User"
        vsc_dir.mkdir(parents=True, exist_ok=True)
        (vsc_dir / "mcp.json").write_text(
            '{\n'
            '  // Existing MCP server with URL literal inside string\n'
            '  "servers": {\n'
            '    "github": { "type": "http", "url": "https://api.githubcopilot.com/mcp/" },\n'
            '  },\n'
            '}\n',
            encoding="utf-8",
        )
        # Windsurf (`serverUrl` for HTTP)
        ws_dir = self.fake_home / ".codeium" / "windsurf"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}),
            encoding="utf-8",
        )
        # OpenCode (`mcp` root key, list `command`, `type: local` / `remote`)
        oc_dir = self.fake_home / ".config" / "opencode"
        oc_dir.mkdir(parents=True, exist_ok=True)
        (oc_dir / "opencode.json").write_text(
            json.dumps({"mcp": {"pencil": {"type": "local", "command": ["npx", "pencil"], "enabled": True}}}),
            encoding="utf-8",
        )
        # Zed (`context_servers` root key + JSONC comment)
        zed_dir = (app_sup / "Zed") if sys.platform.startswith("win") else (self.fake_home / ".config" / "zed")
        zed_dir.mkdir(parents=True, exist_ok=True)
        (zed_dir / "settings.json").write_text(
            '{\n  // Zed user settings\n  "theme": "One Dark",\n  "context_servers": {\n    "postgres": { "command": "npx", "args": ["pg-mcp"] }\n  }\n}\n',
            encoding="utf-8",
        )
        # Gemini CLI (`~/.gemini/settings.json` with existing `httpUrl`), Qwen Code, Kimi CLI, Kiro, Claude Desktop, LM Studio
        (self.fake_home / ".gemini" / "settings.json").write_text(
            json.dumps({"general": {"vim": False}, "mcpServers": {"pencil": {"command": "pencil"}}}),
            encoding="utf-8",
        )
        (self.fake_home / ".qwen").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".qwen" / "settings.json").write_text("{}", encoding="utf-8")
        (self.fake_home / ".kimi-code").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".kimi-code" / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        (self.fake_home / ".kiro" / "settings").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".kiro" / "settings" / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        (app_sup / "Claude").mkdir(parents=True, exist_ok=True)
        (app_sup / "Claude" / "claude_desktop_config.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        (self.fake_home / ".lmstudio").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".lmstudio" / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

        # Skills-only decoy directories MUST NOT trigger false installation positives
        (self.fake_home / ".roo" / "skills").mkdir(parents=True, exist_ok=True)
        (self.fake_home / ".trae" / "skills").mkdir(parents=True, exist_ok=True)

        # 2. Prepare 1 vault and scan all supported mainstream agents
        docs_dir = self.root / "agent-scan-vault"
        docs_dir.mkdir(parents=True)
        (docs_dir / "spec.md").write_text("# 架构规范\n\n服务间通过 HTTPS 通信。\n", encoding="utf-8")
        self._api("/api/vaults/onboard", {"path": str(docs_dir), "vault_id": "wiki-core", "name": "核心规范", "layout": "zh"})

        scan = self._api("/api/clients/scan")
        self.assertGreaterEqual(scan["total_supported"], 19)
        by_id = {c["id"]: c for c in scan["clients"]}
        for expected_installed in ("vscode", "windsurf", "opencode", "zed", "gemini-cli", "qwen-code", "kimi", "kiro", "claude-desktop", "lmstudio"):
            self.assertTrue(by_id[expected_installed]["installed"], f"{expected_installed} should be detected")
        self.assertFalse(by_id["roo-code"]["installed"], "skills-only ~/.roo/skills must not trigger roo-code")
        self.assertFalse(by_id["trae"]["installed"], "skills-only ~/.trae/skills must not trigger trae")
        self.assertEqual(by_id["vscode"]["other_mcp_count"], 1)
        self.assertEqual(by_id["opencode"]["other_mcp_count"], 1)
        self.assertEqual(by_id["zed"]["other_mcp_count"], 1)

        # 3. Sync stdio configs into the diverse schemas & verify lossless preservation
        sync_targets = ["vscode", "windsurf", "opencode", "zed", "gemini-cli", "kimi", "lmstudio"]
        sync_res = self._api("/api/clients/sync", {
            "client_ids": sync_targets,
            "mode": "stdio",
            "vault_ids": ["wiki-core"],
        })
        self.assertEqual(len(sync_res["results"]), len(sync_targets))

        vsc_cfg = json.loads((vsc_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertIn("github", vsc_cfg["servers"])
        self.assertEqual(vsc_cfg["servers"]["wiki-core"]["type"], "stdio")

        oc_cfg = json.loads((oc_dir / "opencode.json").read_text(encoding="utf-8"))
        self.assertIn("pencil", oc_cfg["mcp"])
        self.assertEqual(oc_cfg["mcp"]["wiki-core"]["type"], "local")
        self.assertIsInstance(oc_cfg["mcp"]["wiki-core"]["command"], list)
        self.assertTrue(oc_cfg["mcp"]["wiki-core"]["enabled"])

        zed_cfg = json.loads((zed_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(zed_cfg["theme"], "One Dark")
        self.assertIn("postgres", zed_cfg["context_servers"])
        self.assertIn("wiki-core", zed_cfg["context_servers"])

        # 4. Verify schema-specific HTTP snippets via /api/clients/preview
        p_ws = self._api("/api/clients/preview", {
            "client_id": "windsurf",
            "mode": "http",
            "vault_ids": ["wiki-core"],
            "gateway_url": "https://kb.example.com",
            "token": "wctx_demo_tok",
        })
        self.assertEqual(json.loads(p_ws["snippet"])["mcpServers"]["wiki-core"]["serverUrl"], "https://kb.example.com/mcp/wiki-core")

        p_gem = self._api("/api/clients/preview", {
            "client_id": "gemini-cli",
            "mode": "http",
            "vault_ids": ["wiki-core"],
            "gateway_url": "https://kb.example.com",
            "token": "wctx_demo_tok",
        })
        self.assertEqual(json.loads(p_gem["snippet"])["mcpServers"]["wiki-core"]["httpUrl"], "https://kb.example.com/mcp/wiki-core")

        p_oc = self._api("/api/clients/preview", {
            "client_id": "opencode",
            "mode": "http",
            "vault_ids": ["wiki-core"],
            "gateway_url": "https://kb.example.com",
            "token": "wctx_demo_tok",
        })
        oc_http = json.loads(p_oc["snippet"])["mcp"]["wiki-core"]
        self.assertEqual(oc_http["type"], "remote")
        self.assertEqual(oc_http["url"], "https://kb.example.com/mcp/wiki-core")

        # 5. Re-scan must count matched wiki-core across synced diverse clients
        scan_after = self._api("/api/clients/scan")
        after_by_id = {c["id"]: c for c in scan_after["clients"]}
        for cid in sync_targets:
            self.assertEqual(after_by_id[cid]["sync_status"], "synced", f"{cid} not synced after write")
            self.assertEqual(after_by_id[cid]["matched_count"], 1)

        # 6. Disconnect opencode & vscode -> removes wiki-core, preserves unrelated MCPs AND preserves Magpie!
        disc = self._api("/api/clients/disconnect", {"client_ids": ["opencode", "vscode"]})
        self.assertEqual(len(disc["results"]), 2)
        oc_after_disc = json.loads((oc_dir / "opencode.json").read_text(encoding="utf-8"))
        self.assertIn("pencil", oc_after_disc["mcp"])
        self.assertNotIn("wiki-core", oc_after_disc["mcp"])
        vsc_after_disc = json.loads((vsc_dir / "mcp.json").read_text(encoding="utf-8"))
        self.assertIn("github", vsc_after_disc["servers"])
        self.assertNotIn("wiki-core", vsc_after_disc["servers"])
        magpie_after_disc = json.loads((self.fake_home / ".config" / "magpie" / "library.json").read_text(encoding="utf-8"))
        self.assertIn("wiki-core", [m["name"] for m in magpie_after_disc["mcp"]])


if __name__ == "__main__":
    unittest.main()
