import argparse
import json
import tempfile
from pathlib import Path
import pytest

from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus, StorageTier


def test_find_dream_candidates():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id="infra_project")
        try:
            # Store 4 related atomic docker memories
            mem.store("Docker build uses BuildKit multi-stage cache mounts.", category="docker", tier=StorageTier.SEMANTIC)
            mem.store("Pin Docker base image to alpine:3.19 for reproducible builds.", category="docker", tier=StorageTier.SEMANTIC)
            mem.store("Docker Compose defines redis and postgres containers with healthchecks.", category="docker", tier=StorageTier.SEMANTIC)
            mem.store("Set Docker daemon log driver to local with max-size 50m.", category="docker", tier=StorageTier.SEMANTIC)

            # Store 1 isolated memory
            mem.store("User prefers listening to jazz while debugging.", category="music", tier=StorageTier.SEMANTIC)

            candidates = mem.get_dream_candidates(category="docker", min_cluster_size=2)
            assert len(candidates) >= 1

            top_cluster = candidates[0]
            assert top_cluster["category"] == "docker"
            assert top_cluster["size"] == 4
            assert len(top_cluster["parent_ids"]) == 4
            assert top_cluster["suggested_action"] == "consolidate"
        finally:
            mem.close()


def test_consolidate_and_cascading_unlearning():
    """
    Core invariant: Consolidate 4 parent memories into 1 summary node via lethe_consolidate.
    Call forget() on 1 root parent.
    Assert summary node is correctly purged via Lineage DAG cascading unlearning!
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id="devops")
        try:
            # 1. Store 4 micro-decisions
            p1 = mem.store("Root decision: API gateway uses secret token ABC-9988 for internal auth.", category="security")
            p2 = mem.store("Internal services validate secret token ABC-9988 in X-Internal-Token header.", category="security")
            p3 = mem.store("Token ABC-9988 rotates quarterly every 90 days.", category="security")
            p4 = mem.store("Token ABC-9988 failure triggers automated Slack alert.", category="security")

            parent_ids = [p1.id, p2.id, p3.id, p4.id]

            # 2. Consolidate into 1 synthesized summary runbook node
            summary = mem.consolidate(
                parent_ids=parent_ids,
                summary_content="Consolidated Security SOP: Internal API traffic authenticates via quarterly token ABC-9988 in X-Internal-Token header, alerting Slack on failure.",
                category="security",
                target_tier=StorageTier.SEMANTIC,
                mark_parents_historical=True,
            )

            assert summary.id is not None
            assert len(summary.parent_ids) == 4
            assert summary.source_origin == "agent_synthesis"

            # 3. Verify parents are now HISTORICAL, leaving token budget clean
            for pid in parent_ids:
                node = mem.db.get_memory(pid)
                assert node.status == MemoryStatus.HISTORICAL

            # 4. Verify recall returns the consolidated summary node cleanly
            res_before = mem.recall("Internal API token authentication Slack")
            assert res_before.is_abstention is False
            assert "ABC-9988" in res_before.context_text
            # The top memory recalled is the consolidated summary node
            assert any(m.id == summary.id for m in res_before.memories)

            # 5. CASCADING UNLEARNING TRIGGER: Forget the root parent fact (p1)
            receipt = mem.forget(p1.id)
            assert receipt.purged_count >= 2
            assert p1.id in receipt.purged_node_ids
            # The synthesized summary node MUST be purged through the Lineage DAG!
            assert summary.id in receipt.purged_node_ids

            # 6. Verify summary node status is TOMBSTONED and content is scrubbed
            purged_summary = mem.db.get_memory(summary.id)
            assert purged_summary.status == MemoryStatus.TOMBSTONED
            assert purged_summary.content == "[PURGED_VIA_UNLEARNING]"
            assert "ABC-9988" not in purged_summary.content

            # 7. Verify embeddings for summary node were physically eradicated
            assert mem.db.get_embedding(summary.id) is None

            # 8. Verify recall now honestly abstains and does NOT leak ABC-9988
            res_after = mem.recall("Internal API token authentication Slack")
            assert "ABC-9988" not in res_after.context_text
            assert res_after.is_abstention is True
        finally:
            mem.close()


def test_mcp_dream_and_consolidate_tools():
    from lethe.mcp.server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        server = create_mcp_server(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            t_store = server._tool_manager._tools["lethe_store"].fn
            t_dream = server._tool_manager._tools["lethe_dream_candidates"].fn
            t_cons = server._tool_manager._tools["lethe_consolidate"].fn

            # Store memories
            res_m1 = json.loads(t_store(content="Kubernetes pod memory limit set to 512Mi.", category="k8s"))
            res_m2 = json.loads(t_store(content="Kubernetes pod CPU limit set to 500m.", category="k8s"))

            # Scan dream candidates
            dream_res = json.loads(t_dream(category="k8s"))
            assert dream_res["total_clusters"] == 1
            cluster = dream_res["clusters"][0]
            assert cluster["size"] == 2

            # Consolidate via tool
            cons_res = json.loads(
                t_cons(
                    parent_ids=[res_m1["memory_id"], res_m2["memory_id"]],
                    summary_content="Kubernetes pod resource bounds: 500m CPU and 512Mi memory.",
                    category="k8s",
                )
            )
            assert cons_res["status"] == "CONSOLIDATED"
            assert cons_res["parent_count"] == 2
        finally:
            server._lethe_memory.close()


def test_cli_dream_and_consolidate():
    from lethe.cli import cmd_store, cmd_dream, cmd_consolidate

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        procs_dir = str(Path(tmp_dir) / "procs")

        # Store two facts
        cmd_store(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            content="Postgres max_connections set to 150.",
            category="postgres",
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
        cmd_store(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            content="Postgres shared_buffers set to 256MB.",
            category="postgres",
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

        # Dream
        cmd_dream(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            category="postgres",
            workspace=None,
            min_size=2,
        ))

        mem = LetheMemory(db_path=db_path, procedures_dir=procs_dir)
        parents = [m.id for m in mem.db.list_memories(category="postgres")]
        mem.close()

        # Consolidate via CLI
        cmd_consolidate(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            parents=",".join(parents),
            content="Postgres sizing: 150 max connections, 256MB shared buffers.",
            category="postgres",
            tier="semantic",
            keep_active=False,
            workspace=None,
            scope="project",
        ))
