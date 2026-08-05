"""Cross-backend logical and physical isolation profile contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lightrag.kg import STORAGE_IMPLEMENTATIONS
from lightrag.kg.storage_profiles import (
    STORAGE_ISOLATION_CAPABILITIES,
    build_default_resource_profile,
    forced_workspace_variables,
    physical_profile_lifecycle,
    profile_binding_fingerprint,
    profile_resource_fingerprints,
    required_profile_sections,
    validate_storage_profile,
)
from lightrag.utils import check_storage_env_vars


pytestmark = pytest.mark.offline


def _selectable_implementations() -> set[str]:
    return {
        implementation
        for storage_type in STORAGE_IMPLEMENTATIONS.values()
        for implementation in storage_type["implementations"]
    }


def test_every_selectable_backend_has_an_isolation_capability() -> None:
    selectable = _selectable_implementations()

    assert len(selectable) == 23
    assert set(STORAGE_ISOLATION_CAPABILITIES) == selectable


@pytest.mark.parametrize(
    ("implementation", "section", "workspace_variable"),
    [
        ("JsonKVStorage", None, None),
        ("JsonDocStatusStorage", None, None),
        ("NetworkXStorage", None, None),
        ("NanoVectorDBStorage", None, None),
        ("FaissVectorDBStorage", None, None),
        ("RedisKVStorage", "redis", "REDIS_WORKSPACE"),
        ("RedisDocStatusStorage", "redis", "REDIS_WORKSPACE"),
        ("PGKVStorage", "postgres", "POSTGRES_WORKSPACE"),
        ("PGVectorStorage", "postgres", "POSTGRES_WORKSPACE"),
        ("PGGraphStorage", "postgres", "POSTGRES_WORKSPACE"),
        ("PGDocStatusStorage", "postgres", "POSTGRES_WORKSPACE"),
        ("Neo4JStorage", "neo4j", "NEO4J_WORKSPACE"),
        ("MongoKVStorage", "mongo", "MONGODB_WORKSPACE"),
        ("MongoDocStatusStorage", "mongo", "MONGODB_WORKSPACE"),
        ("MongoGraphStorage", "mongo", "MONGODB_WORKSPACE"),
        ("MongoVectorDBStorage", "mongo", "MONGODB_WORKSPACE"),
        ("MilvusVectorDBStorage", "milvus", "MILVUS_WORKSPACE"),
        ("QdrantVectorDBStorage", "qdrant", "QDRANT_WORKSPACE"),
        ("MemgraphStorage", "memgraph", "MEMGRAPH_WORKSPACE"),
        ("OpenSearchKVStorage", "opensearch", "OPENSEARCH_WORKSPACE"),
        ("OpenSearchDocStatusStorage", "opensearch", "OPENSEARCH_WORKSPACE"),
        ("OpenSearchGraphStorage", "opensearch", "OPENSEARCH_WORKSPACE"),
        ("OpenSearchVectorDBStorage", "opensearch", "OPENSEARCH_WORKSPACE"),
    ],
)
def test_backend_capability_matrix_is_explicit(
    implementation: str, section: str | None, workspace_variable: str | None
) -> None:
    capability = STORAGE_ISOLATION_CAPABILITIES[implementation]

    assert capability.profile_section == section
    assert capability.workspace_environment_variable == workspace_variable


def test_required_sections_follow_the_active_four_storage_types() -> None:
    implementations = (
        "MongoKVStorage",
        "MilvusVectorDBStorage",
        "MemgraphStorage",
        "OpenSearchDocStatusStorage",
    )

    assert required_profile_sections(implementations) == (
        "input_dir",
        "memgraph",
        "milvus",
        "mongo",
        "opensearch",
        "working_dir",
    )
    assert forced_workspace_variables(implementations) == (
        "MEMGRAPH_WORKSPACE",
        "MILVUS_WORKSPACE",
        "MONGODB_WORKSPACE",
        "OPENSEARCH_WORKSPACE",
    )


@pytest.mark.parametrize(
    ("implementation", "section", "config"),
    [
        (
            "PGKVStorage",
            "postgres",
            {
                "host": "pg-a",
                "port": 5432,
                "user": "rag",
                "password": "secret",
                "database": "rag_a",
            },
        ),
        (
            "Neo4JStorage",
            "neo4j",
            {
                "uri": "bolt://neo-a:7687",
                "username": "neo4j",
                "password": "secret",
                "database": "neo4j_a",
            },
        ),
        ("RedisKVStorage", "redis", {"uri": "redis://redis-a:6379/1"}),
        (
            "MongoKVStorage",
            "mongo",
            {"uri": "mongodb://mongo-a:27017", "database": "rag_a"},
        ),
        (
            "MilvusVectorDBStorage",
            "milvus",
            {"uri": "http://milvus-a:19530", "db_name": "rag_a"},
        ),
        (
            "QdrantVectorDBStorage",
            "qdrant",
            {"url": "http://qdrant-a:6333", "collection_prefix": "rag_a"},
        ),
        (
            "MemgraphStorage",
            "memgraph",
            {"uri": "bolt://memgraph-a:7687", "database": "memgraph_a"},
        ),
        (
            "OpenSearchKVStorage",
            "opensearch",
            {"hosts": ["search-a:9200"], "index_prefix": "rag_a"},
        ),
    ],
)
def test_each_external_backend_accepts_a_complete_physical_section(
    tmp_path: Path, implementation: str, section: str, config: dict
) -> None:
    profile = {
        "dedicated": True,
        "working_dir": str(tmp_path / implementation / "rag"),
        "input_dir": str(tmp_path / implementation / "inputs"),
        section: config,
    }

    validate_storage_profile(
        implementation,
        profile,
        required_profile_sections((implementation,)),
    )


@pytest.mark.parametrize(
    ("implementation", "section", "config"),
    [
        (
            "PGKVStorage",
            "postgres",
            {
                "host": "pg-a",
                "port": 5432,
                "user": "rag",
                "password": "secret",
                "database": "rag",
            },
        ),
        (
            "Neo4JStorage",
            "neo4j",
            {
                "uri": "bolt://neo4j-a:7687",
                "username": "neo4j",
                "password": "secret",
                "database": "neo4j",
            },
        ),
        ("RedisKVStorage", "redis", {"uri": "redis://redis-a:6379/0"}),
        (
            "MongoKVStorage",
            "mongo",
            {"uri": "mongodb://mongo-a:27017", "database": "rag"},
        ),
        (
            "MilvusVectorDBStorage",
            "milvus",
            {"uri": "http://milvus-a:19530", "db_name": "default"},
        ),
        (
            "QdrantVectorDBStorage",
            "qdrant",
            {"url": "http://qdrant-a:6333", "collection_prefix": "rag_a"},
        ),
        (
            "MemgraphStorage",
            "memgraph",
            {"uri": "bolt://memgraph-a:7687", "database": "memgraph"},
        ),
        (
            "OpenSearchKVStorage",
            "opensearch",
            {"hosts": "search-a:9200", "index_prefix": "rag_a"},
        ),
    ],
)
def test_complete_physical_section_replaces_process_environment_requirements(
    monkeypatch: pytest.MonkeyPatch,
    implementation: str,
    section: str,
    config: dict,
) -> None:
    from lightrag.kg import STORAGE_ENV_REQUIREMENTS

    for variable in STORAGE_ENV_REQUIREMENTS.get(implementation, ()):
        monkeypatch.delenv(variable, raising=False)

    check_storage_env_vars(implementation, {section: config})


def test_incomplete_physical_section_fails_before_environment_fallback() -> None:
    with pytest.raises(ValueError, match="collection_prefix"):
        check_storage_env_vars(
            "QdrantVectorDBStorage",
            {"qdrant": {"url": "http://qdrant-a:6333"}},
        )


def test_resource_fingerprint_ignores_credentials_but_detects_resource_changes(
    tmp_path: Path,
) -> None:
    required = ("working_dir", "input_dir", "mongo")
    base = {
        "working_dir": str(tmp_path / "rag"),
        "input_dir": str(tmp_path / "inputs"),
        "mongo": {
            "uri": "mongodb://first:secret@mongo:27017",
            "database": "rag",
        },
    }
    changed_credentials = {
        **base,
        "mongo": {
            "uri": "mongodb://second:different@mongo:27017",
            "database": "rag",
        },
    }
    changed_database = {
        **base,
        "mongo": {
            "uri": "mongodb://first:secret@mongo:27017",
            "database": "rag_b",
        },
    }

    first = profile_resource_fingerprints(base, required)
    assert profile_resource_fingerprints(changed_credentials, required) == first
    assert (
        profile_resource_fingerprints(changed_database, required)["mongo"]
        != first["mongo"]
    )
    assert profile_binding_fingerprint(base, required) == profile_binding_fingerprint(
        changed_credentials, required
    )
    assert profile_binding_fingerprint(base, required) != profile_binding_fingerprint(
        changed_database, required
    )


def test_physical_lifecycle_is_explicit_and_rejects_destructive_escalation() -> None:
    expected = {
        "resource_ownership": "operator",
        "provisioning": "preprovisioned",
        "deletion": "drop_workspace_namespaces",
        "backup": "operator_managed",
    }

    assert physical_profile_lifecycle("profile-a", {}).public_dict() == expected
    assert (
        physical_profile_lifecycle("profile-a", {"lifecycle": expected}).public_dict()
        == expected
    )
    with pytest.raises(ValueError, match="deletion"):
        physical_profile_lifecycle(
            "profile-a", {"lifecycle": {"deletion": "drop_database"}}
        )
    with pytest.raises(ValueError, match="unknown fields"):
        physical_profile_lifecycle(
            "profile-a", {"lifecycle": {"delete_endpoint": True}}
        )


def test_malformed_uri_has_a_stable_resource_fingerprint(tmp_path: Path) -> None:
    profile = {
        "working_dir": str(tmp_path / "rag"),
        "input_dir": str(tmp_path / "inputs"),
        "redis": {"uri": "redis://cache.example:invalid/0"},
    }

    fingerprints = profile_resource_fingerprints(
        profile, ("working_dir", "input_dir", "redis")
    )

    assert set(fingerprints) == {"working_dir", "input_dir", "redis"}


@pytest.mark.parametrize(
    ("section", "first", "second"),
    [
        (
            "redis",
            {"uri": "redis://cache.example:6379/0"},
            {"uri": "redis://other:secret@cache.example:6379/12"},
        ),
        (
            "qdrant",
            {"url": "http://qdrant.example:6333", "collection_prefix": "first"},
            {"url": "http://qdrant.example:6333/", "collection_prefix": "second"},
        ),
        (
            "opensearch",
            {"hosts": ["search.example:9200"], "index_prefix": "first"},
            {"hosts": ["https://SEARCH.example:9200/"], "index_prefix": "second"},
        ),
    ],
)
def test_logical_namespace_changes_do_not_create_a_physical_resource(
    tmp_path: Path, section: str, first: dict, second: dict
) -> None:
    required = ("working_dir", "input_dir", section)
    common = {
        "working_dir": str(tmp_path / "rag"),
        "input_dir": str(tmp_path / "inputs"),
    }

    first_fingerprints = profile_resource_fingerprints(
        {**common, section: first}, required
    )
    second_fingerprints = profile_resource_fingerprints(
        {**common, section: second}, required
    )

    assert first_fingerprints[section] == second_fingerprints[section]


def test_default_resource_profile_resolves_non_secret_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("QDRANT_URL", "http://default-qdrant:6333")
    required = required_profile_sections(("QdrantVectorDBStorage",))

    profile = build_default_resource_profile(
        working_dir=str(tmp_path / "rag"),
        input_dir=str(tmp_path / "inputs"),
        workspace="default-workspace",
        required_sections=required,
    )

    assert profile["qdrant"] == {"url": "http://default-qdrant:6333"}
    assert "api_key" not in profile["qdrant"]
