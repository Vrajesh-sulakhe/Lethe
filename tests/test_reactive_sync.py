import io
import json
from contextlib import redirect_stdout
import pytest

from lethe.core.engine import LetheMemory


def test_sync_delta_cursor_and_strict_token_budget(tmp_path):
    mem = LetheMemory(db_path=tmp_path / "sync.db", procedures_dir=tmp_path / "procs")
    for i in range(25):
        mem.store(f"Cross-tool decision #{i}: " + ("detail " * 20), category="arch")
    mem.export_handover("task-1", "Migrate auth service to JWT")

    delta1 = mem.get_sync_delta(client_id="claude", max_tokens=150)
    assert 0 < delta1.token_count <= 150
    assert "Lethe Sync" in delta1.formatted_text
    assert delta1.latest_audit_id > 0

    # Immediate second call with no new writes returns empty delta
    delta2 = mem.get_sync_delta(client_id="claude", max_tokens=150)
    assert delta2.formatted_text == ""
    assert delta2.events == []
    assert delta2.token_count == 0

    # Separate client ('cursor') retains its own independent cursor
    delta_cursor = mem.get_sync_delta(client_id="cursor", max_tokens=150)
    assert delta_cursor.formatted_text != ""
    assert delta_cursor.token_count <= 150

    # Peek mode must not advance a fresh client's cursor
    delta_peek = mem.get_sync_delta(client_id="codex", max_tokens=150, peek=True)
    assert delta_peek.formatted_text != ""
    delta_codex = mem.get_sync_delta(client_id="codex", max_tokens=150, peek=False)
    assert delta_codex.formatted_text != ""
    mem.close()


@pytest.mark.anyio
async def test_mcp_resources_tools_and_cross_process_subscription_bus(tmp_path):
    from lethe.mcp.server import create_mcp_server, poll_db_changes_once

    db_path = tmp_path / "mcp_sync.db"
    procs_dir = tmp_path / "procs"
    mem_a = LetheMemory(db_path=db_path, procedures_dir=procs_dir)
    server = create_mcp_server(db_path=str(db_path), procedures_dir=str(procs_dir))

    # Verify MCP resources are registered
    resources = await server.list_resources()
    resource_uris = {str(r.uri) for r in resources}
    assert {
        "lethe://sync/latest",
        "lethe://handovers/active",
        "lethe://branches",
        "lethe://conflicts",
    }.issubset(resource_uris)

    received_uris = []
    def on_event(event):
        if hasattr(event, "uri"):
            received_uris.append(str(event.uri))

    unsub = server._subscriptions.subscribe(on_event)
    try:
        baseline_cursor = {
            "last_data_version": mem_a.db.get_data_version(),
            "last_audit_id": mem_a.db.get_max_audit_id(),
        }

        # Process B (separate SQLite connection) writes a memory + handover
        mem_b = LetheMemory(db_path=db_path, procedures_dir=procs_dir)
        mem_b.store("Cursor changed API base path to /v2", category="decision")
        mem_b.export_handover("t-v2", "Roll out /v2 routes")
        mem_b.close()

        state = await poll_db_changes_once(server, mem_a, **baseline_cursor)
        assert state["changed"] is True
        assert "lethe://sync/latest" in received_uris
        assert "lethe://handovers/active" in received_uris

        # Immediate second poll with no changes must NOT emit duplicate notifications
        received_uris.clear()
        state2 = await poll_db_changes_once(server, mem_a, **state["cursor"])
        assert state2["changed"] is False
        assert received_uris == []

        # Verify Memory Git tools on the MCP server
        tools_list = {t.name for t in await server.list_tools()}
        assert {"lethe_branch", "lethe_commit", "lethe_rollback"}.issubset(tools_list)
        commit_res = await server.call_tool("lethe_commit", {"message": "mcp checkpoint"})
        assert "mcp checkpoint" in str(commit_res)
        branch_res = await server.call_tool("lethe_branch", {"action": "create", "name": "mcp-exp"})
        assert "mcp-exp" in str(branch_res)
    finally:
        unsub()
        mem_a.close()
        server._lethe_memory.close()


def test_cli_branch_rollback_and_hook_inject(tmp_path, monkeypatch):
    from lethe.cli import main as cli_main

    db_path = tmp_path / "cli_sync.db"
    procs_dir = tmp_path / "procs"
    base_args = ["--db", str(db_path), "--procedures-dir", str(procs_dir)]

    # Store, commit, create branch, and switch via CLI
    assert cli_main([*base_args, "store", "Initial CLI architecture choice"]) == 0
    assert cli_main([*base_args, "commit", "-m", "v1"]) == 0
    assert cli_main([*base_args, "branch", "create", "feat-hook", "--switch"]) == 0
    assert cli_main([*base_args, "branch", "list"]) == 0
    assert cli_main([*base_args, "store", "Temporary experiment fact"]) == 0
    assert cli_main([*base_args, "rollback", "HEAD~0"]) == 0

    # hook inject emits delta on first call, then empty on immediate repeat
    buf1 = io.StringIO()
    with redirect_stdout(buf1):
        rc = cli_main([*base_args, "hook", "inject", "--client", "claude", "--max-tokens", "200"])
    assert rc == 0
    assert "Lethe Sync" in buf1.getvalue()

    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        rc2 = cli_main([*base_args, "hook", "inject", "--client", "claude", "--max-tokens", "200"])
    assert rc2 == 0
    assert buf2.getvalue().strip() == ""


def test_install_client_hooks_idempotent_with_backup(tmp_path):
    from lethe.platform.setup import install_claude_hooks, install_cursor_sync_rule

    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text(json.dumps({"existingKey": True}), encoding="utf-8")

    res1 = install_claude_hooks(settings_path=claude_settings)
    assert res1["status"] == "configured"
    assert (tmp_path / ".claude" / "settings.json.bak").exists()
    data = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert data["existingKey"] is True
    assert "UserPromptSubmit" in data["hooks"]
    assert "SessionStart" in data["hooks"]

    # Second run is idempotent (no duplicate hook entries)
    res2 = install_claude_hooks(settings_path=claude_settings)
    assert res2["status"] == "already_configured"
    data2 = json.loads(claude_settings.read_text(encoding="utf-8"))
    assert len(data2["hooks"]["UserPromptSubmit"]) == 1
    assert len(data2["hooks"]["SessionStart"]) == 1

    # Cursor sync rule is idempotent too
    cursor_dir = tmp_path / ".cursor"
    cur1 = install_cursor_sync_rule(cursor_dir=cursor_dir)
    assert cur1["status"] == "configured"
    assert (cursor_dir / "rules" / "lethe-sync.mdc").exists()
    cur2 = install_cursor_sync_rule(cursor_dir=cursor_dir)
    assert cur2["status"] == "already_configured"
