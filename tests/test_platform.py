import json
import tempfile
from pathlib import Path
import pytest

from lethe.platform.setup import (
    generate_mcp_config,
    detect_installed_tools,
    configure_tool,
    setup_all_tools,
)


def test_generate_mcp_config_structure():
    config = generate_mcp_config(
        python_path="/custom/path/python",
        db_path="/custom/path/lethe.db"
    )
    assert config["command"] == "/custom/path/python"
    assert config["args"] == ["-m", "lethe.cli", "mcp", "--db", "/custom/path/lethe.db"]
    assert config["env"]["PYTHONUNBUFFERED"] == "1"


def test_configure_tool_non_destructive_merge():
    with tempfile.TemporaryDirectory() as tmpdir:
        target_path = Path(tmpdir) / "subfolder" / "mcp.json"

        # Pre-seed target file with existing servers
        target_path.parent.mkdir(parents=True, exist_ok=True)
        initial_data = {
            "mcpServers": {
                "pre_existing_tool": {
                    "command": "node",
                    "args": ["server.js"]
                }
            }
        }
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(initial_data, f)

        # Configure Lethe into the same target
        result = configure_tool(
            tool_name="cursor",
            target_path=target_path,
            python_path="/bin/python3",
            db_path="/tmp/lethe.db",
            dry_run=False,
        )

        assert result["status"] == "configured"
        assert target_path.exists()

        # Verify that both pre-existing server and lethe exist
        with open(target_path, "r", encoding="utf-8") as f:
            updated_data = json.load(f)

        assert "pre_existing_tool" in updated_data["mcpServers"]
        assert "lethe" in updated_data["mcpServers"]
        assert updated_data["mcpServers"]["lethe"]["args"] == [
            "-m", "lethe.cli", "mcp", "--db", "/tmp/lethe.db"
        ]


def test_configure_tool_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        target_path = Path(tmpdir) / "test_mcp.json"

        result = configure_tool(
            tool_name="claude",
            target_path=target_path,
            python_path="/bin/python3",
            dry_run=True,
        )

        assert result["status"] == "dry_run"
        # In dry run, file must NOT be written to disk
        assert not target_path.exists()


def test_setup_all_tools_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = Path(tmpdir)
        (ws / ".cursor").mkdir()

        outcomes = setup_all_tools(
            tools=["cursor"],
            python_path="/bin/python3",
            db_path=str(ws / "custom.db"),
            workspace_dir=ws,
        )

        assert len(outcomes) == 1
        assert outcomes[0]["tool"] == "cursor"
        target_file = ws / ".cursor" / "mcp.json"
        assert target_file.exists()

        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "lethe" in data["mcpServers"]


def test_workspace_isolation():
    from lethe.core.engine import LetheMemory
    from lethe.models import StorageTier

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test_workspace.db"
        mem = LetheMemory(db_path=db_file)

        # 1. Store memory in Project Alpha
        node_alpha = mem.store(
            content="Alpha Service runs on port 8080 with PostgreSQL",
            category="architecture",
            tier=StorageTier.SEMANTIC,
            workspace_id="repo_alpha",
            scope="project"
        )

        # 2. Store memory in Project Beta
        node_beta = mem.store(
            content="Beta Service runs on port 9090 with MongoDB",
            category="architecture",
            tier=StorageTier.SEMANTIC,
            workspace_id="repo_beta",
            scope="project"
        )

        # 3. Store global memory
        node_global = mem.store(
            content="Engineer prefers TypeScript and Dark Mode across all repos",
            category="preference",
            tier=StorageTier.SEMANTIC,
            workspace_id="repo_alpha",
            scope="global"
        )

        # Query from Project Alpha perspective
        res_alpha = mem.recall("What port does the service run on?", workspace_id="repo_alpha")
        assert "8080" in res_alpha.context_text
        assert "9090" not in res_alpha.context_text  # Must not leak Beta's architecture!

        # Query from Project Beta perspective
        res_beta = mem.recall("What port does the service run on?", workspace_id="repo_beta")
        assert "9090" in res_beta.context_text
        assert "8080" not in res_beta.context_text  # Must not leak Alpha's architecture!

        # Both perspectives should recall the global preference
        res_pref = mem.recall("What is the preferred language or dark mode preference?", workspace_id="repo_beta")
        assert "Dark Mode" in res_pref.context_text

        mem.close()


