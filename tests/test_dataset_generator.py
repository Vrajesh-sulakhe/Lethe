import json
import tempfile
from pathlib import Path

from lethe.core.engine import LetheMemory
from lethe.eval.dataset_generator import generate_dataset, replay_dataset
from lethe.models import MemoryStatus


def test_generate_and_replay_30_sessions():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dataset_path = tmp / "sessions_30.json"
        db_path = tmp / "lethe_eval.db"

        dataset = generate_dataset(dataset_path)
        assert dataset_path.exists()
        loaded = json.loads(dataset_path.read_text(encoding="utf-8"))

        assert loaded["session_count"] == 30
        assert len(loaded["sessions"]) == 30
        assert loaded["full_history_tokens"] >= 50000

        # Verify expected trap tags exist across sessions
        trap_tags = {s["session_number"]: s["trap_tag"] for s in loaded["sessions"]}
        assert trap_tags[2] == "foundational_diet"
        assert trap_tags[5] == "update_trap_old_fact"
        assert trap_tags[10] == "contradiction_trap"
        assert trap_tags[15] == "planted_memory_injection_trap"
        assert trap_tags[18] == "update_trap_new_fact"
        assert trap_tags[22] == "deletion_target_established"
        assert trap_tags[25] == "deletion_request"
        assert trap_tags[30] == "audit_session"

        # Replay into LetheMemory and close to test persistence across restart
        mem = LetheMemory(db_path=db_path)
        summary = replay_dataset(mem, dataset_path)
        assert summary["sessions_replayed"] == 30
        assert summary["stored_memories"] >= 10
        assert len(summary["quarantined_ids"]) == 1
        assert len(summary["deletion_receipts"]) == 1
        assert summary["deletion_receipts"][0]["purged_count"] == 3
        mem.close()

        # Reopen in a fresh LetheMemory instance (Crit-1: restart survival)
        restarted = LetheMemory(db_path=db_path)

        # Crit-2: Bitemporal update Pune -> Bengaluru
        current_loc = restarted.bitemporal.query_active_attribute("user", "location")
        assert current_loc is not None
        assert current_loc.metadata["value"] == "Bengaluru"
        old_pune_id = summary["ref_to_id"]["priya_loc_pune"]
        old_pune = restarted.db.get_memory(old_pune_id)
        assert old_pune is not None
        assert old_pune.status == MemoryStatus.HISTORICAL

        # Crit-3: Contradiction surfaced with sources (vegetarian vs external_pdf)
        diet_res = restarted.recall("What is my diet vegetarian or chicken?", token_budget=400)
        assert not diet_res.is_abstention
        assert len(diet_res.conflicts_detected) >= 1
        assert "external_pdf" in diet_res.context_text
        assert "user_explicit" in diet_res.context_text

        # Crit-4: Honest abstention for unseen passport number
        passport_res = restarted.recall("What is my passport number?", token_budget=300)
        assert passport_res.is_abstention is True

        # Crit-5: Planted injection ACCT-9999-EVIL quarantined and never returned
        evil_id = summary["ref_to_id"]["evil_bank_injection"]
        evil_node = restarted.db.get_memory(evil_id)
        assert evil_node is not None
        assert evil_node.status == MemoryStatus.QUARANTINED
        bank_res = restarted.recall(
            "What is my bank account?",
            token_budget=300,
            entity_filter="user.bank_account",
        )
        assert "ACCT-9999-EVIL" not in bank_res.context_text
        assert "ACCT-1234-SAFE" in bank_res.context_text

        # Crit-6 prerequisite: Root phone fact + summary + delivery note all tombstoned & scrubbed
        for ref in ("priya_phone", "priya_phone_summary", "priya_delivery_note"):
            node = restarted.db.get_memory(summary["ref_to_id"][ref])
            assert node is not None
            assert node.status == MemoryStatus.TOMBSTONED
            assert "9876543210" not in node.content

        restarted.close()

