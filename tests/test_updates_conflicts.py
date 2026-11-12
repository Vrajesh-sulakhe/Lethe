import tempfile
import time
from pathlib import Path
import pytest
from lethe.models import MemoryNode, StorageTier, MemoryStatus
from lethe.storage.db import DatabaseManager
from lethe.core.bitemporal import BitemporalManager

def test_bitemporal_update_and_superseding():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(Path(tmpdir) / "test_bitemporal.db")
        bitemp = BitemporalManager(db)

        t1 = 1000.0  # Session 5 (Jan)
        t2 = 2000.0  # Session 18 (Mar)

        # 1. Old fact: "I live in Pune"
        node1 = MemoryNode(
            id="loc_1",
            content="I live in Pune",
            category="location",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="s5",
            source_origin="user_explicit",
            valid_from=t1,
            metadata={"entity": "user", "attribute": "city", "value": "Pune"}
        )
        bitemp.process_statement(node1)

        # Query as of t1 + 500 (Feb): Should be Pune
        res_feb = bitemp.query_active_attribute("user", "city", as_of_time=1500.0)
        assert res_feb is not None
        assert res_feb.metadata["value"] == "Pune"

        # 2. New fact arrives: "I moved to Bengaluru"
        node2 = MemoryNode(
            id="loc_2",
            content="I moved to Bengaluru",
            category="location",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="s18",
            source_origin="user_explicit",
            valid_from=t2,
            metadata={"entity": "user", "attribute": "city", "value": "Bengaluru"}
        )
        bitemp.process_statement(node2)

        # Query as of t2 + 100 (April / Current): Should be Bengaluru
        res_current = bitemp.query_active_attribute("user", "city", as_of_time=2100.0)
        assert res_current is not None
        assert res_current.metadata["value"] == "Bengaluru"
        assert res_current.status == MemoryStatus.ACTIVE

        # Old fact in database must now be HISTORICAL, not deleted!
        old_node = db.get_memory("loc_1")
        assert old_node.status == MemoryStatus.HISTORICAL
        assert old_node.valid_to == t2

        db.close()


def test_contradiction_detection_with_sources():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(Path(tmpdir) / "test_conflict.db")
        bitemp = BitemporalManager(db)

        t1 = 1000.0

        # User says: "I am a strict vegetarian"
        node_veg = MemoryNode(
            id="diet_1",
            content="I am a strict vegetarian",
            category="diet",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="s2",
            source_origin="user_explicit",
            valid_from=t1,
            metadata={"entity": "user", "attribute": "diet", "value": "vegetarian"}
        )
        bitemp.process_statement(node_veg)

        # Document claims: "User loves chicken biryani" (conflicting claim for same interval)
        node_nonveg = MemoryNode(
            id="diet_2",
            content="User loves chicken biryani",
            category="diet",
            tier=StorageTier.RELATIONAL,
            trust_level=0.8,
            status=MemoryStatus.ACTIVE,
            source_session="s10",
            source_origin="external_pdf",
            valid_from=t1,
            metadata={"entity": "user", "attribute": "diet", "value": "non-vegetarian"}
        )
        conflicts = bitemp.process_statement(node_nonveg)

        # Contradiction must be detected, NOT silently overwritten!
        assert len(conflicts) > 0
        assert "diet_1" in conflicts[0]["conflicting_ids"]

        # Check DB statuses: Both are marked CONFLICT
        m1 = db.get_memory("diet_1")
        m2 = db.get_memory("diet_2")
        assert m1.status == MemoryStatus.CONFLICT
        assert m2.status == MemoryStatus.CONFLICT

        db.close()