def test_concurrent_multi_tool_access():
    """
    Simulates Cursor, Claude Code, and Antigravity executing concurrent
    reads and writes simultaneously on the same SQLite WAL database.
    """
    import concurrent.futures
    from lethe.core.engine import LetheMemory
    from lethe.models import StorageTier

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "concurrent.db"

        def simulate_agent_worker(agent_name: str, rounds: int = 10) -> int:
            worker_mem = LetheMemory(db_path=db_file)
            successful_ops = 0
            for i in range(rounds):
                # Write operation
                node = worker_mem.store(
                    content=f"Telemetry event {i} logged by agent {agent_name}",
                    category="telemetry",
                    tier=StorageTier.SEMANTIC,
                    session_id=f"session_{agent_name}_{i}"
                )
                assert node.id is not None
                successful_ops += 1

                # Read operation
                res = worker_mem.recall("Telemetry event logged", token_budget=300)
                assert res.tokens_used > 0
                successful_ops += 1

            worker_mem.close()
            return successful_ops

        agents = ["claude_code", "cursor_ide", "antigravity_agent", "codex_cli"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(simulate_agent_worker, name, 8) for name in agents]
            results = [f.result() for f in futures]

        # Verify all 4 agents completed all operations without lock errors
        assert len(results) == 4
        assert all(count == 16 for count in results)

        # Verify final record count in database
        verify_mem = LetheMemory(db_path=db_file)
        memories = verify_mem.db.list_memories()
        assert len(memories) == 32
        verify_mem.close()


def test_dashboard_server_endpoints():
    import threading
    import urllib.request
    from lethe.core.engine import LetheMemory
    from lethe.models import StorageTier
    from lethe.platform.dashboard.server import DashboardRequestHandler, socketserver, http

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "test_dash.db"
        mem = LetheMemory(db_path=db_file)
        mem.store("Architecture: API gateway on port 8000", category="infra", tier=StorageTier.SEMANTIC)
        mem.close()

        DashboardRequestHandler.db_path = str(db_file)

        # Start a test server on ephemeral port (port 0)
        server = socketserver.TCPServer(("127.0.0.1", 0), DashboardRequestHandler)
        port = server.server_address[1]

        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. Test HTML UI endpoint
            req = urllib.request.Request(f"{base_url}/")
            with urllib.request.urlopen(req) as resp:
                assert resp.status == 200
                html = resp.read().decode("utf-8")
                assert "LETHE" in html
                assert "Knapsack Memory Governor" in html

            # 2. Test /api/stats endpoint
            req = urllib.request.Request(f"{base_url}/api/stats")
            with urllib.request.urlopen(req) as resp:
                assert resp.status == 200
                stats = json.loads(resp.read().decode("utf-8"))
                assert stats["total_records"] == 1
                assert stats["status_counts"]["active"] == 1
                assert "delivered_slice_tokens" in stats

            # 3. Test /api/memories endpoint
            req = urllib.request.Request(f"{base_url}/api/memories")
            with urllib.request.urlopen(req) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["count"] == 1
                assert data["memories"][0]["category"] == "infra"

            # 4. Test /api/lineage endpoint
            req = urllib.request.Request(f"{base_url}/api/lineage")
            with urllib.request.urlopen(req) as resp:
                assert resp.status == 200
                lineage = json.loads(resp.read().decode("utf-8"))
                assert "edges" in lineage
        finally:
            server.shutdown()
            server.server_close()


