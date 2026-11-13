import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from lethe.models import (
    BranchInfo,
    HandoverPacket,
    MemoryCommit,
    MemoryNode,
    MemoryStatus,
    MergeResult,
    StorageTier,
)
from lethe.storage.db import DatabaseManager
from lethe.storage.lineage import LineageManager
from lethe.storage.tiers import EmbeddingHelper, SemanticTier

_BRANCH_NAME_RE = re.compile(r"^[a-zA-Z0-9._/-]{1,80}$")


def _validate_branch_name(name: str) -> str:
    cleaned = (name or "").strip()
    if (
        not cleaned
        or not _BRANCH_NAME_RE.match(cleaned)
        or cleaned.upper() == "HEAD"
        or cleaned.startswith("/")
        or cleaned.endswith("/")
        or ".." in cleaned
    ):
        raise ValueError(f"Invalid branch name: '{name}'")
    return cleaned


def _logical_key(node: MemoryNode) -> str:
    """Stable identity key across branched clones for diff/merge comparison."""
    entity = node.metadata.get("entity")
    attribute = node.metadata.get("attribute")
    if entity and attribute:
        return f"rel:{entity}.{attribute}"
    origin_id = node.metadata.get("origin_memory_id")
    if origin_id:
        return f"node:{origin_id}"
    return f"node:{node.id}"


