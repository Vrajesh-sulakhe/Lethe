import sqlite3
import time
from pathlib import Path
import pytest

from lethe.models import MemoryNode, MemoryStatus, StorageTier, HandoverPacket
from lethe.storage.db import DatabaseManager
from lethe.storage.tiers import RelationalTier, SemanticTier
from lethe.core.bitemporal import BitemporalManager
from lethe.core.trust import TrustGateManager
from lethe.core.engine import LetheMemory


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


def test_memory_git_branch_commit_merge_and_tombstone_invariant(tmp_path):
    mem = LetheMemory(db_path=tmp_path / "git.db", procedures_dir=tmp_path / "procs")
    mem.store(
        "User uses Python 3.12",
        category="stack",
        tier=StorageTier.RELATIONAL,
        valid_from=1000.0,
        metadata={"entity": "user", "attribute": "python", "value": "3.12"},
    )
    c1 = mem.commit("baseline stack")
    assert c1.branch == "main"

    mem.create_branch("try-py313", switch=True)
    assert mem.current_branch == "try-py313"
    mem.store(
        "User uses Python 3.13",
        category="stack",
        tier=StorageTier.RELATIONAL,
        valid_from=2000.0,
        metadata={"entity": "user", "attribute": "python", "value": "3.13"},
    )
    c2 = mem.commit("upgrade python")

    # Diff detects change between branches
    diff = mem.diff_branches("try-py313", "main")
    assert len(diff["added_or_modified"]) >= 1

    # Unlearning before rollback MUST NOT be resurrected by rollback!
    secret = mem.store("Secret key sk-live-999", category="secret")
    mem.commit("recorded secret temporarily")
    mem.forget(secret.id, reason="pii scrub")
    mem.rollback(c2.id)  # rollback to before secret was committed
    purged = mem.db.get_memory(secret.id)
    assert purged is not None
    assert purged.status == MemoryStatus.TOMBSTONED
    assert purged.content == "[PURGED_VIA_UNLEARNING]"
    assert mem.db.get_embedding(secret.id) is None

    # Switch back to main & store conflicting update on main, then test merge conflict detection
    mem.switch_branch("main")
    mem.store(
        "User uses Python 3.11",
        category="stack",
        tier=StorageTier.RELATIONAL,
        valid_from=2000.0,
        metadata={"entity": "user", "attribute": "python", "value": "3.11"},
    )
    merge_res = mem.merge_branch("try-py313", strategy="abort_on_conflict")
    assert len(merge_res.conflicts) == 1
    assert "3.11" in mem.recall("python version", entity_filter="user.python").context_text

    # Non-conflicting addition on another branch merges cleanly
    mem.create_branch("add-db", from_branch="main", switch=True)
    mem.store(
        "Cache uses Redis 7",
        category="stack",
        tier=StorageTier.RELATIONAL,
        valid_from=3000.0,
        metadata={"entity": "cache", "attribute": "engine", "value": "Redis 7"},
    )
    mem.switch_branch("main")
    clean_merge = mem.merge_branch("add-db")
    assert clean_merge.conflicts == []
    assert clean_merge.applied_count >= 1
    assert "Redis 7" in mem.recall("cache engine", entity_filter="cache.engine").context_text

    # Guardrails: cannot delete main or switch/rollback to missing target
    with pytest.raises(ValueError):
        mem.delete_branch("main")
    with pytest.raises(ValueError):
        mem.switch_branch("nonexistent")
    with pytest.raises(ValueError):
        mem.rollback("nonexistent-commit")
    mem.close()
