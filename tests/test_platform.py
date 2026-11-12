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


