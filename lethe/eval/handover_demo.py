from pathlib import Path
import tempfile
from typing import Any, Dict
from lethe.core.engine import LetheMemory
from lethe.models import StorageTier


def simulate_handover(memory: LetheMemory) -> bool:
    """
    Demonstrates mid-task handover between two independent agents
    satisfying Acceptance Criterion 7:
    - Agent 1 (Python Task Agent) begins a task, records findings, and exports state.
    - Agent 2 (Secondary Agent / Claude Code MCP simulation) resumes with zero loss.
    """
    # -------------------------------------------------------------------------
    # AGENT 1: Python Task Agent (Phase 1)
    # -------------------------------------------------------------------------
    m1 = memory.store(
        content="Audit Finding 1: Unattached EBS volumes in us-east-1 are costing $420/month.",
        category="cloud_audit",
        tier=StorageTier.SEMANTIC,
        session_id="agent1_run",
        source="agent_inferred"
    )
    m2 = memory.store(
        content="Audit Finding 2: Kubernetes worker node CPU utilization is only 12%. Recommendation: Downscale from c5.4xlarge to c5.2xlarge.",
        category="cloud_audit",
        tier=StorageTier.SEMANTIC,
        session_id="agent1_run",
        source="agent_inferred"
    )

    # Agent 1 saves mid-task state into Handover Packet
    memory.export_handover(
        task_id="task_cloud_audit_q3",
        task_goal="Audit Q3 AWS infrastructure costs and implement downscaling",
        active_memory_ids=[m1.id, m2.id],
        epistemic_state={
            "phase": "analysis_complete",
            "next_step": "apply_terraform_downscale",
            "findings_count": 2
        },
        tool_trace=[
            {"tool": "aws_cost_explorer", "result": "$420/mo waste found"},
            {"tool": "cloudwatch_metrics", "result": "12% avg CPU"}
        ]
    )

    # -------------------------------------------------------------------------
    # AGENT 2: Secondary Tool / Claude Code MCP client (Phase 2)
    # -------------------------------------------------------------------------
    # Agent 2 imports the packet using the task_id
    resumed_packet = memory.import_handover("task_cloud_audit_q3")
    if not resumed_packet:
        return False

    # Verify state continuity
    assert resumed_packet.task_id == "task_cloud_audit_q3"
    assert resumed_packet.task_goal == "Audit Q3 AWS infrastructure costs and implement downscaling"
    assert resumed_packet.epistemic_state["phase"] == "analysis_complete"
    assert len(resumed_packet.active_memory_ids) == 2

    # Agent 2 queries Lethe for the active memories associated with the task
    res = memory.recall("unattached EBS volumes and kubernetes utilization", token_budget=300)
    assert "$420/month" in res.context_text
    assert "c5.2xlarge" in res.context_text

    # Agent 2 completes the task
    memory.store(
        content="Audit Resolution: Downscaled Kubernetes nodes and deleted unattached EBS volumes. Monthly savings: $1,240.",
        category="cloud_audit",
        tier=StorageTier.SEMANTIC,
        session_id="agent2_claude_code",
        source="tool_verified",
        parent_ids=[m1.id, m2.id]
    )

    return True


def run_cross_process_handover(
    db_path: Path | str,
    task_id: str = "task_cloud_audit_01",
) -> Dict[str, Any]:
    """
    Simulates a true cross-agent handover across separate process lifecycles:
    1. Agent A (Python Agent) stores audit notes, exports HandoverPacket, and closes DB.
    2. Agent B (fresh LetheMemory instance) imports HandoverPacket from disk, recalls
       active task context, and completes the audit without re-asking the user.
    """
    db_file = Path(db_path)

    # --- Agent A lifecycle ---
    agent_a = LetheMemory(db_path=db_file)
    m1 = agent_a.store(
        content="Audit Finding 1: Unattached EBS volumes in us-east-1 are costing $420/month.",
        category="cloud_audit",
        tier=StorageTier.SEMANTIC,
        session_id="agent_a_python",
        source="agent_inferred",
    )
    m2 = agent_a.store(
        content="Audit Finding 2: Kubernetes worker node CPU utilization is only 12%. Next step: Downscale c5.4xlarge to c5.2xlarge.",
        category="cloud_audit",
        tier=StorageTier.SEMANTIC,
        session_id="agent_a_python",
        source="agent_inferred",
    )
    exported = agent_a.export_handover(
        task_id=task_id,
        task_goal="Conduct competitive cloud infrastructure audit.",
        active_memory_ids=[m1.id, m2.id],
        epistemic_state={
            "phase": "findings_collected",
            "next_step": "apply_terraform_downscale",
            "findings_count": 2,
        },
        tool_trace=[
            {"tool": "aws_cost_explorer", "result": "$420/mo waste found"},
            {"tool": "cloudwatch_metrics", "result": "12% avg CPU"},
        ],
    )
    agent_a.close()

    # --- Agent B lifecycle (fresh process / DB handle) ---
    agent_b = LetheMemory(db_path=db_file)
    try:
        imported = agent_b.import_handover(task_id)
        if imported is None:
            raise RuntimeError(f"Handover packet '{task_id}' not found in {db_file}")

        recalled = agent_b.recall("unattached EBS volumes and kubernetes downscale", token_budget=350)
        assert not recalled.is_abstention
        assert "$420/month" in recalled.context_text
        assert "c5.2xlarge" in recalled.context_text

        resolution = agent_b.store(
            content="Audit Completed by Agent B: Removed unattached EBS volumes and downscaled Kubernetes nodes. Saved $1,240/month.",
            category="cloud_audit",
            tier=StorageTier.SEMANTIC,
            session_id="agent_b_mcp",
            source="tool_verified",
            parent_ids=imported.active_memory_ids,
        )
    finally:
        agent_b.close()

    return {
        "task_id": imported.task_id,
        "task_goal": imported.task_goal,
        "active_memory_ids": imported.active_memory_ids,
        "epistemic_state": imported.epistemic_state,
        "recalled_tokens": recalled.tokens_used,
        "resolution_id": resolution.id,
        "exported_at": exported.created_at,
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "lethe_handover.db"
        summary = run_cross_process_handover(db_path=db_path)
        print("\n" + "=" * 75)
        print(" CROSS-AGENT MID-TASK HANDOVER SIMULATION")
        print("=" * 75)
        print(f" Task ID           : {summary['task_id']}")
        print(f" Task Goal         : {summary['task_goal']}")
        print(f" Active Memories   : {len(summary['active_memory_ids'])} restored")
        print(f" Epistemic Phase   : {summary['epistemic_state']['phase']}")
        print(f" Next Step         : {summary['epistemic_state']['next_step']}")
        print(f" Recalled Tokens   : {summary['recalled_tokens']}")
        print(f" Resolution Node   : {summary['resolution_id']}")
        print(" Status            : SUCCESS (Zero context loss across restart)")
        print("=" * 75)


if __name__ == "__main__":
    main()

