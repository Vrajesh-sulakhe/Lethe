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
)
from lethe.storage.db import DatabaseManager, get_current_workspace_id
from lethe.storage.lineage import LineageManager
from lethe.storage.tiers import RelationalTier, SemanticTier, ProceduralTier, EmbeddingHelper
from lethe.core.bitemporal import BitemporalManager
from lethe.core.trust import TrustGateManager
from lethe.core.knapsack import KnapsackPacker, count_tokens

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
    ):
        self.db = DatabaseManager(db_path)
        self.workspace_id = workspace_id or get_current_workspace_id()
        self.lineage = LineageManager(self.db)
        self.relational = RelationalTier(self.db)
        self.semantic = SemanticTier(self.db)
        self.procedural = ProceduralTier(procedures_dir)
        self.bitemporal = BitemporalManager(self.db)
        self.trust_gate = TrustGateManager(self.db)
        self.knapsack = KnapsackPacker()

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
    ) -> MemoryNode:
        """
        Stores a memory through the trust gate, bitemporal clock, and lineage DAG.
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

        now = time.time()
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
            workspace_id=workspace_id or self.workspace_id,
            scope=scope,
            metadata=metadata or {}
        )

        # 1. Trust Gate Check
        verdict = self.trust_gate.process_input(node)
        if not verdict["allowed"]:
            return node

        # 2. Bitemporal & Conflict Check
        conflicts = self.bitemporal.process_statement(node)

        # 3. Store in appropriate tier
        if tier == StorageTier.RELATIONAL:
            self.relational.store(node)
        # Always maintain semantic indexing for natural-language query recall
        self.semantic.store(node)

        # 4. Record Lineage DAG parent links
        for pid in node.parent_ids:
            self.lineage.add_lineage_edge(pid, node.id)

        return node

    def recall(
        self,
        query: str,
        token_budget: int = 500,
        as_of_time: Optional[float] = None,
        entity_filter: Optional[str] = None,
        min_relevance: float = 0.12,
        workspace_id: Optional[str] = None,
    ) -> QueryResult:
        """
        Retrieves relevant memories within strict token budget.
        Triggers honest abstention if no verified memories match or critical query terms are missing.
        """
        start_time = time.perf_counter()
        candidates: List[Tuple[MemoryNode, float]] = []
        conflicts_detected: List[str] = []

        # 1. If entity filter provided, check Relational Tier
        if entity_filter:
            parts = entity_filter.split(".")
            if len(parts) == 2:
                exact_match = self.bitemporal.query_active_attribute(parts[0], parts[1], as_of_time=as_of_time)
                if exact_match and exact_match.status in (MemoryStatus.ACTIVE, MemoryStatus.HISTORICAL, MemoryStatus.CONFLICT):
                    candidates.append((exact_match, 1.0))

        # 2. Search Semantic Tier (scoped by workspace if specified)
        semantic_matches = self.semantic.search(
            query, top_k=10, min_score=0.0, workspace_id=workspace_id
        )
        for node, score in semantic_matches:
            # Check if memory was valid at as_of_time
            if as_of_time is not None:
                if node.valid_from > as_of_time or (node.valid_to and node.valid_to <= as_of_time):
                    continue
            candidates.append((node, score))

        # 3. Honest Abstention & Epistemic Term Coverage Gate
        generic_words = {'number', 'name', 'info', 'information', 'details', 'detail', 'secret', 'give', 'tell', 'show', 'my', 'the'}
        specific_q_words = [
            EmbeddingHelper._stem(w)
            for w in re.findall(r'\b[a-zA-Z0-9_]+\b', query.lower())
            if w not in EmbeddingHelper.STOP_WORDS and w not in generic_words
        ]
        # Fall back to all non-stopwords if query consisted entirely of generic words
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

        # Filter candidates: exact relational lookups (score >= 0.99) bypass fuzzy term coverage;
        # semantic matches require at least 30% query term coverage and score >= min_relevance.
        qualified_candidates: List[Tuple[MemoryNode, float]] = []
        for node, score in candidates:
            cov = calculate_term_coverage(node)
            if (score >= 0.99 or cov >= 0.30) and score >= min_relevance:
                qualified_candidates.append((node, score))

        if not qualified_candidates:
            latency = (time.perf_counter() - start_time) * 1000.0
            abstain_msg = "I do not have verified memory regarding this query."
            tokens = count_tokens(abstain_msg)
            return QueryResult(
                query=query,
                memories=[],
                context_text=abstain_msg,
                tokens_used=tokens,
                latency_ms=latency,
                is_abstention=True
            )

        candidates = qualified_candidates

        # 4. Deduplicate candidates keeping highest score
        deduped: Dict[str, Tuple[MemoryNode, float]] = {}
        for node, score in candidates:
            if node.id not in deduped or score > deduped[node.id][1]:
                deduped[node.id] = (node, score)
                if node.status == MemoryStatus.CONFLICT:
                    conflicts_detected.append(f"Conflict on {node.category}: '{node.content}'")

        # 5. Greedy Knapsack Packing into token_budget
        candidate_list = list(deduped.values())
        packed_memories, used_tokens = self.knapsack.pack(candidate_list, token_budget)

        if not packed_memories:
            latency = (time.perf_counter() - start_time) * 1000.0
            abstain_msg = "I do not have verified memory regarding this query within the requested token budget."
            return QueryResult(
                query=query,
                memories=[],
                context_text=abstain_msg,
                tokens_used=count_tokens(abstain_msg),
                latency_ms=latency,
                is_abstention=True
            )

        # 6. Format formatted context text with source provenance citations
        lines = []
        for mem in packed_memories:
            citation = f"[Memory #{mem.id[:6]} | Category: {mem.category} | Source: {mem.source_session} ({mem.source_origin}) | Trust: {mem.trust_level:.1f}]"
            lines.append(f"{citation} {mem.content}")

        if conflicts_detected:
            lines.append("\n[EPISTEMIC CONFLICT WARNING: Multiple conflicting records exist for this topic.]")

        context_text = "\n".join(lines)
        latency = (time.perf_counter() - start_time) * 1000.0

        return QueryResult(
            query=query,
            memories=packed_memories,
            context_text=context_text,
            tokens_used=used_tokens,
            latency_ms=latency,
            is_abstention=False,
            conflicts_detected=conflicts_detected
        )

    def forget(self, target_id_or_query: str, reason: str = "user_deletion_request") -> DeletionReceipt:
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
        matches = self.semantic.search(target_id_or_query, top_k=3, min_score=0.45)
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

    def close(self):
        self.db.close()
