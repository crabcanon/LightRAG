"""Knowledge-base catalog, lifecycle management, and request routing.

The API historically captured one ``LightRAG`` and one ``DocumentManager`` in
all route closures.  This module keeps that public route surface compatible
while introducing a request-scoped context that selects an isolated pair.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import threading
from types import SimpleNamespace
from typing import Annotated, Any, AsyncIterator, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from fastapi import Header, HTTPException, Request

from lightrag.api.catalog import (
    CATALOG_SCHEMA_VERSION,
    CatalogCASConflict,
    CatalogIdempotencyConflict,
    CatalogOperation,
    CatalogOperationNotFound,
    CatalogOperationState,
    CatalogOperationType,
    CatalogProvider,
    LocalCatalogProvider,
    WorkspaceLifecycleState,
    canonical_payload_hash,
)
from lightrag.api.endpoint_policy import ENDPOINT_POLICIES, EndpointPolicy
from lightrag.api.workspace_pool import (
    WorkspaceContextMissingError,
    WorkspaceExecutionContext,
    WorkspaceInstancePool,
    WorkspacePoolBusyError,
    WorkspacePoolCapacityError,
    WorkspacePoolInitializationError,
)
from lightrag.api.workspace_coordinator import (
    LocalWorkspaceCoordinator,
    WorkspaceCoordinator,
)
from lightrag.api.workspace_recovery import (
    WorkspaceRecoveryCoordinator,
    WorkspaceRecoveryReport,
)
from lightrag.exceptions import PipelineNotInitializedError
from lightrag.file_atomic import atomic_write
from lightrag.kg.shared_storage import get_namespace_data
from lightrag.kg.storage_profiles import (
    forced_workspace_variables,
    profile_resource_fingerprints,
    required_profile_sections,
    validate_storage_profile,
)
from lightrag.utils import logger, validate_workspace
from lightrag.workspace import (
    LEGACY_DEFAULT_CANONICAL_KEY,
    LEGACY_NAMESPACE_CODEC,
    NAMED_NAMESPACE_CODEC,
    NamespaceCodec,
    WorkspaceBinding,
    WorkspaceKind,
)
from lightrag.workspace_scope import (
    bind_background_lease_factory,
    bind_workspace_execution_scope,
    reset_background_lease_factory,
    reset_workspace_execution_scope,
)


KNOWLEDGE_BASE_HEADER = "LIGHTRAG-KNOWLEDGE-BASE"
KNOWLEDGE_BASE_HEADER_DESCRIPTION = (
    "Knowledge-base ID used to route this operation. Omit the header to use "
    "the backward-compatible default knowledge base. Available IDs can be "
    "listed with GET /knowledge-bases."
)
KnowledgeBaseHeader = Annotated[
    str | None,
    Header(
        alias=KNOWLEDGE_BASE_HEADER,
        description=KNOWLEDGE_BASE_HEADER_DESCRIPTION,
    ),
]
DEFAULT_KNOWLEDGE_BASE_ID = "default"
CATALOG_VERSION = 1
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STORAGE_ATTRIBUTES = (
    "llm_response_cache",
    "text_chunks",
    "full_docs",
    "full_entities",
    "full_relations",
    "entity_chunks",
    "relation_chunks",
    "chunk_entity_relation_graph",
    "entities_vdb",
    "relationships_vdb",
    "chunks_vdb",
    "doc_status",
)
_DEFAULT_STORAGE_IMPLEMENTATIONS = (
    "RedisKVStorage",
    "PGVectorStorage",
    "Neo4JStorage",
    "PGDocStatusStorage",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeBaseError(RuntimeError):
    """Base error for knowledge-base management failures."""


class KnowledgeBaseNotFoundError(KnowledgeBaseError):
    """Raised when a knowledge-base ID is not present in the catalog."""


class KnowledgeBaseConflictError(KnowledgeBaseError):
    """Raised when a requested lifecycle operation is unsafe or ambiguous."""


class KnowledgeBaseUnavailableError(KnowledgeBaseError):
    """Raised when a known workspace is not currently data-plane ACTIVE."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class StorageProfileError(KnowledgeBaseError):
    """Raised when strict physical isolation cannot be guaranteed."""


