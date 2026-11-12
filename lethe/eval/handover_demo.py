from typing import Optional
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
    packet = memory.export_handover(
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

if __name__ == "__main__":
    mem = LetheMemory("lethe_handover_test.db")
    success = simulate_handover(mem)
    print("Handover test result:", "SUCCESS" if success else "FAILED")
    mem.close()

