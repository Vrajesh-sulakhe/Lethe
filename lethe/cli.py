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
    install_global_cli,
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
    print(f" LETHE MEMORY INSPECTOR - Total Records: {len(memories)}")
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

    code_anchor = None
    if getattr(args, "code_file", None):
        from lethe.models import CodeAnchor
        code_anchor = CodeAnchor(
            file_path=args.code_file,
            symbol_name=getattr(args, "code_symbol", None),
            git_sha=getattr(args, "code_sha", None),
        )

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
            code_anchor=code_anchor,
            metadata=metadata,
        )
    output_dict = {
        "action": "STORED",
        "id": node.id,
        "status": node.status.value,
        "tier": node.tier.value,
        "trust_level": node.trust_level,
        "workspace_id": node.workspace_id,
    }
    if node.code_anchor:
        output_dict["code_anchor"] = node.code_anchor.model_dump()
    print(json.dumps(output_dict, indent=2))


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
    if res.drift_warnings:
        print("\nDrift Warnings:")
        for w in res.drift_warnings:
            print(f"  - {w}")


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
        print(" Status: SUCCESS - 100% permanently eradicated.")
    else:
        print(" Status: NOT FOUND - zero records modified.")


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
        print(f" [OK] {r['tool'].upper():<12} -> {r['path']}")

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


def cmd_install_cli(args):
    res = install_global_cli(
        bin_dir=getattr(args, "bin_dir", None),
        update_path=not getattr(args, "no_path", False),
        dry_run=getattr(args, "dry_run", False),
    )
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
        return

    print("=" * 80)
    print(" LETHE GLOBAL CLI INSTALLER")
    print("=" * 80)
    print(f" Target Bin Dir: {res['bin_dir']}")
    print(f" Python Runtime: {res['python_executable']}")
    for item in res["installed"]:
        print(f" [OK] {item['command']:<10} -> {item['path']} ({item['mode']})")
    if res["platform"] == "win32":
        if res["path_updated"]:
            print(f"\n [OK] Added {res['bin_dir']} to Windows User PATH.")
            print(" Open a new PowerShell or CMD window and run 'lethe' to open the menu.")
        elif res["on_path"]:
            print("\n [OK] Target bin directory is already on PATH. Run 'lethe' anywhere.")
        else:
            print(f"\n Add {res['bin_dir']} to your Windows PATH to run 'lethe' from any folder.")
    else:
        if not res["on_path"]:
            print(f"\n Add this line to your shell profile (~/.zshrc or ~/.bashrc):")
            print(f'   export PATH="{res["bin_dir"]}:$PATH"')
        else:
            print("\n [OK] Target bin directory is already on PATH. Run 'lethe' anywhere.")
    print("=" * 80)


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
        print(f" LETHE HEALTH CHECK - Status: {health['status'].upper()}")
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


def cmd_trust(args):
    with _open_memory(args) as memory:
        if getattr(args, "list", False):
            trusted = memory.security.list_trusted_workspaces()
            untrusted = memory.security.list_untrusted_workspaces()
            print("=" * 80)
            print(" LETHE WORKSPACE TRUST MANAGER")
            print("=" * 80)
            print(f" Trusted Workspaces ({len(trusted)}):")
            for ws in trusted:
                print(f"   [+] {ws}")
            print(f" Untrusted Workspaces ({len(untrusted)}):")
            for ws in untrusted:
                print(f"   [-] {ws}")
            print("=" * 80)
            return

        if not args.workspace_id:
            print("Error: Specify a workspace_id or use --list.", file=sys.stderr)
            sys.exit(1)

        memory.security.trust_workspace(args.workspace_id)
        print(f"Workspace '{args.workspace_id}' is now TRUSTED (cross-repo global access enabled).")


def cmd_untrust(args):
    if not args.workspace_id:
        print("Error: Specify a workspace_id.", file=sys.stderr)
        sys.exit(1)
    with _open_memory(args) as memory:
        memory.security.untrust_workspace(args.workspace_id)
        print(f"Workspace '{args.workspace_id}' is now UNTRUSTED (isolated to private sandbox).")


def cmd_dream(args):
    with _open_memory(args, workspace_id=args.workspace) as memory:
        clusters = memory.get_dream_candidates(
            category=args.category,
            workspace_id=args.workspace,
            min_cluster_size=args.min_size,
        )
    print("=" * 80)
    print(f" LETHE MEMORY SYNTHESIS (DREAMING) - Candidate Clusters: {len(clusters)}")
    print("=" * 80)
    if not clusters:
        print(" [No memory clusters found meeting consolidation threshold.]")
        return
    for c in clusters:
        print(f" Cluster [{c['cluster_id']}] | Category: {c['category']} | Size: {c['size']} memories")
        print(f" Topic: {c['topic']}")
        print(f" Parent IDs: {c['parent_ids']}")
        for m in c["candidate_memories"][:3]:
            print(f"   - [#{m['id'][:6]}] {m['content'][:70]}")
        if len(c["candidate_memories"]) > 3:
            print(f"   ... and {len(c['candidate_memories']) - 3} more")
        print("-" * 80)


