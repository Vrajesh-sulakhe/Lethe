import argparse
from contextlib import contextmanager
import json
import os
import sys
from pathlib import Path
from typing import Optional
from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus, StorageTier
from lethe.platform.setup import (
    build_mcp_credentials,
    format_mcp_credentials,
    get_default_db_path,
    get_default_procedures_dir,
)


@contextmanager
def _open_memory(
    args,
    procedures_dir: Optional[str] = None,
    workspace_id: Optional[str] = None,
):
    """Opens LetheMemory for a CLI command and guarantees clean close on exit or error."""
    memory = LetheMemory(
        db_path=args.db,
        procedures_dir=procedures_dir or args.procedures_dir,
        workspace_id=workspace_id,
    )
    try:
        yield memory
    finally:
        memory.close()


def cmd_inspect(args):
    target_status = MemoryStatus(args.status) if getattr(args, "status", None) else None
    target_tier = StorageTier(args.tier) if getattr(args, "tier", None) else None
    with _open_memory(args) as memory:
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


def cmd_store(args):
    rel_fields = (args.entity, args.attribute, args.value)
    if any(v is not None for v in rel_fields) and not (
        args.entity and args.attribute and args.value is not None
    ):
        print(
            "Error: --entity, --attribute, and --value must all be provided together.",
            file=sys.stderr,
        )
        sys.exit(1)

    metadata = {}
    if args.entity and args.attribute and args.value is not None:
        metadata = {"entity": args.entity, "attribute": args.attribute, "value": args.value}

    with _open_memory(args, workspace_id=args.workspace) as memory:
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


def cmd_recall(args):
    with _open_memory(args, workspace_id=args.workspace) as memory:
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


def cmd_procedures(args):
    proc_dir = getattr(args, "dir", None) or args.procedures_dir
    action = args.proc_action

    with _open_memory(args, procedures_dir=proc_dir) as memory:
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
                sys.exit(1)
            print(content)
        elif action == "write":
            content = args.content
            if args.file:
                try:
                    content = Path(args.file).read_text(encoding="utf-8")
                except OSError as exc:
                    print(f"Error reading '{args.file}': {exc}", file=sys.stderr)
                    sys.exit(1)
            if content is None:
                print("Error: Provide --content or --file.", file=sys.stderr)
                sys.exit(1)
            saved = memory.save_procedure(args.filename, content)
            print(f"Saved procedure: {saved}")


def cmd_flush_ephemeral(args):
    with _open_memory(args) as memory:
        info = memory.flush_ephemeral(
            session_id=args.session,
            workspace_id=args.workspace,
            promote_summary=args.promote_summary,
        )
    print(json.dumps(info, indent=2))


def cmd_forget(args):
    print(f"Initiating verifiable cascading unlearning for: '{args.target}'...")
    with _open_memory(args) as memory:
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


def cmd_demo(args):
    from lethe.eval.dataset_generator import run_interactive_demo
    run_interactive_demo(db_path=args.db)


def cmd_benchmark(args):
    from lethe.eval.benchmark import run_benchmark
    run_benchmark(db_path=args.db)


def cmd_setup(args):
    from lethe.platform.setup import detect_installed_tools, setup_all_tools

    resolved_db = os.path.abspath(os.path.expanduser(args.db))
    resolved_procs = os.path.abspath(os.path.expanduser(args.procedures_dir))
    use_wrapper = getattr(args, "use_wrapper", False)

    if getattr(args, "print_creds", False):
        print(
            format_mcp_credentials(
                db_path=resolved_db,
                procedures_dir=resolved_procs,
                use_wrapper=use_wrapper,
            )
        )
        return

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
                else ("Ready to wire" if info["exists"] else "Will create")
            )
            print(f" {name:<15} | {path_str:<42} | {status}")
        print("=" * 80)
        print("Run 'lethe setup' to wire tools, or 'lethe creds' to copy-paste MCP credentials.")
        return

    tools_to_wire = [args.tool] if args.tool != "all" else None
    results = setup_all_tools(
        tools=tools_to_wire,
        db_path=resolved_db,
        procedures_dir=resolved_procs,
        use_wrapper=use_wrapper,
    )

    print(f"\nSuccessfully configured {len(results)} tool integrations (shared vault: {resolved_db}):\n")
    for r in results:
        print(f" [✓] {r['tool'].upper():<12} -> {r['path']}")

    print("=" * 80)
    print(" All selected tools are now wired to Lethe's universal MCP memory bus!")
    print(" Open Cursor, Claude Code, Antigravity, Codex, or VS Code and restart/reload MCP.")
    print("=" * 80)


