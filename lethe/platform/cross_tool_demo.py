import time
import json
from pathlib import Path
from lethe.core.engine import LetheMemory
from lethe.models import StorageTier

def run_cross_tool_handover_simulation(db_path: str = "lethe.db") -> None:
    """
    Simulates a live developer workflow across two distinct tools:
    - Tool 1: Claude Code CLI (in terminal) audits infrastructure and records findings.
    - Tool 2: Cursor IDE (via MCP) reads the context and executes the remediation.
    Both tools share the same local SQLite Lethe memory bus with zero context loss.
    """
    print("\n" + "=" * 80)
    print(" LETHE PLATFORM: LIVE CROSS-TOOL MEMORY BUS DEMONSTRATION")
    print("=" * 80)
    print(" Scenario: Developer uses Claude Code in terminal, then switches to Cursor IDE.")
    print(" Goal: Verify 100% state continuity without re-explaining architecture.")
    print("-" * 80)

    # -------------------------------------------------------------
    # STEP 1: CLAUDE CODE (CLI SESSION)
    # -------------------------------------------------------------
    print("\n[STEP 1: CLAUDE CODE CLI] Engineer runs infrastructure audit in terminal...")
    mem_claude = LetheMemory(db_path=db_path)

    node_audit = mem_claude.store(
        content="Production Audit finding: Redis cluster memory usage is at 88% due to unexpiring sessions.",
        category="infrastructure",
        tier=StorageTier.SEMANTIC,
        source="claude_code_terminal",
        trust_level=0.8,
        session_id="session_terminal_101",
        metadata={"service": "redis", "severity": "HIGH", "action": "add_ttl_expiry"}
    )
    print(f"      --> Stored finding #{node_audit.id[:8]} into Lethe:")
    print(f"          '{node_audit.content}'")

    # Claude Code creates an export handover packet before developer switches to Cursor
    handover = mem_claude.export_handover(
        task_id="TASK-REDIS-HOTFIX",
        task_goal="Fix Redis session TTL exhaustion before morning peak load"
    )
    print(f"      --> Created Handover Packet '{handover.task_id}' for next agent.")
    mem_claude.close()
    print("      --> [Claude Code CLI Closed]")

    # -------------------------------------------------------------
    # STEP 2: CURSOR IDE (MCP SESSION)
    # -------------------------------------------------------------
    print("\n[STEP 2: CURSOR IDE] Developer switches to Cursor IDE. Cursor connects via MCP...")
    mem_cursor = LetheMemory(db_path=db_path)

    # Cursor retrieves the task handover packet
    imported_packet = mem_cursor.import_handover("TASK-REDIS-HOTFIX")
    assert imported_packet is not None
    print(f"      --> Cursor automatically imported task: '{imported_packet.task_goal}'")

    # Cursor queries Lethe for the Redis architecture context
    print("      --> Cursor asks Lethe over MCP: 'What Redis issue was found during the audit?'")
    recall_result = mem_cursor.recall("What Redis issue was found during the audit?", token_budget=300)

    print(f"      --> Lethe recalled {recall_result.tokens_used} tokens (latency: {recall_result.latency_ms:.2f}ms):")
    print(f"          {recall_result.context_text.strip()}")

    assert "88%" in recall_result.context_text
    assert "Redis" in recall_result.context_text

    # Cursor completes the remediation and logs the resolution into Lethe
    node_fix = mem_cursor.store(
        content="Cursor fix applied: Added 86400s TTL on session keys and evicted stale guest sessions.",
        category="infrastructure",
        tier=StorageTier.SEMANTIC,
        source="cursor_ide_mcp",
        trust_level=0.8,
        session_id="session_cursor_202",
        parent_ids=[node_audit.id]
    )
    print(f"      --> Cursor committed resolution #{node_fix.id[:8]} (linked in Lineage DAG).")
    mem_cursor.close()

    print("\n" + "=" * 80)
    print(" [✓] CROSS-TOOL DEMO SUCCESSFUL: ZERO CONTEXT LOST BETWEEN TOOLS!")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    run_cross_tool_handover_simulation()
