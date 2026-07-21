"""Driver-level tests for instance-scoped physical storage profiles."""

from __future__ import annotations

import numpy as np
import pytest


pytestmark = pytest.mark.offline


class _Embedding:
    embedding_dim = 8
    max_token_size = 512
    model_name = "profile-test"

    async def __call__(self, texts, **_kwargs):
        return np.zeros((len(texts), self.embedding_dim), dtype=np.float32)


def _vector_config(section: str, value: dict) -> dict:
    return {
        "embedding_batch_num": 4,
        "working_dir": ".",
        "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.2},
        "storage_profile": {section: value},
    }


def test_mongo_profile_suppresses_global_workspace_override(monkeypatch) -> None:
    pytest.importorskip("pymongo")
    from lightrag.kg.mongo_impl import MongoKVStorage

    monkeypatch.setenv("MONGODB_WORKSPACE", "forced")
    storage = MongoKVStorage(
        namespace="full_docs",
        workspace="kb_a",
        global_config={
            "storage_profile": {
                "mongo": {
                    "uri": "mongodb://mongo-a:27017",
                    "database": "rag_a",
                }
            }
        },
        embedding_func=None,
    )

    assert storage.workspace == "kb_a"
    assert storage._collection_name == "kb_a_full_docs"


def test_milvus_profile_controls_connection_database_and_workspace(monkeypatch) -> None:
    pytest.importorskip("pymilvus")
    from lightrag.kg.milvus_impl import MilvusVectorDBStorage

    monkeypatch.setenv("MILVUS_WORKSPACE", "forced")
    storage = MilvusVectorDBStorage(
        namespace="chunks",
        workspace="kb_a",
        global_config=_vector_config(
            "milvus",
            {
                "uri": "http://milvus-a:19530",
                "db_name": "rag_a",
                "token": "test-token",
            },
        ),
        embedding_func=_Embedding(),
        meta_fields={"content"},
    )

    connection = storage._get_milvus_connection_kwargs()
    assert storage.workspace == "kb_a"
    assert connection["uri"] == "http://milvus-a:19530"
    assert connection["db_name"] == "rag_a"
    assert connection["token"] == "test-token"


def test_qdrant_profile_controls_endpoint_collection_and_workspace(monkeypatch) -> None:
    pytest.importorskip("qdrant_client")
    from lightrag.kg.qdrant_impl import QdrantVectorDBStorage

    monkeypatch.setenv("QDRANT_WORKSPACE", "forced")
    storage = QdrantVectorDBStorage(
        namespace="chunks",
        workspace="kb_a",
        global_config=_vector_config(
            "qdrant",
            {
                "url": "http://qdrant-a:6333",
                "api_key": "test-key",
                "collection_prefix": "project_a",
            },
        ),
        embedding_func=_Embedding(),
        meta_fields={"content"},
    )

    assert storage.effective_workspace == "kb_a"
    assert storage.final_namespace.startswith("project_a_lightrag_vdb_chunks_")
    assert storage._get_qdrant_connection_kwargs() == {
        "url": "http://qdrant-a:6333",
        "api_key": "test-key",
    }


def test_memgraph_profile_controls_driver_and_workspace(monkeypatch) -> None:
    pytest.importorskip("neo4j")
    from lightrag.kg.memgraph_impl import MemgraphStorage

    monkeypatch.setenv("MEMGRAPH_WORKSPACE", "forced")
    storage = MemgraphStorage(
        namespace="chunk_entity_relation",
        workspace="kb_a",
        global_config={
            "storage_profile": {
                "memgraph": {
                    "uri": "bolt://memgraph-a:7687",
                    "username": "rag",
                    "password": "test-secret",
                    "database": "rag_a",
                }
            }
        },
        embedding_func=None,
    )

    assert storage.workspace == "kb_a"
    assert storage._get_connection_config() == {
        "uri": "bolt://memgraph-a:7687",
        "username": "rag",
        "password": "test-secret",
        "database": "rag_a",
    }


def test_opensearch_profile_controls_index_prefix_and_workspace(monkeypatch) -> None:
    pytest.importorskip("opensearchpy")
    from lightrag.kg.opensearch_impl import _build_index_name

    monkeypatch.setenv("OPENSEARCH_WORKSPACE", "forced")
    workspace, namespace, index_name = _build_index_name(
        "kb_a",
        "full_docs",
        {
            "storage_profile": {
                "opensearch": {
                    "hosts": ["search-a:9200"],
                    "index_prefix": "project_a",
                }
            }
        },
    )

    assert workspace == "kb_a"
    assert namespace == "kb_a_full_docs"
    assert index_name == "project_a_kb_a_full_docs"
