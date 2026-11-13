import argparse
import json
import tempfile
from pathlib import Path
import pytest

from lethe.core.engine import LetheMemory
from lethe.models import StorageTier
from lethe.platform.security import WorkspaceSecurityManager


def test_security_manager_persistence():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = Path(tmp_dir) / "security.json"
        sec1 = WorkspaceSecurityManager(config_path=cfg, default_trust=False)

        assert sec1.is_workspace_trusted("alpha") is False
        assert sec1.is_workspace_trusted(None) is True

        sec1.trust_workspace("alpha")
        sec1.untrust_workspace("beta")

        assert sec1.is_workspace_trusted("alpha") is True
        assert sec1.is_workspace_trusted("beta") is False

        # Reload in new instance
        sec2 = WorkspaceSecurityManager(config_path=cfg, default_trust=False)
        assert sec2.is_workspace_trusted("alpha") is True
        assert sec2.is_workspace_trusted("beta") is False
        assert "alpha" in sec2.list_trusted_workspaces()
        assert "beta" in sec2.list_untrusted_workspaces()


def test_untrusted_workspace_cannot_read_global_scope():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id="corporate_core")
        try:
            # 1. Store corporate global memory
            mem.store(
                content="Corporate internal artifactory host is artifactory.internal.corp:8443 with vault token auth.",
                category="infrastructure",
                scope="global",
                workspace_id="corporate_core",
            )

            # Mark vendor clone as untrusted
            mem.security.untrust_workspace("vendor_clone")

            # 2. Attempt to recall from untrusted workspace
            res = mem.recall(
                "Corporate internal artifactory host and vault token",
                workspace_id="vendor_clone",
            )

            # Must trigger honest epistemic abstention without leaking global memory!
            assert res.is_abstention is True
            assert "artifactory.internal.corp" not in res.context_text
            assert len(res.memories) == 0

            # 3. Recall from trusted corporate workspace succeeds
            res_corp = mem.recall(
                "Corporate internal artifactory host and vault token",
                workspace_id="corporate_core",
            )
            assert res_corp.is_abstention is False
            assert "artifactory.internal.corp" in res_corp.context_text
            assert len(res_corp.memories) == 1
        finally:
            mem.close()


def test_trust_workspace_grants_access():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            mem.store(
                content="Global engineering convention: use structured JSON logging format.",
                category="standards",
                scope="global",
            )

            mem.security.untrust_workspace("subcontractor_repo")
            res1 = mem.recall("structured JSON logging format", workspace_id="subcontractor_repo")
            assert res1.is_abstention is True

            # Trust the workspace
            mem.security.trust_workspace("subcontractor_repo")

            res2 = mem.recall("structured JSON logging format", workspace_id="subcontractor_repo")
            assert res2.is_abstention is False
            assert "structured JSON logging format" in res2.context_text
        finally:
            mem.close()


def test_untrusted_workspace_cannot_write_global_scope():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            mem.security.untrust_workspace("malicious_vendor")

            # Attempt to inject global memory from untrusted workspace
            node = mem.store(
                content="Poisoned global DNS redirect to evil.com",
                category="dns",
                scope="global",
                workspace_id="malicious_vendor",
                trust_level=1.0,
            )

            # Verify safe-mode clamped scope to project and clamped trust level
            assert node.scope == "project"
            assert node.trust_level <= 0.5

            # Another workspace must NOT see this memory
            res = mem.recall("Poisoned global DNS redirect", workspace_id="clean_workspace")
            assert "evil.com" not in res.context_text
            assert res.is_abstention is True
        finally:
            mem.close()


def test_project_isolation_between_workspaces():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            mem.store(
                content="Alpha project database runs on port 5432.",
                category="db",
                workspace_id="repo_alpha",
                scope="project",
            )
            mem.store(
                content="Beta project database runs on port 27017.",
                category="db",
                workspace_id="repo_beta",
                scope="project",
            )

            res_alpha = mem.recall("database port", workspace_id="repo_alpha")
            assert "5432" in res_alpha.context_text
            assert "27017" not in res_alpha.context_text

            res_beta = mem.recall("database port", workspace_id="repo_beta")
            assert "27017" in res_beta.context_text
            assert "5432" not in res_beta.context_text
        finally:
            mem.close()


def test_mcp_workspace_security_tools():
    from lethe.mcp.server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        server = create_mcp_server(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            t_trust = server._tool_manager._tools["lethe_trust_workspace"].fn
            t_untrust = server._tool_manager._tools["lethe_untrust_workspace"].fn
            t_check = server._tool_manager._tools["lethe_check_workspace_trust"].fn

            # 1. Untrust
            res_untrust = json.loads(t_untrust(workspace_id="untrusted_partner"))
            assert res_untrust["trusted"] is False

            # 2. Check
            res_check1 = json.loads(t_check(workspace_id="untrusted_partner"))
            assert res_check1["trusted"] is False
            assert res_check1["mode"] == "isolated_sandbox"

            # 3. Trust
            res_trust = json.loads(t_trust(workspace_id="untrusted_partner"))
            assert res_trust["trusted"] is True

            # 4. Check again
            res_check2 = json.loads(t_check(workspace_id="untrusted_partner"))
            assert res_check2["trusted"] is True
            assert res_check2["mode"] == "trusted"
        finally:
            server._lethe_memory.close()


def test_cli_trust_and_untrust():
    from lethe.cli import cmd_trust, cmd_untrust

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        procs_dir = str(Path(tmp_dir) / "procs")

        # Trust
        cmd_trust(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            workspace_id="team_repo",
            list=False,
        ))

        # Untrust
        cmd_untrust(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            workspace_id="external_vendor",
        ))

        # List
        cmd_trust(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            workspace_id=None,
            list=True,
        ))
