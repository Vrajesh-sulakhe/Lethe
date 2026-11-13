import sqlite3
import time
from pathlib import Path
import pytest

from lethe.models import MemoryNode, MemoryStatus, StorageTier, HandoverPacket
from lethe.storage.db import DatabaseManager
from lethe.storage.tiers import RelationalTier, SemanticTier
from lethe.core.bitemporal import BitemporalManager
from lethe.core.trust import TrustGateManager


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


def test_branch_isolation_across_bitemporal_trust_and_tiers(tmp_path):
    db = DatabaseManager(tmp_path / "iso.db")
    rel = RelationalTier(db)
    sem = SemanticTier(db)
    bi = BitemporalManager(db)
    tg = TrustGateManager(db)

    node_main = MemoryNode(
        content="Database is Postgres",
        category="stack",
        tier=StorageTier.RELATIONAL,
        branch="main",
        valid_from=1000.0,
        metadata={"entity": "db", "attribute": "engine", "value": "Postgres"},
    )
    sem.store(node_main)

    # Same entity.attribute on branch 'exp-sqlite' must NOT archive or conflict with 'main'
    node_exp = MemoryNode(
        content="Database is SQLite",
        category="stack",
        tier=StorageTier.RELATIONAL,
        branch="exp-sqlite",
        valid_from=1000.0,
        metadata={"entity": "db", "attribute": "engine", "value": "SQLite"},
    )
    tg_res = tg.process_input(node_exp)
    assert tg_res["allowed"] is True
    conflicts = bi.process_statement(node_exp)
    assert conflicts == []
    sem.store(node_exp)

    assert rel.lookup_attribute("db", "engine", branch="main").metadata["value"] == "Postgres"
    assert rel.lookup_attribute("db", "engine", branch="exp-sqlite").metadata["value"] == "SQLite"
    assert bi.query_active_attribute("db", "engine", branch="main").metadata["value"] == "Postgres"
    assert bi.query_active_attribute("db", "engine", branch="exp-sqlite").metadata["value"] == "SQLite"
    assert db.get_memory(node_main.id).status == MemoryStatus.ACTIVE
    sem_main = [m.id for m, _ in sem.search("SQLite", branch="main")]
    assert node_exp.id not in sem_main
    db.close()
