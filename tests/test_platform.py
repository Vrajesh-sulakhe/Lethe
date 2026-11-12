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





