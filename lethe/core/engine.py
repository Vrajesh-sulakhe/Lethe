import time
import uuid
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from lethe.models import (
    MemoryNode,
    MemoryStatus,
    StorageTier,
    QueryResult,
    HandoverPacket,
    DeletionReceipt,
    CodeAnchor,
    BranchInfo,
    MemoryCommit,
    MergeResult,
    SyncDelta,
)
from lethe.core.drift import (
    check_code_drift,
    compute_file_content_hash,
    get_git_commit_sha,
)
from lethe.core.git import MemoryGitManager
from lethe.core.retrieval import (
    RecallIntent,
    assemble_session_context,
    classify_recall_intent,
    default_scope_for_category,
    retrieve_browse_candidates,
    retrieve_identity_candidates,
    retrieve_specific_candidates,
)
from lethe.core.synthesis import MemorySynthesizer
from lethe.core.capture import extract_durable_facts, parse_lethe_invocation
from lethe.core.retrieval import GLOBAL_PERSONAL_CATEGORIES
from lethe.platform.security import WorkspaceSecurityManager
from lethe.storage.db import DatabaseManager, get_current_workspace_id
from lethe.storage.ledger import CryptographicLedger
from lethe.storage.lineage import LineageManager
from lethe.storage.tiers import RelationalTier, SemanticTier, ProceduralTier, EmbeddingHelper
from lethe.core.bitemporal import BitemporalManager
from lethe.core.trust import TrustGateManager
from lethe.core.knapsack import (
    KnapsackPacker,
    compute_effective_token_budget,
    count_tokens,
    format_citation_line,
    truncate_to_tokens,
)

