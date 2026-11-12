import argparse
import json
import sys
from pathlib import Path
from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus, StorageTier


def cmd_inspect(args):
    memory = LetheMemory(db_path=args.db, procedures_dir=args.procedures_dir)
    target_status = MemoryStatus(args.status) if getattr(args, "status", None) else None
    target_tier = StorageTier(args.tier) if getattr(args, "tier", None) else None
    memories = memory.db.list_memories(
        status=target_status,
        workspace_id=getattr(args, "workspace", None),
        tier=target_tier,
    )
    print("=" * 80)
    print(f" LETHE MEMORY INSPECTOR — Total Records: {len(memories)}")
    print("=" * 80)
    if not memories:
        print(" [No memories stored yet.]")
        memory.close()
        return

    for m in memories:
        status_color = m.status.value.upper()
        print(
            f"[{m.id[:8]}] [{status_color}] Category: {m.category:12} | "
            f"Tier: {m.tier.value:10} | Trust: {m.trust_level:.1f} | WS: {m.workspace_id or '-'}"
        )
        print(f"  Content: {m.content}")
        print(f"  Origin:  {m.source_session} ({m.source_origin})")
        if m.parent_ids:
            print(f"  Lineage: Derived from parents {m.parent_ids}")
        print("-" * 80)
    memory.close()


def cmd_store(args):
    memory = LetheMemory(
        db_path=args.db,
        procedures_dir=args.procedures_dir,
        workspace_id=args.workspace,
    )
    metadata = {}
    if args.entity and args.attribute and args.value is not None:
        metadata = {"entity": args.entity, "attribute": args.attribute, "value": args.value}

    node = memory.store(
        content=args.content,
        category=args.category,
        tier=StorageTier(args.tier),
        source=args.source,
        session_id=args.session,
        workspace_id=args.workspace,
        scope=args.scope,
        valid_from=args.valid_from,
        metadata=metadata,
    )
    print(
        json.dumps(
            {
                "action": "STORED",
                "id": node.id,
                "status": node.status.value,
                "tier": node.tier.value,
                "trust_level": node.trust_level,
                "workspace_id": node.workspace_id,
            },
            indent=2,
        )
    )
    memory.close()


def cmd_recall(args):
    memory = LetheMemory(
        db_path=args.db,
        procedures_dir=args.procedures_dir,
        workspace_id=args.workspace,
    )
    res = memory.recall(
        query=args.query,
        token_budget=args.budget,
        as_of_time=args.as_of,
        entity_filter=args.entity,
        workspace_id=args.workspace,
        session_id=args.session,
        model_name=args.model,
    )
    print("=" * 80)
    print(
        f" LETHE RECALL | Tokens Used: {res.tokens_used}/{args.budget} | "
        f"Latency: {res.latency_ms:.2f} ms | Abstention: {res.is_abstention}"
    )
    print("=" * 80)
    print(res.context_text)
    if res.conflicts_detected:
        print("\nConflicts:")
        for c in res.conflicts_detected:
            print(f"  - {c}")
    memory.close()


def cmd_procedures(args):
    proc_dir = getattr(args, "dir", None) or args.procedures_dir
    memory = LetheMemory(db_path=args.db, procedures_dir=proc_dir)
    action = args.proc_action

    if action == "list":
        files = memory.list_procedures()
        if not files:
            print("No procedures found.")
        else:
            for name in files:
                print(f"- {name}")
    elif action == "read":
        content = memory.read_procedure(args.filename)
        if content is None:
            print(f"Error: Procedure '{args.filename}' not found.", file=sys.stderr)
            memory.close()
            sys.exit(1)
        print(content)
    elif action == "write":
        content = args.content
        if args.file:
            content = Path(args.file).read_text(encoding="utf-8")
        if content is None:
            print("Error: Provide --content or --file.", file=sys.stderr)
            memory.close()
            sys.exit(1)
        saved = memory.save_procedure(args.filename, content)
        print(f"Saved procedure: {saved}")
    memory.close()


def cmd_flush_ephemeral(args):
    memory = LetheMemory(db_path=args.db, procedures_dir=args.procedures_dir)
    info = memory.flush_ephemeral(
        session_id=args.session,
        workspace_id=args.workspace,
        promote_summary=args.promote_summary,
    )
    print(json.dumps(info, indent=2))
    memory.close()


