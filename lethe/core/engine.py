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
)
from lethe.core.drift import (
    check_code_drift,
    compute_file_content_hash,
    get_git_commit_sha,
)
from lethe.core.synthesis import MemorySynthesizer
from lethe.platform.security import WorkspaceSecurityManager
from lethe.storage.db import DatabaseManager, get_current_workspace_id
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
        self.lineage = LineageManager(self.db)
        self.relational = RelationalTier(self.db)
        self.semantic = SemanticTier(self.db)
        self.procedural = ProceduralTier(procedures_dir)
        self.bitemporal = BitemporalManager(self.db)
        self.trust_gate = TrustGateManager(self.db)
        self.knapsack = KnapsackPacker()
        self.synthesizer = MemorySynthesizer(self.db)

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
        scope: str = "project",
        code_anchor: Optional[CodeAnchor | Dict[str, Any]] = None,
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

        # Security validation for workspace and scope
        allowed, _ = self.security.validate_write(
            workspace_id=resolved_workspace,
            scope=scope,
            tier=tier,
            trust_level=float(trust_level),
            strict=False,
        )
        if not allowed:
            if scope == "global":
                scope = "project"
            if trust_level > 0.8:
                trust_level = 0.5
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
            code_anchor=anchor_obj,
            metadata=node_metadata,
        )

        # 1. Trust Gate Check
        verdict = self.trust_gate.process_input(node)
        if not verdict["allowed"]:
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

        candidates: List[Tuple[MemoryNode, float]] = []
        target_ws = workspace_id or self.workspace_id
        is_trusted = self.security.is_workspace_trusted(target_ws)

        # 1. If entity filter provided, check Relational Tier within workspace scope
        if entity_filter:
            parts = entity_filter.split(".")
            if len(parts) == 2:
                exact_match = self.bitemporal.query_active_attribute(
                    parts[0],
                    parts[1],
                    as_of_time=as_of_time,
                    workspace_id=workspace_id,
                    is_trusted=is_trusted,
                )
                if exact_match and exact_match.status in (
                    MemoryStatus.ACTIVE, MemoryStatus.HISTORICAL, MemoryStatus.CONFLICT
                ):
                    candidates.append((exact_match, 1.0))

        # 2. Search Semantic Tier with scalable candidate pool for 128K+ corpora
        search_k = max(40, min(200, effective_budget // 10 + 30))
        semantic_matches = self.semantic.search(
            query,
            top_k=search_k,
            min_score=0.0,
            workspace_id=workspace_id,
            session_id=session_id,
            include_ephemeral=include_ephemeral,
            as_of_time=as_of_time,
            is_trusted=is_trusted,
        )
        for node, score in semantic_matches:
            if as_of_time is not None:
                if node.valid_from > as_of_time or (node.valid_to and node.valid_to <= as_of_time):
                    continue
            candidates.append((node, score))

        # Enforce workspace security access validation on all candidate memories
        candidates = [
            (node, score)
            for node, score in candidates
            if self.security.validate_read(target_ws, node)
        ]

        # 3. Honest Abstention & Epistemic Term Coverage Gate
        generic_words = {'number', 'name', 'info', 'information', 'details', 'detail', 'secret', 'give', 'tell', 'show', 'my', 'the'}
        specific_q_words = [
            EmbeddingHelper._stem(w)
            for w in re.findall(r'\b[a-zA-Z0-9_]+\b', query.lower())
            if w not in EmbeddingHelper.STOP_WORDS and w not in generic_words
        ]
        if not specific_q_words:
            specific_q_words = [
                EmbeddingHelper._stem(w)
                for w in re.findall(r'\b[a-zA-Z0-9_]+\b', query.lower())
                if w not in EmbeddingHelper.STOP_WORDS
            ]

        def calculate_term_coverage(node: MemoryNode) -> float:
            if not specific_q_words:
                return 1.0
            doc_text = f"[{node.category}] {node.content}"
            doc_stemmed = ' '.join(
                EmbeddingHelper._stem(w)
                for w in re.findall(r'\b[a-zA-Z0-9_]+\b', doc_text.lower())
            )
            matched = [w for w in specific_q_words if w in doc_stemmed]
            return len(matched) / len(specific_q_words)

        qualified_candidates: List[Tuple[MemoryNode, float]] = []
        for node, score in candidates:
            cov = calculate_term_coverage(node)
            if (score >= 0.99 or cov >= 0.30) and score >= min_relevance:
                combined_score = score if score >= 0.99 else (0.65 * score + 0.35 * cov)
                qualified_candidates.append((node, combined_score))

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
            return self.lineage.cascading_purge(target_node.id, reason=reason)

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
            target_id_or_query, top_k=3, min_score=0.45, workspace_id=workspace_id
        )
        if matches:
            query_words = set(re.findall(r'\b[a-zA-Z0-9_]+\b', target_id_or_query.lower())) - EmbeddingHelper.STOP_WORDS
            for top_node, score in matches:
                doc_words = set(re.findall(r'\b[a-zA-Z0-9_]+\b', top_node.content.lower()))
                if query_words and len(query_words & doc_words) >= 1:
                    return self.lineage.cascading_purge(top_node.id, reason=reason)

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
        tool_trace: Optional[List[Dict[str, Any]]] = None
    ) -> HandoverPacket:
        """
        Serializes mid-task state into an atomic Handover Packet.
        """
        packet = HandoverPacket(
            task_id=task_id,
            task_goal=task_goal,
            active_memory_ids=active_memory_ids or [],
            epistemic_state=epistemic_state or {},
            last_tool_trace=tool_trace or []
        )
        self.db.save_handover_packet(packet)
        return packet

    def import_handover(self, task_id: str) -> Optional[HandoverPacket]:
        """
        Restores mid-task state for a secondary agent.
        """
        return self.db.get_handover_packet(task_id)

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
        valid_parents = []
        inferred_category = category
        for pid in parent_ids:
            parent_node = self.db.get_memory(pid)
            if parent_node and parent_node.status in (MemoryStatus.ACTIVE, MemoryStatus.HISTORICAL):
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

    def check_health(self) -> Dict[str, Any]:
        """
        Checks the health of the database, WAL journal mode, schema version,
        and procedures directory.
        """
        return self.db.check_health(procedures_dir=self.procedural.procedures_dir)

    def close(self):
        self.db.close()