class LetheMemory:
    """
    Main coordinator for the Lethe Memory Operating System.
    Orchestrates tiered storage, lineage DAG unlearning, trust gates,
    bitemporal clocks, workspace scoping, and token budgeting.
    """
    def __init__(
        self,
        db_path: Path | str = "lethe.db",
        procedures_dir: Path | str = "procedures",
        workspace_id: Optional[str] = None,
        security_config_path: Optional[Path | str] = None,
        default_trust: bool = True,
    ):
        self.db = DatabaseManager(db_path)
        self.workspace_id = workspace_id or get_current_workspace_id()

        sec_path = security_config_path
        if sec_path is None and str(db_path) != ":memory:":
            p = Path(db_path)
            if p.parent != Path.home() / ".lethe" and p.parent != Path.cwd():
                sec_path = p.parent / "trusted_workspaces.json"

        self.security = WorkspaceSecurityManager(config_path=sec_path, default_trust=default_trust)
        self.ledger = CryptographicLedger(self.db)
        self.lineage = LineageManager(self.db, ledger=self.ledger)
        self.relational = RelationalTier(self.db)
        self.semantic = SemanticTier(self.db)
        self.procedural = ProceduralTier(procedures_dir)
        self.bitemporal = BitemporalManager(self.db)
        self.trust_gate = TrustGateManager(self.db)
        self.knapsack = KnapsackPacker()
        self.synthesizer = MemorySynthesizer(self.db)
        self.git = MemoryGitManager(self.db, self.semantic, self.lineage)

    def store(
        self,
        content: str,
        category: str = "general",
        tier: StorageTier = StorageTier.SEMANTIC,
        source: str = "user_explicit",
        trust_level: Optional[float] = None,
        session_id: str = "default_session",
        parent_ids: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        valid_from: Optional[float] = None,
        workspace_id: Optional[str] = None,
        scope: Optional[str] = None,
        code_anchor: Optional[CodeAnchor | Dict[str, Any]] = None,
        branch: Optional[str] = None,
    ) -> MemoryNode:
        """
        Stores a memory through the trust gate, bitemporal clock, and lineage DAG.
        Optionally anchors the memory to a code file, symbol, and git commit SHA.
        """
        if trust_level is None:
            if source == "user_explicit":
                trust_level = 1.0
            elif "tool" in source or "database" in source:
                trust_level = 0.8
            elif "inferred" in source or "summary" in source:
                trust_level = 0.5
            else:
                trust_level = 0.1

        resolved_workspace = workspace_id or self.workspace_id
        resolved_scope = default_scope_for_category(category, scope)

        # Security validation for workspace and scope
        allowed, _ = self.security.validate_write(
            workspace_id=resolved_workspace,
            scope=resolved_scope,
            tier=tier,
            trust_level=float(trust_level),
            strict=False,
        )
        if not allowed:
            if resolved_scope == "global":
                resolved_scope = "project"
            if trust_level > 0.8:
                trust_level = 0.5
        scope = resolved_scope
        anchor_obj: Optional[CodeAnchor] = None
        if code_anchor:
            if isinstance(code_anchor, dict):
                anchor_obj = CodeAnchor.model_validate(code_anchor)
            elif isinstance(code_anchor, CodeAnchor):
                anchor_obj = code_anchor

            # Auto-populate content hash or git SHA if file exists and hash is missing
            if anchor_obj:
                raw_path = Path(anchor_obj.file_path)
                file_target = raw_path if raw_path.is_absolute() else (
                    Path(resolved_workspace) / raw_path if resolved_workspace else raw_path
                )
                if file_target.is_file():
                    if not anchor_obj.content_hash:
                        anchor_obj.content_hash = compute_file_content_hash(file_target)
                    if not anchor_obj.git_sha:
                        anchor_obj.git_sha = get_git_commit_sha(file_target.parent)

        now = time.time()
        node_metadata = metadata.copy() if metadata else {}
        if anchor_obj:
            node_metadata["code_anchor"] = anchor_obj.model_dump()

        target_branch = branch or self.current_branch
        node = MemoryNode(
            id=str(uuid.uuid4()),
            content=content,
            category=category,
            tier=tier,
            trust_level=float(trust_level),
            status=MemoryStatus.ACTIVE,
            source_session=session_id,
            source_origin=source,
            created_at=now,
            valid_from=valid_from if valid_from is not None else now,
            parent_ids=parent_ids or [],
            workspace_id=resolved_workspace,
            scope=scope,
            branch=target_branch,
            code_anchor=anchor_obj,
            metadata=node_metadata,
        )

        # 1. Trust Gate Check
        verdict = self.trust_gate.process_input(node)
        if not verdict["allowed"]:
            self.db.log_audit_event(
                "MEMORY_QUARANTINED",
                target_id=node.id,
                details={
                    "branch": target_branch,
                    "category": category,
                    "workspace_id": resolved_workspace,
                    "scope": scope,
                    "reason": verdict.get("reason", ""),
                },
            )
            return node

        # 2. Bitemporal & Conflict Check
        conflicts = self.bitemporal.process_statement(node)

        # 3. Store in appropriate tier (preserving exact StorageTier classification)
        if tier == StorageTier.PROCEDURAL and node.metadata.get("filename"):
            saved_path = self.procedural.save_procedure(str(node.metadata["filename"]), content)
            node.metadata["filename"] = saved_path.name

        if tier == StorageTier.RELATIONAL:
            self.relational.store(node)
        # Always maintain semantic indexing for natural-language query recall
        self.semantic.store(node)

        # 4. Record Lineage DAG parent links
        for pid in node.parent_ids:
            self.lineage.add_lineage_edge(pid, node.id)

        # 5. Record state transition in append-only cryptographic ledger
        try:
            self.ledger.record_store(node)
        except Exception:
            pass

        # 6. Emit audit event for cross-tool reactive sync
        self.db.log_audit_event(
            "MEMORY_CONFLICT" if conflicts else "MEMORY_STORED",
            target_id=node.id,
            details={
                "branch": target_branch,
                "category": category,
                "tier": tier.value if isinstance(tier, StorageTier) else str(tier),
                "workspace_id": resolved_workspace,
                "scope": scope,
                "summary": content[:140],
                "conflicts": len(conflicts),
            },
        )

        return node

    def save_procedure(self, filename: str, content: str) -> Path:
        """Writes a Markdown runbook into Tier 3 (ProceduralTier)."""
        return self.procedural.save_procedure(filename, content)

    def read_procedure(self, filename: str) -> Optional[str]:
        """Reads a Markdown runbook from Tier 3 (ProceduralTier)."""
        return self.procedural.read_procedure(filename)

    def list_procedures(self) -> List[str]:
        """Lists stored Markdown runbooks in Tier 3 (ProceduralTier)."""
        return self.procedural.list_procedures()

    def flush_ephemeral(
        self,
        session_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        promote_summary: Optional[str] = None,
        category: str = "summary",
    ) -> Dict[str, Any]:
        """
        Flushes Tier 4 (EPHEMERAL) working memory at session boundaries so temporary
        scratchpad steps do not pollute long-term recall, optionally promoting a concise
        summary into long-term SEMANTIC memory.
        """
        ephemeral_nodes = self.db.list_memories(
            tier=StorageTier.EPHEMERAL,
            session_id=session_id,
            workspace_id=workspace_id,
            branch=self.current_branch,
        )
        flushed_ids: List[str] = []
        seen_ids = set()
        for node in ephemeral_nodes:
            if node.status in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT) and node.id not in seen_ids:
                receipt = self.lineage.cascading_purge(node.id, reason="ephemeral_session_flush")
                for pid in receipt.purged_node_ids:
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        flushed_ids.append(pid)

        promoted_memory_id: Optional[str] = None
        if promote_summary:
            promoted = self.store(
                content=promote_summary,
                category=category,
                tier=StorageTier.SEMANTIC,
                source="agent_inferred",
                session_id=session_id or "default_session",
                workspace_id=workspace_id or self.workspace_id,
            )
            promoted_memory_id = promoted.id

        return {
            "flushed_count": len(flushed_ids),
            "flushed_ids": flushed_ids,
            "promoted_memory_id": promoted_memory_id,
        }

    @staticmethod
    def _bounded_abstention_text(base_msg: str, token_budget: int) -> str:
        if token_budget <= 0:
            return ""
        if count_tokens(base_msg) <= token_budget:
            return base_msg
        short_msg = "No verified memory."
        if count_tokens(short_msg) <= token_budget:
            return short_msg
        return truncate_to_tokens(short_msg, token_budget, suffix="")

    def recall(
        self,
        query: str,
        token_budget: int = 500,
        as_of_time: Optional[float] = None,
        entity_filter: Optional[str] = None,
        min_relevance: float = 0.12,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        include_ephemeral: bool = False,
        model_name: Optional[str] = None,
        max_context_tokens: Optional[int] = None,
        remaining_context_tokens: Optional[int] = None,
        branch: Optional[str] = None,
    ) -> QueryResult:
        """
        Retrieves relevant memories within strict post-format token budget.
        Respects model context window ceilings and triggers honest abstention
        when no verified memories match or critical query terms are missing.
        """
        start_time = time.perf_counter()
        effective_budget = compute_effective_token_budget(
            token_budget=token_budget,
            model_name=model_name,
            max_context_tokens=max_context_tokens,
            remaining_context_tokens=remaining_context_tokens,
        )
        if effective_budget <= 0:
            latency = (time.perf_counter() - start_time) * 1000.0
            return QueryResult(
                query=query,
                memories=[],
                context_text="",
                tokens_used=0,
                latency_ms=latency,
                is_abstention=True,
            )

        target_ws = workspace_id or self.workspace_id
        target_branch = branch or self.current_branch
        is_trusted = self.security.is_workspace_trusted(target_ws)

        intent, broad_profile = classify_recall_intent(query, entity_filter=entity_filter)
        if intent == RecallIntent.USER_IDENTITY:
            qualified_candidates = retrieve_identity_candidates(
                db=self.db,
                security=self.security,
                target_ws=target_ws,
                target_branch=target_branch,
                is_trusted=is_trusted,
                as_of_time=as_of_time,
                session_id=session_id,
                include_ephemeral=include_ephemeral,
                broad_profile=broad_profile,
            )
        elif intent == RecallIntent.BROWSE_RECENT:
            qualified_candidates = retrieve_browse_candidates(
                db=self.db,
                security=self.security,
                target_ws=target_ws,
                target_branch=target_branch,
                is_trusted=is_trusted,
                as_of_time=as_of_time,
                session_id=session_id,
                include_ephemeral=include_ephemeral,
                limit=max(40, min(150, effective_budget // 8 + 20)),
            )
        else:
            qualified_candidates = retrieve_specific_candidates(
                query=query,
                db=self.db,
                bitemporal=self.bitemporal,
                semantic=self.semantic,
                security=self.security,
                target_ws=target_ws,
                target_branch=target_branch,
                is_trusted=is_trusted,
                effective_budget=effective_budget,
                min_relevance=min_relevance,
                as_of_time=as_of_time,
                entity_filter=entity_filter,
                session_id=session_id,
                include_ephemeral=include_ephemeral,
            )

        if not qualified_candidates:
            latency = (time.perf_counter() - start_time) * 1000.0
            abstain_msg = self._bounded_abstention_text(
                "I do not have verified memory regarding this query.", effective_budget
            )
            return QueryResult(
                query=query,
                memories=[],
                context_text=abstain_msg,
                tokens_used=count_tokens(abstain_msg),
                latency_ms=latency,
                is_abstention=True,
            )

        # 4. Deduplicate candidates keeping highest score
        deduped: Dict[str, Tuple[MemoryNode, float]] = {}
        for node, score in qualified_candidates:
            if node.id not in deduped or score > deduped[node.id][1]:
                deduped[node.id] = (node, score)

        candidate_list = list(deduped.values())
        any_candidate_conflict = any(n.status == MemoryStatus.CONFLICT for n, _ in candidate_list)

        # Check code drift for candidates anchored to files/symbols
        drift_warnings: List[str] = []
        drift_map: Dict[str, str] = {}
        target_ws = workspace_id or self.workspace_id
        for node, _ in candidate_list:
            if node.code_anchor:
                is_drifted, reason = check_code_drift(node.code_anchor, workspace_dir=target_ws)
                if is_drifted and reason:
                    drift_warnings.append(f"Memory #{node.id[:6]}: {reason}")
                    drift_map[node.id] = reason

        verbose_warning = "\n[EPISTEMIC CONFLICT WARNING: Multiple conflicting records exist for this topic.]"
        compact_warning = "[CONFLICT WARNING]"
        banner_to_append = ""
        reserved_banner = 0
        if any_candidate_conflict:
            full_banner_cost = count_tokens("\n" + verbose_warning)
            if effective_budget > full_banner_cost + 20:
                banner_to_append = verbose_warning
                reserved_banner = full_banner_cost
            elif effective_budget >= 20:
                banner_to_append = compact_warning
                reserved_banner = count_tokens("\n" + compact_warning)

        # 5. Greedy Knapsack Packing using EXACT final citation formatting + banner reservation
        packed_memories, _ = self.knapsack.pack(
            candidate_list,
            effective_budget,
            formatter=lambda n: format_citation_line(n, compact=False, drift_warning=drift_map.get(n.id)),
            reserved_tokens=reserved_banner,
        )
        use_compact = False

        if not packed_memories:
            packed_memories, _ = self.knapsack.pack(
                candidate_list,
                effective_budget,
                formatter=lambda n: format_citation_line(n, compact=True, drift_warning=drift_map.get(n.id)),
                reserved_tokens=reserved_banner,
            )
            use_compact = bool(packed_memories)

        lines: List[str] = []
        if packed_memories:
            lines = [
                format_citation_line(m, compact=use_compact, drift_warning=drift_map.get(m.id))
                for m in packed_memories
            ]
        elif effective_budget >= 20:
            # Top verified candidate is individually larger than budget -> deliver a clean bounded slice
            best_node, _ = max(
                candidate_list,
                key=lambda pair: (float(pair[1]) * float(pair[0].trust_level))
            )
            best_drift = f" [DRIFT WARNING: {drift_map[best_node.id]}]" if best_node.id in drift_map else ""
            header = f"[#{best_node.id[:6]}|{best_node.category}|{best_node.trust_level:.1f}]{best_drift} "
            header_cost = count_tokens(header)
            avail = effective_budget - reserved_banner - header_cost
            if avail >= 6:
                sliced_content = truncate_to_tokens(best_node.content, avail)
                if sliced_content:
                    packed_memories = [best_node]
                    lines = [header + sliced_content]

        if not packed_memories:
            latency = (time.perf_counter() - start_time) * 1000.0
            abstain_msg = self._bounded_abstention_text(
                "I do not have verified memory regarding this query within the requested token budget.",
                effective_budget,
            )
            return QueryResult(
                query=query,
                memories=[],
                context_text=abstain_msg,
                tokens_used=count_tokens(abstain_msg),
                latency_ms=latency,
                is_abstention=True,
                drift_warnings=drift_warnings,
            )

        conflicts_detected = [
            f"Conflict on {m.category}: '{m.content}'"
            for m in packed_memories
            if m.status == MemoryStatus.CONFLICT
        ]
        if conflicts_detected and banner_to_append:
            lines.append(banner_to_append)

        # Final BPE invariant guard: guarantee rendered context_text <= effective_budget
        while lines and count_tokens("\n".join(lines)) > effective_budget:
            if len(lines) > 1:
                if conflicts_detected and lines[-1] == banner_to_append and len(lines) > 2:
                    lines.pop(-2)
                    packed_memories.pop(-1)
                else:
                    removed = lines.pop()
                    if removed != banner_to_append and packed_memories:
                        packed_memories.pop()
            else:
                lines[0] = truncate_to_tokens(lines[0], effective_budget, suffix="...")
                break

        context_text = "\n".join(lines)
        actual_tokens = count_tokens(context_text)
        latency = (time.perf_counter() - start_time) * 1000.0

        return QueryResult(
            query=query,
            memories=packed_memories,
            context_text=context_text,
            tokens_used=actual_tokens,
            latency_ms=latency,
            is_abstention=False,
            conflicts_detected=conflicts_detected,
            drift_warnings=drift_warnings,
        )

    def forget(
        self,
        target_id_or_query: str,
        reason: str = "user_deletion_request",
        workspace_id: Optional[str] = None,
    ) -> DeletionReceipt:
        """
        Executes verifiable cascading unlearning for target fact and all downstream lineages.
        """
        # 1. If passed an explicit ID or unique prefix in memories table
        target_node = self.db.get_memory(target_id_or_query)
        if target_node:
            receipt = self.lineage.cascading_purge(target_node.id, reason=reason)
            if receipt.purged_count > 0:
                self.db.log_audit_event(
                    "MEMORY_PURGED",
                    target_id=target_node.id,
                    details={"purged_count": receipt.purged_count, "branch": target_node.branch, "reason": reason},
                )
            return receipt

        # 2. If it looks like an identifier/token (no spaces, len >= 6) and was not found,
        # never attempt fuzzy semantic unlearning to prevent deleting arbitrary memories.
        cleaned = target_id_or_query.strip()
        if " " not in cleaned and len(cleaned) >= 6:
            return DeletionReceipt(
                target_id=target_id_or_query,
                purged_node_ids=[],
                purged_count=0,
                verification_hash="0" * 64,
                timestamp=time.time()
            )

        # 3. If passed natural language query text, search with strict term overlap and threshold
        matches = self.semantic.search(
            target_id_or_query, top_k=3, min_score=0.45, workspace_id=workspace_id, branch=self.current_branch
        )
        if matches:
            query_words = set(re.findall(r'\b[a-zA-Z0-9_]+\b', target_id_or_query.lower())) - EmbeddingHelper.STOP_WORDS
            for top_node, score in matches:
                doc_words = set(re.findall(r'\b[a-zA-Z0-9_]+\b', top_node.content.lower()))
                if query_words and len(query_words & doc_words) >= 1:
                    receipt = self.lineage.cascading_purge(top_node.id, reason=reason)
                    if receipt.purged_count > 0:
                        self.db.log_audit_event(
                            "MEMORY_PURGED",
                            target_id=top_node.id,
                            details={"purged_count": receipt.purged_count, "branch": top_node.branch, "reason": reason},
                        )
                    return receipt

        # 4. If nothing verified found, return empty receipt
        return DeletionReceipt(
            target_id=target_id_or_query,
            purged_node_ids=[],
            purged_count=0,
            verification_hash="0" * 64,
            timestamp=time.time()
        )

    def export_handover(
        self,
        task_id: str,
        task_goal: str,
        active_memory_ids: Optional[List[str]] = None,
        epistemic_state: Optional[Dict[str, Any]] = None,
        tool_trace: Optional[List[Dict[str, Any]]] = None,
        branch: Optional[str] = None,
    ) -> HandoverPacket:
        """
        Serializes mid-task state into an atomic Handover Packet.
        """
        target_branch = branch or self.current_branch
        packet = HandoverPacket(
            task_id=task_id,
            task_goal=task_goal,
            active_memory_ids=active_memory_ids or [],
            epistemic_state=epistemic_state or {},
            last_tool_trace=tool_trace or [],
            branch=target_branch,
        )
        self.db.save_handover_packet(packet)
        self.db.log_audit_event(
            "HANDOVER_EXPORTED",
            target_id=task_id,
            details={"task_goal": task_goal[:140], "branch": target_branch},
        )
        return packet

    def import_handover(self, task_id: str, branch: Optional[str] = None) -> Optional[HandoverPacket]:
        """
        Restores mid-task state for a secondary agent.
        """
        return self.db.get_handover_packet(task_id, branch=branch or self.current_branch)

    def get_dream_candidates(
        self,
        category: Optional[str] = None,
        workspace_id: Optional[str] = None,
        min_cluster_size: int = 2,
    ) -> List[Dict[str, Any]]:
        """Identifies clusters of related atomic memories across sessions ripe for consolidation."""
        resolved_ws = workspace_id or self.workspace_id
        return self.synthesizer.find_dream_candidates(
            category=category,
            workspace_id=resolved_ws,
            min_cluster_size=min_cluster_size,
            branch=self.current_branch,
        )

    def consolidate(
        self,
        parent_ids: List[str],
        summary_content: str,
        category: Optional[str] = None,
        target_tier: StorageTier = StorageTier.SEMANTIC,
        mark_parents_historical: bool = True,
        workspace_id: Optional[str] = None,
        scope: str = "project",
    ) -> MemoryNode:
        """
        Synthesizes multiple micro-memories into a single high-level node,
        wiring all parent_ids into the Lineage DAG.
        Optionally marks parents as HISTORICAL to conserve token budget while
        preserving 100% cascading unlearning compliance.
        """
        active_branch = self.current_branch
        valid_parents = []
        inferred_category = category
        for pid in parent_ids:
            parent_node = self.db.get_memory(pid)
            if (
                parent_node
                and parent_node.branch == active_branch
                and parent_node.status in (MemoryStatus.ACTIVE, MemoryStatus.HISTORICAL)
            ):
                valid_parents.append(parent_node)
                if not inferred_category:
                    inferred_category = parent_node.category

        if not valid_parents:
            raise ValueError("No valid active or historical parent memories found for consolidation.")

        target_category = inferred_category or "synthesized_summary"
        resolved_workspace = workspace_id or self.workspace_id

        # Store the synthesized summary node
        summary_node = self.store(
            content=summary_content,
            category=target_category,
            tier=target_tier,
            source="agent_synthesis",
            trust_level=0.8,
            parent_ids=[p.id for p in valid_parents],
            workspace_id=resolved_workspace,
            scope=scope,
        )

        # Wire Lineage DAG edges and optionally mark parents historical
        for p in valid_parents:
            self.lineage.add_lineage_edge(p.id, summary_node.id)
            if mark_parents_historical and p.status == MemoryStatus.ACTIVE:
                self.db.update_memory_status(p.id, MemoryStatus.HISTORICAL, valid_to=summary_node.created_at)

        return summary_node

    # --- Memory Git Workflows ---

    @property
    def current_branch(self) -> str:
        return self.git.current_branch()

    def create_branch(
        self,
        name: str,
        from_branch: Optional[str] = None,
        switch: bool = False,
        description: str = "",
    ) -> BranchInfo:
        return self.git.create_branch(name=name, from_branch=from_branch, switch=switch, description=description)

    def switch_branch(self, name: str) -> BranchInfo:
        return self.git.switch_branch(name)

    def list_branches(self) -> List[BranchInfo]:
        return self.git.list_branches()

    def delete_branch(self, name: str) -> None:
        self.git.delete_branch(name)

    def commit(self, message: str, branch: Optional[str] = None) -> MemoryCommit:
        return self.git.commit(message=message, branch=branch)

    def list_commits(self, branch: Optional[str] = None, limit: int = 50) -> List[MemoryCommit]:
        return self.git.list_commits(branch=branch, limit=limit)

    def diff_branches(self, source: str, target: str = "main") -> Dict[str, Any]:
        return self.git.diff_branches(source=source, target=target)

    def merge_branch(
        self,
        source: str,
        target: Optional[str] = None,
        strategy: str = "abort_on_conflict",
    ) -> MergeResult:
        return self.git.merge_branch(source=source, target=target, strategy=strategy)

    def rollback(self, target: str, branch: Optional[str] = None) -> MemoryCommit:
        return self.git.rollback(target=target, branch=branch)

    # --- Reactive Cross-Tool Sync Engine ---

    @staticmethod
    def format_sync_delta(
        branch: str,
        events: List[Dict[str, Any]],
        active_handover: Optional[HandoverPacket],
        max_tokens: int = 300,
    ) -> str:
        if max_tokens <= 0 or not events:
            return ""

        lines: List[str] = [f"[Lethe Sync | branch={branch} | updates={len(events)}]"]
        if active_handover:
            lines.append(f"- Active Handover ({active_handover.task_id}): {active_handover.task_goal}")

        # Show newest events first so recent state fits tight prompt hook budgets
        for ev in reversed(events):
            etype = ev.get("event_type", "EVENT")
            details = ev.get("details") or {}
            summary = (
                details.get("summary")
                or details.get("task_goal")
                or details.get("message")
                or details.get("branch")
                or ev.get("target_id")
                or ""
            )
            line = f"- {etype}: {summary}".strip()
            candidate = "\n".join(lines + [line])
            if count_tokens(candidate) <= max_tokens:
                lines.append(line)
            else:
                break

        text = "\n".join(lines)
        while text and count_tokens(text) > max_tokens:
            if len(lines) > 1:
                lines.pop()
                text = "\n".join(lines)
            else:
                text = truncate_to_tokens(lines[0], max_tokens, suffix="...")
                break
        return text

    def get_sync_delta(
        self,
        client_id: str,
        max_tokens: int = 300,
        peek: bool = False,
    ) -> SyncDelta:
        active_branch = self.current_branch
        cursor = self.db.get_sync_cursor(client_id)
        last_id = int(cursor.get("last_audit_id", 0))
        batch_limit = 500
        raw_events = self.db.get_audit_events_since(last_id, limit=batch_limit)
        latest_id = self.db.get_max_audit_id()
        cursor_target_id = (
            int(raw_events[-1]["id"])
            if len(raw_events) >= batch_limit
            else latest_id
        )

        is_trusted = self.security.is_workspace_trusted(self.workspace_id)
        if not is_trusted:
            events: List[Dict[str, Any]] = []
            for ev in raw_events:
                details = ev.get("details") or {}
                ev_ws = details.get("workspace_id")
                ev_scope = details.get("scope")
                if ev_ws is not None and ev_ws != self.workspace_id and ev_scope != "global":
                    continue
                events.append(ev)
        else:
            events = raw_events

        if not events:
            if not peek and cursor_target_id > last_id:
                self.db.update_sync_cursor(client_id, last_audit_id=cursor_target_id, branch=active_branch)
            return SyncDelta(
                client_id=client_id,
                branch=active_branch,
                last_audit_id=last_id,
                latest_audit_id=cursor_target_id,
                events=[],
                active_handover=None,
                token_count=0,
                formatted_text="",
            )

        active_hp = self.db.get_latest_handover_packet(branch=active_branch)
        formatted = self.format_sync_delta(
            branch=active_branch,
            events=events,
            active_handover=active_hp,
            max_tokens=max_tokens,
        )
        tokens = count_tokens(formatted) if formatted else 0

        if not peek:
            self.db.update_sync_cursor(client_id, last_audit_id=cursor_target_id, branch=active_branch)

        return SyncDelta(
            client_id=client_id,
            branch=active_branch,
            last_audit_id=last_id,
            latest_audit_id=cursor_target_id,
            events=events,
            active_handover=active_hp,
            token_count=tokens,
            formatted_text=formatted,
        )

    def build_session_context(
        self,
        token_budget: int = 800,
        workspace_id: Optional[str] = None,
        model_name: Optional[str] = None,
        remaining_context_tokens: Optional[int] = None,
        client_id: Optional[str] = None,
        branch: Optional[str] = None,
        session_id: Optional[str] = None,
        include_handover: bool = True,
        mark_read: bool = False,
    ) -> Dict[str, Any]:
        """Builds a BPE-budgeted session boot context bundle across identity, recent memories, and sync state."""
        target_ws = workspace_id or self.workspace_id
        target_branch = branch or self.current_branch
        is_trusted = self.security.is_workspace_trusted(target_ws)

        ctx = assemble_session_context(
            db=self.db,
            security=self.security,
            packer=self.knapsack,
            target_ws=target_ws,
            target_branch=target_branch,
            token_budget=token_budget,
            model_name=model_name,
            remaining_context_tokens=remaining_context_tokens,
            session_id=session_id,
        )

        active_hp = None
        if include_handover and is_trusted:
            hp = self.db.get_latest_handover_packet(branch=target_branch)
            if hp:
                active_hp = {
                    "task_id": hp.task_id,
                    "task_goal": hp.task_goal,
                    "branch": hp.branch,
                    "active_memory_count": len(hp.active_memory_ids),
                    "epistemic_state": hp.epistemic_state,
                    "created_at": hp.created_at,
                }

        sync_info = None
        if client_id:
            delta = self.get_sync_delta(
                client_id=client_id,
                max_tokens=min(200, max(60, ctx["effective_token_budget"] // 4)),
                peek=not mark_read,
            )
            sync_info = {
                "client_id": delta.client_id,
                "unread_count": len(delta.events),
                "last_audit_id": delta.last_audit_id,
                "latest_audit_id": delta.latest_audit_id,
                "formatted_text": delta.formatted_text,
            }

        return {
            "status": "READY" if ctx["recent_memories"] or active_hp else "EMPTY",
            "workspace_id": target_ws,
            "trusted": is_trusted,
            "branch": target_branch,
            "effective_token_budget": ctx["effective_token_budget"],
            "tokens_used": ctx["tokens_used"],
            "identity_memories": ctx["identity_memories"],
            "recent_memories": ctx["recent_memories"],
            "context_text": ctx["context_text"],
            "active_handover": active_hp,
            "sync_delta": sync_info,
            "drift_warnings": ctx["drift_warnings"],
        }

    # ------------------------------------------------------------------ turn API
    def _find_active_capture(self, content_hash: str, target_ws: Optional[str]) -> Optional[str]:
        with self.db._lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                SELECT id FROM memories
                WHERE status IN ('active', 'conflict')
                  AND json_extract(metadata_json, '$.capture_hash') = ?
                  AND (scope = 'global' OR workspace_id IS NULL OR workspace_id = ?)
                LIMIT 1
                """,
                (content_hash, target_ws),
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def capture_turn_facts(
        self,
        message: str,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extracts durable facts from a raw user message and stores new ones (idempotent)."""
        target_ws = workspace_id or self.workspace_id
        facts, skipped_sensitive = extract_durable_facts(message)
        stored: List[Dict[str, Any]] = []
        duplicates = 0
        for fact in facts:
            if self._find_active_capture(fact.capture_hash, target_ws):
                duplicates += 1
                continue
            scope = None
            if target_ws == "global_desktop" or fact.category in GLOBAL_PERSONAL_CATEGORIES:
                scope = "global"
            node = self.store(
                content=fact.content,
                category=fact.category,
                tier=StorageTier.SEMANTIC,
                source="user_auto_capture",
                trust_level=0.95 if fact.explicit else 0.9,
                session_id=session_id or (f"{client_id}_session" if client_id else "auto_session"),
                metadata={
                    "capture_hash": fact.capture_hash,
                    "capture_rule": fact.rule,
                    "capture_client": client_id or "unknown",
                },
                workspace_id=target_ws,
                scope=scope,
            )
            if node.status == MemoryStatus.QUARANTINED:
                continue
            stored.append({"id": node.id[:6], "category": node.category, "content": node.content})
        return {"stored": stored, "duplicates": duplicates, "skipped_sensitive": skipped_sensitive}

    def process_turn(
        self,
        message: str,
        client_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        token_budget: int = 600,
        capture: bool = True,
        include_sync: bool = True,
    ) -> Dict[str, Any]:
        """
        One call per user message: serves the relevant slice of memory AND silently
        captures any durable facts the user just stated. Recall runs before capture
        so the slice never echoes what the user just said.
        """
        start = time.perf_counter()
        target_ws = workspace_id or self.workspace_id
        budget = max(80, int(token_budget))
        inv = parse_lethe_invocation(message)
        is_boot, query = inv.is_boot, inv.query
        if getattr(inv, "workspace_id", None):
            target_ws = inv.workspace_id
        elif not is_boot and query and len(query.split()) == 1:
            # Check if user typed 'lethe <ProjectName>' matching an existing workspace
            known = {w.lower(): w for w in self.db.list_workspaces()}
            if query.lower() in known:
                target_ws = known[query.lower()]
                is_boot = True
                query = ""

        sections: List[str] = []
        used_ids: set[str] = set()
        abstained = False

        if is_boot:
            boot = self.build_session_context(
                token_budget=budget, workspace_id=target_ws, client_id=client_id, mark_read=bool(client_id)
            )
            if boot.get("context_text"):
                sections.append("## Memory\n" + boot["context_text"])
            if boot.get("active_handover"):
                hp = boot["active_handover"]
                sections.append(f"## Active handover\n- {hp['task_id']}: {hp['task_goal']}")
            abstained = not sections
        else:
            id_budget = min(160, budget // 3)
            identity = self.recall("who am i", token_budget=id_budget, workspace_id=target_ws)
            if not identity.is_abstention:
                sections.append("## Who the user is\n" + identity.context_text)
                used_ids.update(m.id for m in identity.memories)

            remaining = budget - (count_tokens("\n\n".join(sections)) if sections else 0)
            if query and remaining > 40:
                rel = self.recall(query[:600], token_budget=remaining, workspace_id=target_ws)
                if rel.is_abstention:
                    abstained = True
                else:
                    seen_tags = {f"#{uid[:6]}" for uid in used_ids}
                    lines = [
                        ln for ln in rel.context_text.splitlines()
                        if ln.strip() and not any(tag in ln for tag in seen_tags)
                    ]
                    if lines:
                        sections.append("## Relevant to this message\n" + "\n".join(lines))

        if include_sync and client_id:
            delta = self.get_sync_delta(client_id=client_id, max_tokens=min(150, max(40, budget // 4)))
            if delta.formatted_text:
                sections.append("## New from other tools\n" + delta.formatted_text)

        captured = {"stored": [], "duplicates": 0, "skipped_sensitive": 0}
        if capture and not is_boot:
            captured = self.capture_turn_facts(
                message, workspace_id=target_ws, session_id=session_id, client_id=client_id
            )

        if sections:
            context_text = "\n\n".join(sections)
        else:
            context_text = "No stored memory is relevant to this message."
        if captured["stored"]:
            context_text += "\n\n## Saved from this message\n" + "\n".join(
                f"- #{c['id']} [{c['category']}] {c['content']}" for c in captured["stored"]
            )

        return {
            "context_text": context_text,
            "tokens_used": count_tokens(context_text),
            "is_abstention": abstained and not any(s.startswith("## Who") for s in sections),
            "captured": captured["stored"],
            "duplicates_skipped": captured["duplicates"],
            "sensitive_skipped": captured["skipped_sensitive"],
            "workspace_id": target_ws,
            "latency_ms": round((time.perf_counter() - start) * 1000.0, 2),
        }

    def check_health(self) -> Dict[str, Any]:
        """
        Checks the health of the database, WAL journal mode, schema version,
        and procedures directory.
        """
        return self.db.check_health(procedures_dir=self.procedural.procedures_dir)

    def close(self):
        self.db.close()