def cmd_forget(args):
    memory = LetheMemory(db_path=args.db, procedures_dir=args.procedures_dir)
    print(f"Initiating verifiable cascading unlearning for: '{args.target}'...")
    receipt = memory.forget(args.target, workspace_id=getattr(args, "workspace", None))
    print("=" * 80)
    print(" DELETION RECEIPT (CRYPTOGRAPHIC AUDIT PROOF)")
    print("=" * 80)
    print(f" Target:            {receipt.target_id}")
    print(f" Purged Nodes:      {receipt.purged_count} nodes (target + derived summaries)")
    print(f" Purged IDs:        {receipt.purged_node_ids}")
    print(f" Verification Hash: {receipt.verification_hash}")
    print(f" Timestamp:         {receipt.timestamp}")
    print("=" * 80)
    if receipt.purged_count > 0:
        print(" Status: SUCCESS — 100% permanently eradicated.")
    else:
        print(" Status: NOT FOUND — zero records modified.")
    memory.close()


def cmd_demo(args):
    from lethe.eval.dataset_generator import run_interactive_demo
    run_interactive_demo(db_path=args.db)


def cmd_benchmark(args):
    from lethe.eval.benchmark import run_benchmark
    run_benchmark(db_path=args.db)


def cmd_setup(args):
    from lethe.platform.setup import detect_installed_tools, setup_all_tools

    print("=" * 80)
    print(" LETHE CROSS-TOOL INTEGRATION HUB: ZERO-CONFIG SETUP")
    print("=" * 80)

    if args.check:
        print("[Mode: Dry Run / Inspection Only]\n")
        detected = detect_installed_tools()
        print(f" {'Tool':<15} | {'Detected Path':<42} | {'Configured'}")
        print("-" * 80)
        for name, info in detected.items():
            path_str = str(info["path"])
            if len(path_str) > 40:
                path_str = "..." + path_str[-37:]
            status = (
                "YES (Connected)"
                if info["is_configured"]
                else ("Ready to wire" if info["exists"] else "Not installed")
            )
            print(f" {name:<15} | {path_str:<42} | {status}")
        print("=" * 80)
        print("Run 'lethe setup' without --check to automatically wire detected tools.")
        return

    tools_to_wire = [args.tool] if args.tool != "all" else None
    results = setup_all_tools(tools=tools_to_wire, db_path=args.db)

    print(f"\nSuccessfully configured {len(results)} tool integrations:\n")
    for r in results:
        print(f" [✓] {r['tool'].upper():<12} -> {r['path']}")

    print("=" * 80)
    print(" All selected tools are now wired to Lethe's universal MCP memory bus!")
    print(" Open Cursor, Claude Code, or Antigravity and start working seamlessly.")
    print("=" * 80)


def cmd_ui(args):
    from lethe.platform.dashboard.server import start_dashboard
    start_dashboard(
        db_path=args.db,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        procedures_dir=args.procedures_dir,
    )