def cmd_creds(args):
    resolved_db = os.path.abspath(os.path.expanduser(args.db))
    resolved_procs = os.path.abspath(os.path.expanduser(args.procedures_dir))
    use_wrapper = getattr(args, "use_wrapper", False)

    if getattr(args, "json", False):
        creds = build_mcp_credentials(
            db_path=resolved_db,
            procedures_dir=resolved_procs,
            use_wrapper=use_wrapper,
        )
        print(json.dumps(creds, indent=2))
        return

    print(
        format_mcp_credentials(
            db_path=resolved_db,
            procedures_dir=resolved_procs,
            use_wrapper=use_wrapper,
        )
    )


def cmd_mcp(args):
    from lethe.mcp.server import run_mcp_server

    if getattr(args, "check_only", False):
        with _open_memory(args) as memory:
            health = memory.check_health()
        print(json.dumps(health, indent=2))
        if not health.get("healthy", False):
            sys.exit(1)
        return

    run_mcp_server(
        db_path=args.db,
        procedures_dir=args.procedures_dir,
        transport=args.transport,
        host=args.host,
        port=args.port,
    )


def cmd_ui(args):
    from lethe.platform.dashboard.server import start_dashboard
    start_dashboard(
        db_path=args.db,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        procedures_dir=args.procedures_dir,
    )


