import tempfile
from pathlib import Path
import pytest
from lethe.models import StorageTier, MemoryStatus
from lethe.core.engine import LetheMemory

def test_lethe_memory_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "engine_test.db"
        memory = LetheMemory(db_path=db_path)

        # 1. Store memories
        m1 = memory.store(
            content="User preferred language is Python",
            category="preference",
            tier=StorageTier.SEMANTIC,
            source="user_explicit",
            session_id="s1"
        )
        assert m1.id is not None
        assert m1.status == MemoryStatus.ACTIVE

        # 2. Honest Abstention Test: Query something never stored
        res_unknown = memory.recall("What is my secret passport number?", token_budget=300)
        assert res_unknown.is_abstention is True
        assert "not have verified" in res_unknown.context_text.lower()
        assert res_unknown.tokens_used <= 300

        # 3. Recall known memory
        res_known = memory.recall("What programming language do I prefer?", token_budget=300)
        assert res_known.is_abstention is False
        assert "Python" in res_known.context_text
        assert res_known.tokens_used <= 300
        assert res_known.latency_ms > 0

        # 4. Verifiable Deletion
        receipt = memory.forget(m1.id)
        assert receipt.purged_count >= 1

        # 5. Probe after deletion -> Must now abstain!
        res_after = memory.recall("What programming language do I prefer?", token_budget=300)
        assert res_after.is_abstention is True
        assert "Python" not in res_after.context_text

        # 6. Handover Test
        packet = memory.export_handover(
            task_id="task_research_01",
            task_goal="Perform market analysis",
            active_memory_ids=[]
        )
        assert packet.task_id == "task_research_01"

        loaded_packet = memory.import_handover("task_research_01")
        assert loaded_packet is not None
        assert loaded_packet.task_goal == "Perform market analysis"

        memory.close()