def test_configure_tool_codex_toml_merge_and_detection():
    import tomllib
    from lethe.platform.setup import _is_lethe_configured, get_known_tool_targets

    targets = get_known_tool_targets()
    assert "codex" in targets
    assert targets["codex"][0].name == "config.toml"
    assert targets["antigravity"][0].name == "mcp_config.json"

    with tempfile.TemporaryDirectory() as tmpdir:
        config_toml = Path(tmpdir) / "config.toml"
        config_toml.write_text(
            '# Codex settings\nmodel = "o3"\n\n'
            '[mcp_servers.other_tool]\ncommand = "node"\nargs = ["other.js"]\n\n'
            '[mcp_servers.lethe]\ncommand = "/old/python"\nargs = ["old"]\n\n'
            '[mcp_servers.lethe.env]\nFOO = "bar"\n\n'
            '[hooks.state]\nenabled = true\n',
            encoding="utf-8",
        )

        res = configure_tool(
            tool_name="codex",
            target_path=config_toml,
            python_path="/custom/venv/bin/python",
            db_path="/custom/shared/lethe.db",
            procedures_dir="/custom/shared/procedures",
            dry_run=False,
        )
        assert res["status"] == "configured"
        assert _is_lethe_configured(config_toml) is True

        parsed = tomllib.loads(config_toml.read_text(encoding="utf-8"))
        assert parsed["model"] == "o3"
        assert parsed["hooks"]["state"]["enabled"] is True
        assert parsed["mcp_servers"]["other_tool"]["command"] == "node"
        assert parsed["mcp_servers"]["lethe"]["command"] == "/custom/venv/bin/python"
        assert parsed["mcp_servers"]["lethe"]["args"] == [
            "-m", "lethe.cli", "mcp",
            "--db", "/custom/shared/lethe.db",
            "--procedures-dir", "/custom/shared/procedures",
        ]
        assert parsed["mcp_servers"]["lethe"]["env"]["PYTHONUNBUFFERED"] == "1"


def test_default_python_preserves_venv_symlink():
    import os
    import sys
    from lethe.platform.setup import get_default_python_executable

    py = get_default_python_executable()
    assert py == os.path.abspath(sys.executable)


def test_mcp_stdio_cross_process_roundtrip():
    import asyncio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _run():
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = generate_mcp_config(
                db_path=str(Path(tmpdir) / "shared.db"),
                procedures_dir=str(Path(tmpdir) / "procedures"),
            )
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg["args"],
                env=cfg["env"],
            )

            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    assert {"lethe_store", "lethe_recall", "lethe_handover_export", "lethe_handover_import", "lethe_forget"} <= names

                    res_store = await session.call_tool("lethe_store", {
                        "content": "Auth service uses port 9090 with Redis caching.",
                        "category": "architecture",
                        "workspace_id": "mcp_roundtrip_ws",
                    })
                    stored = json.loads(res_store.content[0].text)
                    assert stored["status"] == "active"

                    await session.call_tool("lethe_handover_export", {
                        "task_id": "stdio_handover",
                        "task_goal": "Continue auth refactor in second client",
                        "active_memory_ids": [stored["memory_id"]],
                        "epistemic_state": {"step": "redis_wired"},
                    })

            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    ho = await session.call_tool("lethe_handover_import", {"task_id": "stdio_handover"})
                    ho_data = json.loads(ho.content[0].text)
                    assert ho_data["task_goal"] == "Continue auth refactor in second client"

                    rec = await session.call_tool("lethe_recall", {
                        "query": "What port does auth service use?",
                        "workspace_id": "mcp_roundtrip_ws",
                        "token_budget": 200,
                    })
                    rec_data = json.loads(rec.content[0].text)
                    assert "9090" in rec_data["context_text"]

    asyncio.run(_run())


