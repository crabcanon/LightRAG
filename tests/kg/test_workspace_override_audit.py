"""Startup audit tests for storage-level workspace override consistency."""

from pathlib import Path

import pytest

from lightrag.kg.storage_profiles import (
    StorageWorkspaceConsistencyError,
    audit_workspace_overrides,
    validate_storage_profile,
)


pytestmark = pytest.mark.offline


ACTIVE_MIXED = {
    "kv": "RedisKVStorage",
    "vector": "PGVectorStorage",
    "graph": "Neo4JStorage",
    "doc_status": "PGDocStatusStorage",
}


@pytest.mark.parametrize(
    ("implementation", "variable"),
    [
        ("PGKVStorage", "POSTGRES_WORKSPACE"),
        ("MongoKVStorage", "MONGODB_WORKSPACE"),
        ("RedisKVStorage", "REDIS_WORKSPACE"),
        ("Neo4JStorage", "NEO4J_WORKSPACE"),
        ("MilvusVectorDBStorage", "MILVUS_WORKSPACE"),
        ("QdrantVectorDBStorage", "QDRANT_WORKSPACE"),
        ("MemgraphStorage", "MEMGRAPH_WORKSPACE"),
        ("OpenSearchKVStorage", "OPENSEARCH_WORKSPACE"),
    ],
)
def test_multi_workspace_rejects_every_active_environment_override(
    implementation: str, variable: str
) -> None:
    with pytest.raises(StorageWorkspaceConsistencyError, match=variable):
        audit_workspace_overrides(
            mode="multi",
            storage_implementations={"family": implementation},
            server_workspace="",
            environment={variable: "collapsed"},
            config_path=Path("does-not-exist.ini"),
        )


def test_multi_workspace_rejects_postgres_config_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text("[postgres]\nworkspace = collapsed\n", encoding="utf-8")

    with pytest.raises(
        StorageWorkspaceConsistencyError,
        match=r"config\.ini\[postgres\]\.workspace",
    ):
        audit_workspace_overrides(
            mode="multi",
            storage_implementations=ACTIVE_MIXED,
            server_workspace="",
            environment={},
            config_path=config_path,
        )


def test_legacy_mode_allows_one_consistent_backend_override() -> None:
    all_postgres = {
        "kv": "PGKVStorage",
        "vector": "PGVectorStorage",
        "graph": "PGGraphStorage",
        "doc_status": "PGDocStatusStorage",
    }

    audit = audit_workspace_overrides(
        mode="legacy",
        storage_implementations=all_postgres,
        server_workspace="legacy",
        environment={"POSTGRES_WORKSPACE": "existing"},
        config_path=Path("does-not-exist.ini"),
    )

    assert audit.resolved_workspace == "existing"
    assert audit.public_dict()["override_sources"] == ["POSTGRES_WORKSPACE"] * 4


def test_legacy_mode_rejects_mixed_family_resolution() -> None:
    with pytest.raises(
        StorageWorkspaceConsistencyError, match="different legacy workspaces"
    ):
        audit_workspace_overrides(
            mode="legacy",
            storage_implementations=ACTIVE_MIXED,
            server_workspace="legacy",
            environment={"POSTGRES_WORKSPACE": "other"},
            config_path=Path("does-not-exist.ini"),
        )


def test_storage_profile_cannot_override_logical_workspace() -> None:
    profile = {
        "dedicated": True,
        "working_dir": "./rag",
        "input_dir": "./inputs",
        "redis": {
            "uri": "redis://localhost:6379/1",
            "workspace": "collapsed",
        },
    }

    with pytest.raises(ValueError, match="cannot override logical workspace"):
        validate_storage_profile(
            "unsafe", profile, ("working_dir", "input_dir", "redis")
        )
