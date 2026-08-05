"""Durable workspace catalog provider and lifecycle primitives.

The catalog is the control-plane source of truth.  It deliberately contains no
``LightRAG`` instances or event-loop state, so the PostgreSQL implementation can
be shared by independent workers and, later, independent nodes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from typing import Any, AsyncIterator, Mapping, Sequence
from uuid import uuid4


CATALOG_SCHEMA_VERSION = 1


class CatalogError(RuntimeError):
    """Base error for catalog provider failures."""


class CatalogCASConflict(CatalogError):
    """A revision/state/fencing compare-and-swap did not match."""


class CatalogIdempotencyConflict(CatalogError):
    """An idempotency key was reused with a different request payload."""


class CatalogOperationNotFound(CatalogError):
    """A lifecycle operation does not exist."""


class WorkspaceLifecycleState(str, Enum):
    CREATING = "CREATING"
    MIGRATING = "MIGRATING"
    ACTIVE = "ACTIVE"
    DELETING = "DELETING"
    TOMBSTONED = "TOMBSTONED"
    ERROR = "ERROR"


class CatalogOperationType(str, Enum):
    CREATE = "CREATE"
    DELETE = "DELETE"
    MIGRATE = "MIGRATE"
    RECOVER = "RECOVER"


class CatalogOperationState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _postgres_timestamp(value: str | datetime) -> datetime:
    """Convert the catalog's JSON-safe timestamp into asyncpg's native type."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CatalogError(f"Invalid catalog timestamp {value!r}") from exc
    else:
        raise CatalogError(f"Invalid catalog timestamp type {type(value).__name__}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


@dataclass(frozen=True, slots=True)
class CatalogOperation:
    operation_id: str
    workspace_id: str
    operation_type: CatalogOperationType
    state: CatalogOperationState
    payload_hash: str
    idempotency_key: str | None
    owner_id: str | None
    fencing_token: int
    revision: int
    created_at: str
    updated_at: str
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CatalogOperation":
        metadata = value.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return cls(
            operation_id=str(value["operation_id"]),
            workspace_id=str(value["workspace_id"]),
            operation_type=CatalogOperationType(str(value["operation_type"])),
            state=CatalogOperationState(str(value["state"])),
            payload_hash=str(value["payload_hash"]),
            idempotency_key=(
                str(value["idempotency_key"]) if value.get("idempotency_key") else None
            ),
            owner_id=str(value["owner_id"]) if value.get("owner_id") else None,
            fencing_token=int(value.get("fencing_token", 0)),
            revision=int(value.get("revision", 1)),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            error_code=(str(value["error_code"]) if value.get("error_code") else None),
            error_message=(
                str(value["error_message"]) if value.get("error_message") else None
            ),
            retry_count=int(value.get("retry_count", 0)),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["operation_type"] = self.operation_type.value
        payload["state"] = self.state.value
        return payload


@dataclass(frozen=True, slots=True)
class CatalogPage:
    records: tuple[Any, ...]
    next_cursor: str | None


class CatalogProvider(ABC):
    """Async provider contract shared by local and PostgreSQL catalogs."""

    provider_kind: str
    shared: bool

    @abstractmethod
    async def initialize(self, default_record: Any) -> Any:
        """Create provider schema and idempotently bootstrap the default record."""

    @abstractmethod
    async def finalize(self) -> None:
        """Release provider resources."""

    @abstractmethod
    async def get_record(
        self, workspace_id: str, *, include_tombstoned: bool = False
    ) -> Any:
        """Read the latest durable record revision."""

    @abstractmethod
    async def list_records(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        states: Sequence[WorkspaceLifecycleState] | None = None,
    ) -> CatalogPage:
        """Return one deterministic page ordered by public workspace ID."""

    @abstractmethod
    async def update_name(
        self, workspace_id: str, *, expected_revision: int, name: str
    ) -> Any:
        """Revision-CAS a mutable display name without changing the binding."""

    @abstractmethod
    async def create_workspace_operation(
        self,
        *,
        record: Any,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        """Atomically create CREATING + PENDING, or replay an idempotent request."""

    @abstractmethod
    async def create_delete_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        """Atomically move ACTIVE to DELETING and persist a PENDING operation."""

    @abstractmethod
    async def create_migration_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        """Atomically move ACTIVE to MIGRATING for startup-owned migration."""

    @abstractmethod
    async def get_operation(self, operation_id: str) -> CatalogOperation:
        """Return the latest operation revision."""

    @abstractmethod
    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        reclaim_running: bool = False,
    ) -> CatalogOperation:
        """Claim work and allocate a monotonic fencing token.

        ``reclaim_running`` is reserved for the supported single-worker startup
        coordinator, where every previous owner is known to be dead.
        """

    @abstractmethod
    async def transition_record(
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
        storage_profile_fingerprint: str | None = None,
        storage_resource_fingerprints: Mapping[str, str] | None = None,
    ) -> Any:
        """Fenced record revision CAS."""

    @abstractmethod
    async def finish_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        state: CatalogOperationState,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> CatalogOperation:
        """Fenced terminal operation update."""

    @abstractmethod
    async def update_operation_metadata(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        metadata: Mapping[str, Any],
    ) -> CatalogOperation:
        """Fenced durable progress checkpoint for resumable lifecycle work."""

    @abstractmethod
    async def list_unfinished_operations(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[CatalogOperation, ...]:
        """List durable work ordered by operation ID after an optional cursor."""


class LocalCatalogProvider(CatalogProvider):
    """Single-worker adapter over the crash-safe local JSON catalog."""

    provider_kind = "local"
    shared = False

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog

    async def initialize(self, default_record: Any) -> Any:
        durable = self.catalog.get(default_record.id)
        if (
            durable.canonical_workspace_key != default_record.canonical_workspace_key
            or durable.effective_workspace != default_record.effective_workspace
        ):
            raise CatalogError(
                "Local default workspace binding differs from server configuration"
            )
        return durable

    async def finalize(self) -> None:
        return None

    def get_cached(self, workspace_id: str) -> Any:
        return self.catalog.get(workspace_id)

    def list_cached(self) -> list[Any]:
        return self.catalog.list()

    async def get_record(
        self, workspace_id: str, *, include_tombstoned: bool = False
    ) -> Any:
        record = self.catalog.get(workspace_id)
        if (
            not include_tombstoned
            and record.lifecycle_state == WorkspaceLifecycleState.TOMBSTONED.value
        ):
            from lightrag.api.knowledge_bases import KnowledgeBaseNotFoundError

            raise KnowledgeBaseNotFoundError(
                f"Knowledge base {workspace_id!r} does not exist"
            )
        return record

    async def list_records(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        states: Sequence[WorkspaceLifecycleState] | None = None,
    ) -> CatalogPage:
        if not 1 <= limit <= 1000:
            raise ValueError("Catalog page limit must be between 1 and 1000")
        state_values = {state.value for state in states} if states else None
        records = sorted(
            (
                record
                for record in self.catalog.list()
                if record.id > (cursor or "")
                and (state_values is None or record.lifecycle_state in state_values)
            ),
            key=lambda record: record.id,
        )
        page = records[:limit]
        next_cursor = page[-1].id if len(records) > limit and page else None
        return CatalogPage(tuple(page), next_cursor)

    async def update_name(
        self, workspace_id: str, *, expected_revision: int, name: str
    ) -> Any:
        current = self.catalog.get(workspace_id)
        if current.revision != expected_revision:
            raise CatalogCASConflict(
                f"Workspace {workspace_id!r} name update revision was stale"
            )
        return self.catalog.rename(workspace_id, name)

    async def create_workspace_operation(
        self,
        *,
        record: Any,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        return self.catalog.create_workspace_operation(
            record=record,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    async def create_delete_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        return self.catalog.create_delete_operation(
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    async def create_migration_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        return self.catalog.create_migration_operation(
            workspace_id=workspace_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    async def get_operation(self, operation_id: str) -> CatalogOperation:
        return self.catalog.get_operation(operation_id)

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        reclaim_running: bool = False,
    ) -> CatalogOperation:
        return self.catalog.claim_operation(
            operation_id,
            owner_id=owner_id,
            reclaim_running=reclaim_running,
        )

    async def transition_record(
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
        storage_profile_fingerprint: str | None = None,
        storage_resource_fingerprints: Mapping[str, str] | None = None,
    ) -> Any:
        return self.catalog.transition_record(
            workspace_id,
            expected_revision=expected_revision,
            expected_states=expected_states,
            target_state=target_state,
            operation_id=operation_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
            error_code=error_code,
            error_message=error_message,
            storage_profile_fingerprint=storage_profile_fingerprint,
            storage_resource_fingerprints=storage_resource_fingerprints,
        )

    async def finish_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        state: CatalogOperationState,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> CatalogOperation:
        return self.catalog.finish_operation(
            operation_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
            state=state,
            error_code=error_code,
            error_message=error_message,
        )

    async def update_operation_metadata(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        metadata: Mapping[str, Any],
    ) -> CatalogOperation:
        return self.catalog.update_operation_metadata(
            operation_id,
            owner_id=owner_id,
            fencing_token=fencing_token,
            metadata=metadata,
        )

    async def list_unfinished_operations(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[CatalogOperation, ...]:
        return self.catalog.list_unfinished_operations(limit=limit, cursor=cursor)


_CATALOG_TABLE = "lightrag_workspace_catalog"
_OPERATION_TABLE = "lightrag_workspace_operations"
_FENCE_SEQUENCE = "lightrag_workspace_fencing_seq"


_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS {_CATALOG_TABLE} (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        effective_workspace VARCHAR(64) NOT NULL UNIQUE,
        isolation_level VARCHAR(16) NOT NULL,
        storage_profile_id VARCHAR(128),
        storage_profile_fingerprint CHAR(64),
        storage_resource_fingerprints JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        workspace_kind VARCHAR(32) NOT NULL,
        canonical_workspace_key VARCHAR(128) NOT NULL UNIQUE,
        namespace_codec_version VARCHAR(32) NOT NULL,
        lifecycle_state VARCHAR(32) NOT NULL,
        revision BIGINT NOT NULL,
        schema_version INTEGER NOT NULL,
        current_operation_id VARCHAR(64),
        error_code VARCHAR(128),
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        tombstoned_at TIMESTAMPTZ
    )
    """,
    f"ALTER TABLE {_CATALOG_TABLE} ADD COLUMN IF NOT EXISTS "
    "storage_profile_fingerprint CHAR(64)",
    f"ALTER TABLE {_CATALOG_TABLE} ADD COLUMN IF NOT EXISTS "
    "storage_resource_fingerprints JSONB NOT NULL DEFAULT '{}'::jsonb",
    f"""
    CREATE TABLE IF NOT EXISTS {_OPERATION_TABLE} (
        operation_id VARCHAR(64) PRIMARY KEY,
        workspace_id VARCHAR(64) NOT NULL,
        operation_type VARCHAR(32) NOT NULL,
        state VARCHAR(32) NOT NULL,
        payload_hash CHAR(64) NOT NULL,
        idempotency_key VARCHAR(128) UNIQUE,
        owner_id VARCHAR(128),
        fencing_token BIGINT NOT NULL DEFAULT 0,
        revision BIGINT NOT NULL,
        retry_count INTEGER NOT NULL DEFAULT 0,
        error_code VARCHAR(128),
        error_message TEXT,
        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    f"CREATE SEQUENCE IF NOT EXISTS {_FENCE_SEQUENCE}",
    f"CREATE INDEX IF NOT EXISTS idx_{_CATALOG_TABLE}_state_id "
    f"ON {_CATALOG_TABLE}(lifecycle_state, id)",
    f"CREATE INDEX IF NOT EXISTS idx_{_OPERATION_TABLE}_state_updated "
    f"ON {_OPERATION_TABLE}(state, updated_at)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{_CATALOG_TABLE}_storage_profile "
    f"ON {_CATALOG_TABLE}(storage_profile_id) WHERE storage_profile_id IS NOT NULL",
)


class PostgresCatalogProvider(CatalogProvider):
    """Shared durable catalog backed by PostgreSQL and asyncpg."""

    provider_kind = "postgres"
    shared = True

    def __init__(self, connection_config: Mapping[str, Any]) -> None:
        self._connection_config = dict(connection_config)
        self._pool: Any = None
        self._cache: dict[str, Any] = {}

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "PostgresCatalogProvider":
        environ = os.environ if environment is None else environment

        def setting(name: str, fallback_name: str, default: str | None = None):
            return environ.get(name) or environ.get(fallback_name) or default

        config = {
            "host": setting(
                "LIGHTRAG_CATALOG_POSTGRES_HOST", "POSTGRES_HOST", "localhost"
            ),
            "port": int(
                setting("LIGHTRAG_CATALOG_POSTGRES_PORT", "POSTGRES_PORT", "5432")
                or "5432"
            ),
            "user": setting(
                "LIGHTRAG_CATALOG_POSTGRES_USER", "POSTGRES_USER", "postgres"
            ),
            "password": setting(
                "LIGHTRAG_CATALOG_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"
            ),
            "database": setting(
                "LIGHTRAG_CATALOG_POSTGRES_DATABASE",
                "POSTGRES_DATABASE",
                "postgres",
            ),
            "min_size": int(
                environ.get("LIGHTRAG_CATALOG_POSTGRES_MIN_CONNECTIONS", "1")
            ),
            "max_size": int(
                environ.get("LIGHTRAG_CATALOG_POSTGRES_MAX_CONNECTIONS", "10")
            ),
        }
        if not config["password"]:
            raise CatalogError(
                "PostgreSQL catalog requires LIGHTRAG_CATALOG_POSTGRES_PASSWORD "
                "or POSTGRES_PASSWORD"
            )
        return cls(config)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        if self._pool is None:
            raise CatalogError("PostgreSQL catalog provider is not initialized")
        async with self._pool.acquire() as connection:
            yield connection

    @staticmethod
    def _record_from_row(row: Mapping[str, Any]) -> Any:
        from lightrag.api.knowledge_bases import KnowledgeBaseRecord

        return KnowledgeBaseRecord.from_dict(dict(row))

    @staticmethod
    def _operation_from_row(row: Mapping[str, Any]) -> CatalogOperation:
        return CatalogOperation.from_dict(dict(row))

    async def initialize(self, default_record: Any) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover - dependency gate
                raise CatalogError(
                    "PostgreSQL catalog provider requires asyncpg"
                ) from exc
            self._pool = await asyncpg.create_pool(**self._connection_config)

        async with self._connection() as connection:
            async with connection.transaction():
                for statement in _SCHEMA_STATEMENTS:
                    await connection.execute(statement)
                await connection.execute(
                    f"""
                    INSERT INTO {_CATALOG_TABLE} (
                        id, name, effective_workspace, isolation_level,
                        storage_profile_id, storage_profile_fingerprint,
                        storage_resource_fingerprints, workspace_kind,
                        canonical_workspace_key, namespace_codec_version,
                        lifecycle_state, revision, schema_version,
                        current_operation_id, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10,
                        $11, $12, $13, $14, $15::timestamptz, $16::timestamptz
                    ) ON CONFLICT (id) DO NOTHING
                    """,
                    *_record_parameters(default_record),
                )
                row = await connection.fetchrow(
                    f"SELECT * FROM {_CATALOG_TABLE} WHERE id = $1",
                    default_record.id,
                )
        if row is None:
            raise CatalogError("Default workspace bootstrap did not produce a record")
        durable = self._record_from_row(row)
        if (
            durable.canonical_workspace_key != default_record.canonical_workspace_key
            or durable.effective_workspace != default_record.effective_workspace
            or durable.namespace_codec_version != default_record.namespace_codec_version
        ):
            raise CatalogError(
                "Durable default workspace binding differs from server configuration"
            )
        self._cache[durable.id] = durable
        return durable

    async def finalize(self) -> None:
        pool, self._pool = self._pool, None
        self._cache.clear()
        if pool is not None:
            await pool.close()

    def get_cached(self, workspace_id: str) -> Any:
        try:
            return self._cache[workspace_id]
        except KeyError as exc:
            raise CatalogError(f"Workspace {workspace_id!r} is not cached") from exc

    def list_cached(self) -> list[Any]:
        return sorted(self._cache.values(), key=lambda item: item.id)

    async def get_record(
        self, workspace_id: str, *, include_tombstoned: bool = False
    ) -> Any:
        predicate = "" if include_tombstoned else "AND lifecycle_state <> 'TOMBSTONED'"
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"SELECT * FROM {_CATALOG_TABLE} WHERE id = $1 {predicate}",
                workspace_id,
            )
        if row is None:
            from lightrag.api.knowledge_bases import KnowledgeBaseNotFoundError

            raise KnowledgeBaseNotFoundError(
                f"Knowledge base {workspace_id!r} does not exist"
            )
        record = self._record_from_row(row)
        self._cache[record.id] = record
        return record

    async def list_records(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        states: Sequence[WorkspaceLifecycleState] | None = None,
    ) -> CatalogPage:
        if not 1 <= limit <= 1000:
            raise ValueError("Catalog page limit must be between 1 and 1000")
        state_values = [state.value for state in states] if states else None
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"""
                SELECT * FROM {_CATALOG_TABLE}
                WHERE id > $1
                  AND ($2::text[] IS NULL OR lifecycle_state = ANY($2::text[]))
                ORDER BY id
                LIMIT $3
                """,
                cursor or "",
                state_values,
                limit + 1,
            )
        records = [self._record_from_row(row) for row in rows[:limit]]
        self._cache.update({record.id: record for record in records})
        next_cursor = records[-1].id if len(rows) > limit and records else None
        return CatalogPage(tuple(records), next_cursor)

    async def update_name(
        self, workspace_id: str, *, expected_revision: int, name: str
    ) -> Any:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE {_CATALOG_TABLE}
                SET name = $3, revision = revision + 1, updated_at = NOW()
                WHERE id = $1 AND revision = $2
                  AND lifecycle_state <> 'TOMBSTONED'
                RETURNING *
                """,
                workspace_id,
                expected_revision,
                name,
            )
        if row is None:
            raise CatalogCASConflict(
                f"Workspace {workspace_id!r} name update revision was stale"
            )
        record = self._record_from_row(row)
        self._cache[record.id] = record
        return record

    async def create_workspace_operation(
        self,
        *,
        record: Any,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        payload_hash = canonical_payload_hash(payload)
        operation_id = f"op_{uuid4().hex}"
        now = _postgres_timestamp(utc_now())
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO {_OPERATION_TABLE} (
                        operation_id, workspace_id, operation_type, state,
                        payload_hash, idempotency_key, owner_id,
                        fencing_token, revision, retry_count, metadata,
                        created_at, updated_at
                    ) VALUES ($1, $2, 'CREATE', 'PENDING', $3, $4, NULL,
                              0, 1, 0, $5::jsonb, $6::timestamptz, $6::timestamptz)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING *
                    """,
                    operation_id,
                    record.id,
                    payload_hash,
                    idempotency_key,
                    json.dumps(dict(payload)),
                    now,
                )
                created = row is not None
                if row is None:
                    row = await connection.fetchrow(
                        f"SELECT * FROM {_OPERATION_TABLE} WHERE idempotency_key = $1",
                        idempotency_key,
                    )
                    if row is None:
                        raise CatalogError("Idempotent create lost its operation row")
                    operation = self._operation_from_row(row)
                    if (
                        operation.operation_type is not CatalogOperationType.CREATE
                        or operation.payload_hash != payload_hash
                    ):
                        raise CatalogIdempotencyConflict(
                            "Idempotency key is already used by a different request"
                        )
                    durable_record = await connection.fetchrow(
                        f"SELECT * FROM {_CATALOG_TABLE} WHERE id = $1",
                        operation.workspace_id,
                    )
                    if durable_record is None:
                        raise CatalogError(
                            "Idempotent operation references a missing workspace"
                        )
                    parsed = self._record_from_row(durable_record)
                    self._cache[parsed.id] = parsed
                    return parsed, operation, False

                await connection.execute(
                    f"""
                    INSERT INTO {_CATALOG_TABLE} (
                        id, name, effective_workspace, isolation_level,
                        storage_profile_id, storage_profile_fingerprint,
                        storage_resource_fingerprints, workspace_kind,
                        canonical_workspace_key, namespace_codec_version,
                        lifecycle_state, revision, schema_version,
                        current_operation_id, created_at, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10,
                        'CREATING', 1, $11, $12,
                        $13::timestamptz, $14::timestamptz
                    )
                    """,
                    record.id,
                    record.name,
                    record.effective_workspace,
                    record.isolation_level,
                    record.storage_profile_id,
                    record.storage_profile_fingerprint,
                    json.dumps(dict(record.storage_resource_fingerprints)),
                    record.workspace_kind,
                    record.canonical_workspace_key,
                    record.namespace_codec_version,
                    record.schema_version,
                    operation_id,
                    _postgres_timestamp(record.created_at),
                    _postgres_timestamp(record.updated_at),
                )
        operation = self._operation_from_row(row)
        durable = await self.get_record(record.id, include_tombstoned=True)
        return durable, operation, created

    async def create_delete_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        payload_hash = canonical_payload_hash(payload)
        operation_id = f"op_{uuid4().hex}"
        now = _postgres_timestamp(utc_now())
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO {_OPERATION_TABLE} (
                        operation_id, workspace_id, operation_type, state,
                        payload_hash, idempotency_key, owner_id,
                        fencing_token, revision, retry_count, metadata,
                        created_at, updated_at
                    ) VALUES ($1, $2, 'DELETE', 'PENDING', $3, $4, NULL,
                              0, 1, 0, $5::jsonb, $6::timestamptz, $6::timestamptz)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING *
                    """,
                    operation_id,
                    workspace_id,
                    payload_hash,
                    idempotency_key,
                    json.dumps(dict(payload)),
                    now,
                )
                created = row is not None
                if row is None:
                    row = await connection.fetchrow(
                        f"SELECT * FROM {_OPERATION_TABLE} WHERE idempotency_key = $1",
                        idempotency_key,
                    )
                    if row is None:
                        raise CatalogError("Idempotent delete lost its operation row")
                    operation = self._operation_from_row(row)
                    if (
                        operation.operation_type is not CatalogOperationType.DELETE
                        or operation.payload_hash != payload_hash
                    ):
                        raise CatalogIdempotencyConflict(
                            "Idempotency key is already used by a different request"
                        )
                    durable_row = await connection.fetchrow(
                        f"SELECT * FROM {_CATALOG_TABLE} WHERE id = $1",
                        operation.workspace_id,
                    )
                    if durable_row is None:
                        raise CatalogError(
                            "Idempotent delete references a missing workspace"
                        )
                    durable = self._record_from_row(durable_row)
                    self._cache[durable.id] = durable
                    return durable, operation, False

                durable_row = await connection.fetchrow(
                    f"""
                    UPDATE {_CATALOG_TABLE}
                    SET lifecycle_state = 'DELETING', revision = revision + 1,
                        current_operation_id = $2, error_code = NULL,
                        error_message = NULL, updated_at = NOW()
                    WHERE id = $1 AND lifecycle_state = 'ACTIVE'
                    RETURNING *
                    """,
                    workspace_id,
                    operation_id,
                )
                if durable_row is None:
                    raise CatalogCASConflict(
                        f"Workspace {workspace_id!r} is not ACTIVE and cannot be deleted"
                    )
        operation = self._operation_from_row(row)
        durable = self._record_from_row(durable_row)
        self._cache[durable.id] = durable
        return durable, operation, created

    async def create_migration_operation(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[Any, CatalogOperation, bool]:
        payload_hash = canonical_payload_hash(payload)
        operation_id = f"op_{uuid4().hex}"
        now = _postgres_timestamp(utc_now())
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO {_OPERATION_TABLE} (
                        operation_id, workspace_id, operation_type, state,
                        payload_hash, idempotency_key, owner_id,
                        fencing_token, revision, retry_count, metadata,
                        created_at, updated_at
                    ) VALUES ($1, $2, 'MIGRATE', 'PENDING', $3, $4, NULL,
                              0, 1, 0, $5::jsonb, $6::timestamptz, $6::timestamptz)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING *
                    """,
                    operation_id,
                    workspace_id,
                    payload_hash,
                    idempotency_key,
                    json.dumps(dict(payload)),
                    now,
                )
                created = row is not None
                if row is None:
                    row = await connection.fetchrow(
                        f"SELECT * FROM {_OPERATION_TABLE} WHERE idempotency_key = $1",
                        idempotency_key,
                    )
                    if row is None:
                        raise CatalogError(
                            "Idempotent migration lost its operation row"
                        )
                    operation = self._operation_from_row(row)
                    if (
                        operation.operation_type is not CatalogOperationType.MIGRATE
                        or operation.payload_hash != payload_hash
                        or operation.workspace_id != workspace_id
                    ):
                        raise CatalogIdempotencyConflict(
                            "Idempotency key is already used by a different request"
                        )
                    durable_row = await connection.fetchrow(
                        f"SELECT * FROM {_CATALOG_TABLE} WHERE id = $1",
                        operation.workspace_id,
                    )
                    if durable_row is None:
                        raise CatalogError(
                            "Idempotent migration references a missing workspace"
                        )
                    durable = self._record_from_row(durable_row)
                    self._cache[durable.id] = durable
                    return durable, operation, False

                durable_row = await connection.fetchrow(
                    f"""
                    UPDATE {_CATALOG_TABLE}
                    SET lifecycle_state = 'MIGRATING', revision = revision + 1,
                        current_operation_id = $2, error_code = NULL,
                        error_message = NULL, updated_at = NOW()
                    WHERE id = $1 AND lifecycle_state = 'ACTIVE'
                    RETURNING *
                    """,
                    workspace_id,
                    operation_id,
                )
                if durable_row is None:
                    raise CatalogCASConflict(
                        f"Workspace {workspace_id!r} is not ACTIVE and cannot migrate"
                    )
        operation = self._operation_from_row(row)
        durable = self._record_from_row(durable_row)
        self._cache[durable.id] = durable
        return durable, operation, created

    async def get_operation(self, operation_id: str) -> CatalogOperation:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"SELECT * FROM {_OPERATION_TABLE} WHERE operation_id = $1",
                operation_id,
            )
        if row is None:
            raise CatalogOperationNotFound(
                f"Catalog operation {operation_id!r} does not exist"
            )
        return self._operation_from_row(row)

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        reclaim_running: bool = False,
    ) -> CatalogOperation:
        async with self._connection() as connection:
            async with connection.transaction():
                token = await connection.fetchval(
                    f"SELECT nextval('{_FENCE_SEQUENCE}')"
                )
                row = await connection.fetchrow(
                    f"""
                    UPDATE {_OPERATION_TABLE}
                    SET state = 'RUNNING', owner_id = $2, fencing_token = $3,
                        revision = revision + 1,
                        retry_count = retry_count + CASE
                            WHEN state IN ('FAILED', 'RUNNING') THEN 1 ELSE 0 END,
                        error_code = NULL, error_message = NULL, updated_at = NOW()
                    WHERE operation_id = $1
                      AND (
                          state IN ('PENDING', 'FAILED')
                          OR ($4::boolean AND state = 'RUNNING' AND owner_id <> $2)
                      )
                    RETURNING *
                    """,
                    operation_id,
                    owner_id,
                    token,
                    reclaim_running,
                )
        if row is None:
            raise CatalogCASConflict(
                f"Catalog operation {operation_id!r} is not claimable"
            )
        return self._operation_from_row(row)

    async def transition_record(
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
        storage_profile_fingerprint: str | None = None,
        storage_resource_fingerprints: Mapping[str, str] | None = None,
    ) -> Any:
        resource_fingerprints_json = (
            json.dumps(dict(storage_resource_fingerprints))
            if storage_resource_fingerprints
            else None
        )
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE {_CATALOG_TABLE} AS c
                SET lifecycle_state = $4::varchar(32), revision = revision + 1,
                    error_code = $8, error_message = $9, updated_at = NOW(),
                    storage_profile_fingerprint = COALESCE(
                        c.storage_profile_fingerprint, $10
                    ),
                    storage_resource_fingerprints = CASE
                        WHEN c.storage_resource_fingerprints = '{{}}'::jsonb
                        THEN COALESCE($11::jsonb, '{{}}'::jsonb)
                        ELSE c.storage_resource_fingerprints
                    END,
                    tombstoned_at = CASE
                        WHEN $4::varchar(32) = 'TOMBSTONED'
                        THEN NOW() ELSE tombstoned_at
                    END
                WHERE c.id = $1 AND c.revision = $2
                  AND c.lifecycle_state = ANY($3::text[])
                  AND c.current_operation_id = $5
                  AND EXISTS (
                      SELECT 1 FROM {_OPERATION_TABLE} AS o
                      WHERE o.operation_id = $5 AND o.owner_id = $6
                        AND o.fencing_token = $7 AND o.state = 'RUNNING'
                  )
                  AND (
                      $10::char(64) IS NULL
                      OR c.storage_profile_fingerprint IS NULL
                      OR c.storage_profile_fingerprint = $10
                  )
                  AND (
                      $11::jsonb IS NULL
                      OR c.storage_resource_fingerprints = '{{}}'::jsonb
                      OR c.storage_resource_fingerprints = $11::jsonb
                  )
                RETURNING c.*
                """,
                workspace_id,
                expected_revision,
                [state.value for state in expected_states],
                target_state.value,
                operation_id,
                owner_id,
                fencing_token,
                error_code,
                error_message,
                storage_profile_fingerprint,
                resource_fingerprints_json,
            )
        if row is None:
            raise CatalogCASConflict(
                f"Workspace {workspace_id!r} lifecycle CAS was rejected"
            )
        record = self._record_from_row(row)
        self._cache[record.id] = record
        return record

    async def finish_operation(
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
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE {_OPERATION_TABLE}
                SET state = $4, revision = revision + 1,
                    error_code = $5, error_message = $6, updated_at = NOW()
                WHERE operation_id = $1 AND owner_id = $2
                  AND fencing_token = $3 AND state = 'RUNNING'
                RETURNING *
                """,
                operation_id,
                owner_id,
                fencing_token,
                state.value,
                error_code,
                error_message,
            )
        if row is None:
            raise CatalogCASConflict(
                f"Catalog operation {operation_id!r} finish was fenced out"
            )
        return self._operation_from_row(row)

    async def update_operation_metadata(
        self,
        operation_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        metadata: Mapping[str, Any],
    ) -> CatalogOperation:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE {_OPERATION_TABLE}
                SET metadata = metadata || $4::jsonb,
                    revision = revision + 1, updated_at = NOW()
                WHERE operation_id = $1 AND owner_id = $2
                  AND fencing_token = $3 AND state = 'RUNNING'
                RETURNING *
                """,
                operation_id,
                owner_id,
                fencing_token,
                json.dumps(dict(metadata)),
            )
        if row is None:
            raise CatalogCASConflict(
                f"Catalog operation {operation_id!r} progress was fenced out"
            )
        return self._operation_from_row(row)

    async def list_unfinished_operations(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[CatalogOperation, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Operation page limit must be between 1 and 1000")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"""
                SELECT * FROM {_OPERATION_TABLE}
                WHERE state IN ('PENDING', 'RUNNING', 'FAILED')
                  AND ($2::text IS NULL OR operation_id > $2)
                ORDER BY operation_id
                LIMIT $1
                """,
                limit,
                cursor,
            )
        return tuple(self._operation_from_row(row) for row in rows)


def _record_parameters(record: Any) -> tuple[Any, ...]:
    return (
        record.id,
        record.name,
        record.effective_workspace,
        record.isolation_level,
        record.storage_profile_id,
        record.storage_profile_fingerprint,
        json.dumps(dict(record.storage_resource_fingerprints)),
        record.workspace_kind,
        record.canonical_workspace_key,
        record.namespace_codec_version,
        record.lifecycle_state,
        record.revision,
        record.schema_version,
        record.current_operation_id,
        _postgres_timestamp(record.created_at),
        _postgres_timestamp(record.updated_at),
    )
