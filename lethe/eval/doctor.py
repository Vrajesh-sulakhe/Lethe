import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from lethe.core.drift import check_code_drift
from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus


class LetheDoctor:
    """
    Enterprise system diagnostics and integrity verification engine.
    Audits SQLite WAL storage, Merkle chain continuity, zero-leakage redaction,
    epistemic conflict invariants, decision-to-code drift, and MCP wiring.
    """

    def __init__(self, memory: LetheMemory):
        self.memory = memory
        self.db = memory.db

    def run_all_checks(self, skip_mcp_check: bool = False) -> Dict[str, Any]:
        """Runs the complete diagnostic suite and returns a structured report."""
        checks: Dict[str, Any] = {}
        issues: List[str] = []

        # 1. Database Integrity & WAL Health
        db_check = self._check_database()
        checks["database"] = db_check
        if db_check["status"] != "PASS":
            issues.append(f"Database error: {db_check.get('error')}")

        # 2. Cryptographic Ledger Chain
        ledger_check = self._check_ledger()
        checks["ledger"] = ledger_check
        if ledger_check["status"] == "FAIL":
            issues.append(f"Ledger error: {ledger_check.get('error')}")
        elif ledger_check["status"] == "WARN":
            issues.append(f"Ledger warning: {ledger_check.get('error')}")

        # 3. Tombstone Zero-Leakage Audit
        tombstone_check = self._check_tombstones()
        checks["tombstones"] = tombstone_check
        if tombstone_check["status"] != "PASS":
            issues.append(f"Tombstone leakage: {tombstone_check.get('error')}")

        # 4. Epistemic Conflict Invariants
        conflict_check = self._check_conflicts()
        checks["conflicts"] = conflict_check
        if conflict_check["count"] > 0:
            issues.append(f"Unresolved contradictions: {conflict_check['count']} conflicts detected")

        # 5. Decision-to-Code Drift
        drift_check = self._check_code_drift()
        checks["code_drift"] = drift_check
        if drift_check["drifted_count"] > 0:
            issues.append(f"Decision-to-code drift: {drift_check['drifted_count']} anchored decisions have drifted")

        # 6. Workspace Security Trust Boundaries
        security_check = self._check_security()
        checks["security"] = security_check

        # 7. MCP Platform Integrations (optional)
        if not skip_mcp_check:
            try:
                from lethe.platform.setup import detect_installed_tools
                tools = detect_installed_tools()
                connected = [k for k, v in tools.items() if v.get("is_configured")]
                checks["mcp_tools"] = {
                    "status": "PASS",
                    "detected_count": len(tools),
                    "connected_count": len(connected),
                    "connected_tools": connected,
                }
            except Exception as e:
                checks["mcp_tools"] = {"status": "SKIP", "error": str(e)}
        else:
            checks["mcp_tools"] = {"status": "SKIPPED"}

        is_healthy = (
            db_check["status"] == "PASS"
            and ledger_check["status"] in ("PASS", "WARN")
            and tombstone_check["status"] == "PASS"
            and drift_check["drifted_count"] == 0
            and conflict_check["count"] == 0
        )

        return {
            "healthy": is_healthy,
            "checks": checks,
            "issues": issues,
            "summary": {
                "total_memories": db_check.get("total_memories", 0),
                "ledger_blocks": ledger_check.get("total_blocks", 0),
                "unresolved_conflicts": conflict_check["count"],
                "drifted_anchors": drift_check["drifted_count"],
                "tombstoned_records": tombstone_check["tombstone_count"],
            },
        }

    def run_lint(self) -> Dict[str, Any]:
        """Lints memories for duplicate records, code drift, and unresolved conflicts."""
        all_memories = self.db.list_memories(status=MemoryStatus.ACTIVE)
        warnings: List[str] = []

        # 1. Duplicate / identical content check within categories
        from collections import defaultdict
        by_cat = defaultdict(list)
        for m in all_memories:
            by_cat[m.category].append(m)

        duplicate_count = 0
        for cat, items in by_cat.items():
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    if items[i].content.strip().lower() == items[j].content.strip().lower():
                        duplicate_count += 1
                        warnings.append(f"Duplicate memory in '{cat}': #{items[i].id[:6]} and #{items[j].id[:6]}")

        # 2. Code drift check
        drift_check = self._check_code_drift()
        for d in drift_check["drifted_memories"]:
            warnings.append(f"Code drift in #{d['id'][:6]}: {d['reason']}")

        # 3. Conflict check
        conflict_check = self._check_conflicts()
        for c in conflict_check["conflicts"]:
            warnings.append(f"Unresolved conflict in #{c['id'][:6]} ({c['category']}): '{c['content']}'")

        return {
            "clean": len(warnings) == 0,
            "warnings": warnings,
            "total_memories": len(all_memories),
            "duplicates": duplicate_count,
            "drifted": drift_check["drifted_count"],
            "conflicts": conflict_check["count"],
        }

    def _check_database(self) -> Dict[str, Any]:
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("PRAGMA integrity_check;")
            integrity_row = cursor.fetchone()
            integrity = integrity_row[0] if integrity_row else "unknown"

            cursor.execute("PRAGMA foreign_key_check;")
            fk_violations = cursor.fetchall()

            cursor.execute("PRAGMA journal_mode;")
            jm_row = cursor.fetchone()
            journal_mode = jm_row[0] if jm_row else "unknown"

            schema_ver = self.db.get_schema_version()
            cursor.execute("SELECT COUNT(*) FROM memories")
            total_memories = int(cursor.fetchone()[0])

            status = "PASS" if integrity == "ok" and not fk_violations else "FAIL"
            err = None
            if integrity != "ok":
                err = f"Integrity check failed: {integrity}"
            elif fk_violations:
                err = f"Foreign key violations: {len(fk_violations)} broken keys"

            return {
                "status": status,
                "integrity": integrity,
                "journal_mode": journal_mode,
                "schema_version": schema_ver,
                "total_memories": total_memories,
                "error": err,
            }

    def _check_ledger(self) -> Dict[str, Any]:
        valid, reason = self.memory.ledger.verify_chain()
        count = self.memory.ledger.count_blocks()
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories")
            mem_count = int(cursor.fetchone()[0])

        status = "PASS"
        if not valid:
            status = "FAIL"
        elif count == 0 and mem_count > 0:
            status = "WARN"
            reason = f"Ledger has 0 commitment blocks while database contains {mem_count} memory rows"

        return {
            "status": status,
            "total_blocks": count,
            "error": reason,
        }

    def _check_tombstones(self) -> Dict[str, Any]:
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT id, content FROM memories WHERE status = 'tombstoned'")
            tombstones = cursor.fetchall()

            content_leaks = 0
            vector_leaks = 0
            salt_leaks = 0

            for r in tombstones:
                nid = r["id"]
                content = r["content"]
                if content != "[PURGED_VIA_UNLEARNING]":
                    content_leaks += 1

                cursor.execute("SELECT 1 FROM embeddings WHERE memory_id = ?", (nid,))
                if cursor.fetchone():
                    vector_leaks += 1

                cursor.execute("SELECT 1 FROM ledger_salts WHERE node_id = ?", (nid,))
                if cursor.fetchone():
                    salt_leaks += 1

            has_leaks = content_leaks > 0 or vector_leaks > 0 or salt_leaks > 0
            err = None
            if has_leaks:
                err = f"Leaks detected: {content_leaks} content leaks, {vector_leaks} vector leaks, {salt_leaks} salt leaks"

            return {
                "status": "FAIL" if has_leaks else "PASS",
                "tombstone_count": len(tombstones),
                "content_leaks": content_leaks,
                "vector_leaks": vector_leaks,
                "salt_leaks": salt_leaks,
                "error": err,
            }

    def _check_conflicts(self) -> Dict[str, Any]:
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT id, content, category FROM memories WHERE status = 'conflict'")
            rows = cursor.fetchall()
            return {
                "status": "PASS" if not rows else "WARN",
                "count": len(rows),
                "conflicts": [{"id": r["id"], "content": r["content"], "category": r["category"]} for r in rows],
            }

    def _check_code_drift(self) -> Dict[str, Any]:
        all_memories = self.db.list_memories(status=MemoryStatus.ACTIVE)
        drifted: List[Dict[str, Any]] = []

        for m in all_memories:
            if m.code_anchor:
                is_drifted, reason = check_code_drift(m.code_anchor, workspace_dir=m.workspace_id)
                if is_drifted:
                    drifted.append({
                        "id": m.id,
                        "file_path": m.code_anchor.file_path,
                        "symbol": m.code_anchor.symbol_name,
                        "reason": reason,
                    })

        return {
            "status": "PASS" if not drifted else "WARN",
            "total_anchors": sum(1 for m in all_memories if m.code_anchor),
            "drifted_count": len(drifted),
            "drifted_memories": drifted,
        }

    def _check_security(self) -> Dict[str, Any]:
        trusted = self.memory.security.list_trusted_workspaces()
        untrusted = self.memory.security.list_untrusted_workspaces()
        return {
            "status": "PASS",
            "trusted_count": len(trusted),
            "untrusted_count": len(untrusted),
            "trusted_workspaces": trusted,
            "untrusted_workspaces": untrusted,
        }
