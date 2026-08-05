"""Canonical workspace identity and namespace descriptor regression tests."""

from dataclasses import FrozenInstanceError
import pytest

from lightrag.workspace import (
    LEGACY_EMPTY_WORKSPACE_ALIASES,
    LEGACY_DEFAULT_CANONICAL_KEY,
    NamespaceCodec,
    WorkspaceBinding,
    WorkspaceBindingError,
    WorkspaceKind,
    describe_storage_namespace,
    validate_named_workspace_key,
    validate_storage_namespace_descriptors,
)
from lightrag.kg.storage_profiles import STORAGE_ISOLATION_CAPABILITIES


pytestmark = pytest.mark.offline


def test_every_selectable_backend_has_a_legacy_codec_registration() -> None:
    assert set(LEGACY_EMPTY_WORKSPACE_ALIASES) == set(STORAGE_ISOLATION_CAPABILITIES)


def test_legacy_default_is_tagged_and_immutable() -> None:
    binding = WorkspaceBinding.legacy_default("")

    assert binding.kind is WorkspaceKind.LEGACY_DEFAULT
    assert binding.canonical_key == LEGACY_DEFAULT_CANONICAL_KEY
    assert binding.codec_version is NamespaceCodec.LEGACY_V1
    assert binding.canonical_key not in {"", "default", "_"}

    with pytest.raises(FrozenInstanceError):
        binding.canonical_key = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    ["", "default", "DEFAULT", "_", "@legacy-default", "../escape"],
)
def test_named_workspace_rejects_legacy_aliases_and_unsafe_keys(value: str) -> None:
    with pytest.raises(WorkspaceBindingError):
        validate_named_workspace_key(value)


def test_named_workspace_round_trip_preserves_tags() -> None:
    binding = WorkspaceBinding.named(
        "kb_1234abcd", storage_profile_id="profile-a", catalog_revision=7
    )

    restored = WorkspaceBinding.from_dict(binding.public_dict())

    assert restored == binding
    assert restored.kind is WorkspaceKind.NAMED
    assert restored.codec_version is NamespaceCodec.NAMESPACE_V1


def _storage(
    implementation: str,
    family: str,
    role: str,
    workspace: str,
    binding: WorkspaceBinding,
    **attributes: str,
):
    storage_type = type(implementation, (), {})
    storage = storage_type()
    storage.storage_family = family
    storage.namespace = role
    storage.workspace = workspace
    storage.global_config = {
        "workspace_binding": binding,
        "storage_profile": {
            "postgres": {"uri": "postgresql://secret-user:secret-password@db/private"}
        },
    }
    for name, value in attributes.items():
        setattr(storage, name, value)
    return storage


def test_named_descriptor_matrix_requires_exact_physical_key() -> None:
    binding = WorkspaceBinding.named("kb_1234abcd")
    storages = [
        _storage("JsonKVStorage", "kv", "full_docs", binding.canonical_key, binding),
        _storage(
            "NanoVectorDBStorage",
            "vector",
            "entities",
            binding.canonical_key,
            binding,
        ),
        _storage(
            "NetworkXStorage",
            "graph",
            "chunk_entity_relation",
            binding.canonical_key,
            binding,
        ),
        _storage(
            "JsonDocStatusStorage",
            "doc_status",
            "doc_status",
            binding.canonical_key,
            binding,
        ),
    ]

    descriptors = validate_storage_namespace_descriptors(
        storages, binding, stage="construction"
    )
    assert {item.storage_family for item in descriptors} == {
        "kv",
        "vector",
        "graph",
        "doc_status",
    }
    assert {item.canonical_workspace_key for item in descriptors} == {
        binding.canonical_key
    }

    storages[-1].workspace = "kb_other"
    with pytest.raises(WorkspaceBindingError, match="physical namespace mismatch"):
        validate_storage_namespace_descriptors(storages, binding, stage="construction")


def test_legacy_codec_normalizes_backend_default_spellings() -> None:
    binding = WorkspaceBinding.legacy_default("")
    storages = [
        _storage("PGKVStorage", "kv", "full_docs", "default", binding),
        _storage("QdrantVectorDBStorage", "vector", "entities", "_", binding),
        _storage("Neo4JStorage", "graph", "chunk_entity_relation", "base", binding),
        _storage("PGDocStatusStorage", "doc_status", "doc_status", "", binding),
    ]

    descriptors = validate_storage_namespace_descriptors(
        storages, binding, stage="post-connect"
    )

    assert len(descriptors) == 4
    assert {item.canonical_workspace_key for item in descriptors} == {
        LEGACY_DEFAULT_CANONICAL_KEY
    }


def test_legacy_descriptor_detects_mixed_family_override() -> None:
    binding = WorkspaceBinding.legacy_default("")
    storages = [
        _storage("PGKVStorage", "kv", "full_docs", "tenant-a", binding),
        _storage("PGVectorStorage", "vector", "entities", "tenant-a", binding),
        _storage("Neo4JStorage", "graph", "graph", "base", binding),
        _storage("PGDocStatusStorage", "doc_status", "doc_status", "tenant-a", binding),
    ]

    with pytest.raises(WorkspaceBindingError, match="storage families disagree"):
        validate_storage_namespace_descriptors(storages, binding, stage="post-connect")


def test_descriptor_fingerprint_is_credential_free() -> None:
    binding = WorkspaceBinding.named("kb_1234abcd")
    first = _storage(
        "PGKVStorage",
        "kv",
        "full_docs",
        binding.canonical_key,
        binding,
        final_namespace="full_docs",
    )
    second = _storage(
        "PGKVStorage",
        "kv",
        "full_docs",
        binding.canonical_key,
        binding,
        final_namespace="full_docs",
    )
    second.global_config = {
        **first.global_config,
        "storage_profile": {
            "postgres": {"uri": "postgresql://another:credential@db/private"}
        },
    }

    assert (
        describe_storage_namespace(first).physical_namespace_fingerprint
        == describe_storage_namespace(second).physical_namespace_fingerprint
    )
    assert "secret" not in str(describe_storage_namespace(first).public_dict())
