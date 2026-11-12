import json
import math
import re
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from lethe.models import MemoryNode, MemoryStatus, StorageTier
from lethe.storage.db import DatabaseManager

import hashlib

class EmbeddingHelper:
    """
    Lightweight, deterministic text embedder using stemmed tokens and n-grams.
    Zero C-extension or API key dependency. Guarantees 100% cross-platform parity on Windows & Mac.
    """
    DIM = 256

    STOP_WORDS = {
        'what', 'is', 'my', 'the', 'a', 'an', 'in', 'on', 'at', 'of', 'for', 'to',
        'and', 'or', 'are', 'was', 'were', 'it', 'its', 'this', 'that', 'do', 'i'
    }

    SUFFIXES = ('ary', 'preferences', 'preference', 'ing', 'tion', 'tions', 'ed', 'es', 's')

    @classmethod
    def _stem(cls, word: str) -> str:
        for s in cls.SUFFIXES:
            if word.endswith(s) and len(word) > len(s) + 2:
                return word[:-len(s)]
        return word

    @classmethod
    def _hash_bucket(cls, token: str) -> int:
        return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % cls.DIM

    @classmethod
    def embed(cls, text: str) -> List[float]:
        raw_words = re.findall(r'\b[a-zA-Z0-9_]+\b', text.lower())
        content_words = [w for w in raw_words if w not in cls.STOP_WORDS]
        if not content_words:
            content_words = raw_words

        vec = [0.0] * cls.DIM
        if not content_words:
            return vec

        # Token + character 3-gram hashing
        for i, word in enumerate(content_words):
            # Token hash (strong weight)
            h1 = cls._hash_bucket(word)
            vec[h1] += 2.0

            # Stemmed token hash
            st = cls._stem(word)
            if st != word:
                h_st = cls._hash_bucket(st)
                vec[h_st] += 3.0

            # Character 3-gram hashing (subword stem matching)
            for j in range(max(1, len(word) - 2)):
                tri = word[j:j+3]
                h2 = cls._hash_bucket(tri)
                vec[h2] += 0.5

            # Word bigram
            if i < len(content_words) - 1:
                bigram = f"{word}_{content_words[i+1]}"
                h3 = cls._hash_bucket(bigram)
                vec[h3] += 1.5

        # L2 Normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 1e-9:
            vec = [x / norm for x in vec]
        return vec

    @classmethod
    def cosine_similarity(cls, vec1: List[float], vec2: List[float]) -> float:
        if not vec1 or not vec2:
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 < 1e-9 or norm2 < 1e-9:
            return 0.0
        return float(dot / (norm1 * norm2))


class RelationalTier:
    """
    Tier 1: Exact facts and structured attributes (e.g. user.home_city = Bengaluru).
    Guarantees deterministic, sub-millisecond retrieval with 0 token waste.
    """
    def __init__(self, db: DatabaseManager):
        self.db = db

    def store(self, node: MemoryNode) -> None:
        node.tier = StorageTier.RELATIONAL
        self.db.save_memory(node)

    def lookup_attribute(self, entity: str, attribute: str) -> Optional[MemoryNode]:
        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT * FROM memories 
            WHERE status = 'active'
              AND json_extract(metadata_json, '$.entity') = ?
              AND json_extract(metadata_json, '$.attribute') = ?
            ORDER BY valid_from DESC
            LIMIT 1
        """, (entity, attribute))
        row = cursor.fetchone()
        if not row:
            return None
        return self.db._row_to_node(row)


class SemanticTier:
    """
    Tier 2: Episodic and conversational memory.
    Uses dense vector similarity over SQLite with pure-Python cosine calculations.
    """
    def __init__(self, db: DatabaseManager):
        self.db = db

    def store(self, node: MemoryNode, vector: Optional[List[float]] = None) -> None:
        node.tier = StorageTier.SEMANTIC
        self.db.save_memory(node)
        
        # Calculate embedding if not provided (include category for semantic routing)
        if vector is None:
            embed_text = f"[{node.category}] {node.content}"
            vector = EmbeddingHelper.embed(embed_text)
        self.db.save_embedding(node.id, vector)

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        workspace_id: Optional[str] = None,
    ) -> List[Tuple[MemoryNode, float]]:
        query_vec = EmbeddingHelper.embed(query)
        active_embeddings = self.db.get_all_active_embeddings(workspace_id=workspace_id)

        scored = []
        for item in active_embeddings:
            score = EmbeddingHelper.cosine_similarity(query_vec, item["vector"])
            if score >= min_score:
                scored.append((item["memory_id"], score))

        scored.sort(key=lambda x: x[1], reverse=True)
        results = []
        for mem_id, score in scored[:top_k]:
            node = self.db.get_memory(mem_id)
            if node and node.status in (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT):
                results.append((node, score))
        return results


class ProceduralTier:
    """
    Tier 3: Runbooks, SOPs, and system playbooks stored in human-readable Markdown files.
    Loaded only when explicitly requested via tool call.
    """
    def __init__(self, procedures_dir: Path | str = "procedures"):
        self.procedures_dir = Path(procedures_dir)
        self.procedures_dir.mkdir(parents=True, exist_ok=True)

    def read_procedure(self, filename: str) -> Optional[str]:
        # Path traversal guard
        safe_path = (self.procedures_dir / filename).resolve()
        if not str(safe_path).startswith(str(self.procedures_dir.resolve())):
            return None
        if safe_path.exists() and safe_path.is_file():
            return safe_path.read_text(encoding="utf-8")
        return None

    def list_procedures(self) -> List[str]:
        if not self.procedures_dir.exists():
            return []
        return [f.name for f in self.procedures_dir.glob("*.md")]
