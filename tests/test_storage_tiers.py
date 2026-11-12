import tempfile
from pathlib import Path
import pytest
from lethe.models import MemoryNode, StorageTier, MemoryStatus
from lethe.storage.db import DatabaseManager
from lethe.storage.tiers import RelationalTier, SemanticTier, ProceduralTier

def test_storage_tiers_routing():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_tiers.db"
        db = DatabaseManager(db_path)

        # 1. Test Relational Tier
        rel_tier = RelationalTier(db)
        node_rel = MemoryNode(
            id="rel_1",
            content="user.home_city = Bengaluru",
            category="location",
            tier=StorageTier.RELATIONAL,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="s18",
            source_origin="user_explicit",
            metadata={"entity": "user", "attribute": "home_city", "value": "Bengaluru"}
        )
        rel_tier.store(node_rel)

        found_rel = rel_tier.lookup_attribute("user", "home_city")
        assert found_rel is not None
        assert found_rel.metadata["value"] == "Bengaluru"

        # 2. Test Semantic Tier (Vector embeddings)
        sem_tier = SemanticTier(db)
        node_sem1 = MemoryNode(
            id="sem_1",
            content="We discussed hiking in the mountains of Himachal",
            category="travel",
            tier=StorageTier.SEMANTIC,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="s3",
            source_origin="user_explicit"
        )
        node_sem2 = MemoryNode(
            id="sem_2",
            content="The user loves eating thin crust Margherita pizza",
            category="food",
            tier=StorageTier.SEMANTIC,
            trust_level=1.0,
            status=MemoryStatus.ACTIVE,
            source_session="s4",
            source_origin="user_explicit"
        )
        sem_tier.store(node_sem1)
        sem_tier.store(node_sem2)

        results = sem_tier.search("mountain trekking and hiking vacations", top_k=2)
        assert len(results) >= 1
        # Top result must be mountain hiking
        assert results[0][0].id == "sem_1"
        assert results[0][1] > 0.3  # similarity score

        # 3. Test Procedural Tier (Markdown Runbooks on disk)
        proc_dir = Path(tmpdir) / "procedures"
        proc_dir.mkdir()
        deploy_md = proc_dir / "deploy.md"
        deploy_md.write_text("# Deploy Steps\n1. Run docker build\n2. Push to registry", encoding="utf-8")

        proc_tier = ProceduralTier(proc_dir)
        playbook = proc_tier.read_procedure("deploy.md")
        assert playbook is not None
        assert "docker build" in playbook

        db.close()