def cmd_consolidate(args):
    parent_ids = [p.strip() for p in args.parents.split(",") if p.strip()]
    if not parent_ids:
        print("Error: Specify at least one parent ID via --parents.", file=sys.stderr)
        sys.exit(1)

    with _open_memory(args, workspace_id=args.workspace) as memory:
        node = memory.consolidate(
            parent_ids=parent_ids,
            summary_content=args.content,
            category=args.category,
            target_tier=StorageTier(args.tier),
            mark_parents_historical=not args.keep_active,
            workspace_id=args.workspace,
            scope=args.scope,
        )
    print(
        json.dumps(
            {
                "action": "CONSOLIDATED",
                "id": node.id,
                "category": node.category,
                "tier": node.tier.value,
                "parent_ids": node.parent_ids,
                "parents_marked_historical": not args.keep_active,
            },
            indent=2,
        )
    )


def cmd_ledger(args):
    with _open_memory(args) as memory:
        if getattr(args, "verify", False):
            valid, reason = memory.ledger.verify_chain()
            count = memory.ledger.count_blocks()
            print("=" * 80)
            print(" LETHE CRYPTOGRAPHIC LEDGER VERIFICATION")
            print("=" * 80)
            print(f" Total Immutable Blocks: {count}")
            print(f" Cryptographic Status:  {'VALID (Unbroken Hash Chain)' if valid else 'CORRUPT'}")
            if not valid:
                print(f" Error Detail:          {reason}")
            print("=" * 80)
            if not valid:
                sys.exit(1)
            return

        blocks = memory.ledger.list_blocks(limit=args.limit)
        print("=" * 80)
        print(f" LETHE APPEND-ONLY CRYPTOGRAPHIC LEDGER - Showing {len(blocks)} Blocks")
        print("=" * 80)
        if not blocks:
            print(" [No ledger transactions recorded yet.]")
            return
        for b in blocks:
            print(f" Block #{b.sequence:04d} | Action: {b.action:10} | Node: #{b.node_id[:8]} | Time: {b.timestamp}")
            print(f"   Prev Hash:    {b.prev_hash[:20]}...{b.prev_hash[-12:]}")
            print(f"   Block Hash:   {b.block_hash[:20]}...{b.block_hash[-12:]}")
            print(f"   Payload Hash: {b.payload_hash[:20]}...{b.payload_hash[-12:]}")
            if b.metadata:
                print(f"   Metadata:     {b.metadata}")
            print("-" * 80)


def cmd_doctor(args):
    from lethe.eval.doctor import LetheDoctor

    with _open_memory(args) as memory:
        doc = LetheDoctor(memory)
        res = doc.run_all_checks()

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        status_label = "HEALTHY" if res["healthy"] else "ATTENTION NEEDED"
        print("=" * 80)
        print(f" LETHE ENTERPRISE SYSTEM DIAGNOSTICS (DOCTOR) - Status: {status_label}")
        print("=" * 80)
        db = res["checks"]["database"]
        print(f" SQLite Storage:         [{db['status']}] WAL Mode: {db['journal_mode'].upper()}, Schema: v{db['schema_version']}, Integrity: {db['integrity'].upper()}")

        ledger = res["checks"]["ledger"]
        print(f" Cryptographic Ledger:   [{ledger['status']}] Total Blocks: {ledger['total_blocks']}")

        tomb = res["checks"]["tombstones"]
        leak_msg = f"{tomb['tombstone_count']} Tombstones audited ({tomb['content_leaks']} content, {tomb['vector_leaks']} vector, {tomb['salt_leaks']} salt leaks)"
        print(f" Zero-Leakage Redaction: [{tomb['status']}] {leak_msg}")

        conf = res["checks"]["conflicts"]
        print(f" Conflict Invariants:    [{conf['status']}] {conf['count']} unresolved contradictions")

        drift = res["checks"]["code_drift"]
        print(f" Decision-to-Code Drift: [{drift['status']}] {drift['total_anchors']} Anchors checked ({drift['drifted_count']} drifted)")
        if drift["drifted_memories"]:
            for d in drift["drifted_memories"]:
                print(f"   [!] #{d['id'][:6]} ({d['file_path']}): {d['reason']}")

        sec = res["checks"]["security"]
        print(f" Workspace Security:     [{sec['status']}] {sec['trusted_count']} Trusted, {sec['untrusted_count']} Untrusted Workspaces")

        mcp_res = res["checks"].get("mcp_tools", {})
        if mcp_res.get("status") == "PASS":
            tools_list = ", ".join(mcp_res.get("connected_tools", [])) or "None yet"
            print(f" MCP Platform Wiring:    [PASS] {mcp_res['connected_count']}/{mcp_res['detected_count']} tools connected ({tools_list})")

        print("=" * 80)
        if res["issues"]:
            print(" Diagnostic Warnings / Issues:")
            for issue in res["issues"]:
                print(f"  - {issue}")
            print("=" * 80)

    if getattr(args, "strict", False) and not res["healthy"]:
        sys.exit(1)


