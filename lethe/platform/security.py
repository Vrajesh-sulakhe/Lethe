import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from lethe.models import MemoryNode, StorageTier


class WorkspaceSecurityManager:
    """
    Manages workspace trust boundaries and enforces safe-mode cross-repository isolation.
    Guarantees that third-party cloned repositories or untrusted workspaces cannot exfiltrate
    corporate memories or inject poisoned global state across the local MCP bus.
    """

    def __init__(
        self,
        config_path: Optional[Path | str] = None,
        default_trust: bool = True,
    ):
        self._lock = threading.Lock()
        self.default_trust = default_trust
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = Path.home() / ".lethe" / "trusted_workspaces.json"

        self.trusted_workspaces: Set[str] = set()
        self.untrusted_workspaces: Set[str] = set()
        self._loaded_mtime_ns: Optional[int] = None
        self._load_config()

    def _load_config(self) -> None:
        with self._lock:
            self._reload_if_changed_locked()

    def _reload_if_changed_locked(self) -> None:
        if not self.config_path.is_file():
            self._loaded_mtime_ns = None
            return
        try:
            mtime_ns = self.config_path.stat().st_mtime_ns
            if mtime_ns == self._loaded_mtime_ns:
                return
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.trusted_workspaces = set(data.get("trusted_workspaces", []))
            self.untrusted_workspaces = set(data.get("untrusted_workspaces", []))
            if "default_trust" in data:
                self.default_trust = bool(data["default_trust"])
            self._loaded_mtime_ns = mtime_ns
        except Exception:
            pass

    def _save_config(self) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "trusted_workspaces": sorted(list(self.trusted_workspaces)),
                "untrusted_workspaces": sorted(list(self.untrusted_workspaces)),
                "default_trust": self.default_trust,
            }
            self.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._loaded_mtime_ns = self.config_path.stat().st_mtime_ns
        except OSError:
            pass

    def trust_workspace(self, workspace_id: str) -> None:
        """Grants trusted status to a workspace ID."""
        with self._lock:
            self.untrusted_workspaces.discard(workspace_id)
            self.trusted_workspaces.add(workspace_id)
            self._save_config()

    def untrust_workspace(self, workspace_id: str) -> None:
        """Marks a workspace ID as untrusted, locking it into an isolated sandbox."""
        with self._lock:
            self.trusted_workspaces.discard(workspace_id)
            self.untrusted_workspaces.add(workspace_id)
            self._save_config()

    def is_workspace_trusted(self, workspace_id: Optional[str]) -> bool:
        """
        Determines whether the given workspace is trusted.
        - None / omitted is treated as trusted host/admin context.
        - If explicitly listed in untrusted_workspaces, returns False.
        - If explicitly listed in trusted_workspaces, returns True.
        - Otherwise returns self.default_trust.
        """
        if workspace_id is None:
            return True
        with self._lock:
            self._reload_if_changed_locked()
            if workspace_id in self.untrusted_workspaces:
                return False
            if workspace_id in self.trusted_workspaces:
                return True
            return self.default_trust

    def list_trusted_workspaces(self) -> List[str]:
        with self._lock:
            return sorted(list(self.trusted_workspaces))

    def list_untrusted_workspaces(self) -> List[str]:
        with self._lock:
            return sorted(list(self.untrusted_workspaces))

    def validate_read(self, workspace_id: Optional[str], node: MemoryNode) -> bool:
        """
        Enforces read access control:
        - If node.scope == 'global': allowed ONLY if workspace is trusted.
        - If workspace_id == 'global_desktop' (desktop apps launched at '/' or $HOME):
          allowed to read across all trusted project workspaces, while still blocking
          memories from explicitly untrusted workspaces.
        - If node.scope == 'project': allowed if node.workspace_id matches or is None.
        """
        if node.scope == "global":
            return self.is_workspace_trusted(workspace_id)

        if workspace_id == "global_desktop" and self.is_workspace_trusted("global_desktop"):
            return self.is_workspace_trusted(node.workspace_id)

        if workspace_id and node.workspace_id:
            if node.workspace_id != workspace_id:
                return False

        return True

    def validate_write(
        self,
        workspace_id: Optional[str],
        scope: str,
        tier: StorageTier,
        trust_level: float,
        strict: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Enforces write policy:
        - Untrusted workspaces cannot write global scope memories.
        - Untrusted workspaces cannot claim user-explicit trust (1.0).
        """
        if not self.is_workspace_trusted(workspace_id):
            if scope == "global":
                msg = f"Untrusted workspace '{workspace_id}' is not authorized to create global-scope memories."
                if strict:
                    raise PermissionError(msg)
                return False, msg

            if trust_level > 0.8:
                msg = f"Untrusted workspace '{workspace_id}' cannot store high-trust memories (level {trust_level})."
                if strict:
                    raise PermissionError(msg)
                return False, msg

        return True, None

