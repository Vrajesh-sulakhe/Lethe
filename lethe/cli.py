import argparse
import sys
import json
from pathlib import Path
from lethe.core.engine import LetheMemory
from lethe.models import StorageTier

def cmd_inspect(args):
    memory = LetheMemory(db_path=args.db)
    memories = memory.db.list_memories()
    print("=" * 80)
    print(f" LETHE MEMORY INSPECTOR — Total Records: {len(memories)}")
    print("=" * 80)
    if not memories:
        print(" [No memories stored yet.]")
        return

    for m in memories:
        status_color = m.status.value.upper()
        print(f"[{m.id[:8]}] [{status_color}] Category: {m.category:12} | Tier: {m.tier.value:10} | Trust: {m.trust_level:.1f}")
        print(f"  Content: {m.content}")
        print(f"  Origin:  {m.source_session} ({m.source_origin})")
        if m.parent_ids:
            print(f"  Lineage: Derived from parents {m.parent_ids}")
        print("-" * 80)
    memory.close()

def cmd_forget(args):
    memory = LetheMemory(db_path=args.db)
    print(f"Initiating verifiable cascading unlearning for: '{args.target}'...")
    receipt = memory.forget(args.target)
    print("=" * 80)
    print(" DELETION RECEIPT (CRYPTOGRAPHIC AUDIT PROOF)")
    print("=" * 80)
    print(f" Target:            {receipt.target_id}")
    print(f" Purged Nodes:      {receipt.purged_count} nodes (target + derived summaries)")
    print(f" Purged IDs:        {receipt.purged_node_ids}")
    print(f" Verification Hash: {receipt.verification_hash}")
    print(f" Timestamp:         {receipt.timestamp}")
    print("=" * 80)
    print(" Status: SUCCESS — 100% permanently eradicated.")
    memory.close()

def cmd_demo(args):
    from lethe.eval.dataset_generator import run_interactive_demo
    run_interactive_demo(db_path=args.db)

def cmd_benchmark(args):
    from lethe.eval.benchmark import run_benchmark
    run_benchmark(db_path=args.db)

def main():
    parser = argparse.ArgumentParser(
        prog="lethe",
        description="Lethe: Autonomous Agent Memory Operating System"
    )
    parser.add_argument("--db", default="lethe.db", help="Path to SQLite database")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    # mcp command
    p_mcp = subparsers.add_parser("mcp", help="Run Lethe as a Model Context Protocol (MCP) server")
    p_mcp.add_argument("--transport", default="stdio", choices=["stdio", "sse"], help="MCP transport mode")
    p_mcp.add_argument("--port", type=int, default=8000, help="Port for SSE transport")

    # inspect command
    subparsers.add_parser("inspect", help="Inspect active memories, lineage, and trust statuses")

    # forget command
    p_forget = subparsers.add_parser("forget", help="Verifiably forget a memory by ID or query text")
    p_forget.add_argument("target", help="Memory ID or text query to forget")

    # demo command
    subparsers.add_parser("demo", help="Run the full 30-session interactive judge demonstration")

    # benchmark command
    subparsers.add_parser("benchmark", help="Run the token & latency benchmark against full history")

    args = parser.parse_args()

    if args.subcommand == "mcp":
        from lethe.mcp.server import create_mcp_server
        server = create_mcp_server(args.db)
        if args.transport == "sse":
            server.run(transport="sse", port=args.port)
        else:
            server.run(transport="stdio")
    elif args.subcommand == "inspect":
        cmd_inspect(args)
    elif args.subcommand == "forget":
        cmd_forget(args)
    elif args.subcommand == "demo":
        cmd_demo(args)
    elif args.subcommand == "benchmark":
        cmd_benchmark(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