def test_mcp_4_concurrent_clients_stress_test():
    """
    Spawns 4 independent concurrent MCP server subprocesses over stdio
    (mocking Cursor, Claude Code, Antigravity, and Codex) all sharing the
    same SQLite WAL database under concurrent read/write/handover load.
    """
    import asyncio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _client_worker(client_name: str, params: StdioServerParameters, iterations: int = 5):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 1. Verify server health via lethe_health tool
                h_res = await session.call_tool("lethe_health", {})
                h_data = json.loads(h_res.content[0].text)
                assert h_data["healthy"] is True
                assert h_data["journal_mode"] == "wal"

                stored_ids = []
                for idx in range(iterations):
                    s_res = await session.call_tool(
                        "lethe_store",
                        {
                            "content": f"Service node from {client_name} iteration {idx} listening on port {8100 + idx}",
                            "category": "stress_test",
                            "tier": "semantic",
                            "source": "tool_verified",
                            "session_id": f"{client_name}_sess_{idx}",
                            "workspace_id": "stress_ws",
                        },
                    )
                    s_data = json.loads(s_res.content[0].text)
                    assert s_data["status"] == "active"
                    stored_ids.append(s_data["memory_id"])

                    r_res = await session.call_tool(
                        "lethe_recall",
                        {
                            "query": f"Service node from {client_name} listening on port",
                            "workspace_id": "stress_ws",
                            "token_budget": 250,
                        },
                    )
                    r_data = json.loads(r_res.content[0].text)
                    assert r_data["tokens_used"] <= 250

                # Export a client-specific handover packet
                ho_res = await session.call_tool(
                    "lethe_handover_export",
                    {
                        "task_id": f"task_{client_name}",
                        "task_goal": f"Complete {client_name} stress verification",
                        "active_memory_ids": stored_ids,
                        "epistemic_state": {"client": client_name, "completed": iterations},
                    },
                )
                ho_data = json.loads(ho_res.content[0].text)
                assert ho_data["status"] == "HANDOVER_PACKET_CREATED"
                return len(stored_ids)

    async def _run_all():
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_db = str(Path(tmpdir) / "stress_shared.db")
            shared_procs = str(Path(tmpdir) / "procedures")
            cfg = generate_mcp_config(
                db_path=shared_db,
                procedures_dir=shared_procs,
                use_wrapper=True,
            )
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg["args"],
                env=cfg["env"],
            )

            clients = ["cursor", "claude_code", "antigravity", "codex"]
            counts = await asyncio.gather(
                *[_client_worker(name, params, iterations=5) for name in clients]
            )
            assert counts == [5, 5, 5, 5]

            # Final verification of database integrity and total records
            from lethe.core.engine import LetheMemory
            verify_mem = LetheMemory(db_path=shared_db, procedures_dir=shared_procs)
            try:
                health = verify_mem.check_health()
                assert health["healthy"] is True
                assert health["integrity"] == "ok"
                assert health["schema_version"] >= 2
                assert health["total_memories"] == 20
                assert health["handover_packets"] == 4
            finally:
                verify_mem.close()

    asyncio.run(_run_all())


def test_mcp_custom_http_health_and_stats_routes():
    """Verifies the MCP server's /health and /stats Starlette HTTP routes."""
    import asyncio
    from starlette.requests import Request
    from lethe.mcp.server import create_mcp_server

    async def _check_routes():
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "http_routes.db"
            proc_dir = Path(tmpdir) / "procedures"
            server = create_mcp_server(db_path=db_path, procedures_dir=proc_dir)

            routes = {r.path: r.endpoint for r in server._custom_starlette_routes}
            assert "/health" in routes
            assert "/stats" in routes

            scope_health = {"type": "http", "method": "GET", "path": "/health", "query_string": b"", "headers": []}
            resp_health = await routes["/health"](Request(scope_health))
            assert resp_health.status_code == 200
            payload_health = json.loads(resp_health.body.decode("utf-8"))
            assert payload_health["healthy"] is True
            assert payload_health["schema_version"] >= 2

            scope_stats = {"type": "http", "method": "GET", "path": "/stats", "query_string": b"", "headers": []}
            resp_stats = await routes["/stats"](Request(scope_stats))
            assert resp_stats.status_code == 200
            payload_stats = json.loads(resp_stats.body.decode("utf-8"))
            assert "health" in payload_stats
            assert payload_stats["health"]["status"] == "ok"

    asyncio.run(_check_routes())


def test_universal_default_db_and_procedures_paths(monkeypatch):
    import os
    from lethe.platform.setup import get_default_db_path, get_default_procedures_dir

    monkeypatch.delenv("LETHE_DB", raising=False)
    monkeypatch.delenv("LETHE_PROCEDURES_DIR", raising=False)

    expected_db = os.path.abspath(str(Path.home() / ".lethe" / "lethe.db"))
    expected_procs = os.path.abspath(str(Path.home() / ".lethe" / "procedures"))
    assert get_default_db_path() == expected_db
    assert get_default_procedures_dir() == expected_procs

    monkeypatch.setenv("LETHE_DB", "/tmp/env_lethe.db")
    monkeypatch.setenv("LETHE_PROCEDURES_DIR", "/tmp/env_procs")
    assert get_default_db_path() == "/tmp/env_lethe.db"
    assert get_default_procedures_dir() == "/tmp/env_procs"


def test_generate_mcp_config_prefers_global_lethe_cli():
    cfg = generate_mcp_config(
        db_path="/tmp/shared.db",
        procedures_dir="/tmp/procs",
        lethe_cmd="/usr/local/bin/lethe",
    )
    assert cfg["command"] == "/usr/local/bin/lethe"
    assert cfg["args"] == [
        "mcp",
        "--db", "/tmp/shared.db",
        "--procedures-dir", "/tmp/procs",
    ]