def main():
    parser = argparse.ArgumentParser(
        prog="lethe",
        description="Lethe: Autonomous Agent Memory Operating System",
    )
    parser.add_argument("--db", default="lethe.db", help="Path to SQLite database")
    parser.add_argument("--procedures-dir", default="procedures", help="Directory for Markdown runbooks")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    # mcp command
    p_mcp = subparsers.add_parser("mcp", help="Run Lethe as a Model Context Protocol (MCP) server")
    p_mcp.add_argument("--transport", default="stdio", choices=["stdio", "sse"], help="MCP transport mode")
    p_mcp.add_argument("--port", type=int, default=8000, help="Port for SSE transport")

    # store command
    p_store = subparsers.add_parser("store", help="Store a memory across relational/semantic/procedural/ephemeral tiers")
    p_store.add_argument("content", help="Memory content text")
    p_store.add_argument("--category", default="general", help="Memory category")
    p_store.add_argument("--tier", default="semantic", choices=[t.value for t in StorageTier], help="Storage tier")
    p_store.add_argument("--source", default="user_explicit", help="Provenance source")
    p_store.add_argument("--session", default="cli_session", help="Source session ID")
    p_store.add_argument("--workspace", default=None, help="Workspace ID")
    p_store.add_argument("--scope", default="project", choices=["project", "global"], help="Memory scope")
    p_store.add_argument("--entity", default=None, help="Relational entity key")
    p_store.add_argument("--attribute", default=None, help="Relational attribute key")
    p_store.add_argument("--value", default=None, help="Relational attribute value")
    p_store.add_argument("--valid-from", type=float, default=None, help="Bitemporal valid_from epoch timestamp")

    # recall command
    p_recall = subparsers.add_parser("recall", help="Recall token-budgeted memories")
    p_recall.add_argument("query", help="Natural language query")
    p_recall.add_argument("--budget", type=int, default=400, help="Strict token budget")
    p_recall.add_argument("--model", default=None, help="Target model context profile (e.g. tiny-4k, claude-sonnet-4)")
    p_recall.add_argument("--workspace", default=None, help="Workspace filter")
    p_recall.add_argument("--entity", default=None, help="Optional entity.attribute relational filter")
    p_recall.add_argument("--as-of", type=float, default=None, help="Bitemporal point-in-time epoch")
    p_recall.add_argument("--session", default=None, help="Active session ID (includes matching ephemeral working memory)")

    # procedures command
    p_proc = subparsers.add_parser("procedures", help="Manage Tier 3 Markdown runbooks")
    p_proc.add_argument("--dir", default=None, help="Override procedures directory")
    proc_sub = p_proc.add_subparsers(dest="proc_action", required=True)
    proc_sub.add_parser("list", help="List stored runbooks")
    p_proc_read = proc_sub.add_parser("read", help="Read a runbook")
    p_proc_read.add_argument("filename", help="Runbook filename")
    p_proc_write = proc_sub.add_parser("write", help="Write a runbook")
    p_proc_write.add_argument("filename", help="Runbook filename")
    p_proc_write.add_argument("--content", default=None, help="Markdown text")
    p_proc_write.add_argument("--file", default=None, help="Read Markdown from file path")

    # flush-ephemeral command
    p_flush = subparsers.add_parser("flush-ephemeral", help="Flush Tier 4 ephemeral session scratchpad")
    p_flush.add_argument("--session", default=None, help="Session ID to flush")
    p_flush.add_argument("--workspace", default=None, help="Workspace filter")
    p_flush.add_argument("--promote-summary", default=None, help="Concise takeaway to promote to long-term memory")

    # inspect command
    p_inspect = subparsers.add_parser("inspect", help="Inspect active memories, lineage, and trust statuses")
    p_inspect.add_argument("--status", default=None, choices=[s.value for s in MemoryStatus], help="Filter by status")
    p_inspect.add_argument("--tier", default=None, choices=[t.value for t in StorageTier], help="Filter by tier")
    p_inspect.add_argument("--workspace", default=None, help="Filter by workspace ID")

    # forget command
    p_forget = subparsers.add_parser("forget", help="Verifiably forget a memory by ID or query text")
    p_forget.add_argument("target", help="Memory ID or text query to forget")
    p_forget.add_argument("--workspace", default=None, help="Workspace scope for query-based unlearning")

    # demo command
    subparsers.add_parser("demo", help="Run the full 30-session interactive judge demonstration")

    # benchmark command
    subparsers.add_parser("benchmark", help="Run the token & latency benchmark against full history")

    # setup command
    p_setup = subparsers.add_parser("setup", help="Auto-configure MCP integration for Cursor, Claude Code, Antigravity, and VS Code")
    p_setup.add_argument("--check", action="store_true", help="Inspect and check detected tools without modifying files")
    p_setup.add_argument("--tool", default="all", choices=["all", "cursor", "claude", "antigravity", "vscode"], help="Target specific tool")

    # ui command
    p_ui = subparsers.add_parser("ui", help="Launch the Lethe real-time developer dashboard")
    p_ui.add_argument("--host", default="127.0.0.1", help="Dashboard host interface")
    p_ui.add_argument("--port", type=int, default=8765, help="Dashboard port")
    p_ui.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")

    args = parser.parse_args()

    if args.subcommand == "mcp":
        from lethe.mcp.server import create_mcp_server
        server = create_mcp_server(args.db, procedures_dir=args.procedures_dir)
        if args.transport == "sse":
            server.run(transport="sse", port=args.port)
        else:
            server.run(transport="stdio")
    elif args.subcommand == "store":
        cmd_store(args)
    elif args.subcommand == "recall":
        cmd_recall(args)
    elif args.subcommand == "procedures":
        cmd_procedures(args)
    elif args.subcommand == "flush-ephemeral":
        cmd_flush_ephemeral(args)
    elif args.subcommand == "inspect":
        cmd_inspect(args)
    elif args.subcommand == "forget":
        cmd_forget(args)
    elif args.subcommand == "demo":
        cmd_demo(args)
    elif args.subcommand == "benchmark":
        cmd_benchmark(args)
    elif args.subcommand == "setup":
        cmd_setup(args)
    elif args.subcommand == "ui":
        cmd_ui(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
