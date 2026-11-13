from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import time
import uuid

class StorageTier(str, Enum):
    RELATIONAL = "relational"   # Exact key-value, attributes
    SEMANTIC = "semantic"       # Conversational, opinions, discussions
    PROCEDURAL = "procedural"   # Runbooks, markdown files
    EPHEMERAL = "ephemeral"     # Working context / active session

class TrustLevel(float, Enum):
    USER_EXPLICIT = 1.0         # Direct user utterance
    TOOL_VERIFIED = 0.8         # Verified tool output / database read
    AGENT_INFERRED = 0.5        # Agent deduction / summary
    UNTRUSTED_EXTERNAL = 0.1    # Web search, uploaded untrusted document

class MemoryStatus(str, Enum):
    ACTIVE = "active"
    HISTORICAL = "historical"
    CONFLICT = "conflict"
    TOMBSTONED = "tombstoned"
    QUARANTINED = "quarantined"

class CodeAnchor(BaseModel):
    file_path: str
    symbol_name: Optional[str] = None
    git_sha: Optional[str] = None
    content_hash: Optional[str] = None

class MemoryNode(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    category: str = "general"
    tier: StorageTier = StorageTier.SEMANTIC
    trust_level: float = 1.0
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_session: str = "default_session"
    source_origin: str = "user_explicit"
    created_at: float = Field(default_factory=time.time)
    valid_from: float = Field(default_factory=time.time)
    valid_to: Optional[float] = None
    parent_ids: List[str] = Field(default_factory=list)  # Lineage DAG links
    workspace_id: Optional[str] = None                   # Scoped project/repo identifier
    scope: str = "project"                                # 'project' or 'global'
    code_anchor: Optional[CodeAnchor] = None             # Decision-to-code drift anchor
    metadata: Dict[str, Any] = Field(default_factory=dict)

class QueryResult(BaseModel):
    query: str
    memories: List[MemoryNode]
    context_text: str
    tokens_used: int
    latency_ms: float
    is_abstention: bool = False
    conflicts_detected: List[str] = Field(default_factory=list)
    drift_warnings: List[str] = Field(default_factory=list)

class HandoverPacket(BaseModel):
    task_id: str
    task_goal: str
    active_memory_ids: List[str]
    epistemic_state: Dict[str, Any] = Field(default_factory=dict)
    last_tool_trace: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)

class DeletionReceipt(BaseModel):
    target_id: str
    purged_node_ids: List[str]
    purged_count: int
    verification_hash: str
    timestamp: float = Field(default_factory=time.time)