def test_cli_creds_setup_print_and_error_handling():
    import subprocess
    import sys

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = str(Path(tmpdir) / "cli_creds.db")
        procs_dir = str(Path(tmpdir) / "procedures")

        # 1. `lethe creds --json`
        r_creds = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "--db", db_file, "--procedures-dir", procs_dir, "creds", "--json"],
            capture_output=True, text=True, check=True,
        )
        creds = json.loads(r_creds.stdout)
        assert creds["db_path"] == db_file
        assert creds["procedures_dir"] == procs_dir
        assert "mcpServers" in json.loads(creds["json_snippet"])
        assert "[mcp_servers.lethe]" in creds["toml_snippet"]
        assert "claude mcp add -s user lethe --" in creds["claude_cli"]
        assert "codex mcp add lethe --" in creds["codex_cli"]

        # 2. `lethe setup --print`
        r_print = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "--db", db_file, "--procedures-dir", procs_dir, "setup", "--print"],
            capture_output=True, text=True, check=True,
        )
        assert "LETHE MCP CONNECTION CREDENTIALS" in r_print.stdout
        assert db_file in r_print.stdout

        # 3. `lethe mcp --check-only`
        r_chk = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "mcp", "--db", db_file, "--procedures-dir", procs_dir, "--check-only"],
            capture_output=True, text=True, check=True,
        )
        assert json.loads(r_chk.stdout)["healthy"] is True

        # 4. Partial relational args in `store` fail cleanly
        r_partial = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "--db", db_file, "store", "Partial fact", "--entity", "user"],
            capture_output=True, text=True,
        )
        assert r_partial.returncode != 0
        assert "must all be provided together" in r_partial.stderr

        # 5. Missing `--file` in `procedures write` fails cleanly without raw traceback
        r_missing_file = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "--db", db_file, "procedures", "write", "x.md", "--file", "/nonexistent/nope.md"],
            capture_output=True, text=True,
        )
        assert r_missing_file.returncode != 0
        assert "Traceback" not in r_missing_file.stderr
        assert "Error reading" in r_missing_file.stderr


def test_windows_executable_resolution_and_quoting(monkeypatch):
    import lethe.platform.setup as setup_mod

    monkeypatch.setattr(setup_mod.sys, "platform", "win32")

    # 1. Must never choose cwd lethe.cmd or extensionless shim when use_wrapper=False
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: r"C:\repo\lethe.cmd")
    monkeypatch.setattr(setup_mod.sys, "executable", r"C:\nonexistent\python.exe")
    assert setup_mod.get_default_lethe_executable() is None

    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: r"C:\Users\Ajit\.local\bin\lethe")
    assert setup_mod.get_default_lethe_executable() is None

    # 2. Accepts real .exe on PATH and formats double-quoted Windows command line
    monkeypatch.setattr(
        setup_mod.shutil,
        "which",
        lambda name: r"C:\Users\Ajit\.local\bin\lethe.exe" if name == "lethe.exe" else None,
    )
    assert setup_mod.get_default_lethe_executable().endswith("lethe.exe")

    joined = setup_mod._shell_join([r"C:\Program Files\lethe.exe", "mcp", "--db", r"C:\Users\My Name\.lethe\lethe.db"])
    assert '"' in joined
    assert not joined.startswith("'")


def test_cli_handles_cp437_windows_console_encoding():
    import os
    import subprocess
    import sys

    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = str(Path(tmpdir) / "win_cp437.db")
        procs_dir = str(Path(tmpdir) / "procedures")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp437"

        base = [sys.executable, "-m", "lethe.cli", "--db", db_file, "--procedures-dir", procs_dir]
        for cmd_args in [
            ["store", "Unicode memory: Café — ✓ OK"],
            ["inspect"],
            ["health"],
            ["forget", "Unicode memory"],
            ["creds"],
        ]:
            res = subprocess.run(base + cmd_args, capture_output=True, text=True, env=env)
            assert res.returncode == 0, f"Command {cmd_args} failed under cp437: {res.stderr}"
            assert "UnicodeEncodeError" not in res.stderr


