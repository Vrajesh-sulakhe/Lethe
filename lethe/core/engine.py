import time
import uuid
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
from lethe.storage.db import DatabaseManager
from lethe.storage.lineage import LineageManager
from lethe.storage.tiers import RelationalTier, SemanticTier, ProceduralTier
from lethe.core.bitemporal import BitemporalManager
from lethe.core.trust import TrustGateManager
from lethe.core.knapsack import KnapsackPacker, count_tokens

class LetheMemory:
    """
    Main coordinator for the Lethe Memory Operating System.
    Orchestrates tiered storage, lineage DAG unlearning, trust gates,
    bitemporal clocks, and token budgeting.
    """
    def __init__(
        self,
        db_path: Path | str = "lethe.db",
        procedures_dir: Path | str = "procedures"
    ):
        self.db = DatabaseManager(db_path)
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
        valid_from: Optional[float] = None
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
            metadata=metadata or {}
        )

        # 1. Trust Gate Check
        verdict = self.trust_gate.process_input(node)
        if not verdict["allowed"]:
            return node

        # 2. Bitemporal & Conflict Check
        conflicts = self.bitemporal.process_statement(node)

        # 3. Store in appropriate tier
        if tier == StorageTier.SEMANTIC or tier == StorageTier.EPHEMERAL:
            self.semantic.store(node)
        else:
            self.relational.store(node)

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
        min_relevance: float = 0.15
    ) -> QueryResult:
        """
        Retrieves relevant memories within strict token budget.
        Triggers honest abstention if no verified memories match.
        """
        start_time = time.perf_counter()
        candidates: List[Tuple[MemoryNode, float]] = []
        conflicts_detected: List[str] = []

        # 1. If entity filter provided, check Relational Tier
        if entity_filter:
            parts = entity_filter.split(".")
            if len(parts) == 2:
                exact_match = self.bitemporal.query_active_attribute(parts[0], parts[1], as_of_time=as_of_time)
                if exact_match and exact_match.status == MemoryStatus.ACTIVE:
                    candidates.append((exact_match, 1.0))

        # 2. Search Semantic Tier
        semantic_matches = self.semantic.search(query, top_k=10, min_score=0.0)
        for node, score in semantic_matches:
            # Check if memory was valid at as_of_time
            if as_of_time is not None:
                if node.valid_from > as_of_time or (node.valid_to and node.valid_to <= as_of_time):
                    continue
            candidates.append((node, score))

        # 3. Honest Abstention Gate
        # If no candidates or best candidate score is below min_relevance
        top_score = max((score for _, score in candidates), default=0.0)
        if not candidates or top_score < min_relevance:
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
        # If passed an explicit ID in memories table
        target_node = self.db.get_memory(target_id_or_query)
        if target_node:
            return self.lineage.cascading_purge(target_node.id, reason=reason)

        # If passed query text, find closest matching active memory
        matches = self.semantic.search(target_id_or_query, top_k=1, min_score=0.1)
        if matches:
            return self.lineage.cascading_purge(matches[0][0].id, reason=reason)

        # If nothing found, return empty receipt
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
