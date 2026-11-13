import sqlite3
import time
from pathlib import Path
import pytest

from lethe.models import MemoryNode, MemoryStatus, StorageTier, HandoverPacket
from lethe.storage.db import DatabaseManager


def test_schema_v4_migration_and_branch_defaults(tmp_path):
    db_path = tmp_path / "v4.db"
    db = DatabaseManager(db_path)
    assert db.get_schema_version() == 4
    assert db.get_active_branch() == "main"
    branches = db.list_branches()
    assert [b["name"] for b in branches] == ["main"]

    db.update_sync_cursor("claude", last_audit_id=42, branch="main")
    cursor = db.get_sync_cursor("claude")
    assert cursor["last_audit_id"] == 42
    assert cursor["branch"] == "main"

    # Verify default branch scoping on memories & handover packets
    n = MemoryNode(content="Baseline memory", category="arch")
    db.save_memory(n)
    fetched = db.get_memory(n.id)
    assert fetched is not None
    assert fetched.branch == "main"

    pkt = HandoverPacket(task_id="t1", task_goal="Ship auth", active_memory_ids=[n.id])
    db.save_handover_packet(pkt)
    latest_pkt = db.get_latest_handover_packet(branch="main")
    assert latest_pkt is not None
    assert latest_pkt.task_id == "t1"
    db.close()
