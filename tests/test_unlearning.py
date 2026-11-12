import tempfile
from pathlib import Path
import pytest
from lethe.models import MemoryNode, StorageTier, MemoryStatus
from lethe.storage.db import DatabaseManager
from lethe.storage.lineage import LineageManager

def test_cascading_unlearning_dag():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lineage.db"
        db = DatabaseManager(db_path)
        lineage = LineageManager(db)

        # 1. Root secret fact (Node A)
        node_a = MemoryNode(
            id="node_a",
            content="User personal phone number is 9876543210",
            category="contact",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="session_22",
            source_origin="user_explicit"
        )
        db.save_memory(node_a)

        # 2. Derived summary (Node B - Child of Node A)
        node_b = MemoryNode(
            id="node_b",
            content="Profile summary: User contact 9876543210 on file for evening calls",
            category="summary",
            tier=StorageTier.SEMANTIC,
            trust_level=0.5,
            status=MemoryStatus.ACTIVE,
            source_session="session_23",
            source_origin="agent_inferred",
            parent_ids=["node_a"]
        )
        db.save_memory(node_b)

        # 3. Third-degree inference (Node C - Child of Node B)
        node_c = MemoryNode(
            id="node_c",
            content="Preferred evening contact method verified via phone 9876543210",
            category="preference",
            tier=StorageTier.SEMANTIC,
            trust_level=0.5,
            status=MemoryStatus.ACTIVE,
            source_session="session_24",
            source_origin="agent_inferred",
            parent_ids=["node_b"]
        )
        db.save_memory(node_c)

        # 4. Unrelated fact (Node D - Unrelated to phone number)
        node_d = MemoryNode(
            id="node_d",
            content="User prefers Python for backend development",
            category="programming",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="session_1",
            source_origin="user_explicit",
            parent_ids=[]
        )
        db.save_memory(node_d)

        # Verify lineage discovery
        descendants = lineage.get_descendants("node_a")
        assert "node_b" in descendants
        assert "node_c" in descendants
        assert "node_d" not in descendants

        # 5. Execute cascading unlearning
        receipt = lineage.cascading_purge("node_a")
        assert receipt.target_id == "node_a"
        assert set(receipt.purged_node_ids) == {"node_a", "node_b", "node_c"}
        assert receipt.purged_count == 3
        assert len(receipt.verification_hash) == 64  # Valid SHA-256

        # 6. Check statuses in database
        mem_a = db.get_memory("node_a")
        mem_b = db.get_memory("node_b")
        mem_c = db.get_memory("node_c")
        mem_d = db.get_memory("node_d")

        assert mem_a.status == MemoryStatus.TOMBSTONED
        assert mem_b.status == MemoryStatus.TOMBSTONED
        assert mem_c.status == MemoryStatus.TOMBSTONED

        # Unrelated memory is perfectly preserved!
        assert mem_d.status == MemoryStatus.ACTIVE
        assert mem_d.content == "User prefers Python for backend development"

        db.close()


def test_forget_non_existent_target_does_not_purge_random_memory():
    from lethe.core.engine import LetheMemory

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_nonexistent_forget.db"
        mem = LetheMemory(db_path=db_path)

        # Store a legitimate memory
        node = mem.store(
            "Priya loves non-vegetarian chicken biryani on weekends",
            category="diet",
            tier=StorageTier.SEMANTIC,
            source="user_explicit"
        )
        assert node.status == MemoryStatus.ACTIVE

        # Attempt to forget a non-existent ID
        receipt = mem.forget("non_existent_target_123")
        assert receipt.purged_count == 0
        assert len(receipt.purged_node_ids) == 0

        # Verify legitimate memory was NOT accidentally deleted!
        check_node = mem.db.get_memory(node.id)
        assert check_node is not None
        assert check_node.status == MemoryStatus.ACTIVE
        assert "chicken biryani" in check_node.content

        mem.close()


def test_forget_short_prefix_id():
    from lethe.core.engine import LetheMemory

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_prefix_forget.db"
        mem = LetheMemory(db_path=db_path)

        node = mem.store(
            "Secret token is ABC-987-XYZ",
            category="credentials",
            tier=StorageTier.SEMANTIC,
        )

        short_id = node.id[:8]
        receipt = mem.forget(short_id)

        assert receipt.purged_count == 1
        assert receipt.target_id == node.id

        check_node = mem.db.get_memory(node.id)
        assert check_node.status == MemoryStatus.TOMBSTONED
        assert check_node.content == "[PURGED_VIA_UNLEARNING]"

        mem.close()


