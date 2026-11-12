import json
import sys
from pathlib import Path
from typing import Optional
from mcp.server.mcpserver import MCPServer
from lethe.models import StorageTier
from lethe.core.engine import LetheMemory

def create_mcp_server(db_path: Path | str = "lethe.db") -> MCPServer:
    """
    Creates and configures the Lethe Model Context Protocol (MCP) server.
    Exposes verifiable memory tools over stdio to Claude Code, Cursor, and any MCP client.
    """
    server = MCPServer("lethe-memory")
    memory = LetheMemory(db_path=db_path)

    @server.tool()
    def lethe_recall(
        query: str,
        token_budget: int = 500,
        workspace_id: Optional[str] = None,
    ) -> str:
        """
        Retrieve relevant long-term memories within a strict token budget.
        Returns verified context text or an honest abstention ('I do not have verified memory...').
        """
        res = memory.recall(query, token_budget=token_budget, workspace_id=workspace_id)
        payload = {
            "query": res.query,
            "is_abstention": res.is_abstention,
            "tokens_used": res.tokens_used,
            "latency_ms": round(res.latency_ms, 2),
            "context_text": res.context_text,
            "conflicts": res.conflicts_detected
        }
        return json.dumps(payload, indent=2)

    @server.tool()
    def lethe_store(
        content: str,
        category: str = "general",
        tier: str = "semantic",
        source: str = "user_explicit",
        workspace_id: Optional[str] = None,
        scope: str = "project",
    ) -> str:
        """
        Store a new fact, preference, or decision with provenance and trust level verification.
        Tiers: 'relational' (exact facts), 'semantic' (conversations), 'procedural' (runbooks).
        """
        try:
            tier_enum = StorageTier(tier.lower())
        except ValueError:
            tier_enum = StorageTier.SEMANTIC

        node = memory.store(
            content=content,
            category=category,
            tier=tier_enum,
            source=source,
            workspace_id=workspace_id,
            scope=scope,
        )
        return json.dumps({
            "status": node.status.value,
            "memory_id": node.id,
            "trust_level": node.trust_level,
            "content": node.content,
            "workspace_id": node.workspace_id,
            "scope": node.scope,
        }, indent=2)

    @server.tool()
    def lethe_forget(target: str, reason: str = "user_deletion_request") -> str:
        """
        Executes verifiable cascading unlearning. Permanently erases the target fact,
        its vector embeddings, and all downstream derived summaries and inferences.
        Returns a cryptographic SHA-256 deletion audit receipt.
        """
        receipt = memory.forget(target, reason=reason)
        return json.dumps({
            "target": receipt.target_id,
            "purged_count": receipt.purged_count,
            "purged_node_ids": receipt.purged_node_ids,
            "verification_hash": receipt.verification_hash,
            "timestamp": receipt.timestamp
        }, indent=2)

    @server.tool()
    def lethe_handover_export(task_id: str, task_goal: str) -> str:
        """
        Export current mid-task state and active memories into a Handover Packet
        for another AI agent or tool to resume without context loss.
        """
        packet = memory.export_handover(task_id=task_id, task_goal=task_goal)
        return json.dumps({
            "status": "HANDOVER_PACKET_CREATED",
            "task_id": packet.task_id,
            "task_goal": packet.task_goal,
            "created_at": packet.created_at
        }, indent=2)

    @server.tool()
    def lethe_handover_import(task_id: str) -> str:
        """
        Import a Handover Packet from a previous agent session to seamlessly continue work.
        """
        packet = memory.import_handover(task_id)
        if not packet:
            return json.dumps({"error": f"Handover packet '{task_id}' not found."}, indent=2)
        return json.dumps({
            "status": "HANDOVER_PACKET_LOADED",
            "task_id": packet.task_id,
            "task_goal": packet.task_goal,
            "active_memory_ids": packet.active_memory_ids,
            "epistemic_state": packet.epistemic_state,
            "created_at": packet.created_at
        }, indent=2)

    @server.tool()
    def lethe_inspect(limit: int = 10) -> str:
        """
        Inspect active memories in the Lethe database with categories, trust levels, and provenance.
        """
        memories = memory.db.list_memories()
        output = []
        for m in memories[:limit]:
            output.append({
                "id": m.id[:8],
                "content": m.content,
                "category": m.category,
                "tier": m.tier.value,
                "trust": m.trust_level,
                "status": m.status.value,
                "session": m.source_session
            })
        return json.dumps({"total_count": len(memories), "items": output}, indent=2)

    return server

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Lethe MCP Server")
    parser.add_argument("--db", default="lethe.db", help="Path to SQLite database file")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"], help="MCP transport mode")
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE transport")
    args = parser.parse_args()

    server = create_mcp_server(args.db)
    if args.transport == "sse":
        server.run(transport="sse", port=args.port)
    else:
        server.run(transport="stdio")

if __name__ == "__main__":
    main()

