import tempfile
from pathlib import Path

from lethe.core.engine import LetheMemory
from lethe.eval.handover_demo import run_cross_process_handover


def test_cross_process_handover_persists_and_completes_task():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "lethe_handover.db"
        summary = run_cross_process_handover(db_path=db_path, task_id="task_cloud_audit_01")

        assert summary["task_id"] == "task_cloud_audit_01"
        assert summary["task_goal"] == "Conduct competitive cloud infrastructure audit."
        assert len(summary["active_memory_ids"]) == 2
        assert summary["epistemic_state"]["next_step"] == "apply_terraform_downscale"
        assert summary["recalled_tokens"] > 0

        # Verify Agent B's resolution survived DB reopen in a third fresh handle
        verifier = LetheMemory(db_path=db_path)
        try:
            res = verifier.recall("Removed unattached EBS volumes Saved $1,240/month", token_budget=300)
            assert not res.is_abstention
            assert "$1,240/month" in res.context_text
            packet = verifier.import_handover("task_cloud_audit_01")
            assert packet is not None
            assert len(packet.last_tool_trace) == 2
        finally:
            verifier.close()
