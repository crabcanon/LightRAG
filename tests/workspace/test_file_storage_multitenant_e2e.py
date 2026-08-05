"""End-to-end regression coverage for the default file-backed tenant boundary."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from lightrag import LightRAG, WorkspaceBinding
from lightrag.base import DocStatus
from lightrag.kg.shared_storage import finalize_share_data, initialize_share_data
from lightrag.utils import EmbeddingFunc, Tokenizer
from lightrag.workspace import WorkspaceKind


class _TestTokenizer:
    """Small deterministic tokenizer sufficient for the file-storage pipeline test."""

    def encode(self, content: str) -> list[str]:
        return content.split()

    def decode(self, tokens: list[str]) -> str:
        return " ".join(tokens)


async def _test_embedding(texts: list[str]) -> np.ndarray:
    """Return a stable, small embedding without calling an external provider."""

    vector = np.asarray([0.1, 0.2, 0.3, 0.4], dtype=np.float32)
    return np.tile(vector, (len(texts), 1))


def _make_test_llm(entity_name: str):
    async def _complete(*_args, **_kwargs) -> str:
        related_entity = f"{entity_name}_RELATED"
        return (
            f"entity<|#|>{entity_name}<|#|>ORGANIZATION<|#|>"
            f"{entity_name} belongs only to this knowledge base.\n"
            f"entity<|#|>{related_entity}<|#|>PERSON<|#|>"
            f"{related_entity} belongs only to this knowledge base.\n"
            f"relation<|#|>{entity_name}<|#|>{related_entity}<|#|>"
            f"belongs only to this knowledge base<|#|>"
            f"{entity_name} is related to {related_entity}.\n<|COMPLETE|>"
        )

    return _complete


async def _new_rag(working_dir: Path, workspace: str, entity_name: str) -> LightRAG:
    rag = LightRAG(
        working_dir=str(working_dir),
        workspace=workspace,
        workspace_binding=WorkspaceBinding.named(workspace),
        llm_model_func=_make_test_llm(entity_name),
        embedding_func=EmbeddingFunc(
            embedding_dim=4,
            max_token_size=128,
            func=_test_embedding,
        ),
        tokenizer=Tokenizer("test-tokenizer", _TestTokenizer()),
        chunk_token_size=32,
        chunk_overlap_token_size=0,
        max_parallel_insert=1,
    )
    await rag.initialize_storages()
    return rag


def _storage_file_path(storage: object) -> Path:
    """Return the backing file path exposed by each default storage implementation."""

    for attribute in ("_file_name", "_client_file_name", "_graphml_xml_file"):
        value = getattr(storage, attribute, None)
        if value is not None:
            return Path(value)
    raise AssertionError(f"{type(storage).__name__} did not expose a file path")


@pytest.mark.offline
@pytest.mark.asyncio
async def test_default_file_storages_isolate_same_document_and_deletion(
    tmp_path: Path,
) -> None:
    """Default JSON/Nano/NetworkX stores must not cross tenant data or deletion."""

    initialize_share_data()
    rag_alpha: LightRAG | None = None
    rag_beta: LightRAG | None = None
    try:
        rag_alpha, rag_beta = await asyncio.gather(
            _new_rag(tmp_path, "alpha-kb", "ALPHA_ONLY_ENTITY"),
            _new_rag(tmp_path, "beta-kb", "BETA_ONLY_ENTITY"),
        )

        shared_content = "The same source content is intentionally inserted twice."
        await asyncio.gather(
            rag_alpha.ainsert(shared_content),
            rag_beta.ainsert(shared_content),
        )

        alpha_docs = await rag_alpha.doc_status.get_docs_by_status(DocStatus.PROCESSED)
        beta_docs = await rag_beta.doc_status.get_docs_by_status(DocStatus.PROCESSED)
        assert len(alpha_docs) == len(beta_docs) == 1

        # Content hashes deliberately match. Isolation must come from the workspace,
        # not from relying on document IDs to be globally unique.
        document_id = next(iter(alpha_docs))
        assert set(beta_docs) == {document_id}
        assert await rag_alpha.full_docs.get_by_id(document_id) is not None
        assert await rag_beta.full_docs.get_by_id(document_id) is not None

        alpha_chunk_id = alpha_docs[document_id].chunks_list[0]
        beta_chunk_id = beta_docs[document_id].chunks_list[0]
        assert alpha_chunk_id == beta_chunk_id
        assert await rag_alpha.text_chunks.get_by_id(alpha_chunk_id) is not None
        assert await rag_beta.text_chunks.get_by_id(beta_chunk_id) is not None
        assert await rag_alpha.chunks_vdb.get_by_id(alpha_chunk_id) is not None
        assert await rag_beta.chunks_vdb.get_by_id(beta_chunk_id) is not None

        alpha_labels = await rag_alpha.chunk_entity_relation_graph.get_all_labels()
        beta_labels = await rag_beta.chunk_entity_relation_graph.get_all_labels()
        assert any("ALPHA_ONLY_ENTITY" in label for label in alpha_labels)
        assert not any("BETA_ONLY_ENTITY" in label for label in alpha_labels)
        assert any("BETA_ONLY_ENTITY" in label for label in beta_labels)
        assert not any("ALPHA_ONLY_ENTITY" in label for label in beta_labels)

        # Every default storage family resolves to the workspace directory, including
        # all seven JSON namespaces, three NanoVectorDB files, graph, and doc status.
        storage_attributes = (
            "llm_response_cache",
            "full_docs",
            "text_chunks",
            "full_entities",
            "full_relations",
            "entity_chunks",
            "relation_chunks",
            "entities_vdb",
            "relationships_vdb",
            "chunks_vdb",
            "chunk_entity_relation_graph",
            "doc_status",
        )
        for rag, workspace in ((rag_alpha, "alpha-kb"), (rag_beta, "beta-kb")):
            assert len(rag.storage_namespace_descriptors) == 12
            assert {
                descriptor.workspace_kind
                for descriptor in rag.storage_namespace_descriptors
            } == {WorkspaceKind.NAMED}
            assert {
                descriptor.canonical_workspace_key
                for descriptor in rag.storage_namespace_descriptors
            } == {workspace}
            for attribute in storage_attributes:
                path = _storage_file_path(getattr(rag, attribute))
                assert path.parent == tmp_path / workspace
                assert path.exists()

        await rag_alpha.adelete_by_doc_id(document_id, delete_llm_cache=True)
        assert await rag_alpha.full_docs.get_by_id(document_id) is None
        assert await rag_alpha.text_chunks.get_by_id(alpha_chunk_id) is None
        assert await rag_alpha.chunks_vdb.get_by_id(alpha_chunk_id) is None
        assert not await rag_alpha.doc_status.get_docs_by_status(DocStatus.PROCESSED)

        # The same document ID, chunk ID, and document status in beta remain intact.
        assert await rag_beta.full_docs.get_by_id(document_id) is not None
        assert await rag_beta.text_chunks.get_by_id(beta_chunk_id) is not None
        assert await rag_beta.chunks_vdb.get_by_id(beta_chunk_id) is not None
        assert set(
            await rag_beta.doc_status.get_docs_by_status(DocStatus.PROCESSED)
        ) == {document_id}
    finally:
        if rag_alpha is not None:
            await rag_alpha.finalize_storages()
        if rag_beta is not None:
            await rag_beta.finalize_storages()
        finalize_share_data()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_doc_status_same_hash_isolated_across_lifecycle_retry_and_restart(
    tmp_path: Path,
) -> None:
    """The recovery queue must remain tenant-scoped at every pipeline state."""

    initialize_share_data()
    rag_alpha: LightRAG | None = None
    rag_beta: LightRAG | None = None
    document_id = "doc-shared-content-hash"
    content_hash = "same-content-hash"

    def status_record(workspace: str, status: DocStatus) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "content_summary": workspace,
            "content_length": 17,
            "file_path": "shared.txt",
            "status": status.value,
            "created_at": now,
            "updated_at": now,
            "track_id": f"track-{workspace}",
            "chunks_count": 0,
            "chunks_list": [],
            "error_msg": "simulated" if status is DocStatus.FAILED else None,
            "metadata": {"workspace_marker": workspace},
            "content_hash": content_hash,
        }

    try:
        rag_alpha, rag_beta = await asyncio.gather(
            _new_rag(tmp_path, "alpha-status", "ALPHA_STATUS_ENTITY"),
            _new_rag(tmp_path, "beta-status", "BETA_STATUS_ENTITY"),
        )
        await rag_beta.doc_status.upsert(
            {document_id: status_record("beta", DocStatus.PENDING)}
        )

        for status in (
            DocStatus.PENDING,
            DocStatus.PARSING,
            DocStatus.ANALYZING,
            DocStatus.PROCESSING,
            DocStatus.PROCESSED,
            DocStatus.FAILED,
            DocStatus.PENDING,  # retry
        ):
            await rag_alpha.doc_status.upsert(
                {document_id: status_record("alpha", status)}
            )
            alpha = await rag_alpha.doc_status.get_by_id(document_id)
            beta = await rag_beta.doc_status.get_by_id(document_id)
            assert alpha is not None and alpha["status"] == status.value
            assert alpha["content_hash"] == content_hash
            assert alpha["metadata"]["workspace_marker"] == "alpha"
            assert beta is not None and beta["status"] == DocStatus.PENDING.value
            assert beta["content_hash"] == content_hash
            assert beta["metadata"]["workspace_marker"] == "beta"

        await asyncio.gather(
            rag_alpha.finalize_storages(), rag_beta.finalize_storages()
        )
        rag_alpha = rag_beta = None
        finalize_share_data()
        initialize_share_data()

        rag_alpha, rag_beta = await asyncio.gather(
            _new_rag(tmp_path, "alpha-status", "ALPHA_STATUS_ENTITY"),
            _new_rag(tmp_path, "beta-status", "BETA_STATUS_ENTITY"),
        )
        assert (await rag_alpha.doc_status.get_by_id(document_id))["metadata"][
            "workspace_marker"
        ] == "alpha"
        assert (await rag_beta.doc_status.get_by_id(document_id))["metadata"][
            "workspace_marker"
        ] == "beta"

        await rag_alpha.doc_status.delete([document_id])
        await rag_alpha.doc_status.index_done_callback()
        assert await rag_alpha.doc_status.get_by_id(document_id) is None
        beta = await rag_beta.doc_status.get_by_id(document_id)
        assert beta is not None and beta["content_hash"] == content_hash
    finally:
        if rag_alpha is not None:
            await rag_alpha.finalize_storages()
        if rag_beta is not None:
            await rag_beta.finalize_storages()
        finalize_share_data()
