"""Real-service strict physical-isolation integration test.

The two profiles use different PostgreSQL, Neo4j, and Redis endpoints.  Both
write the same logical IDs so namespace mistakes cannot be hidden by different
test keys.  Dropping every LightRAG namespace in profile A must leave profile B
intact; endpoint/service deletion is deliberately outside LightRAG ownership.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lightrag import LightRAG
from lightrag.api.catalog import PostgresCatalogProvider
from lightrag.api.knowledge_bases import KnowledgeBaseRecord
from lightrag.base import DocStatus
from lightrag.kg.shared_storage import finalize_share_data, initialize_share_data
from lightrag.kg.storage_profiles import (
    profile_resource_fingerprints,
    required_profile_sections,
    validate_storage_profile,
)
from lightrag.utils import EmbeddingFunc
from lightrag.workspace import WorkspaceBinding


pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _enabled() -> bool:
    return os.getenv("LIGHTRAG_PHYSICAL_IT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _backup_phase() -> str:
    return os.getenv("LIGHTRAG_PHYSICAL_BACKUP_PHASE", "").strip().lower()


_EXTENDED_STORAGES: dict[str, tuple[str, str, str, str]] = {
    "qdrant": (
        "JsonKVStorage",
        "QdrantVectorDBStorage",
        "NetworkXStorage",
        "JsonDocStatusStorage",
    ),
    "memgraph": (
        "JsonKVStorage",
        "NanoVectorDBStorage",
        "MemgraphStorage",
        "JsonDocStatusStorage",
    ),
    "milvus": (
        "JsonKVStorage",
        "MilvusVectorDBStorage",
        "NetworkXStorage",
        "JsonDocStatusStorage",
    ),
    "mongo": (
        "MongoKVStorage",
        "MongoVectorDBStorage",
        "MongoGraphStorage",
        "MongoDocStatusStorage",
    ),
    "opensearch": (
        "OpenSearchKVStorage",
        "OpenSearchVectorDBStorage",
        "OpenSearchGraphStorage",
        "OpenSearchDocStatusStorage",
    ),
}


def _extended_profile(tmp_path: Path, backend: str, suffix: str) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "dedicated": True,
        "working_dir": str(tmp_path / backend / suffix / "rag"),
        "input_dir": str(tmp_path / backend / suffix / "input"),
    }
    is_a = suffix == "a"
    if backend == "qdrant":
        profile[backend] = {
            "url": f"http://127.0.0.1:{56333 if is_a else 56334}",
            "collection_prefix": f"physical_it_{suffix}",
        }
    elif backend == "memgraph":
        profile[backend] = {
            "uri": f"bolt://127.0.0.1:{57689 if is_a else 57690}",
            "database": "memgraph",
        }
    elif backend == "milvus":
        profile[backend] = {
            "uri": f"http://127.0.0.1:{59530 if is_a else 59531}",
            "db_name": "default",
        }
    elif backend == "mongo":
        profile[backend] = {
            "uri": (
                f"mongodb://127.0.0.1:{57017 if is_a else 57018}/?directConnection=true"
            ),
            "database": "lightrag",
        }
    elif backend == "opensearch":
        profile[backend] = {
            "hosts": f"127.0.0.1:{59200 if is_a else 59201}",
            "index_prefix": f"physical_it_{suffix}",
            "username": "admin",
            "password": "PhysicalItAdmin1!",
            "use_ssl": False,
            "verify_certs": False,
        }
    else:  # pragma: no cover - parametrization is the closed backend set
        raise AssertionError(f"Unsupported extended backend {backend}")
    return profile


def _profile(tmp_path: Path, suffix: str) -> dict[str, Any]:
    upper = suffix.upper()
    return {
        "dedicated": True,
        "lifecycle": {
            "resource_ownership": "operator",
            "provisioning": "preprovisioned",
            "deletion": "drop_workspace_namespaces",
            "backup": "operator_managed",
        },
        "working_dir": str(tmp_path / suffix / "rag"),
        "input_dir": str(tmp_path / suffix / "input"),
        "postgres": {
            "host": os.getenv(f"IT_POSTGRES_{upper}_HOST", "127.0.0.1"),
            "port": int(
                os.getenv(
                    f"IT_POSTGRES_{upper}_PORT",
                    "55432" if suffix == "a" else "55433",
                )
            ),
            "user": "rag",
            "password": "rag-physical-it",
            "database": "rag",
        },
        "neo4j": {
            "uri": (
                "neo4j://"
                + os.getenv(f"IT_NEO4J_{upper}_HOST", "127.0.0.1")
                + ":"
                + os.getenv(
                    f"IT_NEO4J_{upper}_BOLT_PORT",
                    "57687" if suffix == "a" else "57688",
                )
            ),
            "username": "neo4j",
            "password": "neo4j-physical-it",
            "database": "neo4j",
        },
        "redis": {
            "uri": (
                "redis://"
                + os.getenv(f"IT_REDIS_{upper}_HOST", "127.0.0.1")
                + ":"
                + os.getenv(
                    f"IT_REDIS_{upper}_PORT",
                    "56379" if suffix == "a" else "56380",
                )
                + "/0"
            )
        },
    }


async def _llm(*_args: Any, **_kwargs: Any) -> str:
    return ""


async def _embed(texts: list[str]) -> np.ndarray:
    # Deterministic and intentionally identical across profiles.
    return np.asarray(
        [
            [
                float((sum(text.encode("utf-8")) + offset) % 31) / 31
                for offset in range(8)
            ]
            for text in texts
        ],
        dtype=np.float32,
    )


async def _new_rag(
    *,
    workspace_id: str,
    profile_id: str,
    profile: dict[str, Any],
    kv_storage: str = "RedisKVStorage",
    vector_storage: str = "PGVectorStorage",
    graph_storage: str = "Neo4JStorage",
    doc_status_storage: str = "PGDocStatusStorage",
) -> LightRAG:
    Path(profile["working_dir"]).mkdir(parents=True, exist_ok=True)
    Path(profile["input_dir"]).mkdir(parents=True, exist_ok=True)
    rag = LightRAG(
        working_dir=profile["working_dir"],
        input_dir=profile["input_dir"],
        workspace=workspace_id,
        workspace_binding=WorkspaceBinding.named(
            workspace_id,
            storage_profile_id=profile_id,
        ),
        storage_profile={"id": profile_id, **profile},
        kv_storage=kv_storage,
        vector_storage=vector_storage,
        graph_storage=graph_storage,
        doc_status_storage=doc_status_storage,
        llm_model_func=_llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=8,
            max_token_size=8192,
            func=_embed,
        ),
    )
    await rag.initialize_storages()
    await rag.check_and_migrate_data()
    return rag


def _status(marker: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "content_summary": marker,
        "content_length": len(marker),
        "file_path": "same-source.txt",
        "status": DocStatus.PROCESSED.value,
        "created_at": now,
        "updated_at": now,
        "track_id": "same-track-id",
        "chunks_count": 1,
        "chunks_list": ["same-chunk-id"],
        "error_msg": None,
        "metadata": {"marker": marker},
        "content_hash": "same-content-hash",
    }


async def _write_sentinel(rag: LightRAG, marker: str) -> None:
    await rag.full_docs.upsert(
        {"same-doc-id": {"content": marker, "file_path": "same-source.txt"}}
    )
    chunk = {
        "content": marker,
        "file_path": "same-source.txt",
        "full_doc_id": "same-doc-id",
        "tokens": 1,
        "chunk_order_index": 0,
    }
    await rag.text_chunks.upsert({"same-chunk-id": chunk})
    await rag.chunks_vdb.upsert({"same-chunk-id": chunk})
    await rag.chunks_vdb.index_done_callback()
    await rag.chunk_entity_relation_graph.upsert_node(
        "SAME_ENTITY",
        {
            "entity_id": "SAME_ENTITY",
            "entity_type": "integration",
            "description": marker,
            "source_id": "same-chunk-id",
            "file_path": "same-source.txt",
        },
    )
    await rag.doc_status.upsert({"same-doc-id": _status(marker)})


async def _assert_sentinel(rag: LightRAG, marker: str) -> None:
    assert (await rag.full_docs.get_by_id("same-doc-id"))["content"] == marker
    assert (await rag.text_chunks.get_by_id("same-chunk-id"))["content"] == marker
    graph_node = await rag.chunk_entity_relation_graph.get_node("SAME_ENTITY")
    assert graph_node is not None and graph_node["description"] == marker
    doc_status = await rag.doc_status.get_by_id("same-doc-id")
    assert doc_status is not None and doc_status["metadata"]["marker"] == marker
    # Atlas Search and OpenSearch make a newly-created vector index visible
    # asynchronously. Poll a bounded deadline instead of weakening the check.
    vector_hits: list[dict[str, Any]] = []
    for _attempt in range(40):
        vector_hits = await rag.chunks_vdb.query(marker, top_k=5)
        if any(
            hit.get("id") == "same-chunk-id" and hit.get("content") == marker
            for hit in vector_hits
        ):
            break
        await asyncio.sleep(0.25)
    else:
        pytest.fail(
            f"Vector sentinel for {marker!r} was not visible before the deadline; "
            f"last hits={vector_hits!r}"
        )


async def _drop_namespaces(rag: LightRAG) -> None:
    rag.validate_storage_bindings(stage="integration-pre-delete")
    for storage in rag._workspace_storages():
        await storage.drop()


@pytest.mark.asyncio
async def test_two_physical_profiles_are_isolated_and_drop_is_scoped(
    tmp_path: Path,
) -> None:
    if not _enabled():
        pytest.skip(
            "Set LIGHTRAG_PHYSICAL_IT=true and start "
            "docker-compose.integration-physical.yml"
        )

    active = (
        "RedisKVStorage",
        "PGVectorStorage",
        "Neo4JStorage",
        "PGDocStatusStorage",
    )
    required = required_profile_sections(active)
    profile_a = _profile(tmp_path, "a")
    profile_b = _profile(tmp_path, "b")
    validate_storage_profile("physical-a", profile_a, required)
    validate_storage_profile("physical-b", profile_b, required)
    fingerprints_a = profile_resource_fingerprints(profile_a, required)
    fingerprints_b = profile_resource_fingerprints(profile_b, required)
    assert all(
        fingerprints_a[section] != fingerprints_b[section] for section in required
    )

    initialize_share_data()
    rag_a: LightRAG | None = None
    rag_b: LightRAG | None = None
    try:
        rag_a, rag_b = await asyncio.gather(
            _new_rag(
                workspace_id="kb_physical_it_a",
                profile_id="physical-a",
                profile=profile_a,
            ),
            _new_rag(
                workspace_id="kb_physical_it_b",
                profile_id="physical-b",
                profile=profile_b,
            ),
        )
        await asyncio.gather(
            _write_sentinel(rag_a, "PROFILE_A"),
            _write_sentinel(rag_b, "PROFILE_B"),
        )
        await asyncio.gather(
            _assert_sentinel(rag_a, "PROFILE_A"),
            _assert_sentinel(rag_b, "PROFILE_B"),
        )

        await _drop_namespaces(rag_a)
        await rag_a.finalize_storages()
        rag_a = None

        # The destructive operation against A must not affect any family in B.
        await _assert_sentinel(rag_b, "PROFILE_B")
    finally:
        if rag_a is not None:
            try:
                await _drop_namespaces(rag_a)
            finally:
                await rag_a.finalize_storages()
        if rag_b is not None:
            try:
                await _drop_namespaces(rag_b)
            finally:
                await rag_b.finalize_storages()
        finalize_share_data()


@pytest.mark.parametrize("backend", tuple(_EXTENDED_STORAGES))
@pytest.mark.asyncio
async def test_extended_backend_uses_distinct_endpoints_and_scoped_drop(
    tmp_path: Path,
    backend: str,
) -> None:
    selected = os.getenv("LIGHTRAG_EXTENDED_PHYSICAL_IT", "").strip().lower()
    if selected != backend:
        pytest.skip(f"Set LIGHTRAG_EXTENDED_PHYSICAL_IT={backend}")

    active = _EXTENDED_STORAGES[backend]
    profile_a = _extended_profile(tmp_path, backend, "a")
    profile_b = _extended_profile(tmp_path, backend, "b")
    required = required_profile_sections(active)
    validate_storage_profile(f"{backend}-a", profile_a, required)
    validate_storage_profile(f"{backend}-b", profile_b, required)
    fingerprints_a = profile_resource_fingerprints(profile_a, required)
    fingerprints_b = profile_resource_fingerprints(profile_b, required)
    assert all(
        fingerprints_a[section] != fingerprints_b[section] for section in required
    )

    initialize_share_data()
    rag_a: LightRAG | None = None
    rag_b: LightRAG | None = None
    try:
        rag_a, rag_b = await asyncio.gather(
            _new_rag(
                workspace_id=f"kb_{backend}_it_a",
                profile_id=f"{backend}-a",
                profile=profile_a,
                kv_storage=active[0],
                vector_storage=active[1],
                graph_storage=active[2],
                doc_status_storage=active[3],
            ),
            _new_rag(
                workspace_id=f"kb_{backend}_it_b",
                profile_id=f"{backend}-b",
                profile=profile_b,
                kv_storage=active[0],
                vector_storage=active[1],
                graph_storage=active[2],
                doc_status_storage=active[3],
            ),
        )
        await asyncio.gather(
            _write_sentinel(rag_a, "PROFILE_A"),
            _write_sentinel(rag_b, "PROFILE_B"),
        )
        await asyncio.gather(
            _assert_sentinel(rag_a, "PROFILE_A"),
            _assert_sentinel(rag_b, "PROFILE_B"),
        )

        await _drop_namespaces(rag_a)
        await rag_a.finalize_storages()
        rag_a = None
        await _assert_sentinel(rag_b, "PROFILE_B")
    finally:
        if rag_a is not None:
            try:
                await _drop_namespaces(rag_a)
            finally:
                await rag_a.finalize_storages()
        if rag_b is not None:
            try:
                await _drop_namespaces(rag_b)
            finally:
                await rag_b.finalize_storages()
        finalize_share_data()


@pytest.mark.asyncio
async def test_operator_backup_restore_recovery_point(tmp_path: Path) -> None:
    """Seed or verify the operator-managed all-family recovery exercise.

    The test deliberately leaves the seed namespaces intact. The compose runbook
    takes one recovery point while no LightRAG process is connected, recreates
    the service volumes, restores them, then runs the ``verify`` phase. Keeping
    the service-native dump/load commands outside pytest makes the ownership
    boundary explicit: LightRAG never receives permission to back up an endpoint.
    """

    phase = _backup_phase()
    if phase not in {"seed", "verify"}:
        pytest.skip("Set LIGHTRAG_PHYSICAL_BACKUP_PHASE=seed or verify")

    profile = _profile(tmp_path, "a")
    initialize_share_data()
    rag: LightRAG | None = None
    try:
        rag = await _new_rag(
            workspace_id="kb_physical_backup",
            profile_id="physical-backup",
            profile=profile,
        )
        if phase == "seed":
            await _write_sentinel(rag, "BACKUP_RECOVERY_POINT")
            provider = PostgresCatalogProvider(
                {
                    **profile["postgres"],
                    "min_size": 1,
                    "max_size": 2,
                }
            )
            try:
                await provider.initialize(KnowledgeBaseRecord.legacy_default(""))
            finally:
                await provider.finalize()
        else:
            await _assert_sentinel(rag, "BACKUP_RECOVERY_POINT")
            import asyncpg

            connection = await asyncpg.connect(**profile["postgres"])
            try:
                catalog_default = await connection.fetchval(
                    "SELECT id FROM lightrag_workspace_catalog WHERE id = 'default'"
                )
            finally:
                await connection.close()
            assert catalog_default == "default"
            await _drop_namespaces(rag)
    finally:
        if rag is not None:
            await rag.finalize_storages()
        finalize_share_data()