def test_install_global_cli_windows_and_posix(monkeypatch):
    import lethe.platform.setup as setup_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        win_bin = Path(tmpdir) / "win_bin"
        posix_bin = Path(tmpdir) / "posix_bin"

        # 1. Windows mode: generates .cmd + Git Bash shims and calls PATH updater
        monkeypatch.setattr(setup_mod.sys, "platform", "win32")
        monkeypatch.setattr(setup_mod, "_ensure_windows_user_path", lambda target: True)
        res_win = setup_mod.install_global_cli(bin_dir=win_bin, update_path=True, dry_run=False)
        assert res_win["path_updated"] is True
        assert (win_bin / "lethe.cmd").exists()
        assert (win_bin / "lethe-mcp.cmd").exists()
        assert (win_bin / "lethe").exists()
        assert (win_bin / "lethe-mcp").exists()
        cmd_text = (win_bin / "lethe.cmd").read_text(encoding="utf-8")
        assert "PYTHONIOENCODING=utf-8" in cmd_text
        assert "%*" in cmd_text

        # PATH merge idempotency (case-insensitive on Windows)
        merged, changed = setup_mod._merge_path_entries(r"C:\Windows\System32", win_bin, sep=";")
        assert changed is True
        merged2, changed2 = setup_mod._merge_path_entries(merged.upper(), win_bin, sep=";")
        assert changed2 is False

        # 2. POSIX mode: installs executable symlinks/wrappers
        monkeypatch.setattr(setup_mod.sys, "platform", "darwin")
        res_mac = setup_mod.install_global_cli(bin_dir=posix_bin, dry_run=False)
        assert len(res_mac["installed"]) == 2
        assert (posix_bin / "lethe").exists()
        assert (posix_bin / "lethe-mcp").exists()


def test_cli_bare_menu_and_install_cli_dry_run():
    import subprocess
    import sys

    # Bare `python -m lethe.cli` prints the command menu
    r_menu = subprocess.run([sys.executable, "-m", "lethe.cli"], capture_output=True, text=True, check=True)
    for sub in ("mcp", "creds", "health", "store", "recall", "setup", "ui", "install-cli"):
        assert sub in r_menu.stdout

    # Bare `./lethe.sh` also prints the menu instead of hanging in MCP stdio
    repo_root = Path(__file__).resolve().parents[1]
    r_sh = subprocess.run([str(repo_root / "lethe.sh")], capture_output=True, text=True, timeout=5, check=True)
    assert "Available commands" in r_sh.stdout
    assert "install-cli" in r_sh.stdout

    # `install-cli --dry-run --json` returns clean plan without touching disk
    with tempfile.TemporaryDirectory() as tmpdir:
        target_bin = Path(tmpdir) / "nonexistent_bin"
        r_inst = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "install-cli", "--bin-dir", str(target_bin), "--dry-run", "--json"],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(r_inst.stdout)
        assert payload["dry_run"] is True
        assert {item["command"] for item in payload["installed"]} == {"lethe", "lethe-mcp"}
        assert not target_bin.exists()


def test_run_mcp_server_tty_notice_only_on_interactive_stdin(monkeypatch, capsys):
    import lethe.mcp.server as mcp_server_mod

    class _DummyServer:
        def run(self, **kwargs):
            pass

    monkeypatch.setattr(mcp_server_mod, "create_mcp_server", lambda db_path, procedures_dir: _DummyServer())

    # Pipe mode (MCP client): silent stdout and stderr
    monkeypatch.setattr(mcp_server_mod.sys.stdin, "isatty", lambda: False)
    mcp_server_mod.run_mcp_server()
    captured_pipe = capsys.readouterr()
    assert captured_pipe.out == ""
    assert captured_pipe.err == ""

    # Interactive terminal mode (human): helpful stderr notice, silent stdout
    monkeypatch.setattr(mcp_server_mod.sys.stdin, "isatty", lambda: True)
    mcp_server_mod.run_mcp_server()
    captured_tty = capsys.readouterr()
    assert captured_tty.out == ""
    assert "MCP server listening on stdio" in captured_tty.err


