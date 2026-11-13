import argparse
import json
import tempfile
from pathlib import Path
import pytest

from lethe.core.engine import LetheMemory
from lethe.eval.doctor import LetheDoctor
from lethe.models import CodeAnchor, MemoryStatus, StorageTier


def test_doctor_clean_environment():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            mem.store("Production service runs on Python 3.12.", category="python")
            mem.store("Redis cache TTL is 3600 seconds.", category="cache")

            doc = LetheDoctor(mem)
            report = doc.run_all_checks(skip_mcp_check=True)

            assert report["healthy"] is True
            assert len(report["issues"]) == 0
            assert report["checks"]["database"]["status"] == "PASS"
            assert report["checks"]["ledger"]["status"] == "PASS"
            assert report["checks"]["tombstones"]["status"] == "PASS"
            assert report["checks"]["code_drift"]["drifted_count"] == 0
            assert report["checks"]["conflicts"]["count"] == 0
        finally:
            mem.close()


def test_doctor_detects_code_drift():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "worker.py"
        source_file.write_text("def start_worker(): pass\n", encoding="utf-8")

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        try:
            mem.store(
                "Worker startup sequence is start_worker.",
                category="worker",
                code_anchor=CodeAnchor(file_path=str(source_file), symbol_name="start_worker"),
            )

            # Mutate file (remove symbol)
            source_file.write_text("def run_worker(): pass\n", encoding="utf-8")

            doc = LetheDoctor(mem)
            report = doc.run_all_checks(skip_mcp_check=True)

            assert report["healthy"] is False
            assert report["checks"]["code_drift"]["drifted_count"] == 1
            assert any("drift" in issue.lower() for issue in report["issues"])
        finally:
            mem.close()


def test_doctor_detects_conflicts():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            t_interval = 1000.0
            # User fact at t_interval
            mem.store(
                "Primary database port is 5432.",
                category="db",
                tier=StorageTier.RELATIONAL,
                trust_level=1.0,
                valid_from=t_interval,
                metadata={"entity": "db", "attribute": "port", "value": "5432"},
            )
            # Conflicting claim for the same interval
            mem.store(
                "Primary database port is 3306.",
                category="db",
                tier=StorageTier.RELATIONAL,
                trust_level=0.8,
                valid_from=t_interval,
                metadata={"entity": "db", "attribute": "port", "value": "3306"},
            )

            doc = LetheDoctor(mem)
            report = doc.run_all_checks(skip_mcp_check=True)

            assert report["checks"]["conflicts"]["count"] >= 1
            assert any("conflict" in issue.lower() or "contradiction" in issue.lower() for issue in report["issues"])
        finally:
            mem.close()


def test_doctor_detects_tombstone_leakage():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            node = mem.store("Secret token 12345", category="secret")
            # Manually corrupt status to tombstoned without redacting content
            cursor = mem.db.conn.cursor()
            cursor.execute("UPDATE memories SET status = 'tombstoned' WHERE id = ?", (node.id,))
            mem.db.conn.commit()

            doc = LetheDoctor(mem)
            tomb_check = doc._check_tombstones()

            assert tomb_check["status"] == "FAIL"
            assert tomb_check["content_leaks"] == 1
        finally:
            mem.close()


def test_doctor_lint():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            # Store exact duplicates in same category
            mem.store("Server runs on port 8080.", category="server")
            mem.store("Server runs on port 8080.", category="server")

            doc = LetheDoctor(mem)
            lint_report = doc.run_lint()

            assert lint_report["clean"] is False
            assert lint_report["duplicates"] >= 1
            assert any("Duplicate memory" in w for w in lint_report["warnings"])
        finally:
            mem.close()


def test_cli_doctor_and_lint_integration():
    from lethe.cli import cmd_store, cmd_doctor, cmd_lint

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        procs_dir = str(Path(tmp_dir) / "procs")

        cmd_store(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            content="Production deployment uses Kubernetes v1.30.",
            category="k8s",
            tier="semantic",
            source="user_explicit",
            session="cli",
            workspace=None,
            scope="project",
            valid_from=None,
            entity=None,
            attribute=None,
            value=None,
            code_file=None,
            code_symbol=None,
            code_sha=None,
        ))

        # Run doctor text output
        cmd_doctor(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            json=False,
            strict=False,
        ))

        # Run doctor JSON output
        cmd_doctor(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            json=True,
            strict=False,
        ))

        # Run lint text output
        cmd_lint(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            json=False,
            strict=False,
        ))

        # Run lint JSON output
        cmd_lint(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            json=True,
            strict=False,
        ))