@dataclass(frozen=True, slots=True)
class KnowledgeBaseRecord:
    id: str
    name: str
    effective_workspace: str
    isolation_level: Literal["logical", "physical"]
    storage_profile_id: str | None
    created_at: str
    updated_at: str
    workspace_kind: Literal["legacy_default", "named"]
    canonical_workspace_key: str
    namespace_codec_version: Literal["legacy-v1", "namespace-v1"]
    lifecycle_state: str = WorkspaceLifecycleState.ACTIVE.value
    revision: int = 1
    schema_version: int = CATALOG_SCHEMA_VERSION
    current_operation_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    tombstoned_at: str | None = None

    @classmethod
    def legacy_default(cls, workspace: str) -> "KnowledgeBaseRecord":
        now = _utc_now()
        return cls(
            id=DEFAULT_KNOWLEDGE_BASE_ID,
            name="Default",
            effective_workspace=workspace,
            isolation_level="logical",
            storage_profile_id=None,
            created_at=now,
            updated_at=now,
            workspace_kind="legacy_default",
            canonical_workspace_key=LEGACY_DEFAULT_CANONICAL_KEY,
            namespace_codec_version=LEGACY_NAMESPACE_CODEC,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "KnowledgeBaseRecord":
        record_id = str(value["id"])
        is_default = record_id == DEFAULT_KNOWLEDGE_BASE_ID
        effective_workspace = str(value.get("effective_workspace", ""))
        record = cls(
            id=record_id,
            name=str(value["name"]),
            effective_workspace=effective_workspace,
            isolation_level=str(value.get("isolation_level", "logical")),  # type: ignore[arg-type]
            storage_profile_id=(
                str(value["storage_profile_id"])
                if value.get("storage_profile_id")
                else None
            ),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            workspace_kind=str(
                value.get("workspace_kind", "legacy_default" if is_default else "named")
            ),  # type: ignore[arg-type]
            canonical_workspace_key=str(
                value.get(
                    "canonical_workspace_key",
                    LEGACY_DEFAULT_CANONICAL_KEY if is_default else effective_workspace,
                )
            ),
            namespace_codec_version=str(
                value.get(
                    "namespace_codec_version",
                    LEGACY_NAMESPACE_CODEC if is_default else NAMED_NAMESPACE_CODEC,
                )
            ),  # type: ignore[arg-type]
            lifecycle_state=str(
                value.get("lifecycle_state", WorkspaceLifecycleState.ACTIVE.value)
            ),
            revision=int(value.get("revision", 1)),
            schema_version=int(value.get("schema_version", CATALOG_SCHEMA_VERSION)),
            current_operation_id=(
                str(value["current_operation_id"])
                if value.get("current_operation_id")
                else None
            ),
            error_code=str(value["error_code"]) if value.get("error_code") else None,
            error_message=(
                str(value["error_message"]) if value.get("error_message") else None
            ),
            tombstoned_at=(
                str(value["tombstoned_at"]) if value.get("tombstoned_at") else None
            ),
        )
        record.validate()
        return record

    def validate(self) -> None:
        if not _ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid knowledge-base ID: {self.id!r}")
        if not self.name.strip() or len(self.name) > 128:
            raise ValueError("Knowledge-base name must contain 1-128 characters")
        validate_workspace(self.effective_workspace)
        if self.isolation_level not in {"logical", "physical"}:
            raise ValueError(f"Invalid isolation level: {self.isolation_level!r}")
        if self.isolation_level == "physical" and not self.storage_profile_id:
            raise ValueError("Physical isolation requires a storage profile")
        if self.isolation_level == "logical" and self.storage_profile_id:
            raise ValueError("Logical isolation cannot reference a storage profile")
        WorkspaceLifecycleState(self.lifecycle_state)
        if self.revision < 1:
            raise ValueError("Knowledge-base revision must be positive")
        if self.schema_version < 1:
            raise ValueError("Knowledge-base schema_version must be positive")
        self.to_workspace_binding().validate()

    def to_workspace_binding(self, *, server_mode: str = "multi") -> WorkspaceBinding:
        return WorkspaceBinding(
            public_id=self.id,
            kind=WorkspaceKind(self.workspace_kind),
            canonical_key=self.canonical_workspace_key,
            codec_version=NamespaceCodec(self.namespace_codec_version),
            physical_workspace=self.effective_workspace,
            storage_profile_id=self.storage_profile_id,
            catalog_revision=self.revision,
            server_mode=server_mode,
        )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeBaseCatalog:
    """Crash-safe JSON catalog with an immutable effective workspace per ID."""

    def __init__(self, path: str | Path, default_workspace: str) -> None:
        validate_workspace(default_workspace)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_workspace = default_workspace
        self._lock = threading.RLock()
        self._records: dict[str, KnowledgeBaseRecord] = {}
        self._operations: dict[str, CatalogOperation] = {}
        self._fencing_token = 0
        self._load_or_create()

    def _default_record(self) -> KnowledgeBaseRecord:
        return KnowledgeBaseRecord.legacy_default(self.default_workspace)

    def _load_or_create(self) -> None:
        with self._lock:
            if not self.path.exists():
                default_record = self._default_record()
                self._records = {default_record.id: default_record}
                self._persist_locked()
                return

            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise KnowledgeBaseError(
                    f"Unable to read knowledge-base catalog {self.path}: {exc}"
                ) from exc

            if payload.get("version") != CATALOG_VERSION:
                raise KnowledgeBaseError(
                    "Unsupported knowledge-base catalog version: "
                    f"{payload.get('version')!r}"
                )

            raw_records = payload.get("knowledge_bases", [])
            if not isinstance(raw_records, list):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog field 'knowledge_bases' must be a list"
                )
            parsed_records = [
                KnowledgeBaseRecord.from_dict(item) for item in raw_records
            ]
            records = {record.id: record for record in parsed_records}
            if len(records) != len(parsed_records):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog contains duplicate IDs"
                )
            workspaces = [record.effective_workspace for record in parsed_records]
            if len(set(workspaces)) != len(workspaces):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog contains duplicate effective workspaces"
                )
            canonical_keys = [
                record.canonical_workspace_key for record in parsed_records
            ]
            if len(set(canonical_keys)) != len(canonical_keys):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog contains duplicate canonical workspaces"
                )
            default_record = records.get(DEFAULT_KNOWLEDGE_BASE_ID)
            if default_record is None:
                raise KnowledgeBaseError(
                    "Knowledge-base catalog is missing the default record"
                )
            if default_record.effective_workspace != self.default_workspace:
                raise KnowledgeBaseError(
                    "Default knowledge-base workspace differs from the server "
                    "workspace; refusing to remap existing data"
                )
            self._records = records
            raw_operations = payload.get("operations", [])
            if not isinstance(raw_operations, list):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog field 'operations' must be a list"
                )
            operations = [CatalogOperation.from_dict(item) for item in raw_operations]
            self._operations = {
                operation.operation_id: operation for operation in operations
            }
            if len(self._operations) != len(operations):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog contains duplicate operation IDs"
                )
            idempotency_keys = [
                operation.idempotency_key
                for operation in operations
                if operation.idempotency_key is not None
            ]
            if len(set(idempotency_keys)) != len(idempotency_keys):
                raise KnowledgeBaseError(
                    "Knowledge-base catalog contains duplicate idempotency keys"
                )
            self._fencing_token = max(
                int(payload.get("fencing_token", 0)),
                max(
                    (operation.fencing_token for operation in operations),
                    default=0,
                ),
            )

    def _persist_locked(self) -> None:
        payload = {
            "version": CATALOG_VERSION,
            "knowledge_bases": [
                record.public_dict()
                for record in sorted(self._records.values(), key=lambda item: item.id)
            ],
            "operations": [
                operation.public_dict()
                for operation in sorted(
                    self._operations.values(), key=lambda item: item.operation_id
                )
            ],
            "fencing_token": self._fencing_token,
        }

        def _write(temp_path: str) -> None:
            Path(temp_path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        atomic_write(str(self.path), _write, self.default_workspace or "default")

    def list(self) -> list[KnowledgeBaseRecord]:
        with self._lock:
            return sorted(
                self._records.values(),
                key=lambda item: (
                    item.id != DEFAULT_KNOWLEDGE_BASE_ID,
                    item.created_at,
                ),
            )

    def get(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        normalized = validate_knowledge_base_id(knowledge_base_id)
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                raise KnowledgeBaseNotFoundError(
                    f"Knowledge base {normalized!r} does not exist"
                )
            return record

    def create(
        self,
        *,
        name: str,
        isolation_level: Literal["logical", "physical"],
        storage_profile_id: str | None,
    ) -> KnowledgeBaseRecord:
        name = name.strip()
        if not name or len(name) > 128:
            raise ValueError("Knowledge-base name must contain 1-128 characters")
        now = _utc_now()
        with self._lock:
            while True:
                knowledge_base_id = f"kb_{uuid4().hex[:12]}"
                if knowledge_base_id not in self._records:
                    break
            record = KnowledgeBaseRecord(
                id=knowledge_base_id,
                name=name,
                effective_workspace=knowledge_base_id,
                isolation_level=isolation_level,
                storage_profile_id=storage_profile_id,
                created_at=now,
                updated_at=now,
                workspace_kind="named",
                canonical_workspace_key=knowledge_base_id,
                namespace_codec_version=NAMED_NAMESPACE_CODEC,
            )
            record.validate()
            self._records[record.id] = record
            try:
                self._persist_locked()
            except BaseException:
                self._records.pop(record.id, None)
                raise
            return record

    def rename(self, knowledge_base_id: str, name: str) -> KnowledgeBaseRecord:
        name = name.strip()
        if not name or len(name) > 128:
            raise ValueError("Knowledge-base name must contain 1-128 characters")
        with self._lock:
            current = self.get(knowledge_base_id)
            updated = KnowledgeBaseRecord(
                **{
                    **current.public_dict(),
                    "name": name,
                    "revision": current.revision + 1,
                    "updated_at": _utc_now(),
                }
            )
            self._records[current.id] = updated
            try:
                self._persist_locked()
            except BaseException:
                self._records[current.id] = current
                raise
            return updated

    def delete(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if normalized == DEFAULT_KNOWLEDGE_BASE_ID:
            raise KnowledgeBaseConflictError(
                "The default knowledge base cannot be deleted"
            )
        with self._lock:
            record = self.get(normalized)
            del self._records[normalized]
            try:
                self._persist_locked()
            except BaseException:
                self._records[normalized] = record
                raise
            return record

    def create_workspace_operation(
        self,
        *,
        record: KnowledgeBaseRecord,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[KnowledgeBaseRecord, CatalogOperation, bool]:
        """Atomically persist CREATING + PENDING in the local provider."""

        payload_hash = canonical_payload_hash(payload)
        with self._lock:
            if idempotency_key:
                existing = next(
                    (
                        operation
                        for operation in self._operations.values()
                        if operation.idempotency_key == idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing.operation_type is not CatalogOperationType.CREATE
                        or existing.payload_hash != payload_hash
                    ):
                        raise CatalogIdempotencyConflict(
                            "Idempotency key is already used by a different request"
                        )
                    return self._records[existing.workspace_id], existing, False
            if record.id in self._records:
                raise KnowledgeBaseConflictError(
                    f"Knowledge base {record.id!r} already exists"
                )
            if any(
                item.canonical_workspace_key == record.canonical_workspace_key
                for item in self._records.values()
            ):
                raise KnowledgeBaseConflictError(
                    "Knowledge-base canonical workspace already exists"
                )
            now = _utc_now()
            operation = CatalogOperation(
                operation_id=f"op_{uuid4().hex}",
                workspace_id=record.id,
                operation_type=CatalogOperationType.CREATE,
                state=CatalogOperationState.PENDING,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
                owner_id=None,
                fencing_token=0,
                revision=1,
                created_at=now,
                updated_at=now,
                metadata=dict(payload),
            )
            pending_record = replace(
                record,
                lifecycle_state=WorkspaceLifecycleState.CREATING.value,
                revision=1,
                current_operation_id=operation.operation_id,
                error_code=None,
                error_message=None,
                updated_at=now,
            )
            pending_record.validate()
            self._records[pending_record.id] = pending_record
            self._operations[operation.operation_id] = operation
            try:
                self._persist_locked()
            except BaseException:
                self._records.pop(pending_record.id, None)
                self._operations.pop(operation.operation_id, None)
                raise
            return pending_record, operation, True

    def get_operation(self, operation_id: str) -> CatalogOperation:
        with self._lock:
            try:
                return self._operations[operation_id]
            except KeyError as exc:
                raise CatalogOperationNotFound(
                    f"Catalog operation {operation_id!r} does not exist"
                ) from exc

    def create_delete_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[KnowledgeBaseRecord, CatalogOperation, bool]:
        """Atomically persist ACTIVE -> DELETING plus its operation."""

        payload_hash = canonical_payload_hash(payload)
        with self._lock:
            if idempotency_key:
                existing = next(
                    (
                        operation
                        for operation in self._operations.values()
                        if operation.idempotency_key == idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing.operation_type is not CatalogOperationType.DELETE
                        or existing.payload_hash != payload_hash
                    ):
                        raise CatalogIdempotencyConflict(
                            "Idempotency key is already used by a different request"
                        )
                    return self._records[existing.workspace_id], existing, False

            record = self.get(workspace_id)
            if record.lifecycle_state != WorkspaceLifecycleState.ACTIVE.value:
                raise CatalogCASConflict(
                    f"Workspace {workspace_id!r} is not ACTIVE and cannot be deleted"
                )
            now = _utc_now()
            operation = CatalogOperation(
                operation_id=f"op_{uuid4().hex}",
                workspace_id=record.id,
                operation_type=CatalogOperationType.DELETE,
                state=CatalogOperationState.PENDING,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
                owner_id=None,
                fencing_token=0,
                revision=1,
                created_at=now,
                updated_at=now,
                metadata=dict(payload),
            )
            deleting = replace(
                record,
                lifecycle_state=WorkspaceLifecycleState.DELETING.value,
                revision=record.revision + 1,
                current_operation_id=operation.operation_id,
                error_code=None,
                error_message=None,
                updated_at=now,
            )
            self._records[record.id] = deleting
            self._operations[operation.operation_id] = operation
            try:
                self._persist_locked()
            except BaseException:
                self._records[record.id] = record
                self._operations.pop(operation.operation_id, None)
                raise
            return deleting, operation, True

    def create_migration_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[KnowledgeBaseRecord, CatalogOperation, bool]:
        """Atomically persist ACTIVE -> MIGRATING for startup recovery."""

        payload_hash = canonical_payload_hash(payload)
        with self._lock:
            existing = next(
                (
                    operation
                    for operation in self._operations.values()
                    if operation.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.operation_type is not CatalogOperationType.MIGRATE
                    or existing.payload_hash != payload_hash
                    or existing.workspace_id != workspace_id
                ):
                    raise CatalogIdempotencyConflict(
                        "Idempotency key is already used by a different request"
                    )
                return self._records[existing.workspace_id], existing, False

            record = self.get(workspace_id)
            if record.lifecycle_state != WorkspaceLifecycleState.ACTIVE.value:
                raise CatalogCASConflict(
                    f"Workspace {workspace_id!r} is not ACTIVE and cannot migrate"
                )
            now = _utc_now()
            operation = CatalogOperation(
                operation_id=f"op_{uuid4().hex}",
                workspace_id=record.id,
                operation_type=CatalogOperationType.MIGRATE,
                state=CatalogOperationState.PENDING,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
                owner_id=None,
                fencing_token=0,
                revision=1,
                created_at=now,
                updated_at=now,
                metadata=dict(payload),
            )
            migrating = replace(
                record,
                lifecycle_state=WorkspaceLifecycleState.MIGRATING.value,
                revision=record.revision + 1,
                current_operation_id=operation.operation_id,
                error_code=None,
                error_message=None,
                updated_at=now,
            )
            self._records[record.id] = migrating
            self._operations[operation.operation_id] = operation
            try:
                self._persist_locked()
            except BaseException:
                self._records[record.id] = record
                self._operations.pop(operation.operation_id, None)
                raise
            return migrating, operation, True

    def claim_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        reclaim_running: bool = False,
    ) -> CatalogOperation:
        with self._lock:
            current = self.get_operation(operation_id)
            claimable = current.state in {
                CatalogOperationState.PENDING,
                CatalogOperationState.FAILED,
            } or (
                reclaim_running
                and current.state is CatalogOperationState.RUNNING
                and current.owner_id != owner_id
            )
            if not claimable:
                raise CatalogCASConflict(
                    f"Catalog operation {operation_id!r} is not claimable"
                )
            self._fencing_token += 1
            updated = replace(
                current,
                state=CatalogOperationState.RUNNING,
                owner_id=owner_id,
                fencing_token=self._fencing_token,
                revision=current.revision + 1,
                retry_count=current.retry_count
                + (
                    1
                    if current.state
                    in {
                        CatalogOperationState.FAILED,
                        CatalogOperationState.RUNNING,
                    }
                    else 0
                ),
                error_code=None,
                error_message=None,
                updated_at=_utc_now(),
            )
            self._operations[operation_id] = updated
            self._persist_locked()
            return updated

    def transition_record(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        expected_states: Sequence[WorkspaceLifecycleState],
        target_state: WorkspaceLifecycleState,
        operation_id: str,
        owner_id: str,
        fencing_token: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> KnowledgeBaseRecord:
        with self._lock:
            record = self.get(workspace_id)
            operation = self.get_operation(operation_id)
            if (
                record.revision != expected_revision
                or WorkspaceLifecycleState(record.lifecycle_state)
                not in set(expected_states)
                or record.current_operation_id != operation_id
                or operation.state is not CatalogOperationState.RUNNING
                or operation.owner_id != owner_id
                or operation.fencing_token != fencing_token
            ):
                raise CatalogCASConflict(
                    f"Workspace {workspace_id!r} lifecycle CAS was rejected"
                )
            now = _utc_now()
            updated = replace(
                record,
                lifecycle_state=target_state.value,
                revision=record.revision + 1,
                error_code=error_code,
                error_message=error_message,
                tombstoned_at=(
                    now
                    if target_state is WorkspaceLifecycleState.TOMBSTONED
                    else record.tombstoned_at
                ),
                updated_at=now,
            )
            self._records[workspace_id] = updated
            self._persist_locked()
            return updated

    def finish_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        state: CatalogOperationState,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> CatalogOperation:
        if state not in {CatalogOperationState.SUCCEEDED, CatalogOperationState.FAILED}:
            raise ValueError("Operation can finish only as SUCCEEDED or FAILED")
        with self._lock:
            current = self.get_operation(operation_id)
            if (
                current.state is not CatalogOperationState.RUNNING
                or current.owner_id != owner_id
                or current.fencing_token != fencing_token
            ):
                raise CatalogCASConflict(
                    f"Catalog operation {operation_id!r} finish was fenced out"
                )
            updated = replace(
                current,
                state=state,
                revision=current.revision + 1,
                error_code=error_code,
                error_message=error_message,
                updated_at=_utc_now(),
            )
            self._operations[operation_id] = updated
            self._persist_locked()
            return updated

    def update_operation_metadata(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        metadata: Mapping[str, Any],
    ) -> CatalogOperation:
        with self._lock:
            current = self.get_operation(operation_id)
            if (
                current.state is not CatalogOperationState.RUNNING
                or current.owner_id != owner_id
                or current.fencing_token != fencing_token
            ):
                raise CatalogCASConflict(
                    f"Catalog operation {operation_id!r} progress was fenced out"
                )
            updated = replace(
                current,
                metadata={**dict(current.metadata), **dict(metadata)},
                revision=current.revision + 1,
                updated_at=_utc_now(),
            )
            self._operations[operation_id] = updated
            self._persist_locked()
            return updated

    def list_unfinished_operations(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[CatalogOperation, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Operation page limit must be between 1 and 1000")
        with self._lock:
            unfinished = [
                operation
                for operation in self._operations.values()
                if operation.state
                in {
                    CatalogOperationState.PENDING,
                    CatalogOperationState.RUNNING,
                    CatalogOperationState.FAILED,
                }
            ]
            return tuple(
                sorted(
                    (
                        operation
                        for operation in unfinished
                        if cursor is None or operation.operation_id > cursor
                    ),
                    key=lambda item: item.operation_id,
                )[:limit]
            )


def validate_knowledge_base_id(value: str | None) -> str:
    if value is None:
        return DEFAULT_KNOWLEDGE_BASE_ID
    normalized = value.strip()
    if not normalized:
        raise ValueError("Knowledge-base selector cannot be empty")
    if not _ID_PATTERN.fullmatch(normalized):
        raise ValueError("Knowledge-base ID must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    return normalized


@dataclass(slots=True)
class KnowledgeBaseContext:
    metadata: KnowledgeBaseRecord
    rag: Any
    document_manager: Any
    active_requests: int = 0


@dataclass(slots=True)
class KnowledgeBaseSideEffectCounters:
    """Attempt counters used to prove observational routes stay side-effect free."""

    instance_constructions: int = 0
    storage_initializations: int = 0
    migrations: int = 0

    def snapshot(self) -> dict[str, int]:
        return asdict(self)


_current_context: ContextVar[WorkspaceExecutionContext | None] = ContextVar(
    "lightrag_knowledge_base_context", default=None
)


class _BoundBackgroundLease:
    """Rebind explicit authority while holding a pool background lease."""

    def __init__(self, lease) -> None:
        self.lease = lease
        self._token = None

    async def __aenter__(self) -> WorkspaceExecutionContext:
        self._token = _current_context.set(self.lease.context)
        return self.lease.context

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._token is not None:
                _current_context.reset(self._token)
        finally:
            await self.lease.release()


class RequestScopedProxy:
    """Forward attribute access to the active request's RAG or doc manager."""

    def __init__(
        self,
        manager: "KnowledgeBaseManager",
        attribute: str,
        *,
        bootstrap_attributes: frozenset[str] = frozenset(),
    ) -> None:
        object.__setattr__(self, "_manager", manager)
        object.__setattr__(self, "_attribute", attribute)
        object.__setattr__(self, "_bootstrap_attributes", bootstrap_attributes)

    def _target(self) -> Any:
        context = _current_context.get()
        if context is None:
            if self._manager.multi_workspace_enabled:
                raise WorkspaceContextMissingError(
                    "Multi-workspace proxy access requires an explicit leased context"
                )
            context = self._manager.default_execution_context
        return getattr(context, self._attribute)

    def __getattr__(self, name: str) -> Any:
        if _current_context.get() is None and name in object.__getattribute__(
            self, "_bootstrap_attributes"
        ):
            default_target = getattr(
                self._manager.default_execution_context, self._attribute
            )
            return getattr(default_target, name)
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target(), name, value)


class KnowledgeBaseManager:
    """Own isolated LightRAG instances and bind them to API requests."""

    def __init__(
        self,
        *,
        catalog: KnowledgeBaseCatalog | CatalogProvider,
        default_record: KnowledgeBaseRecord | None = None,
        default_rag: Any,
        default_document_manager: Any,
        rag_factory: Callable[[KnowledgeBaseRecord, Mapping[str, Any] | None], Any],
        document_manager_factory: Callable[
            [KnowledgeBaseRecord, Mapping[str, Any] | None], Any
        ],
        storage_profiles: Mapping[str, Mapping[str, Any]] | None = None,
        default_storage_profile: Mapping[str, Any] | None = None,
        active_storage_implementations: Sequence[
            str
        ] = _DEFAULT_STORAGE_IMPLEMENTATIONS,
        max_loaded_instances: int = 32,
        max_loaded_resource_weight: int | None = None,
        multi_workspace_enabled: bool = True,
        allow_non_default_writes: bool = True,
        workspace_coordinator: WorkspaceCoordinator | None = None,
    ) -> None:
        if max_loaded_instances < 1:
            raise ValueError("max_loaded_instances must be at least 1")
        if isinstance(catalog, CatalogProvider):
            self.catalog_provider = catalog
            self.catalog = getattr(catalog, "catalog", catalog)
        else:
            self.catalog = catalog
            self.catalog_provider = LocalCatalogProvider(catalog)
        self._rag_factory = rag_factory
        self._document_manager_factory = document_manager_factory
        self._storage_profiles = dict(storage_profiles or {})
        self._active_storage_implementations = tuple(active_storage_implementations)
        self._required_profile_sections = required_profile_sections(
            self._active_storage_implementations
        )
        self._default_resource_fingerprints = (
            profile_resource_fingerprints(
                default_storage_profile, self._required_profile_sections
            )
            if default_storage_profile is not None
            else {}
        )
        self._forced_workspace_variables = forced_workspace_variables(
            self._active_storage_implementations
        )
        self._max_loaded_instances = max_loaded_instances
        self.multi_workspace_enabled = multi_workspace_enabled
        self.allow_non_default_writes = allow_non_default_writes
        self.workspace_coordinator = (
            workspace_coordinator or LocalWorkspaceCoordinator()
        )
        default_record = default_record or self._catalog_get_cached(
            DEFAULT_KNOWLEDGE_BASE_ID
        )
        self.default_context = KnowledgeBaseContext(
            metadata=default_record,
            rag=default_rag,
            document_manager=default_document_manager,
        )
        self._contexts: dict[str, KnowledgeBaseContext] = {
            DEFAULT_KNOWLEDGE_BASE_ID: self.default_context
        }
        self._instance_locks: dict[str, asyncio.Lock] = {}
        self._manager_lock = asyncio.Lock()
        self._initialized_ids: set[str] = set()
        self._deleting_ids: set[str] = set()
        self._lifecycle_tasks: dict[str, asyncio.Task[None]] = {}
        self._lifecycle_owner_id = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        )
        self.recovery_coordinator = WorkspaceRecoveryCoordinator(
            catalog=self.catalog_provider,
            run_create=self._run_create_lifecycle,
            run_migrate=self._run_migration_lifecycle,
            run_delete=self._run_delete_lifecycle,
            page_size=int(os.getenv("LIGHTRAG_RECOVERY_PAGE_SIZE", "100") or "100"),
        )
        self._started = False
        self.side_effect_counters = KnowledgeBaseSideEffectCounters()
        self.default_execution_context = self._execution_context(self.default_context)
        self.instance_pool = WorkspaceInstancePool(
            construct=self._construct_execution_context,
            initialize=self._initialize_execution_context,
            finalize=self._finalize_execution_context,
            can_evict=self._can_evict_execution_context,
            weight_for=self._resource_weight_for,
            max_entries=max_loaded_instances,
            max_weight=(
                max_loaded_resource_weight
                if max_loaded_resource_weight is not None
                else max_loaded_instances
            ),
            failure_backoff_seconds=float(
                os.getenv("LIGHTRAG_WORKSPACE_POOL_FAILURE_BACKOFF_SECONDS", "1") or "1"
            ),
            failure_backoff_max_seconds=float(
                os.getenv("LIGHTRAG_WORKSPACE_POOL_FAILURE_BACKOFF_MAX_SECONDS", "30")
                or "30"
            ),
        )
        self.rag_proxy = RequestScopedProxy(
            self,
            "rag",
            bootstrap_attributes=frozenset({"ollama_server_infos"}),
        )
        self.document_manager_proxy = RequestScopedProxy(self, "document_manager")

    def _catalog_get_cached(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        get_cached = getattr(self.catalog_provider, "get_cached", None)
        if not callable(get_cached):
            raise KnowledgeBaseError(
                "Catalog provider does not expose a local snapshot"
            )
        return get_cached(knowledge_base_id)

    def _catalog_list_cached(self) -> list[KnowledgeBaseRecord]:
        list_cached = getattr(self.catalog_provider, "list_cached", None)
        if not callable(list_cached):
            raise KnowledgeBaseError(
                "Catalog provider does not expose a local snapshot"
            )
        return list(list_cached())

    @classmethod
    def load_storage_profiles(cls, path: str | None) -> dict[str, Mapping[str, Any]]:
        if not path:
            return {}
        profile_path = Path(path)
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageProfileError(
                f"Unable to read storage profile file {profile_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise StorageProfileError("Storage profile file must contain an object")
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict):
            raise StorageProfileError("Storage profile file must contain 'profiles'")
        if not all(
            isinstance(profile_id, str) and isinstance(profile, dict)
            for profile_id, profile in profiles.items()
        ):
            raise StorageProfileError(
                "Every storage profile must have a string ID and object value"
            )
        return profiles

    def list_storage_profiles(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for profile_id, profile in sorted(self._storage_profiles.items()):
            try:
                self._assert_profile_available(profile_id)
                available = True
            except StorageProfileError:
                available = False
            result.append(
                {
                    "id": profile_id,
                    "available": available,
                    "dedicated": profile.get("dedicated") is True,
                }
            )
        return result

    def list_records(self) -> list[KnowledgeBaseRecord]:
        records = self._catalog_list_cached()
        if self.multi_workspace_enabled:
            return records
        return [record for record in records if record.id == DEFAULT_KNOWLEDGE_BASE_ID]

    def get_record(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if not self.multi_workspace_enabled and normalized != DEFAULT_KNOWLEDGE_BASE_ID:
            self._catalog_get_cached(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        return self._catalog_get_cached(normalized)

    async def alist_records(self) -> list[KnowledgeBaseRecord]:
        page = await self.catalog_provider.list_records(limit=1000)
        records = list(page.records)
        if self.multi_workspace_enabled:
            return records
        return [record for record in records if record.id == DEFAULT_KNOWLEDGE_BASE_ID]

    async def alist_record_page(
        self,
        *,
        limit: int,
        cursor: str | None,
        states: Sequence[WorkspaceLifecycleState] | None = None,
    ):
        page = await self.catalog_provider.list_records(
            limit=limit, cursor=cursor, states=states
        )
        if self.multi_workspace_enabled:
            return page
        records = tuple(
            record for record in page.records if record.id == DEFAULT_KNOWLEDGE_BASE_ID
        )
        return type(page)(records=records, next_cursor=None)

    async def aget_record(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if not self.multi_workspace_enabled and normalized != DEFAULT_KNOWLEDGE_BASE_ID:
            await self.catalog_provider.get_record(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        return await self.catalog_provider.get_record(normalized)

    def rename(self, knowledge_base_id: str, name: str) -> KnowledgeBaseRecord:
        current = self.get_record(knowledge_base_id)
        return self.catalog.rename(current.id, name)

    async def arename(self, knowledge_base_id: str, name: str) -> KnowledgeBaseRecord:
        name = name.strip()
        if not name or len(name) > 128:
            raise ValueError("Knowledge-base name must contain 1-128 characters")
        current = await self.aget_record(knowledge_base_id)
        return await self.catalog_provider.update_name(
            current.id, expected_revision=current.revision, name=name
        )

    def _profile_for(self, record: KnowledgeBaseRecord) -> Mapping[str, Any] | None:
        if record.isolation_level == "logical":
            if record.id != DEFAULT_KNOWLEDGE_BASE_ID:
                forced = [
                    name
                    for name in self._forced_workspace_variables
                    if os.getenv(name, "").strip()
                ]
                if forced:
                    raise StorageProfileError(
                        "Dynamic knowledge bases cannot be used while storage "
                        "workspace overrides are set: " + ", ".join(forced)
                    )
            return None
        profile_id = record.storage_profile_id
        profile = self._storage_profiles.get(profile_id or "")
        if profile is None:
            raise StorageProfileError(
                f"Storage profile {profile_id!r} is not configured"
            )
        try:
            validate_storage_profile(
                profile_id or "", profile, self._required_profile_sections
            )
        except ValueError as exc:
            raise StorageProfileError(str(exc)) from exc
        return profile

    def _assert_profile_available(self, profile_id: str | None) -> None:
        if not profile_id:
            raise StorageProfileError("Physical isolation requires storage_profile_id")
        candidate = KnowledgeBaseRecord(
            id="candidate",
            name="candidate",
            effective_workspace="candidate",
            isolation_level="physical",
            storage_profile_id=profile_id,
            created_at=_utc_now(),
            updated_at=_utc_now(),
            workspace_kind="named",
            canonical_workspace_key="candidate",
            namespace_codec_version=NAMED_NAMESPACE_CODEC,
        )
        self._profile_for(candidate)
        for record in self._catalog_list_cached():
            if record.storage_profile_id == profile_id:
                raise StorageProfileError(
                    f"Storage profile {profile_id!r} is already assigned to {record.id!r}"
                )
        candidate_fingerprints = profile_resource_fingerprints(
            self._storage_profiles[profile_id], self._required_profile_sections
        )
        default_reuse = sorted(
            section
            for section, fingerprint in candidate_fingerprints.items()
            if self._default_resource_fingerprints.get(section) == fingerprint
        )
        if default_reuse:
            raise StorageProfileError(
                f"Storage profile {profile_id!r} reuses default resources: "
                + ", ".join(default_reuse)
            )
        for record in self._catalog_list_cached():
            if not record.storage_profile_id:
                continue
            assigned_profile = self._storage_profiles.get(record.storage_profile_id)
            if assigned_profile is None:
                continue
            assigned_fingerprints = profile_resource_fingerprints(
                assigned_profile, self._required_profile_sections
            )
            reused_sections = sorted(
                section
                for section, fingerprint in candidate_fingerprints.items()
                if assigned_fingerprints.get(section) == fingerprint
            )
            if reused_sections:
                raise StorageProfileError(
                    f"Storage profile {profile_id!r} reuses dedicated resources "
                    f"assigned to {record.id!r}: " + ", ".join(reused_sections)
                )

    async def initialize(self) -> None:
        self._started = True
        # The manager may be constructed in Gunicorn's preloaded master. Never
        # let forked workers inherit one lifecycle owner identity.
        self._lifecycle_owner_id = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        )
        try:
            durable_default = await self.catalog_provider.initialize(
                self.default_context.metadata
            )
            self.default_context.metadata = durable_default
            recovery_payload = await self.workspace_coordinator.run_startup_once(
                self._recover_startup_payload
            )
            self.recovery_coordinator.last_report = WorkspaceRecoveryReport(
                **recovery_payload
            )
            durable_default = await self.catalog_provider.get_record(
                DEFAULT_KNOWLEDGE_BASE_ID, include_tombstoned=True
            )
            if durable_default.lifecycle_state != WorkspaceLifecycleState.ACTIVE.value:
                raise KnowledgeBaseError(
                    "Default knowledge base did not recover to ACTIVE during startup"
                )
            self.default_context.metadata = durable_default
            self.default_execution_context = self._execution_context(
                self.default_context
            )
            await self._initialize_execution_context(self.default_execution_context)
            self.instance_pool.add_ready(
                self.default_execution_context,
                pinned=True,
            )
        except BaseException:
            self._started = False
            raise

    async def _recover_startup_payload(self) -> dict[str, Any]:
        report = await self.recovery_coordinator.recover()
        return report.public_dict()

    @staticmethod
    def _execution_context(
        context: KnowledgeBaseContext,
    ) -> WorkspaceExecutionContext:
        return WorkspaceExecutionContext(
            metadata=context.metadata,
            binding=context.metadata.to_workspace_binding(),
            rag=context.rag,
            document_manager=context.document_manager,
        )

    def _resource_weight_for(self, record: KnowledgeBaseRecord) -> int:
        return 2 if record.isolation_level == "physical" else 1

    async def _construct_execution_context(
        self, record: KnowledgeBaseRecord
    ) -> WorkspaceExecutionContext:
        profile = self._profile_for(record)
        async with self._manager_lock:
            context = self._contexts.get(record.id)
            if context is None:
                self.side_effect_counters.instance_constructions += 1
                context = KnowledgeBaseContext(
                    metadata=record,
                    rag=self._rag_factory(record, profile),
                    document_manager=self._document_manager_factory(record, profile),
                )
                self._contexts[record.id] = context
            else:
                context.metadata = record
        return self._execution_context(context)

    async def _initialize_execution_context(
        self, context: WorkspaceExecutionContext
    ) -> None:
        # Test/library callers may assemble routers without entering the ASGI
        # lifespan. Production serving always calls ``initialize`` first; do
        # not manufacture a partial server startup from a request.
        if not self._started:
            return
        knowledge_base_id = context.metadata.id
        if knowledge_base_id in self._initialized_ids:
            return
        lock = self._instance_locks.setdefault(knowledge_base_id, asyncio.Lock())
        async with lock:
            if knowledge_base_id in self._initialized_ids:
                return
            self.side_effect_counters.storage_initializations += 1
            await context.rag.initialize_storages()
            self._initialized_ids.add(knowledge_base_id)

    async def _finalize_execution_context(
        self, context: WorkspaceExecutionContext
    ) -> None:
        knowledge_base_id = context.metadata.id
        if knowledge_base_id in self._initialized_ids:
            await context.rag.finalize_storages()
            self._initialized_ids.discard(knowledge_base_id)
        if knowledge_base_id != DEFAULT_KNOWLEDGE_BASE_ID:
            self._contexts.pop(knowledge_base_id, None)

    async def _can_evict_execution_context(
        self, context: WorkspaceExecutionContext
    ) -> bool:
        if context.metadata.id in self._deleting_ids:
            return False
        try:
            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=context.metadata.effective_workspace
            )
        except PipelineNotInitializedError:
            # No namespace means the pipeline was never admitted for this
            # context, so there is no non-lease work to protect from eviction.
            return True
        except (RuntimeError, ValueError) as exc:
            # Direct library/unit-test managers may not initialize shared state.
            if "not initialized" in str(exc).lower():
                return True
            raise
        return not bool(
            pipeline_status.get("busy")
            or pipeline_status.get("scanning")
            or pipeline_status.get("destructive_busy")
            or int(pipeline_status.get("pending_enqueues", 0) or 0) > 0
        )

    async def _initialize_context(self, context: KnowledgeBaseContext) -> None:
        knowledge_base_id = context.metadata.id
        if knowledge_base_id in self._initialized_ids:
            return
        lock = self._instance_locks.setdefault(knowledge_base_id, asyncio.Lock())
        async with lock:
            if knowledge_base_id in self._initialized_ids:
                return
            try:
                self.side_effect_counters.storage_initializations += 1
                await context.rag.initialize_storages()
                self.side_effect_counters.migrations += 1
                await context.rag.check_and_migrate_data()
            except BaseException:
                try:
                    await context.rag.finalize_storages()
                except BaseException as cleanup_error:
                    logger.error(
                        "Failed to finalize knowledge base %s after initialization error: %s",
                        knowledge_base_id,
                        cleanup_error,
                    )
                if knowledge_base_id != DEFAULT_KNOWLEDGE_BASE_ID:
                    self._contexts.pop(knowledge_base_id, None)
                raise
            self._initialized_ids.add(knowledge_base_id)

    async def get_context(self, knowledge_base_id: str | None) -> KnowledgeBaseContext:
        try:
            normalized = validate_knowledge_base_id(knowledge_base_id)
        except ValueError as exc:
            raise KnowledgeBaseNotFoundError(str(exc)) from exc
        if not self.multi_workspace_enabled and normalized != DEFAULT_KNOWLEDGE_BASE_ID:
            await self.catalog_provider.get_record(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        if normalized in self._deleting_ids:
            raise KnowledgeBaseConflictError(
                f"Knowledge base {normalized!r} is being deleted"
            )
        record = await self.catalog_provider.get_record(normalized)
        if record.lifecycle_state != WorkspaceLifecycleState.ACTIVE.value:
            raise KnowledgeBaseConflictError(
                f"Knowledge base {normalized!r} is {record.lifecycle_state}"
            )
        existing = self._contexts.get(normalized)
        if existing is not None:
            existing.metadata = record
            if self._started:
                await self._initialize_execution_context(
                    self._execution_context(existing)
                )
            return existing

        profile = self._profile_for(record)
        async with self._manager_lock:
            context = self._contexts.get(normalized)
            if context is None:
                if len(self._contexts) >= self._max_loaded_instances:
                    raise KnowledgeBaseConflictError(
                        "Maximum loaded knowledge-base instances reached "
                        f"({self._max_loaded_instances})"
                    )
                self.side_effect_counters.instance_constructions += 1
                context = KnowledgeBaseContext(
                    metadata=record,
                    rag=self._rag_factory(record, profile),
                    document_manager=self._document_manager_factory(record, profile),
                )
                self._contexts[normalized] = context
        if self._started:
            await self._initialize_execution_context(self._execution_context(context))
        return context

    @asynccontextmanager
    async def bind_request(
        self,
        knowledge_base_id: str | None,
        *,
        lease_kind: Literal["foreground", "stream", "background"] = "foreground",
        operation_kind: str = "query",
    ) -> AsyncIterator[WorkspaceExecutionContext]:
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if not self.multi_workspace_enabled and normalized != DEFAULT_KNOWLEDGE_BASE_ID:
            await self.catalog_provider.get_record(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        if normalized in self._deleting_ids:
            raise KnowledgeBaseConflictError(
                f"Knowledge base {normalized!r} is being deleted"
            )
        record = await self.catalog_provider.get_record(
            normalized, include_tombstoned=True
        )
        state = WorkspaceLifecycleState(record.lifecycle_state)
        if state is WorkspaceLifecycleState.TOMBSTONED:
            raise KnowledgeBaseNotFoundError(
                f"Knowledge base {normalized!r} has been deleted"
            )
        if state is WorkspaceLifecycleState.DELETING:
            raise KnowledgeBaseConflictError(
                f"Knowledge base {normalized!r} is being deleted"
            )
        if state is not WorkspaceLifecycleState.ACTIVE:
            raise KnowledgeBaseUnavailableError(
                f"Knowledge base {normalized!r} is {state.value}"
            )
        lease = await self.instance_pool.acquire(record, kind=lease_kind)
        context = lease.context
        token = _current_context.set(context)
        background_factory_token = bind_background_lease_factory(
            lambda: self._capture_background_lease(context)
        )
        execution_scope_token = bind_workspace_execution_scope(
            context.metadata.id, operation_kind
        )
        try:
            yield context
        finally:
            reset_workspace_execution_scope(execution_scope_token)
            reset_background_lease_factory(background_factory_token)
            _current_context.reset(token)
            await lease.release()

    async def _capture_background_lease(
        self, context: WorkspaceExecutionContext
    ) -> _BoundBackgroundLease:
        lease = await self.instance_pool.acquire_existing(context, kind="background")
        return _BoundBackgroundLease(lease)

    @asynccontextmanager
    async def bind_observation(
        self, knowledge_base_id: str | None
    ) -> AsyncIterator[WorkspaceExecutionContext]:
        """Bind catalog/shared-state observation without loading an instance."""

        normalized = validate_knowledge_base_id(knowledge_base_id)
        if not self.multi_workspace_enabled and normalized != DEFAULT_KNOWLEDGE_BASE_ID:
            await self.catalog_provider.get_record(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        record = await self.catalog_provider.get_record(
            normalized, include_tombstoned=True
        )
        state = WorkspaceLifecycleState(record.lifecycle_state)
        if state is WorkspaceLifecycleState.TOMBSTONED:
            raise KnowledgeBaseNotFoundError(
                f"Knowledge base {normalized!r} has been deleted"
            )
        pool_entries = self.instance_pool.peek(normalized)["entries"]
        runtime_state = pool_entries[0]["state"] if pool_entries else "UNLOADED"
        context = WorkspaceExecutionContext(
            metadata=record,
            binding=record.to_workspace_binding(),
            rag=SimpleNamespace(
                workspace=record.effective_workspace,
                runtime_state=runtime_state,
            ),
            document_manager=None,
        )
        token = _current_context.set(context)
        try:
            yield context
        finally:
            _current_context.reset(token)

    async def request_dependency(
        self,
        request: Request,
        knowledge_base_id: KnowledgeBaseHeader = None,
    ) -> AsyncIterator[None]:
        try:
            route = request.scope.get("route")
            route_name = getattr(route, "name", None)
            endpoint_policy = ENDPOINT_POLICIES.get(route_name)
            normalized = validate_knowledge_base_id(knowledge_base_id)
            if (
                endpoint_policy is EndpointPolicy.DATA_WRITE
                and normalized != DEFAULT_KNOWLEDGE_BASE_ID
                and not self.allow_non_default_writes
            ):
                record = await self.catalog_provider.get_record(
                    normalized, include_tombstoned=True
                )
                state = WorkspaceLifecycleState(record.lifecycle_state)
                if state is WorkspaceLifecycleState.TOMBSTONED:
                    raise KnowledgeBaseNotFoundError(
                        f"Knowledge base {normalized!r} has been deleted"
                    )
                if state is not WorkspaceLifecycleState.ACTIVE:
                    raise KnowledgeBaseUnavailableError(
                        f"Knowledge base {normalized!r} is {state.value}"
                    )
                raise KnowledgeBaseUnavailableError(
                    "Non-default knowledge-base writes are disabled until "
                    "pipeline recovery and shared admission are enabled",
                    retryable=False,
                )
            if route_name == "get_pipeline_status":
                async with self.bind_observation(knowledge_base_id) as context:
                    request.state.knowledge_base = context.metadata.public_dict()
                    yield
                return
            lease_kind = (
                "stream"
                if request.url.path.endswith("/query/stream")
                or request.url.path.endswith("/api/chat")
                or request.url.path.endswith("/api/generate")
                else "foreground"
            )
            operation_kind = (
                "ingestion" if endpoint_policy is EndpointPolicy.DATA_WRITE else "query"
            )
            async with self.bind_request(
                knowledge_base_id,
                lease_kind=lease_kind,
                operation_kind=operation_kind,
            ) as context:
                request.state.knowledge_base = context.metadata.public_dict()
                yield
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KnowledgeBaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeBaseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KnowledgeBaseUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": "1"} if exc.retryable else None,
            ) from exc
        except WorkspacePoolCapacityError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "workspace_capacity_exhausted",
                    "message": str(exc),
                },
                headers={"Retry-After": "1"},
            ) from exc
        except (WorkspacePoolBusyError, WorkspacePoolInitializationError) as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={"Retry-After": "1"},
            ) from exc
        except StorageProfileError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def create(
        self,
        *,
        name: str,
        isolation_level: Literal["logical", "physical"],
        storage_profile_id: str | None,
    ) -> KnowledgeBaseRecord:
        if not self.multi_workspace_enabled:
            raise KnowledgeBaseConflictError("Multi-workspace mode is disabled")
        if isolation_level == "logical" and storage_profile_id:
            raise ValueError("storage_profile_id is only valid for physical isolation")
        if isolation_level == "physical":
            self._assert_profile_available(storage_profile_id)
        else:
            candidate = KnowledgeBaseRecord(
                id="candidate",
                name=name,
                effective_workspace="candidate",
                isolation_level="logical",
                storage_profile_id=None,
                created_at=_utc_now(),
                updated_at=_utc_now(),
                workspace_kind="named",
                canonical_workspace_key="candidate",
                namespace_codec_version=NAMED_NAMESPACE_CODEC,
            )
            self._profile_for(candidate)
        return self.catalog.create(
            name=name,
            isolation_level=isolation_level,
            storage_profile_id=storage_profile_id,
        )

    async def create_lifecycle(
        self,
        *,
        name: str,
        isolation_level: Literal["logical", "physical"],
        storage_profile_id: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeBaseRecord, CatalogOperation, bool]:
        """Persist an idempotent create operation and schedule its owner task."""

        if not self.multi_workspace_enabled:
            raise KnowledgeBaseConflictError("Multi-workspace mode is disabled")
        name = name.strip()
        if not name or len(name) > 128:
            raise ValueError("Knowledge-base name must contain 1-128 characters")
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("Idempotency-Key must contain 1-128 characters")
        if isolation_level == "logical" and storage_profile_id:
            raise ValueError("storage_profile_id is only valid for physical isolation")
        if isolation_level == "physical":
            # A shared provider cache may contain only recently accessed rows.
            # Refresh it before enforcing profile ownership/fingerprint reuse.
            await self.alist_records()
            self._assert_profile_available(storage_profile_id)
        else:
            candidate = KnowledgeBaseRecord(
                id="candidate",
                name=name,
                effective_workspace="candidate",
                isolation_level="logical",
                storage_profile_id=None,
                created_at=_utc_now(),
                updated_at=_utc_now(),
                workspace_kind="named",
                canonical_workspace_key="candidate",
                namespace_codec_version=NAMED_NAMESPACE_CODEC,
            )
            self._profile_for(candidate)

        now = _utc_now()
        knowledge_base_id = f"kb_{uuid4().hex[:12]}"
        requested = KnowledgeBaseRecord(
            id=knowledge_base_id,
            name=name,
            effective_workspace=knowledge_base_id,
            isolation_level=isolation_level,
            storage_profile_id=storage_profile_id,
            created_at=now,
            updated_at=now,
            workspace_kind="named",
            canonical_workspace_key=knowledge_base_id,
            namespace_codec_version=NAMED_NAMESPACE_CODEC,
            lifecycle_state=WorkspaceLifecycleState.CREATING.value,
        )
        payload = {
            "name": name,
            "isolation_level": isolation_level,
            "storage_profile_id": storage_profile_id,
        }
        (
            record,
            operation,
            created,
        ) = await self.catalog_provider.create_workspace_operation(
            record=requested,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if created or operation.state in {
            CatalogOperationState.PENDING,
            CatalogOperationState.FAILED,
        }:
            self._schedule_create_lifecycle(operation.operation_id)
        return record, operation, created

    def _schedule_create_lifecycle(self, operation_id: str) -> asyncio.Task[None]:
        existing = self._lifecycle_tasks.get(operation_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._run_create_lifecycle(operation_id),
            name=f"workspace-create:{operation_id}",
        )
        self._lifecycle_tasks[operation_id] = task

        def _discard(completed: asyncio.Task[None]) -> None:
            self._lifecycle_tasks.pop(operation_id, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "Workspace lifecycle task %s failed: %s", operation_id, exc
                )

        task.add_done_callback(_discard)
        return task

    async def _run_create_lifecycle(
        self, operation_id: str, reclaim_running: bool = False
    ) -> None:
        claim: CatalogOperation | None = None
        context: KnowledgeBaseContext | None = None
        execution_scope_token = None
        try:
            claim = await self.catalog_provider.claim_operation(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                reclaim_running=reclaim_running,
            )
            if claim.operation_type is not CatalogOperationType.CREATE:
                raise CatalogCASConflict(
                    f"Operation {operation_id!r} is not a workspace create"
                )
            execution_scope_token = bind_workspace_execution_scope(
                claim.workspace_id,
                "recovery" if reclaim_running else "management",
            )
            record = await self.catalog_provider.get_record(
                claim.workspace_id, include_tombstoned=True
            )
            migrating = await self.catalog_provider.transition_record(
                record.id,
                expected_revision=record.revision,
                expected_states=(
                    WorkspaceLifecycleState.CREATING,
                    WorkspaceLifecycleState.MIGRATING,
                    WorkspaceLifecycleState.ERROR,
                ),
                target_state=WorkspaceLifecycleState.MIGRATING,
                operation_id=operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
            )
            profile = self._profile_for(migrating)
            async with self._manager_lock:
                context = self._contexts.get(migrating.id)
                if context is None:
                    if len(self._contexts) >= self._max_loaded_instances:
                        raise KnowledgeBaseConflictError(
                            "Maximum loaded knowledge-base instances reached "
                            f"({self._max_loaded_instances})"
                        )
                    self.side_effect_counters.instance_constructions += 1
                    context = KnowledgeBaseContext(
                        metadata=migrating,
                        rag=self._rag_factory(migrating, profile),
                        document_manager=self._document_manager_factory(
                            migrating, profile
                        ),
                    )
                    self._contexts[migrating.id] = context
            await self._initialize_context(context)
            # The control-plane migration instance is bound to the MIGRATING
            # catalog revision.  Never publish/reuse it as an ACTIVE data-plane
            # instance; finalize it first and let the pool construct from the
            # immutable ACTIVE snapshot on demand.
            await context.rag.finalize_storages()
            self._initialized_ids.discard(migrating.id)
            self._contexts.pop(migrating.id, None)
            await self.catalog_provider.transition_record(
                migrating.id,
                expected_revision=migrating.revision,
                expected_states=(WorkspaceLifecycleState.MIGRATING,),
                target_state=WorkspaceLifecycleState.ACTIVE,
                operation_id=operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
            )
            await self.catalog_provider.finish_operation(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
                state=CatalogOperationState.SUCCEEDED,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Workspace create operation %s failed: %s", operation_id, exc)
            if claim is not None:
                try:
                    current = await self.catalog_provider.get_record(
                        claim.workspace_id, include_tombstoned=True
                    )
                    if current.lifecycle_state in {
                        WorkspaceLifecycleState.CREATING.value,
                        WorkspaceLifecycleState.MIGRATING.value,
                    }:
                        await self.catalog_provider.transition_record(
                            current.id,
                            expected_revision=current.revision,
                            expected_states=(
                                WorkspaceLifecycleState(current.lifecycle_state),
                            ),
                            target_state=WorkspaceLifecycleState.ERROR,
                            operation_id=operation_id,
                            owner_id=self._lifecycle_owner_id,
                            fencing_token=claim.fencing_token,
                            error_code="workspace_initialization_failed",
                            error_message=type(exc).__name__,
                        )
                    await self.catalog_provider.finish_operation(
                        operation_id,
                        owner_id=self._lifecycle_owner_id,
                        fencing_token=claim.fencing_token,
                        state=CatalogOperationState.FAILED,
                        error_code="workspace_initialization_failed",
                        error_message=type(exc).__name__,
                    )
                except Exception as update_error:
                    logger.error(
                        "Failed to persist workspace operation %s failure: %s",
                        operation_id,
                        update_error,
                    )
        finally:
            if execution_scope_token is not None:
                reset_workspace_execution_scope(execution_scope_token)

    async def _run_migration_lifecycle(
        self, operation_id: str, reclaim_running: bool = False
    ) -> None:
        """Run one startup-owned migration under catalog fencing."""

        claim: CatalogOperation | None = None
        context: KnowledgeBaseContext | None = None
        record: KnowledgeBaseRecord | None = None
        execution_scope_token = None
        try:
            claim = await self.catalog_provider.claim_operation(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                reclaim_running=reclaim_running,
            )
            if claim.operation_type is not CatalogOperationType.MIGRATE:
                raise CatalogCASConflict(
                    f"Operation {operation_id!r} is not a workspace migration"
                )
            execution_scope_token = bind_workspace_execution_scope(
                claim.workspace_id, "recovery"
            )
            record = await self.catalog_provider.get_record(
                claim.workspace_id, include_tombstoned=True
            )
            migrating = await self.catalog_provider.transition_record(
                record.id,
                expected_revision=record.revision,
                expected_states=(
                    WorkspaceLifecycleState.MIGRATING,
                    WorkspaceLifecycleState.ERROR,
                ),
                target_state=WorkspaceLifecycleState.MIGRATING,
                operation_id=operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
            )
            profile = self._profile_for(migrating)
            async with self._manager_lock:
                context = self._contexts.get(migrating.id)
                if context is None:
                    self.side_effect_counters.instance_constructions += 1
                    context = KnowledgeBaseContext(
                        metadata=migrating,
                        rag=self._rag_factory(migrating, profile),
                        document_manager=self._document_manager_factory(
                            migrating, profile
                        ),
                    )
                    self._contexts[migrating.id] = context
                else:
                    context.metadata = migrating
            await self._initialize_context(context)
            recover_pipeline = getattr(
                context.rag, "apipeline_process_enqueue_documents", None
            )
            if callable(recover_pipeline):
                await recover_pipeline()
            await context.rag.finalize_storages()
            self._initialized_ids.discard(migrating.id)
            if migrating.id != DEFAULT_KNOWLEDGE_BASE_ID:
                self._contexts.pop(migrating.id, None)
            await self.catalog_provider.transition_record(
                migrating.id,
                expected_revision=migrating.revision,
                expected_states=(WorkspaceLifecycleState.MIGRATING,),
                target_state=WorkspaceLifecycleState.ACTIVE,
                operation_id=operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
            )
            await self.catalog_provider.finish_operation(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
                state=CatalogOperationState.SUCCEEDED,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Workspace migration operation %s failed: %s", operation_id, exc
            )
            if claim is not None:
                try:
                    current = await self.catalog_provider.get_record(
                        claim.workspace_id, include_tombstoned=True
                    )
                    if current.lifecycle_state in {
                        WorkspaceLifecycleState.MIGRATING.value,
                        WorkspaceLifecycleState.ERROR.value,
                    }:
                        await self.catalog_provider.transition_record(
                            current.id,
                            expected_revision=current.revision,
                            expected_states=(
                                WorkspaceLifecycleState(current.lifecycle_state),
                            ),
                            target_state=WorkspaceLifecycleState.ERROR,
                            operation_id=operation_id,
                            owner_id=self._lifecycle_owner_id,
                            fencing_token=claim.fencing_token,
                            error_code="workspace_migration_failed",
                            error_message=type(exc).__name__,
                        )
                    await self.catalog_provider.finish_operation(
                        operation_id,
                        owner_id=self._lifecycle_owner_id,
                        fencing_token=claim.fencing_token,
                        state=CatalogOperationState.FAILED,
                        error_code="workspace_migration_failed",
                        error_message=type(exc).__name__,
                    )
                except Exception as update_error:
                    logger.error(
                        "Failed to persist workspace migration %s failure: %s",
                        operation_id,
                        update_error,
                    )
        finally:
            if execution_scope_token is not None:
                reset_workspace_execution_scope(execution_scope_token)
            if context is not None and record is not None:
                if record.id in self._initialized_ids:
                    try:
                        await context.rag.finalize_storages()
                    except Exception as cleanup_error:
                        logger.error(
                            "Failed to finalize migration workspace %s: %s",
                            record.id,
                            cleanup_error,
                        )
                    self._initialized_ids.discard(record.id)
                if record.id != DEFAULT_KNOWLEDGE_BASE_ID:
                    self._contexts.pop(record.id, None)

    async def wait_for_operation(
        self, operation_id: str, *, timeout: float
    ) -> CatalogOperation:
        if timeout <= 0:
            return await self.catalog_provider.get_operation(operation_id)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            operation = await self.catalog_provider.get_operation(operation_id)
            if operation.state in {
                CatalogOperationState.SUCCEEDED,
                CatalogOperationState.FAILED,
            }:
                return operation
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return operation
            await asyncio.sleep(min(0.05, remaining))

    async def get_operation(self, operation_id: str) -> CatalogOperation:
        return await self.catalog_provider.get_operation(operation_id)

    async def delete_lifecycle(
        self,
        knowledge_base_id: str,
        *,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeBaseRecord, CatalogOperation, bool]:
        """Fence new local leases, persist deletion intent, and schedule cleanup."""

        if not self.multi_workspace_enabled:
            raise KnowledgeBaseConflictError("Multi-workspace mode is disabled")
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if normalized == DEFAULT_KNOWLEDGE_BASE_ID:
            raise KnowledgeBaseConflictError(
                "The default knowledge base cannot be deleted"
            )
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("Idempotency-Key must contain 1-128 characters")

        reserved = False
        pool_reserved = False
        async with self._manager_lock:
            try:
                if normalized not in self._deleting_ids:
                    try:
                        await self.instance_pool.reserve_delete(normalized)
                        pool_reserved = True
                    except WorkspacePoolBusyError as exc:
                        raise KnowledgeBaseConflictError(
                            f"Knowledge base {normalized!r} has active leases"
                        ) from exc
                    record = await self.catalog_provider.get_record(
                        normalized, include_tombstoned=True
                    )
                    pipeline_status = await get_namespace_data(
                        "pipeline_status", workspace=record.effective_workspace
                    )
                    if pipeline_status and (
                        pipeline_status.get("busy")
                        or pipeline_status.get("scanning")
                        or int(pipeline_status.get("pending_enqueues", 0) or 0) > 0
                    ):
                        raise KnowledgeBaseConflictError(
                            f"Knowledge base {normalized!r} has an active pipeline"
                        )
                    self._deleting_ids.add(normalized)
                    reserved = True
                (
                    record,
                    operation,
                    created,
                ) = await self.catalog_provider.create_delete_operation(
                    workspace_id=normalized,
                    idempotency_key=idempotency_key,
                    payload={"workspace_id": normalized},
                )
            except BaseException:
                if reserved:
                    self._deleting_ids.discard(normalized)
                if pool_reserved:
                    await self.instance_pool.cancel_delete(normalized)
                raise

        if operation.state in {
            CatalogOperationState.PENDING,
            CatalogOperationState.FAILED,
        }:
            self._schedule_delete_lifecycle(operation.operation_id)
        elif operation.state is CatalogOperationState.SUCCEEDED:
            self._deleting_ids.discard(normalized)
            if pool_reserved:
                await self.instance_pool.cancel_delete(normalized)
        return record, operation, created

    def _schedule_delete_lifecycle(self, operation_id: str) -> asyncio.Task[None]:
        existing = self._lifecycle_tasks.get(operation_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._run_delete_lifecycle(operation_id),
            name=f"workspace-delete:{operation_id}",
        )
        self._lifecycle_tasks[operation_id] = task

        def _discard(completed: asyncio.Task[None]) -> None:
            self._lifecycle_tasks.pop(operation_id, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "Workspace delete lifecycle task %s failed: %s",
                    operation_id,
                    exc,
                )

        task.add_done_callback(_discard)
        return task

    async def _run_delete_lifecycle(
        self, operation_id: str, reclaim_running: bool = False
    ) -> None:
        claim: CatalogOperation | None = None
        context: KnowledgeBaseContext | None = None
        record: KnowledgeBaseRecord | None = None
        workspace_id: str | None = None
        execution_scope_token = None
        try:
            pending_operation = await self.catalog_provider.get_operation(operation_id)
            workspace_id = pending_operation.workspace_id
            claim = await self.catalog_provider.claim_operation(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                reclaim_running=reclaim_running,
            )
            if claim.operation_type is not CatalogOperationType.DELETE:
                raise CatalogCASConflict(
                    f"Operation {operation_id!r} is not a workspace delete"
                )
            execution_scope_token = bind_workspace_execution_scope(
                claim.workspace_id,
                "recovery" if reclaim_running else "management",
            )
            record = await self.catalog_provider.get_record(
                claim.workspace_id, include_tombstoned=True
            )
            workspace_id = record.id
            record = await self.catalog_provider.transition_record(
                record.id,
                expected_revision=record.revision,
                expected_states=(
                    WorkspaceLifecycleState.DELETING,
                    WorkspaceLifecycleState.ERROR,
                ),
                target_state=WorkspaceLifecycleState.DELETING,
                operation_id=operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
            )
            async with self._manager_lock:
                context = self._contexts.get(record.id)
                if context is None:
                    profile = self._profile_for(record)
                    self.side_effect_counters.instance_constructions += 1
                    context = KnowledgeBaseContext(
                        metadata=record,
                        rag=self._rag_factory(record, profile),
                        document_manager=self._document_manager_factory(
                            record, profile
                        ),
                    )
                    self._contexts[record.id] = context

            if record.id not in self._initialized_ids:
                self.side_effect_counters.storage_initializations += 1
                await context.rag.initialize_storages()
                self._initialized_ids.add(record.id)

            validate_bindings = getattr(context.rag, "validate_storage_bindings", None)
            if callable(validate_bindings):
                try:
                    validate_bindings(stage="pre-delete")
                except ValueError as exc:
                    raise KnowledgeBaseConflictError(
                        f"Knowledge base {record.id!r} storage binding validation "
                        "failed; destructive cleanup was refused"
                    ) from exc

            completed_steps = [
                str(step)
                for step in claim.metadata.get("cleanup_completed", ())
                if isinstance(step, str)
            ]
            completed_set = set(completed_steps)

            async def checkpoint(step: str) -> None:
                if step not in completed_set:
                    completed_set.add(step)
                    completed_steps.append(step)
                await self.catalog_provider.update_operation_metadata(
                    operation_id,
                    owner_id=self._lifecycle_owner_id,
                    fencing_token=claim.fencing_token,
                    metadata={"cleanup_completed": completed_steps},
                )

            errors: list[str] = []
            for attribute in _STORAGE_ATTRIBUTES:
                cleanup_step = f"storage:{attribute}"
                if cleanup_step in completed_set:
                    continue
                storage = getattr(context.rag, attribute, None)
                drop = getattr(storage, "drop", None)
                if drop is None:
                    await checkpoint(cleanup_step)
                    continue
                try:
                    await drop()
                    await checkpoint(cleanup_step)
                except Exception as exc:
                    errors.append(f"{attribute}: {type(exc).__name__}")
            if errors:
                raise KnowledgeBaseError(
                    "Failed to drop all knowledge-base storages: " + "; ".join(errors)
                )

            await context.rag.finalize_storages()
            self._initialized_ids.discard(record.id)
            self._contexts.pop(record.id, None)
            input_dir = Path(context.document_manager.input_dir).resolve()
            base_input_dir = Path(context.document_manager.base_input_dir).resolve()
            if (
                "input_directory" not in completed_set
                and input_dir != base_input_dir
                and base_input_dir in input_dir.parents
            ):
                if input_dir.exists():
                    shutil.rmtree(input_dir)
                await checkpoint("input_directory")
            elif "input_directory" not in completed_set:
                await checkpoint("input_directory")

            await self.catalog_provider.update_operation_metadata(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
                metadata={
                    "cleanup_completed": completed_steps,
                    "cleanup_complete": True,
                },
            )

            tombstone = await self.catalog_provider.transition_record(
                record.id,
                expected_revision=record.revision,
                expected_states=(WorkspaceLifecycleState.DELETING,),
                target_state=WorkspaceLifecycleState.TOMBSTONED,
                operation_id=operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
            )
            await self.catalog_provider.finish_operation(
                operation_id,
                owner_id=self._lifecycle_owner_id,
                fencing_token=claim.fencing_token,
                state=CatalogOperationState.SUCCEEDED,
            )
            record = tombstone
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Workspace delete operation %s failed: %s", operation_id, exc)
            if claim is not None:
                try:
                    current = await self.catalog_provider.get_record(
                        claim.workspace_id, include_tombstoned=True
                    )
                    if (
                        current.lifecycle_state
                        == WorkspaceLifecycleState.DELETING.value
                    ):
                        await self.catalog_provider.transition_record(
                            current.id,
                            expected_revision=current.revision,
                            expected_states=(WorkspaceLifecycleState.DELETING,),
                            target_state=WorkspaceLifecycleState.ERROR,
                            operation_id=operation_id,
                            owner_id=self._lifecycle_owner_id,
                            fencing_token=claim.fencing_token,
                            error_code="workspace_deletion_failed",
                            error_message=type(exc).__name__,
                        )
                    await self.catalog_provider.finish_operation(
                        operation_id,
                        owner_id=self._lifecycle_owner_id,
                        fencing_token=claim.fencing_token,
                        state=CatalogOperationState.FAILED,
                        error_code="workspace_deletion_failed",
                        error_message=type(exc).__name__,
                    )
                except Exception as update_error:
                    logger.error(
                        "Failed to persist workspace delete %s failure: %s",
                        operation_id,
                        update_error,
                    )
        finally:
            if execution_scope_token is not None:
                reset_workspace_execution_scope(execution_scope_token)
            if context is not None and record is not None:
                if record.id in self._initialized_ids:
                    try:
                        await context.rag.finalize_storages()
                    except Exception as cleanup_error:
                        logger.error(
                            "Failed to finalize deleted workspace %s: %s",
                            record.id,
                            cleanup_error,
                        )
                    self._initialized_ids.discard(record.id)
                    self._contexts.pop(record.id, None)
            if workspace_id is not None:
                self._deleting_ids.discard(workspace_id)
                try:
                    if context is None:
                        await self.instance_pool.cancel_delete(workspace_id)
                    else:
                        await self.instance_pool.forget(workspace_id)
                except WorkspacePoolBusyError as pool_error:
                    logger.error(
                        "Failed to forget deleted workspace %s pool entry: %s",
                        workspace_id,
                        pool_error,
                    )

    async def delete(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        if not self.multi_workspace_enabled:
            raise KnowledgeBaseConflictError("Multi-workspace mode is disabled")
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if normalized == DEFAULT_KNOWLEDGE_BASE_ID:
            raise KnowledgeBaseConflictError(
                "The default knowledge base cannot be deleted"
            )
        record = await self.catalog_provider.get_record(
            normalized, include_tombstoned=True
        )
        context = await self.get_context(normalized)
        async with self._manager_lock:
            if normalized in self._deleting_ids:
                raise KnowledgeBaseConflictError(
                    f"Knowledge base {normalized!r} is already being deleted"
                )
            self._deleting_ids.add(normalized)
        try:
            if context.active_requests:
                raise KnowledgeBaseConflictError(
                    f"Knowledge base {normalized!r} has active requests"
                )
            pipeline_status = await get_namespace_data(
                "pipeline_status", workspace=record.effective_workspace
            )
            if pipeline_status and (
                pipeline_status.get("busy")
                or pipeline_status.get("scanning")
                or int(pipeline_status.get("pending_enqueues", 0) or 0) > 0
            ):
                raise KnowledgeBaseConflictError(
                    f"Knowledge base {normalized!r} has an active pipeline"
                )
            validate_bindings = getattr(context.rag, "validate_storage_bindings", None)
            if callable(validate_bindings):
                try:
                    validate_bindings(stage="pre-delete")
                except ValueError as exc:
                    raise KnowledgeBaseConflictError(
                        f"Knowledge base {normalized!r} storage binding validation "
                        "failed; destructive cleanup was refused"
                    ) from exc
            errors: list[str] = []
            for attribute in _STORAGE_ATTRIBUTES:
                storage = getattr(context.rag, attribute, None)
                drop = getattr(storage, "drop", None)
                if drop is None:
                    continue
                try:
                    await drop()
                except Exception as exc:
                    errors.append(f"{attribute}: {exc}")
            if errors:
                raise KnowledgeBaseError(
                    "Failed to drop all knowledge-base storages: " + "; ".join(errors)
                )
            await context.rag.finalize_storages()
            self._initialized_ids.discard(normalized)
            self._contexts.pop(normalized, None)

            input_dir = Path(context.document_manager.input_dir).resolve()
            base_input_dir = Path(context.document_manager.base_input_dir).resolve()
            if input_dir != base_input_dir and base_input_dir in input_dir.parents:
                if input_dir.exists():
                    shutil.rmtree(input_dir)
            return self.catalog.delete(normalized)
        finally:
            self._deleting_ids.discard(normalized)

    async def finalize(self) -> None:
        errors: list[BaseException] = []
        lifecycle_tasks = list(self._lifecycle_tasks.values())
        for task in lifecycle_tasks:
            task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        self._lifecycle_tasks.clear()
        errors.extend(await self.instance_pool.finalize_all())
        for knowledge_base_id, context in list(self._contexts.items()):
            if knowledge_base_id not in self._initialized_ids:
                continue
            try:
                await context.rag.finalize_storages()
            except BaseException as exc:
                errors.append(exc)
                logger.error(
                    "Failed to finalize knowledge base %s: %s",
                    knowledge_base_id,
                    exc,
                )
        self._initialized_ids.clear()
        self._started = False
        await self.catalog_provider.finalize()
        if errors:
            raise KnowledgeBaseError(
                f"Failed to finalize {len(errors)} knowledge-base instance(s)"
            ) from errors[0]


def storage_profiles_path_from_env() -> str | None:
    return os.getenv("LIGHTRAG_STORAGE_PROFILES_FILE")