def test_desktop_tools_detection_and_configuration(monkeypatch):
    import subprocess
    import sys
    import tomllib
    import lethe.platform.setup as setup_mod
    from lethe.platform.setup import (
        _is_lethe_configured,
        build_mcp_credentials,
        detect_installed_tools,
        get_known_tool_targets,
        setup_all_tools,
    )

    targets = get_known_tool_targets()
    assert "claude-desktop" in targets
    assert any("claude_desktop_config.json" in str(p) for p in targets["claude-desktop"])
    assert any("claude_desktop_config.json" in str(p) for p in targets["claude"])
    assert "gemini-desktop" in targets
    assert "gemini" in targets
    assert any("gemini_desktop_config.json" in str(p) for p in targets["gemini-desktop"])
    assert any("settings.json" in str(p) for p in targets["gemini"])
    assert "openai-desktop" in targets
    assert "chatgpt" in targets
    assert "openai" in targets
    assert any("ChatGPT" in str(p) and p.name == "mcp.json" for p in targets["openai-desktop"])
    assert any("config.toml" in str(p) for p in targets["openai-desktop"])

    # Credentials payload includes desktop paths and CLI commands
    creds = build_mcp_credentials(db_path="/tmp/lethe.db", procedures_dir="/tmp/procs")
    assert "claude_desktop" in creds["desktop_config_paths"]
    assert "gemini_desktop" in creds["desktop_config_paths"]
    assert "openai_desktop" in creds["desktop_config_paths"]
    assert "gemini mcp add lethe" in creds["gemini_cli"]

    # Default detect_installed_tools omits duplicate aliases while keeping canonical desktop tools
    detected_default = detect_installed_tools()
    assert "claude-desktop" in detected_default
    assert "gemini-desktop" in detected_default
    assert "openai-desktop" in detected_default
    assert "chatgpt" not in detected_default
    assert "gemini" not in detected_default

    # 1. macOS Desktop App Support paths (Claude Desktop, Gemini Desktop, OpenAI/ChatGPT Desktop JSON & TOML)
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_home = Path(tmpdir)
        ws = tmp_home / "project"
        ws.mkdir()

        # Claude Desktop (macOS Application Support)
        cd_path = tmp_home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        cd_path.parent.mkdir(parents=True, exist_ok=True)
        cd_path.write_text('{"mcpServers": {"other": {"command": "node"}}}', encoding="utf-8")

        res_cd = setup_all_tools(
            tools=["claude-desktop"],
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=tmp_home,
        )
        assert len(res_cd) == 1
        assert Path(res_cd[0]["path"]) == cd_path.resolve()
        assert _is_lethe_configured(cd_path) is True
        cd_data = json.loads(cd_path.read_text(encoding="utf-8"))
        assert "lethe" in cd_data["mcpServers"]
        assert "other" in cd_data["mcpServers"]

        # Gemini Desktop (macOS Application Support JSON)
        gem_app_path = tmp_home / "Library" / "Application Support" / "Gemini" / "gemini_desktop_config.json"
        gem_app_path.parent.mkdir(parents=True, exist_ok=True)
        gem_app_path.write_text('{"mcpServers": {"g_tool": {"command": "python"}}}', encoding="utf-8")

        res_gem_app = setup_all_tools(
            tools=["gemini-desktop"],
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=tmp_home,
        )
        assert len(res_gem_app) == 1
        assert Path(res_gem_app[0]["path"]) == gem_app_path.resolve()
        assert _is_lethe_configured(gem_app_path) is True
        gem_app_data = json.loads(gem_app_path.read_text(encoding="utf-8"))
        assert "lethe" in gem_app_data["mcpServers"]
        assert "g_tool" in gem_app_data["mcpServers"]

        # OpenAI / ChatGPT Desktop JSON config (macOS Application Support/ChatGPT/mcp.json)
        oai_json_path = tmp_home / "Library" / "Application Support" / "ChatGPT" / "mcp.json"
        oai_json_path.parent.mkdir(parents=True, exist_ok=True)
        oai_json_path.write_text('{"mcpServers": {"existing_oai": {"command": "uvx"}}}', encoding="utf-8")

        res_oai_json = setup_all_tools(
            tools=["openai-desktop"],
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=tmp_home,
        )
        assert len(res_oai_json) == 1
        assert Path(res_oai_json[0]["path"]) == oai_json_path.resolve()
        assert _is_lethe_configured(oai_json_path) is True
        oai_data = json.loads(oai_json_path.read_text(encoding="utf-8"))
        assert "lethe" in oai_data["mcpServers"]
        assert "existing_oai" in oai_data["mcpServers"]

        # Full `setup_all_tools(tools=None)` deduplicates identical paths
        all_outcomes = setup_all_tools(
            tools=None,
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=tmp_home,
        )
        written_paths = [o["path"] for o in all_outcomes]
        assert len(written_paths) == len(set(written_paths))

    # 2. Fallback resolution when ChatGPT/Gemini use ~/.codex/config.toml and ~/.gemini/settings.json
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_home = Path(tmpdir)
        ws = tmp_home / "project"
        ws.mkdir()

        gem_path = tmp_home / ".gemini" / "settings.json"
        gem_path.parent.mkdir(parents=True, exist_ok=True)
        gem_path.write_text('{"mcpServers": {}}', encoding="utf-8")

        res_gem = setup_all_tools(
            tools=["gemini"],
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=tmp_home,
        )
        assert len(res_gem) == 1
        assert _is_lethe_configured(gem_path) is True

        gpt_path = tmp_home / ".codex" / "config.toml"
        gpt_path.parent.mkdir(parents=True, exist_ok=True)
        gpt_path.write_text('model = "o3"\n', encoding="utf-8")

        res_gpt = setup_all_tools(
            tools=["chatgpt"],
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=tmp_home,
        )
        assert len(res_gpt) == 1
        assert _is_lethe_configured(gpt_path) is True
        gpt_data = tomllib.loads(gpt_path.read_text(encoding="utf-8"))
        assert "lethe" in gpt_data["mcp_servers"]

    # 3. Windows MSIX & Roaming paths with isolated home_dir
    monkeypatch.setattr(setup_mod.sys, "platform", "win32")
    with tempfile.TemporaryDirectory() as tmpdir:
        win_home = Path(tmpdir)
        ws = win_home / "repo"
        ws.mkdir()

        msix_claude = (
            win_home
            / "AppData"
            / "Local"
            / "Packages"
            / "Claude_pzs8sxrjxfjjc"
            / "LocalCache"
            / "Roaming"
            / "Claude"
            / "claude_desktop_config.json"
        )
        msix_claude.parent.mkdir(parents=True, exist_ok=True)
        msix_claude.write_text('{"mcpServers": {}}', encoding="utf-8")

        win_gem = win_home / "AppData" / "Roaming" / "Gemini" / "gemini_desktop_config.json"
        win_gem.parent.mkdir(parents=True, exist_ok=True)
        win_gem.write_text('{"mcpServers": {}}', encoding="utf-8")

        win_oai = win_home / "AppData" / "Roaming" / "ChatGPT" / "mcp.json"
        win_oai.parent.mkdir(parents=True, exist_ok=True)
        win_oai.write_text('{"mcpServers": {}}', encoding="utf-8")

        res_win = setup_all_tools(
            tools=["claude-desktop", "gemini-desktop", "openai-desktop"],
            python_path=r"C:\Python312\python.exe",
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=win_home,
        )
        assert len(res_win) == 3
        assert _is_lethe_configured(msix_claude) is True
        assert _is_lethe_configured(win_gem) is True
        assert _is_lethe_configured(win_oai) is True

    # 4. Linux XDG_CONFIG_HOME / ~/.config paths
    monkeypatch.setattr(setup_mod.sys, "platform", "linux")
    with tempfile.TemporaryDirectory() as tmpdir:
        lin_home = Path(tmpdir)
        ws = lin_home / "repo"
        ws.mkdir()

        lin_cd = lin_home / ".config" / "Claude" / "claude_desktop_config.json"
        lin_gem = lin_home / ".config" / "Gemini" / "gemini_desktop_config.json"
        lin_oai = lin_home / ".config" / "ChatGPT" / "mcp.json"

        res_lin = setup_all_tools(
            tools=["claude-desktop", "gemini-desktop", "openai-desktop"],
            python_path="/usr/bin/python3",
            db_path=str(ws / "lethe.db"),
            workspace_dir=ws,
            home_dir=lin_home,
        )
        assert len(res_lin) == 3
        assert _is_lethe_configured(lin_cd) is True
        assert _is_lethe_configured(lin_gem) is True
        assert _is_lethe_configured(lin_oai) is True

    # 5. CLI `setup --check` lists all desktop targets cleanly
    r_check = subprocess.run(
        [sys.executable, "-m", "lethe.cli", "setup", "--check"],
        capture_output=True,
        text=True,
        check=True,
    )
    for desktop_key in ("claude-desktop", "gemini-desktop", "openai-desktop"):
        assert desktop_key in r_check.stdout





