import tempfile
from pathlib import Path
import pytest
from lethe.models import MemoryNode, StorageTier, MemoryStatus
from lethe.storage.db import DatabaseManager

def test_sqlite_persistence_across_restarts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_lethe.db"
        
        # 1. Connect and initialize tables
        db = DatabaseManager(db_path)
        node = MemoryNode(
            id="mem_101",
            content="User lives in Pune",
            category="location",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="session_5",
            source_origin="user_explicit"
        )
        db.save_memory(node)
        
        # Verify it was inserted
        fetched = db.get_memory("mem_101")
        assert fetched is not None
        assert fetched.content == "User lives in Pune"
        
        # 2. Simulate process termination by closing DB
        db.close()
        
        # 3. Simulate process restart by creating a new DatabaseManager pointing to same file
        db_restarted = DatabaseManager(db_path)
        restored_node = db_restarted.get_memory("mem_101")
        
        assert restored_node is not None
        assert restored_node.id == "mem_101"
        assert restored_node.content == "User lives in Pune"
        assert restored_node.category == "location"
        assert restored_node.trust_level == 1.0
        assert restored_node.status == MemoryStatus.ACTIVE
        
        db_restarted.close()