def cmd_health(args):
    with _open_memory(args) as memory:
        health = memory.check_health()

    if getattr(args, "json", False):
        print(json.dumps(health, indent=2))
    else:
        print("=" * 80)
        print(f" LETHE HEALTH CHECK — Status: {health['status'].upper()}")
        print("=" * 80)
        print(f" Database Path:    {health['db_path']}")
        print(f" Journal Mode:     {health['journal_mode'].upper()}")
        print(f" Integrity Check:  {health['integrity'].upper()}")
        print(f" Schema Version:   v{health['schema_version']}")
        print(f" Total Memories:   {health['total_memories']}")
        print(f" Lineage Edges:    {health['lineage_edges']}")
        print(f" Handover Packets: {health['handover_packets']}")
        print(f" Procedures Dir:   {health['procedures'].get('path', '-')} ({health['procedures'].get('count', 0)} runbooks)")
        print(f" Probe Latency:    {health['latency_ms']} ms")
        print("=" * 80)

    if not health.get("healthy", False):
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="lethe",
        description="Lethe: Autonomous Agent Memory Operating System",
    )
    parser.add_argument("--db", default=get_default_db_path(), help="Path to SQLite database (default: ~/.lethe/lethe.db)")
    parser.add_argument("--procedures-dir", default=get_default_procedures_dir(), help="Directory for Markdown runbooks (default: ~/.lethe/procedures)")

    # Allow --db and --procedures-dir either before or after the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database")
    common.add_argument("--procedures-dir", default=argparse.SUPPRESS, help="Directory for Markdown runbooks")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    # mcp command
    p_mcp = subparsers.add_parser("mcp", parents=[common], help="Run Lethe as a Model Context Protocol (MCP) server")
    p_mcp.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport mode",
    )
    p_mcp.add_argument("--host", default="127.0.0.1", help="Host interface for SSE / streamable-http transport")
    p_mcp.add_argument("--port", type=int, default=8000, help="Port for SSE / streamable-http transport")
    p_mcp.add_argument("--check-only", action="store_true", help="Run pre-flight DB & procedure directory health check and exit")

    # creds command
    p_creds = subparsers.add_parser("creds", parents=[common], help="Print copy-pasteable MCP credentials (JSON, TOML, CLI) for Cursor, Antigravity, Claude Code, and Codex")
    p_creds.add_argument("--use-wrapper", action="store_true", help="Use lethe.sh / lethe.cmd wrapper instead of direct CLI/Python path")
    p_creds.add_argument("--json", action="store_true", help="Output credentials payload as JSON")

    # health command
    p_health = subparsers.add_parser("health", parents=[common], help="Verify SQLite WAL health, schema version, and procedure directory readiness")
    p_health.add_argument("--json", action="store_true", help="Output health report as JSON")

    # store command
    p_store = subparsers.add_parser("store", parents=[common], help="Store a memory across relational/semantic/procedural/ephemeral tiers")
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
    p_recall = subparsers.add_parser("recall", parents=[common], help="Recall token-budgeted memories")
    p_recall.add_argument("query", help="Natural language query")
    p_recall.add_argument("--budget", type=int, default=400, help="Strict token budget")
    p_recall.add_argument("--model", default=None, help="Target model context profile (e.g. tiny-4k, claude-sonnet-4)")
    p_recall.add_argument("--workspace", default=None, help="Workspace filter")
    p_recall.add_argument("--entity", default=None, help="Optional entity.attribute relational filter")
    p_recall.add_argument("--as-of", type=float, default=None, help="Bitemporal point-in-time epoch")
    p_recall.add_argument("--session", default=None, help="Active session ID (includes matching ephemeral working memory)")

    # procedures command
    p_proc = subparsers.add_parser("procedures", parents=[common], help="Manage Tier 3 Markdown runbooks")
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
    p_flush = subparsers.add_parser("flush-ephemeral", parents=[common], help="Flush Tier 4 ephemeral session scratchpad")
    p_flush.add_argument("--session", default=None, help="Session ID to flush")
    p_flush.add_argument("--workspace", default=None, help="Workspace filter")
    p_flush.add_argument("--promote-summary", default=None, help="Concise takeaway to promote to long-term memory")

    # inspect command
    p_inspect = subparsers.add_parser("inspect", parents=[common], help="Inspect active memories, lineage, and trust statuses")
    p_inspect.add_argument("--status", default=None, choices=[s.value for s in MemoryStatus], help="Filter by status")
    p_inspect.add_argument("--tier", default=None, choices=[t.value for t in StorageTier], help="Filter by tier")
    p_inspect.add_argument("--workspace", default=None, help="Filter by workspace ID")

    # forget command
    p_forget = subparsers.add_parser("forget", parents=[common], help="Verifiably forget a memory by ID or query text")
    p_forget.add_argument("target", help="Memory ID or text query to forget")
    p_forget.add_argument("--workspace", default=None, help="Workspace scope for query-based unlearning")

    # demo command
    subparsers.add_parser("demo", parents=[common], help="Run the full 30-session interactive judge demonstration")

    # benchmark command
    subparsers.add_parser("benchmark", parents=[common], help="Run the token & latency benchmark against full history")

    # setup command
    p_setup = subparsers.add_parser("setup", parents=[common], help="Auto-configure MCP integration for Cursor, Claude Code, Antigravity, Codex, and VS Code")
    p_setup.add_argument("--check", action="store_true", help="Inspect and check detected tools without modifying files")
    p_setup.add_argument("--print", dest="print_creds", action="store_true", help="Print copy-pasteable MCP credentials without modifying files")
    p_setup.add_argument("--tool", default="all", choices=["all", "cursor", "claude", "antigravity", "codex", "vscode"], help="Target specific tool")
    p_setup.add_argument("--use-wrapper", action="store_true", help="Wire tools using lethe.sh / lethe.cmd wrapper instead of direct CLI/Python path")

    # ui command
    p_ui = subparsers.add_parser("ui", parents=[common], help="Launch the Lethe real-time developer dashboard")
    p_ui.add_argument("--host", default="127.0.0.1", help="Dashboard host interface")
    p_ui.add_argument("--port", type=int, default=8765, help="Dashboard port")
    p_ui.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")

    args = parser.parse_args()

    dispatch = {
        "mcp": cmd_mcp,
        "creds": cmd_creds,
        "health": cmd_health,
        "store": cmd_store,
        "recall": cmd_recall,
        "procedures": cmd_procedures,
        "flush-ephemeral": cmd_flush_ephemeral,
        "inspect": cmd_inspect,
        "forget": cmd_forget,
        "demo": cmd_demo,
        "benchmark": cmd_benchmark,
        "setup": cmd_setup,
        "ui": cmd_ui,
    }
    handler = dispatch.get(args.subcommand)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