class MemoryGitManager:
    """
    Provides Memory Git workflows (branch, switch, commit, diff, merge, rollback)
    with branch-isolated state and strict preservation of Bemula unlearning tombstones.
    """

    def __init__(self, db: DatabaseManager, semantic: SemanticTier, lineage: LineageManager):
        self.db = db
        self.semantic = semantic
        self.lineage = lineage

    def current_branch(self) -> str:
        return self.db.get_active_branch()

    def list_branches(self) -> List[BranchInfo]:
        return [BranchInfo(**row) for row in self.db.list_branches()]

    def create_branch(
        self,
        name: str,
        from_branch: Optional[str] = None,
        switch: bool = False,
        description: str = "",
    ) -> BranchInfo:
        target_name = _validate_branch_name(name)
        source_branch = _validate_branch_name(from_branch) if from_branch else self.current_branch()

        if not self.db.get_branch_record(source_branch):
            raise ValueError(f"Source branch '{source_branch}' does not exist.")
        if self.db.get_branch_record(target_name):
            raise ValueError(f"Branch '{target_name}' already exists.")

        source_rec = self.db.get_branch_record(source_branch)
        branch_row = self.db.create_branch_record(
            name=target_name,
            parent_branch=source_branch,
            head_commit_id=source_rec.get("head_commit_id") if source_rec else None,
            description=description,
        )

        # Snapshot non-tombstoned memories from source_branch into target_name
        source_nodes = self.db.list_memories(branch=source_branch)
        for orig in source_nodes:
            if orig.status == MemoryStatus.TOMBSTONED:
                continue
            meta = dict(orig.metadata)
            meta.setdefault("origin_memory_id", orig.id)
            clone = MemoryNode(
                id=str(uuid.uuid4()),
                content=orig.content,
                category=orig.category,
                tier=orig.tier,
                trust_level=orig.trust_level,
                status=orig.status,
                source_session=orig.source_session,
                source_origin=orig.source_origin,
                created_at=orig.created_at,
                valid_from=orig.valid_from,
                valid_to=orig.valid_to,
                parent_ids=list(orig.parent_ids),
                workspace_id=orig.workspace_id,
                scope=orig.scope,
                branch=target_name,
                code_anchor=orig.code_anchor,
                metadata=meta,
            )
            orig_vec = self.db.get_embedding(orig.id)
            self.semantic.store(clone, vector=orig_vec)
            self.lineage.add_lineage_edge(orig.id, clone.id, relation_type="branched_from")

        latest_hp = self.db.get_latest_handover_packet(branch=source_branch)
        if latest_hp:
            cloned_hp = latest_hp.model_copy(
                update={"task_id": f"{latest_hp.task_id}@{target_name}", "branch": target_name}
            )
            self.db.save_handover_packet(cloned_hp)

        self.commit(f"branch created from {source_branch}", branch=target_name)
        if switch:
            self.switch_branch(target_name)

        self.db.log_audit_event(
            "BRANCH_CREATED",
            target_id=target_name,
            details={"from_branch": source_branch, "switched": switch},
        )
        updated = self.db.get_branch_record(target_name) or branch_row
        return BranchInfo(**updated)

    def switch_branch(self, name: str) -> BranchInfo:
        target_name = _validate_branch_name(name)
        rec = self.db.get_branch_record(target_name)
        if not rec:
            raise ValueError(f"Branch '{target_name}' does not exist.")
        self.db.set_active_branch(target_name)
        self.db.log_audit_event("BRANCH_SWITCHED", target_id=target_name, details={"branch": target_name})
        return BranchInfo(**rec)

    def delete_branch(self, name: str) -> None:
        target_name = _validate_branch_name(name)
        if target_name == "main":
            raise ValueError("Cannot delete the default 'main' branch.")
        if target_name == self.current_branch():
            raise ValueError(f"Cannot delete currently active branch '{target_name}'. Switch branches first.")
        if not self.db.get_branch_record(target_name):
            raise ValueError(f"Branch '{target_name}' does not exist.")
        self.db.delete_branch_record(target_name)
        self.db.log_audit_event("BRANCH_DELETED", target_id=target_name, details={"branch": target_name})

    def commit(self, message: str, branch: Optional[str] = None) -> MemoryCommit:
        target_branch = _validate_branch_name(branch) if branch else self.current_branch()
        branch_rec = self.db.get_branch_record(target_branch)
        if not branch_rec:
            raise ValueError(f"Branch '{target_branch}' does not exist.")

        nodes = self.db.list_memories(branch=target_branch)
        snapshot_memories = [
            n.model_dump()
            for n in nodes
            if n.status != MemoryStatus.TOMBSTONED
        ]
        latest_hp = self.db.get_latest_handover_packet(branch=target_branch)
        snapshot = {
            "branch": target_branch,
            "memories": snapshot_memories,
            "handover": latest_hp.model_dump() if latest_hp else None,
        }
        commit_id = f"c-{uuid.uuid4().hex[:8]}"
        parent_id = branch_rec.get("head_commit_id")
        saved = self.db.save_commit(
            commit_id=commit_id,
            branch=target_branch,
            parent_commit_id=parent_id,
            message=message or "checkpoint",
            snapshot_json=json.dumps(snapshot),
        )
        self.db.log_audit_event(
            "MEMORY_COMMITTED",
            target_id=commit_id,
            details={"branch": target_branch, "message": message, "memory_count": len(snapshot_memories)},
        )
        return MemoryCommit(**saved)

    def list_commits(self, branch: Optional[str] = None, limit: int = 50) -> List[MemoryCommit]:
        target_branch = _validate_branch_name(branch) if branch else self.current_branch()
        return [MemoryCommit(**row) for row in self.db.list_commits(branch=target_branch, limit=limit)]

    def _is_tombstoned(self, node_data: Dict[str, Any]) -> bool:
        """Checks both direct ID and origin_memory_id so unlearning stays irreversible."""
        candidate_ids = [node_data.get("id")]
        origin_id = (node_data.get("metadata") or {}).get("origin_memory_id")
        if origin_id:
            candidate_ids.append(origin_id)
        for cid in candidate_ids:
            if not cid:
                continue
            existing = self.db.get_memory(str(cid))
            if existing and existing.status == MemoryStatus.TOMBSTONED:
                return True
        return False

    def _resolve_commit_target(self, target: str, branch: str) -> Dict[str, Any]:
        cleaned = (target or "").strip()
        if not cleaned:
            raise ValueError("Rollback target commit or step is required.")

        steps_match = re.match(r"^(?:HEAD)?~(\d+)$", cleaned, flags=re.IGNORECASE)
        if steps_match or cleaned.isdigit():
            steps = int(steps_match.group(1) if steps_match else cleaned)
            commits = self.db.list_commits(branch=branch, limit=steps + 10)
            if steps >= len(commits):
                raise ValueError(f"Rollback step '{cleaned}' exceeds commit history ({len(commits)} commits) on '{branch}'.")
            return commits[steps]

        commit_row = self.db.get_commit(cleaned)
        if not commit_row or commit_row["branch"] != branch:
            raise ValueError(f"Commit '{target}' not found on branch '{branch}'.")
        return commit_row

    def rollback(self, target: str, branch: Optional[str] = None) -> MemoryCommit:
        target_branch = _validate_branch_name(branch) if branch else self.current_branch()
        target_commit = self._resolve_commit_target(target, target_branch)

        # Save safety checkpoint before mutating state
        self.commit(f"auto: pre-rollback checkpoint before {target_commit['id']}", branch=target_branch)

        snapshot = json.loads(target_commit["snapshot_json"])
        restored_nodes: List[Dict[str, Any]] = snapshot.get("memories", [])

        # Clear non-tombstoned memories on target_branch while leaving TOMBSTONED rows intact
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "DELETE FROM memories WHERE branch = ? AND status != ?",
                (target_branch, MemoryStatus.TOMBSTONED.value),
            )
            self.db.conn.commit()

        restored_count = 0
        for raw in restored_nodes:
            if self._is_tombstoned(raw):
                continue
            node = MemoryNode.model_validate(raw)
            node.branch = target_branch
            if node.status == MemoryStatus.HISTORICAL:
                self.db.save_memory(node)
            else:
                self.semantic.store(node)
            restored_count += 1

        hp_data = snapshot.get("handover")
        if hp_data:
            pkt = HandoverPacket.model_validate(hp_data)
            pkt.branch = target_branch
            self.db.save_handover_packet(pkt)

        # Record rollback commit pointing back to restored snapshot
        rollback_commit = self.commit(
            f"rollback to {target_commit['id']} ({target_commit['message']})",
            branch=target_branch,
        )
        self.db.log_audit_event(
            "MEMORY_ROLLED_BACK",
            target_id=target_commit["id"],
            details={"branch": target_branch, "restored_count": restored_count, "new_head": rollback_commit.id},
        )
        return rollback_commit

    def _load_branch_base_values(self, branch_name: str) -> Dict[str, Any]:
        """Loads key -> value/content snapshot from the branch's initial fork commit for 3-way diff."""
        commits = self.db.list_commits(branch=branch_name, limit=200)
        if not commits:
            return {}
        base_commit = commits[-1]
        try:
            snap = json.loads(base_commit["snapshot_json"])
        except Exception:
            return {}
        base_map: Dict[str, Any] = {}
        for raw in snap.get("memories", []):
            node = MemoryNode.model_validate(raw)
            if node.status not in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT):
                continue
            key = _logical_key(node)
            base_map[key] = node.metadata.get("value") if key.startswith("rel:") else node.content
        return base_map

    def diff_branches(self, source: str, target: str = "main") -> Dict[str, Any]:
        src_name = _validate_branch_name(source)
        tgt_name = _validate_branch_name(target)
        if not self.db.get_branch_record(src_name):
            raise ValueError(f"Source branch '{src_name}' does not exist.")
        if not self.db.get_branch_record(tgt_name):
            raise ValueError(f"Target branch '{tgt_name}' does not exist.")

        src_nodes = [
            n for n in self.db.list_memories(branch=src_name)
            if n.status in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT)
        ]
        tgt_nodes = [
            n for n in self.db.list_memories(branch=tgt_name)
            if n.status in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT)
        ]

        base_map = self._load_branch_base_values(src_name)
        tgt_by_key: Dict[str, MemoryNode] = {_logical_key(n): n for n in tgt_nodes}
        added_or_modified: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []

        for snode in src_nodes:
            key = _logical_key(snode)
            tnode = tgt_by_key.get(key)
            if tnode is None:
                added_or_modified.append({
                    "key": key,
                    "source_id": snode.id,
                    "target_id": None,
                    "content": snode.content,
                    "status": "added",
                })
                continue

            s_val = snode.metadata.get("value")
            t_val = tnode.metadata.get("value")
            if key.startswith("rel:") and s_val != t_val:
                if key in base_map and base_map[key] == t_val:
                    # Fast-forward relational update: only source changed since branching
                    added_or_modified.append({
                        "key": key,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                        "content": snode.content,
                        "status": "modified",
                    })
                else:
                    conflicts.append({
                        "key": key,
                        "entity": snode.metadata.get("entity"),
                        "attribute": snode.metadata.get("attribute"),
                        "source_value": s_val,
                        "target_value": t_val,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                    })
            elif not key.startswith("rel:") and snode.content != tnode.content:
                if key in base_map and base_map[key] != tnode.content and base_map[key] != snode.content:
                    conflicts.append({
                        "key": key,
                        "entity": None,
                        "attribute": None,
                        "source_value": snode.content,
                        "target_value": tnode.content,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                    })
                else:
                    added_or_modified.append({
                        "key": key,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                        "content": snode.content,
                        "status": "modified",
                    })

        return {
            "source_branch": src_name,
            "target_branch": tgt_name,
            "added_or_modified": added_or_modified,
            "conflicts": conflicts,
        }

    def merge_branch(
        self,
        source: str,
        target: Optional[str] = None,
        strategy: str = "abort_on_conflict",
    ) -> MergeResult:
        if strategy not in ("abort_on_conflict", "ours", "theirs"):
            raise ValueError(f"Unsupported merge strategy: '{strategy}'")

        src_name = _validate_branch_name(source)
        tgt_name = _validate_branch_name(target) if target else self.current_branch()
        if src_name == tgt_name:
            raise ValueError("Cannot merge a branch into itself.")

        diff = self.diff_branches(src_name, tgt_name)
        conflicts = diff["conflicts"]
        if conflicts and strategy == "abort_on_conflict":
            return MergeResult(
                source_branch=src_name,
                target_branch=tgt_name,
                applied_count=0,
                conflicts=conflicts,
            )

        self.commit(f"auto: pre-merge checkpoint from {src_name}", branch=tgt_name)

        src_nodes = {
            n.id: n for n in self.db.list_memories(branch=src_name)
            if n.status in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT)
        }
        applied = 0
        skipped_tombstoned: List[str] = []

        # Apply non-conflicting additions/modifications
        for item in diff["added_or_modified"]:
            snode = src_nodes.get(item["source_id"])
            if not snode:
                continue
            if self._is_tombstoned(snode.model_dump()):
                skipped_tombstoned.append(snode.id)
                continue
            if item.get("target_id"):
                self.db.update_memory_status(item["target_id"], MemoryStatus.HISTORICAL, valid_to=time.time())
            self._apply_node_to_branch(snode, tgt_name)
            applied += 1

        # Apply conflict winners if strategy == 'theirs'
        if conflicts and strategy == "theirs":
            for conf in conflicts:
                snode = src_nodes.get(conf["source_id"])
                if not snode:
                    continue
                if self._is_tombstoned(snode.model_dump()):
                    skipped_tombstoned.append(snode.id)
                    continue
                self.db.update_memory_status(conf["target_id"], MemoryStatus.HISTORICAL, valid_to=time.time())
                self._apply_node_to_branch(snode, tgt_name)
                applied += 1

        merge_commit = self.commit(f"merge {src_name} into {tgt_name} ({strategy})", branch=tgt_name)
        self.db.log_audit_event(
            "BRANCH_MERGED",
            target_id=tgt_name,
            details={"source": src_name, "strategy": strategy, "applied": applied, "conflicts": len(conflicts)},
        )
        return MergeResult(
            source_branch=src_name,
            target_branch=tgt_name,
            applied_count=applied,
            conflicts=conflicts,
            skipped_tombstoned=skipped_tombstoned,
            commit_id=merge_commit.id,
        )

    def _apply_node_to_branch(self, snode: MemoryNode, target_branch: str) -> MemoryNode:
        meta = dict(snode.metadata)
        meta.setdefault("origin_memory_id", snode.id)
        cloned = MemoryNode(
            id=str(uuid.uuid4()),
            content=snode.content,
            category=snode.category,
            tier=snode.tier,
            trust_level=snode.trust_level,
            status=MemoryStatus.ACTIVE,
            source_session=snode.source_session,
            source_origin=snode.source_origin,
            created_at=snode.created_at,
            valid_from=snode.valid_from,
            valid_to=None,
            parent_ids=list(snode.parent_ids),
            workspace_id=snode.workspace_id,
            scope=snode.scope,
            branch=target_branch,
            code_anchor=snode.code_anchor,
            metadata=meta,
        )
        vec = self.db.get_embedding(snode.id)
        self.semantic.store(cloned, vector=vec)
        self.lineage.add_lineage_edge(snode.id, cloned.id, relation_type="branched_from")
        return cloned
