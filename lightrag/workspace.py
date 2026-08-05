"""Canonical workspace identity and storage namespace diagnostics.

The legacy LightRAG API represents a workspace as a plain string.  That is
not sufficient for a multi-workspace server because the unnamed legacy
workspace has several backend-specific physical spellings (for example,
``default`` in PostgreSQL and ``_`` in Qdrant).  This module separates the
canonical identity from those compatibility encodings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


LEGACY_DEFAULT_CANONICAL_KEY = "@legacy-default"
LEGACY_NAMESPACE_CODEC = "legacy-v1"
NAMED_NAMESPACE_CODEC = "namespace-v1"

_NAMED_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RESERVED_NAMED_KEYS = frozenset(
    {
        "default",
        "_",
        LEGACY_DEFAULT_CANONICAL_KEY.casefold(),
    }
)


class WorkspaceBindingError(ValueError):
    """Raised when a workspace binding or namespace descriptor is unsafe."""


class WorkspaceKind(str, Enum):
    LEGACY_DEFAULT = "legacy_default"
    NAMED = "named"


class NamespaceCodec(str, Enum):
    LEGACY_V1 = LEGACY_NAMESPACE_CODEC
    NAMESPACE_V1 = NAMED_NAMESPACE_CODEC


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """Immutable catalog identity passed to every storage in one RAG instance."""

    public_id: str
    kind: WorkspaceKind
    canonical_key: str
    codec_version: NamespaceCodec
    physical_workspace: str
    storage_profile_id: str | None = None
    catalog_revision: int = 0
    server_mode: str = "legacy"

    @classmethod
    def legacy_default(
        cls,
        physical_workspace: str,
        *,
        catalog_revision: int = 0,
        server_mode: str = "legacy",
    ) -> "WorkspaceBinding":
        return cls(
            public_id="default",
            kind=WorkspaceKind.LEGACY_DEFAULT,
            canonical_key=LEGACY_DEFAULT_CANONICAL_KEY,
            codec_version=NamespaceCodec.LEGACY_V1,
            physical_workspace=physical_workspace,
            catalog_revision=catalog_revision,
            server_mode=server_mode,
        )

    @classmethod
    def named(
        cls,
        public_id: str,
        *,
        canonical_key: str | None = None,
        storage_profile_id: str | None = None,
        catalog_revision: int = 0,
        server_mode: str = "multi",
    ) -> "WorkspaceBinding":
        key = canonical_key or public_id
        binding = cls(
            public_id=public_id,
            kind=WorkspaceKind.NAMED,
            canonical_key=key,
            codec_version=NamespaceCodec.NAMESPACE_V1,
            physical_workspace=key,
            storage_profile_id=storage_profile_id,
            catalog_revision=catalog_revision,
            server_mode=server_mode,
        )
        binding.validate()
        return binding

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceBinding":
        binding = cls(
            public_id=str(value["public_id"]),
            kind=WorkspaceKind(str(value["kind"])),
            canonical_key=str(value["canonical_key"]),
            codec_version=NamespaceCodec(str(value["codec_version"])),
            physical_workspace=str(value.get("physical_workspace", "")),
            storage_profile_id=(
                str(value["storage_profile_id"])
                if value.get("storage_profile_id")
                else None
            ),
            catalog_revision=int(value.get("catalog_revision", 0)),
            server_mode=str(value.get("server_mode", "legacy")),
        )
        binding.validate()
        return binding

    def validate(self) -> None:
        if self.catalog_revision < 0:
            raise WorkspaceBindingError("catalog_revision cannot be negative")
        if self.kind is WorkspaceKind.LEGACY_DEFAULT:
            if self.public_id != "default":
                raise WorkspaceBindingError(
                    "The legacy-default binding must use public ID 'default'"
                )
            if self.canonical_key != LEGACY_DEFAULT_CANONICAL_KEY:
                raise WorkspaceBindingError(
                    "The legacy-default binding has an invalid canonical key"
                )
            if self.codec_version is not NamespaceCodec.LEGACY_V1:
                raise WorkspaceBindingError(
                    "The legacy-default binding must use the legacy-v1 codec"
                )
            if self.storage_profile_id is not None:
                raise WorkspaceBindingError(
                    "The legacy-default binding cannot select a storage profile"
                )
            return

        validate_named_workspace_key(self.public_id)
        validate_named_workspace_key(self.canonical_key)
        if self.codec_version is not NamespaceCodec.NAMESPACE_V1:
            raise WorkspaceBindingError(
                "Named workspace bindings must use the namespace-v1 codec"
            )
        if self.physical_workspace != self.canonical_key:
            raise WorkspaceBindingError(
                "Named workspace physical identity must equal its canonical key"
            )

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["codec_version"] = self.codec_version.value
        return payload


def validate_named_workspace_key(value: str) -> str:
    """Validate a service-generated named workspace key.

    Display names are deliberately excluded from physical resource names.  The
    aliases used by legacy backends are reserved even when a backend would
    technically accept them.
    """

    if not _NAMED_KEY_PATTERN.fullmatch(value):
        raise WorkspaceBindingError(
            "Named workspace keys must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}"
        )
    folded = value.casefold()
    if folded in _RESERVED_NAMED_KEYS or folded.startswith("lightrag_internal_"):
        raise WorkspaceBindingError(f"Reserved named workspace key: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class StorageNamespaceDescriptor:
    """Credential-free identity reported by one constructed storage object."""

    storage_family: str
    storage_role: str
    implementation: str
    catalog_id: str
    workspace_kind: WorkspaceKind
    canonical_workspace_key: str
    namespace_codec_version: NamespaceCodec
    storage_profile_id: str | None
    backend_workspace_key: str
    physical_namespace_fingerprint: str

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["workspace_kind"] = self.workspace_kind.value
        payload["namespace_codec_version"] = self.namespace_codec_version.value
        # The raw backend workspace is useful to the validator but unnecessary
        # in API/log diagnostics.  The fingerprint is the redacted identity.
        payload.pop("backend_workspace_key", None)
        return payload


_PHYSICAL_IDENTITY_ATTRIBUTES = (
    "final_namespace",
    "_collection_name",
    "_index_name",
    "graph_name",
    "storage_file",
    "_storage_file_name",
    "_client_file_name",
    "_graphml_xml_file",
)


def _role_text(namespace: Any) -> str:
    value = getattr(namespace, "value", namespace)
    return str(value)


def _safe_physical_identity(storage: Any, backend_workspace_key: str) -> str:
    identity: dict[str, str] = {
        "implementation": type(storage).__name__,
        "namespace": _role_text(storage.namespace),
        "workspace": backend_workspace_key,
    }
    for attribute in _PHYSICAL_IDENTITY_ATTRIBUTES:
        value = getattr(storage, attribute, None)
        if value in (None, ""):
            continue
        if isinstance(value, Path):
            value = os.fspath(value)
        identity[attribute] = str(value)
    encoded = json.dumps(identity, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def binding_from_global_config(
    global_config: Mapping[str, Any], workspace: str
) -> WorkspaceBinding:
    value = global_config.get("workspace_binding")
    if isinstance(value, WorkspaceBinding):
        value.validate()
        return value
    if isinstance(value, Mapping):
        return WorkspaceBinding.from_dict(value)
    # Library callers that do not opt into the API catalog retain legacy
    # single-workspace semantics.
    binding = WorkspaceBinding.legacy_default(workspace)
    binding.validate()
    return binding


def describe_storage_namespace(storage: Any) -> StorageNamespaceDescriptor:
    binding = binding_from_global_config(storage.global_config, storage.workspace)
    backend_workspace = getattr(storage, "effective_workspace", storage.workspace)
    backend_workspace_key = str(backend_workspace or "")
    return StorageNamespaceDescriptor(
        storage_family=str(storage.storage_family),
        storage_role=_role_text(storage.namespace),
        implementation=type(storage).__name__,
        catalog_id=binding.public_id,
        workspace_kind=binding.kind,
        canonical_workspace_key=binding.canonical_key,
        namespace_codec_version=binding.codec_version,
        storage_profile_id=binding.storage_profile_id,
        backend_workspace_key=backend_workspace_key,
        physical_namespace_fingerprint=_safe_physical_identity(
            storage, backend_workspace_key
        ),
    )


LEGACY_EMPTY_WORKSPACE_ALIASES: dict[str, frozenset[str]] = {
    "JsonKVStorage": frozenset({""}),
    "JsonDocStatusStorage": frozenset({""}),
    "NetworkXStorage": frozenset({""}),
    "NanoVectorDBStorage": frozenset({""}),
    "FaissVectorDBStorage": frozenset({""}),
    "RedisKVStorage": frozenset({""}),
    "RedisDocStatusStorage": frozenset({""}),
    "PGKVStorage": frozenset({"", "default"}),
    "PGVectorStorage": frozenset({"", "default"}),
    "PGGraphStorage": frozenset({"", "default"}),
    "PGTableGraphStorage": frozenset({"", "default"}),
    "PGDocStatusStorage": frozenset({"", "default"}),
    "MongoKVStorage": frozenset({""}),
    "MongoDocStatusStorage": frozenset({""}),
    "MongoGraphStorage": frozenset({""}),
    "MongoVectorDBStorage": frozenset({""}),
    "MilvusVectorDBStorage": frozenset({""}),
    "QdrantVectorDBStorage": frozenset({"", "_"}),
    "Neo4JStorage": frozenset({"", "base"}),
    "MemgraphStorage": frozenset({"", "base"}),
    "OpenSearchKVStorage": frozenset({""}),
    "OpenSearchDocStatusStorage": frozenset({""}),
    "OpenSearchGraphStorage": frozenset({""}),
    "OpenSearchVectorDBStorage": frozenset({""}),
}


def _normalized_legacy_physical_key(
    descriptor: StorageNamespaceDescriptor, binding: WorkspaceBinding
) -> str:
    value = descriptor.backend_workspace_key
    if binding.physical_workspace:
        return value
    aliases = LEGACY_EMPTY_WORKSPACE_ALIASES.get(
        descriptor.implementation, frozenset({""})
    )
    return LEGACY_DEFAULT_CANONICAL_KEY if value in aliases else value


def validate_storage_namespace_descriptors(
    storages: Iterable[Any],
    binding: WorkspaceBinding,
    *,
    stage: str,
) -> tuple[StorageNamespaceDescriptor, ...]:
    """Validate every storage object before use and return its descriptors."""

    binding.validate()
    descriptors = tuple(describe_storage_namespace(storage) for storage in storages)
    if not descriptors:
        raise WorkspaceBindingError("No storage namespace descriptors were reported")

    required_families = {"kv", "vector", "graph", "doc_status"}
    reported_families = {item.storage_family for item in descriptors}
    missing = sorted(required_families - reported_families)
    if missing:
        raise WorkspaceBindingError(
            f"Workspace {binding.public_id!r} is missing storage families at "
            f"{stage}: {', '.join(missing)}"
        )

    for descriptor in descriptors:
        if (
            descriptor.catalog_id != binding.public_id
            or descriptor.workspace_kind is not binding.kind
            or descriptor.canonical_workspace_key != binding.canonical_key
            or descriptor.namespace_codec_version is not binding.codec_version
            or descriptor.storage_profile_id != binding.storage_profile_id
        ):
            raise WorkspaceBindingError(
                "Storage binding mismatch for "
                f"catalog={binding.public_id!r}, family={descriptor.storage_family}, "
                f"role={descriptor.storage_role}, "
                f"implementation={descriptor.implementation}, stage={stage}, "
                f"fingerprint={descriptor.physical_namespace_fingerprint}"
            )

    if binding.kind is WorkspaceKind.NAMED:
        mismatches = [
            item
            for item in descriptors
            if item.backend_workspace_key != binding.physical_workspace
        ]
        if mismatches:
            item = mismatches[0]
            raise WorkspaceBindingError(
                "Named workspace physical namespace mismatch for "
                f"catalog={binding.public_id!r}, family={item.storage_family}, "
                f"role={item.storage_role}, implementation={item.implementation}, "
                f"stage={stage}, fingerprint={item.physical_namespace_fingerprint}"
            )
    else:
        normalized = {
            _normalized_legacy_physical_key(item, binding) for item in descriptors
        }
        if len(normalized) != 1:
            summary = ", ".join(
                f"{item.storage_family}/{item.storage_role}/"
                f"{item.implementation}="
                f"{item.physical_namespace_fingerprint[:12]}"
                for item in descriptors
            )
            raise WorkspaceBindingError(
                f"Legacy workspace storage families disagree at {stage} for "
                f"catalog={binding.public_id!r}: {summary}"
            )

    return descriptors
