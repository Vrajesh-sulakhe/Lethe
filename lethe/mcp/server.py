import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from lethe.models import MemoryStatus, StorageTier
from lethe.core.engine import LetheMemory


def verify_mcp_preflight(
    db_path: Path | str = "lethe.db",
    procedures_dir: Path | str = "procedures",
) -> Dict[str, Any]:
    """
    Executes pre-flight connectivity and integrity checks on the SQLite database
    and procedures directory before launching the MCP listener.
    """
    mem = LetheMemory(db_path=db_path, procedures_dir=procedures_dir)
    try:
        health = mem.check_health()
        if not health.get("healthy", False):
            raise RuntimeError(f"Lethe pre-flight health check failed: {json.dumps(health)}")
        return health
    finally:
        mem.close()


def create_mcp_server(
    db_path: Path | str = "lethe.db",
    procedures_dir: Path | str = "procedures",
) -> MCPServer:
    """
    Creates and configures the Lethe Model Context Protocol (MCP) server.
    Exposes compact, low-schema-overhead memory tools across all tiers over
    stdio, SSE, and streamable-http transports, plus HTTP /health and /stats endpoints.
    """
    server = MCPServer("lethe-memory")
    memory = LetheMemory(db_path=db_path, procedures_dir=procedures_dir)

    @server.custom_route("/health", methods=["GET"])
    async def http_health_check(request: Request) -> JSONResponse:
        with memory.db._lock:
            payload = memory.check_health()
        status_code = 200 if payload.get("healthy") else 503
        return JSONResponse(payload, status_code=status_code)

    @server.custom_route("/stats", methods=["GET"])
    async def http_stats_endpoint(request: Request) -> JSONResponse:
        from lethe.platform.dashboard.server import DashboardRequestHandler

        workspace = request.query_params.get("workspace")
        with memory.db._lock:
            stats = DashboardRequestHandler._build_stats_payload(
                memory.db,
                memory.procedural.procedures_dir,
                workspace=workspace,
            )
            health = memory.check_health()
        stats["health"] = {
            "status": health["status"],
            "journal_mode": health["journal_mode"],
            "integrity": health["integrity"],
            "schema_version": health["schema_version"],
            "latency_ms": health["latency_ms"],
        }
        return JSONResponse(stats, status_code=200)

    @server.tool()
    def lethe_health() -> str:
        """Check Lethe MCP server health, SQLite WAL status, schema version, and storage metrics."""
        with memory.db._lock:
            payload = memory.check_health()
        return json.dumps(payload, indent=2)

    @server.tool()
    def lethe_recall(
        query: str,
        token_budget: int = 500,
        workspace_id: Optional[str] = None,
        as_of_time: Optional[float] = None,
        entity_filter: Optional[str] = None,
        model_name: Optional[str] = None,
        remaining_context_tokens: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """Recall verified memories within a strict post-format token budget."""
        with memory.db._lock:
            res = memory.recall(
                query,
                token_budget=token_budget,
                as_of_time=as_of_time,
                entity_filter=entity_filter,
                workspace_id=workspace_id,
                session_id=session_id,
                model_name=model_name,
                remaining_context_tokens=remaining_context_tokens,
            )
        return json.dumps(
            {
                "query": res.query,
                "is_abstention": res.is_abstention,
                "tokens_used": res.tokens_used,
                "latency_ms": round(res.latency_ms, 2),
                "context_text": res.context_text,
                "conflicts": res.conflicts_detected,
            },
            indent=2,
        )

    @server.tool()
    def lethe_store(
        content: str,
        category: str = "general",
        tier: str = "semantic",
        source: str = "user_explicit",
        workspace_id: Optional[str] = None,
        scope: str = "project",
        session_id: str = "mcp_session",
        metadata: Optional[Dict[str, Any]] = None,
        parent_ids: Optional[List[str]] = None,
        valid_from: Optional[float] = None,
    ) -> str:
        """Store fact in relational, semantic, procedural, or ephemeral tier."""
        try:
            tier_enum = StorageTier(tier.lower())
        except ValueError:
            tier_enum = StorageTier.SEMANTIC

        with memory.db._lock:
            node = memory.store(
                content=content,
                category=category,
                tier=tier_enum,
                source=source,
                session_id=session_id,
                parent_ids=parent_ids,
                metadata=metadata,
                valid_from=valid_from,
                workspace_id=workspace_id,
                scope=scope,
            )
        return json.dumps(
            {
                "status": node.status.value,
                "tier": node.tier.value,
                "memory_id": node.id,
                "trust_level": node.trust_level,
                "content": node.content,
                "workspace_id": node.workspace_id,
                "scope": node.scope,
            },
            indent=2,
        )

    @server.tool()
    def lethe_forget(
        target: str,
        reason: str = "user_deletion_request",
        workspace_id: Optional[str] = None,
    ) -> str:
        """Cascade-purge memory and descendants across SQL/vectors with SHA-256 receipt."""
        with memory.db._lock:
            receipt = memory.forget(target, reason=reason, workspace_id=workspace_id)
        return json.dumps(
            {
                "target": receipt.target_id,
                "purged_count": receipt.purged_count,
                "purged_node_ids": receipt.purged_node_ids,
                "verification_hash": receipt.verification_hash,
                "timestamp": receipt.timestamp,
            },
            indent=2,
        )

    @server.tool()
    def lethe_handover_export(
        task_id: str,
        task_goal: str,
        active_memory_ids: Optional[List[str]] = None,
        epistemic_state: Optional[Dict[str, Any]] = None,
        tool_trace: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Export mid-task state packet for cross-tool continuation."""
        with memory.db._lock:
            packet = memory.export_handover(
                task_id=task_id,
                task_goal=task_goal,
                active_memory_ids=active_memory_ids,
                epistemic_state=epistemic_state,
                tool_trace=tool_trace,
            )
        return json.dumps(
            {
                "status": "HANDOVER_PACKET_CREATED",
                "task_id": packet.task_id,
                "task_goal": packet.task_goal,
                "active_memory_ids": packet.active_memory_ids,
                "created_at": packet.created_at,
            },
            indent=2,
        )

    @server.tool()
    def lethe_handover_import(task_id: str) -> str:
        """Import a mid-task Handover Packet saved by another agent tool."""
        with memory.db._lock:
            packet = memory.import_handover(task_id)
        if not packet:
            return json.dumps({"error": f"Handover packet '{task_id}' not found."}, indent=2)
        return json.dumps(
            {
                "status": "HANDOVER_PACKET_LOADED",
                "task_id": packet.task_id,
                "task_goal": packet.task_goal,
                "active_memory_ids": packet.active_memory_ids,
                "epistemic_state": packet.epistemic_state,
                "last_tool_trace": packet.last_tool_trace,
                "created_at": packet.created_at,
            },
            indent=2,
        )

    @server.tool()
    def lethe_inspect(
        limit: int = 10,
        status: Optional[str] = None,
        workspace_id: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> str:
        """Inspect stored memories with status, tier, trust level, and workspace filter."""
        target_status = None
        if status:
            try:
                target_status = MemoryStatus(status.lower())
            except ValueError:
                pass
        target_tier = None
        if tier:
            try:
                target_tier = StorageTier(tier.lower())
            except ValueError:
                pass

        with memory.db._lock:
            memories = memory.db.list_memories(
                status=target_status,
                workspace_id=workspace_id,
                tier=target_tier,
            )
        output = [
            {
                "id": m.id[:8],
                "content": m.content,
                "category": m.category,
                "tier": m.tier.value,
                "trust": m.trust_level,
                "status": m.status.value,
                "workspace_id": m.workspace_id,
                "session": m.source_session,
            }
            for m in memories[:limit]
        ]
        return json.dumps({"total_count": len(memories), "items": output}, indent=2)

    @server.tool()
    def lethe_procedures_list() -> str:
        """List stored Tier 3 Markdown runbooks without loading contents into context."""
        with memory.db._lock:
            procs = memory.list_procedures()
        return json.dumps({"procedures": procs}, indent=2)

    @server.tool()
    def lethe_procedure_read(filename: str) -> str:
        """Read a Tier 3 Markdown runbook on demand."""
        with memory.db._lock:
            content = memory.read_procedure(filename)
        if content is None:
            return json.dumps({"error": f"Procedure '{filename}' not found."}, indent=2)
        return json.dumps({"filename": filename, "content": content}, indent=2)

    @server.tool()
    def lethe_procedure_write(filename: str, content: str) -> str:
        """Save or update a Tier 3 Markdown runbook on disk."""
        with memory.db._lock:
            saved = memory.save_procedure(filename, content)
        return json.dumps({"filename": saved.name, "path": str(saved)}, indent=2)

    @server.tool()
    def lethe_flush_ephemeral(
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        promote_summary: Optional[str] = None,
    ) -> str:
        """Flush Tier 4 ephemeral scratchpad memory and optionally promote a concise summary."""
        with memory.db._lock:
            result = memory.flush_ephemeral(
                session_id=session_id,
                workspace_id=workspace_id,
                promote_summary=promote_summary,
            )
        return json.dumps(result, indent=2)

    return server


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Lethe MCP Server")
    parser.add_argument("--db", default="lethe.db", help="Path to SQLite database file")
    parser.add_argument("--procedures-dir", default="procedures", help="Directory for Markdown runbooks")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport mode",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host interface for SSE / streamable-http transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for SSE / streamable-http transport")
    args = parser.parse_args()

    verify_mcp_preflight(args.db, procedures_dir=args.procedures_dir)
    server = create_mcp_server(args.db, procedures_dir=args.procedures_dir)
    if args.transport == "sse":
        server.run(transport="sse", host=args.host, port=args.port)
    elif args.transport == "streamable-http":
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
