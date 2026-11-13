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
)
from lethe.storage.db import DatabaseManager
from lethe.storage.lineage import LineageManager
from lethe.storage.tiers import SemanticTier

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
            parent_commit_id=source_rec.get("head_commit_id") if source_rec else None,
            head_commit_id=None,
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
            cloned_hp = latest_hp.model_copy(update={"branch": target_name})
            self.db.save_handover_packet(cloned_hp)

        fork_commit = self.commit(f"branch created from {source_branch}", branch=target_name)
        self.db.update_branch_parent_commit(target_name, fork_commit.id)
        if switch:
            self.switch_branch(target_name)

        self.db.log_audit_event(
            "BRANCH_CREATED",
            target_id=target_name,
            details={"from_branch": source_branch, "switched": switch, "branch": target_name},
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
            if n.status != MemoryStatus.TOMBSTONED and not self._is_tombstoned(n.model_dump())
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
        """Checks permanent tombstones and lineage origin so unlearning stays irreversible across branches."""
        candidate_ids: List[str] = []
        if node_data.get("id"):
            candidate_ids.append(str(node_data["id"]))
        origin_id = (node_data.get("metadata") or {}).get("origin_memory_id")
        if origin_id:
            candidate_ids.append(str(origin_id))
        return self.db.is_tombstoned_permanent(candidate_ids)

    def _resolve_commit_target(self, target: str, branch: str) -> Dict[str, Any]:
        cleaned = (target or "").strip()
        if not cleaned:
            raise ValueError("Rollback target commit or step is required.")

        if cleaned.upper() == "HEAD":
            steps = 0
        else:
            steps_match = re.match(r"^(?:HEAD)?~(\d+)$", cleaned, flags=re.IGNORECASE)
            if steps_match or cleaned.isdigit():
                steps = int(steps_match.group(1) if steps_match else cleaned)
            else:
                steps = -1

        if steps >= 0:
            commits = self.db.list_commits(branch=branch, limit=steps + 10)
            if steps >= len(commits):
                raise ValueError(
                    f"Rollback step '{cleaned}' exceeds commit history ({len(commits)} commits) on '{branch}'."
                )
            return commits[steps]

        commit_row = self.db.get_commit(cleaned)
        if not commit_row or commit_row["branch"] != branch:
            raise ValueError(f"Commit '{target}' not found on branch '{branch}'.")
        return commit_row

    def rollback(self, target: str, branch: Optional[str] = None) -> MemoryCommit:
        target_branch = _validate_branch_name(branch) if branch else self.current_branch()
        target_commit = self._resolve_commit_target(target, target_branch)

        # Parse & validate full snapshot BEFORE mutating database state
        snapshot = json.loads(target_commit["snapshot_json"])
        validated_nodes: List[MemoryNode] = []
        for raw in snapshot.get("memories", []):
            if self._is_tombstoned(raw):
                continue
            node = MemoryNode.model_validate(raw)
            node.branch = target_branch
            validated_nodes.append(node)

        hp_data = snapshot.get("handover")
        validated_hp = HandoverPacket.model_validate(hp_data) if hp_data else None
        if validated_hp:
            validated_hp.branch = target_branch

        # Save safety checkpoint before mutating state
        self.commit(f"auto: pre-rollback checkpoint before {target_commit['id']}", branch=target_branch)

        # Atomic branch state restoration
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                DELETE FROM lineage_edges
                WHERE child_id IN (SELECT id FROM memories WHERE branch = ? AND status != ?)
                """,
                (target_branch, MemoryStatus.TOMBSTONED.value),
            )
            cursor.execute(
                "DELETE FROM memories WHERE branch = ? AND status != ?",
                (target_branch, MemoryStatus.TOMBSTONED.value),
            )
            cursor.execute("DELETE FROM handover_packets WHERE branch = ?", (target_branch,))
            self.db.conn.commit()

            for node in validated_nodes:
                # Preserve embeddings on BOTH active and historical nodes so as_of_time search works
                self.semantic.store(node)
            if validated_hp:
                self.db.save_handover_packet(validated_hp)

        rollback_commit = self.commit(
            f"rollback to {target_commit['id']} ({target_commit['message']})",
            branch=target_branch,
        )
        self.db.log_audit_event(
            "MEMORY_ROLLED_BACK",
            target_id=target_commit["id"],
            details={"branch": target_branch, "restored_count": len(validated_nodes), "new_head": rollback_commit.id},
        )
        return rollback_commit

    def _load_branch_base_values(self, src_name: str, tgt_name: str) -> Dict[str, Any]:
        """Loads key -> value/content snapshot from the fork base commit for 3-way diff."""
        src_rec = self.db.get_branch_record(src_name) or {}
        tgt_rec = self.db.get_branch_record(tgt_name) or {}

        commit_id = None
        if src_rec.get("parent_branch") == tgt_name and src_rec.get("parent_commit_id"):
            commit_id = src_rec["parent_commit_id"]
        elif tgt_rec.get("parent_branch") == src_name and tgt_rec.get("parent_commit_id"):
            commit_id = tgt_rec["parent_commit_id"]

        snap_row = self.db.get_commit(commit_id) if commit_id else None
        if not snap_row:
            # Fallback to oldest commit in SQLite directly (unbounded by limit)
            with self.db._lock:
                row = self.db.conn.execute(
                    "SELECT * FROM memory_commits WHERE branch = ? ORDER BY created_at ASC, rowid ASC LIMIT 1",
                    (src_name,),
                ).fetchone()
                snap_row = dict(row) if row else None

        if not snap_row:
            return {}

        try:
            snap = json.loads(snap_row["snapshot_json"])
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

        base_map = self._load_branch_base_values(src_name, tgt_name)
        tgt_by_key: Dict[str, List[MemoryNode]] = {}
        for tn in tgt_nodes:
            tgt_by_key.setdefault(_logical_key(tn), []).append(tn)

        added_or_modified: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []

        for snode in src_nodes:
            key = _logical_key(snode)
            t_matches = tgt_by_key.get(key, [])
            if not t_matches:
                added_or_modified.append({
                    "key": key,
                    "source_id": snode.id,
                    "target_id": None,
                    "content": snode.content,
                    "status": "added",
                })
                continue

            # If multiple CONFLICT rows exist on target for the same key, prefer newest write
            tnode = max(t_matches, key=lambda n: (n.valid_from, n.created_at))
            s_val = snode.metadata.get("value")
            t_val = tnode.metadata.get("value")

            if key.startswith("rel:"):
                t_vals = {tn.metadata.get("value") for tn in t_matches}
                if len(t_vals) == 1 and s_val == t_val:
                    continue
                if len(t_vals) == 1 and key in base_map and base_map[key] == s_val:
                    # Only target changed since fork -> keep target cleanly
                    continue
                if len(t_vals) == 1 and key in base_map and base_map[key] == t_val:
                    added_or_modified.append({
                        "key": key,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                        "target_ids": [tn.id for tn in t_matches],
                        "content": snode.content,
                        "status": "modified",
                    })
                else:
                    if len(t_vals) > 1 and t_val == s_val:
                        other = next((tn for tn in t_matches if tn.metadata.get("value") != s_val), tnode)
                        tnode = other
                        t_val = other.metadata.get("value")
                    conflicts.append({
                        "key": key,
                        "entity": snode.metadata.get("entity"),
                        "attribute": snode.metadata.get("attribute"),
                        "source_value": s_val,
                        "target_value": t_val,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                        "target_ids": [tn.id for tn in t_matches],
                    })
            else:
                t_contents = {tn.content for tn in t_matches}
                if len(t_contents) == 1 and snode.content == tnode.content:
                    continue
                if len(t_contents) == 1 and key in base_map and base_map[key] == snode.content:
                    # Source is still at ancestor content while target advanced -> keep target
                    continue
                if len(t_contents) == 1 and key in base_map and base_map[key] == tnode.content:
                    added_or_modified.append({
                        "key": key,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                        "target_ids": [tn.id for tn in t_matches],
                        "content": snode.content,
                        "status": "modified",
                    })
                else:
                    conflicts.append({
                        "key": key,
                        "entity": None,
                        "attribute": None,
                        "source_value": snode.content,
                        "target_value": tnode.content,
                        "source_id": snode.id,
                        "target_id": tnode.id,
                        "target_ids": [tn.id for tn in t_matches],
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

        for item in diff["added_or_modified"]:
            snode = src_nodes.get(item["source_id"])
            if not snode:
                continue
            if self._is_tombstoned(snode.model_dump()):
                skipped_tombstoned.append(snode.id)
                continue
            target_ids = item.get("target_ids") or ([item["target_id"]] if item.get("target_id") else [])
            for tid in target_ids:
                self.db.update_memory_status(tid, MemoryStatus.HISTORICAL, valid_to=time.time())
            self._apply_node_to_branch(snode, tgt_name)
            applied += 1

        if conflicts and strategy == "theirs":
            for conf in conflicts:
                snode = src_nodes.get(conf["source_id"])
                if not snode:
                    continue
                if self._is_tombstoned(snode.model_dump()):
                    skipped_tombstoned.append(snode.id)
                    continue
                target_ids = conf.get("target_ids") or ([conf["target_id"]] if conf.get("target_id") else [])
                for tid in target_ids:
                    self.db.update_memory_status(tid, MemoryStatus.HISTORICAL, valid_to=time.time())
                self._apply_node_to_branch(snode, tgt_name)
                applied += 1

        merge_commit = self.commit(f"merge {src_name} into {tgt_name} ({strategy})", branch=tgt_name)
        self.db.log_audit_event(
            "BRANCH_MERGED",
            target_id=tgt_name,
            details={"source": src_name, "branch": tgt_name, "strategy": strategy, "applied": applied, "conflicts": len(conflicts)},
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
