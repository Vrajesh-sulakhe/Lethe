import hashlib
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Set
from lethe.models import MemoryNode, MemoryStatus, StorageTier
from lethe.storage.db import DatabaseManager
from lethe.storage.tiers import EmbeddingHelper


class MemorySynthesizer:
    """
    Identifies clusters of related atomic memories across sessions ripe for consolidation.
    Provides Zero-Key Agent-Driven Memory Synthesis ("Dreaming") allowing connected coding
    agents to compress fragmented sprint decisions into consolidated runbooks or architectural nodes.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    def find_dream_candidates(
        self,
        category: Optional[str] = None,
        workspace_id: Optional[str] = None,
        min_cluster_size: int = 2,
        max_clusters: int = 10,
        branch: str = "main",
    ) -> List[Dict[str, Any]]:
        """
        Scans active atomic memories and groups them into thematic clusters based on
        shared category, lexical stem overlap, and semantic embedding similarity.
        """
        all_memories = self.db.list_memories(
            category=category,
            status=MemoryStatus.ACTIVE,
            workspace_id=workspace_id,
            branch=branch,
        )

        # Filter for atomic memories (exclude tombstoned or already heavily derived nodes)
        candidates = [
            m for m in all_memories
            if m.tier in (StorageTier.SEMANTIC, StorageTier.EPHEMERAL, StorageTier.RELATIONAL)
            and m.status == MemoryStatus.ACTIVE
        ]

        if len(candidates) < min_cluster_size:
            return []

        # Group by category first
        by_category: Dict[str, List[MemoryNode]] = {}
        for m in candidates:
            by_category.setdefault(m.category, []).append(m)

        clusters: List[List[MemoryNode]] = []

        for cat, nodes in by_category.items():
            if len(nodes) < min_cluster_size:
                continue

            # If small category group, consider it a candidate cluster directly
            if len(nodes) <= 5:
                clusters.append(nodes)
                continue

            # For larger categories, perform greedy centroid/leader clustering via embeddings
            cat_clusters = self._cluster_nodes(nodes, threshold=0.35)
            for c in cat_clusters:
                if len(c) >= min_cluster_size:
                    clusters.append(c)

        # Sort clusters by size (largest first)
        clusters.sort(key=lambda c: len(c), reverse=True)
        clusters = clusters[:max_clusters]

        results: List[Dict[str, Any]] = []
        for idx, cluster_nodes in enumerate(clusters):
            cat = cluster_nodes[0].category
            # Extract common topic keywords
            words: List[str] = []
            for n in cluster_nodes:
                tokens = [
                    w for w in n.content.lower().split()
                    if len(w) > 3 and w not in EmbeddingHelper.STOP_WORDS
                ]
                words.extend(tokens)
            common_words = [w for w, _ in Counter(words).most_common(3)]
            topic_str = f"{cat}: " + ", ".join(common_words) if common_words else cat

            cluster_id = hashlib.sha256(
                f"{cat}:{','.join(sorted(n.id for n in cluster_nodes))}".encode("utf-8")
            ).hexdigest()[:12]

            results.append({
                "cluster_id": cluster_id,
                "category": cat,
                "topic": topic_str,
                "size": len(cluster_nodes),
                "parent_ids": [n.id for n in cluster_nodes],
                "candidate_memories": [
                    {
                        "id": n.id,
                        "content": n.content,
                        "category": n.category,
                        "tier": n.tier.value,
                        "created_at": n.created_at,
                    }
                    for n in cluster_nodes
                ],
                "suggested_action": "consolidate",
            })

        return results

    def _cluster_nodes(
        self,
        nodes: List[MemoryNode],
        threshold: float = 0.35,
    ) -> List[List[MemoryNode]]:
        """Greedy leader clustering using embedding cosine similarity."""
        embeddings = [EmbeddingHelper.embed(n.content) for n in nodes]
        clusters: List[List[int]] = []
        assigned = set()

        for i in range(len(nodes)):
            if i in assigned:
                continue
            current_cluster = [i]
            assigned.add(i)
            for j in range(i + 1, len(nodes)):
                if j in assigned:
                    continue
                sim = EmbeddingHelper.cosine_similarity(embeddings[i], embeddings[j])
                if sim >= threshold:
                    current_cluster.append(j)
                    assigned.add(j)
            clusters.append(current_cluster)

        return [[nodes[idx] for idx in cl] for cl in clusters]

