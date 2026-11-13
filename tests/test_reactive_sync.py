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
