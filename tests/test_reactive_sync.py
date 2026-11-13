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