def cmd_lint(args):
    from lethe.eval.doctor import LetheDoctor

    with _open_memory(args) as memory:
        doc = LetheDoctor(memory)
        res = doc.run_lint()

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        print("=" * 80)
        print(f" LETHE MEMORY LINTER - Status: {'CLEAN' if res['clean'] else 'WARNINGS DETECTED'}")
        print("=" * 80)
        print(f" Total Memories Audited: {res['total_memories']}")
        print(f" Duplicate Memories:     {res['duplicates']}")
        print(f" Code-Drifted Memories:  {res['drifted']}")
        print(f" Unresolved Conflicts:   {res['conflicts']}")
        print("=" * 80)
        if res["warnings"]:
            for w in res["warnings"]:
                print(f"  [WARN] {w}")
            print("=" * 80)

    if getattr(args, "strict", False) and not res["clean"]:
        sys.exit(1)


def _configure_console_encoding() -> None:
    """Ensures stdout/stderr never raise UnicodeEncodeError on Windows cp1252/cp437 consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main():
    _configure_console_encoding()
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
    p_store.add_argument("--code-file", default=None, help="File path to anchor this architectural decision to")
    p_store.add_argument("--code-symbol", default=None, help="Function or class symbol name anchored in the file")
    p_store.add_argument("--code-sha", default=None, help="Explicit Git commit SHA anchor (defaults to HEAD if available)")

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

    # install-cli command
    p_inst = subparsers.add_parser("install-cli", help="Install global 'lethe' and 'lethe-mcp' CLI commands on PATH (Mac/Linux/Windows)")
    p_inst.add_argument("--bin-dir", default=None, help="Target bin directory (default: ~/.local/bin)")
    p_inst.add_argument("--no-path", action="store_true", help="Do not modify Windows User PATH")
    p_inst.add_argument("--dry-run", action="store_true", help="Inspect installation plan without writing files")
    p_inst.add_argument("--json", action="store_true", help="Output result as JSON")

    # trust command
    p_trust = subparsers.add_parser("trust", parents=[common], help="Grant trusted cross-repo status to a workspace")
    p_trust.add_argument("workspace_id", nargs="?", default=None, help="Workspace ID to trust")
    p_trust.add_argument("--list", action="store_true", help="List all trusted and untrusted workspaces")

    # untrust command
    p_untrust = subparsers.add_parser("untrust", parents=[common], help="Lock workspace into an untrusted isolated sandbox")
    p_untrust.add_argument("workspace_id", help="Workspace ID to untrust")

    # dream command
    p_dream = subparsers.add_parser("dream", parents=[common], help="Identify clusters of related atomic memories ripe for synthesis")
    p_dream.add_argument("--category", default=None, help="Filter by memory category")
    p_dream.add_argument("--workspace", default=None, help="Workspace filter")
    p_dream.add_argument("--min-size", type=int, default=2, help="Minimum cluster size")

    # consolidate command
    p_cons = subparsers.add_parser("consolidate", parents=[common], help="Synthesize multiple memories into a single node with Lineage DAG tracking")
    p_cons.add_argument("--parents", required=True, help="Comma-separated parent memory IDs")
    p_cons.add_argument("--content", required=True, help="Synthesized summary content")
    p_cons.add_argument("--category", default=None, help="Consolidated memory category")
    p_cons.add_argument("--tier", default="semantic", choices=[t.value for t in StorageTier], help="Target storage tier")
    p_cons.add_argument("--keep-active", action="store_true", help="Do not mark parent memories as HISTORICAL")
    p_cons.add_argument("--workspace", default=None, help="Workspace ID")
    p_cons.add_argument("--scope", default="project", choices=["project", "global"], help="Scope")

    # ledger command
    p_ledger = subparsers.add_parser("ledger", parents=[common], help="Inspect or verify the append-only cryptographic redaction ledger")
    p_ledger.add_argument("--verify", action="store_true", help="Mathematically verify unbroken cryptographic hash chain")
    p_ledger.add_argument("--limit", type=int, default=20, help="Number of recent blocks to display")

    # doctor command
    p_doc = subparsers.add_parser("doctor", parents=[common], help="Run enterprise system diagnostics across WAL, ledger, drift, and security")
    p_doc.add_argument("--strict", action="store_true", help="Exit with non-zero status if any warning or failure exists")
    p_doc.add_argument("--json", action="store_true", help="Output diagnostic report as structured JSON for CI/CD")

    # lint command
    p_lint = subparsers.add_parser("lint", parents=[common], help="Lint memory vault for duplicate memories, drift, and unresolved conflicts")
    p_lint.add_argument("--strict", action="store_true", help="Exit with non-zero status if any lint warning exists")
    p_lint.add_argument("--json", action="store_true", help="Output lint report as structured JSON")

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
        "install-cli": cmd_install_cli,
        "trust": cmd_trust,
        "untrust": cmd_untrust,
        "dream": cmd_dream,
        "consolidate": cmd_consolidate,
        "ledger": cmd_ledger,
        "doctor": cmd_doctor,
        "lint": cmd_lint,
    }
    handler = dispatch.get(args.subcommand)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
