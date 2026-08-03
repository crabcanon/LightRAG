"""Knowledge-base catalog, lifecycle management, and request routing.

The API historically captured one ``LightRAG`` and one ``DocumentManager`` in
all route closures.  This module keeps that public route surface compatible
while introducing a request-scoped context that selects an isolated pair.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Annotated, Any, AsyncIterator, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from fastapi import Header, HTTPException, Request

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
        self.to_workspace_binding().validate()

    def to_workspace_binding(self, *, server_mode: str = "multi") -> WorkspaceBinding:
        return WorkspaceBinding(
            public_id=self.id,
            kind=WorkspaceKind(self.workspace_kind),
            canonical_key=self.canonical_workspace_key,
            codec_version=NamespaceCodec(self.namespace_codec_version),
            physical_workspace=self.effective_workspace,
            storage_profile_id=self.storage_profile_id,
            catalog_revision=0,
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
        self._load_or_create()

    def _default_record(self) -> KnowledgeBaseRecord:
        now = _utc_now()
        return KnowledgeBaseRecord(
            id=DEFAULT_KNOWLEDGE_BASE_ID,
            name="Default",
            effective_workspace=self.default_workspace,
            isolation_level="logical",
            storage_profile_id=None,
            created_at=now,
            updated_at=now,
            workspace_kind="legacy_default",
            canonical_workspace_key=LEGACY_DEFAULT_CANONICAL_KEY,
            namespace_codec_version=LEGACY_NAMESPACE_CODEC,
        )

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

    def _persist_locked(self) -> None:
        payload = {
            "version": CATALOG_VERSION,
            "knowledge_bases": [
                record.public_dict()
                for record in sorted(self._records.values(), key=lambda item: item.id)
            ],
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


def validate_knowledge_base_id(value: str | None) -> str:
    normalized = (value or DEFAULT_KNOWLEDGE_BASE_ID).strip()
    if not normalized:
        return DEFAULT_KNOWLEDGE_BASE_ID
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


_current_context: ContextVar[KnowledgeBaseContext | None] = ContextVar(
    "lightrag_knowledge_base_context", default=None
)


class RequestScopedProxy:
    """Forward attribute access to the active request's RAG or doc manager."""

    def __init__(self, manager: "KnowledgeBaseManager", attribute: str) -> None:
        object.__setattr__(self, "_manager", manager)
        object.__setattr__(self, "_attribute", attribute)

    def _target(self) -> Any:
        context = _current_context.get() or self._manager.default_context
        return getattr(context, self._attribute)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target(), name, value)


class KnowledgeBaseManager:
    """Own isolated LightRAG instances and bind them to API requests."""

    def __init__(
        self,
        *,
        catalog: KnowledgeBaseCatalog,
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
        multi_workspace_enabled: bool = True,
    ) -> None:
        if max_loaded_instances < 1:
            raise ValueError("max_loaded_instances must be at least 1")
        self.catalog = catalog
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
        default_record = catalog.get(DEFAULT_KNOWLEDGE_BASE_ID)
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
        self._started = False
        self.side_effect_counters = KnowledgeBaseSideEffectCounters()
        self.rag_proxy = RequestScopedProxy(self, "rag")
        self.document_manager_proxy = RequestScopedProxy(self, "document_manager")

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
        records = self.catalog.list()
        if self.multi_workspace_enabled:
            return records
        return [record for record in records if record.id == DEFAULT_KNOWLEDGE_BASE_ID]

    def get_record(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if not self.multi_workspace_enabled and normalized != DEFAULT_KNOWLEDGE_BASE_ID:
            self.catalog.get(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        return self.catalog.get(normalized)

    def rename(self, knowledge_base_id: str, name: str) -> KnowledgeBaseRecord:
        current = self.get_record(knowledge_base_id)
        return self.catalog.rename(current.id, name)

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
        for record in self.catalog.list():
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
        for record in self.catalog.list():
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
        try:
            await self._initialize_context(self.default_context)
        except BaseException:
            self._started = False
            raise

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
            self.catalog.get(normalized)
            raise KnowledgeBaseNotFoundError(
                "Multi-workspace mode is disabled; only the default knowledge "
                "base is available"
            )
        if normalized in self._deleting_ids:
            raise KnowledgeBaseConflictError(
                f"Knowledge base {normalized!r} is being deleted"
            )
        existing = self._contexts.get(normalized)
        if existing is not None:
            if self._started:
                await self._initialize_context(existing)
            return existing

        record = self.catalog.get(normalized)
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
            await self._initialize_context(context)
        return context

    @asynccontextmanager
    async def bind_request(
        self, knowledge_base_id: str | None
    ) -> AsyncIterator[KnowledgeBaseContext]:
        context = await self.get_context(knowledge_base_id)
        context.active_requests += 1
        token = _current_context.set(context)
        try:
            yield context
        finally:
            _current_context.reset(token)
            context.active_requests = max(0, context.active_requests - 1)

    async def request_dependency(
        self,
        request: Request,
        knowledge_base_id: KnowledgeBaseHeader = None,
    ) -> AsyncIterator[None]:
        try:
            async with self.bind_request(knowledge_base_id) as context:
                request.state.knowledge_base = context.metadata.public_dict()
                yield
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KnowledgeBaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KnowledgeBaseConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    async def delete(self, knowledge_base_id: str) -> KnowledgeBaseRecord:
        if not self.multi_workspace_enabled:
            raise KnowledgeBaseConflictError("Multi-workspace mode is disabled")
        normalized = validate_knowledge_base_id(knowledge_base_id)
        if normalized == DEFAULT_KNOWLEDGE_BASE_ID:
            raise KnowledgeBaseConflictError(
                "The default knowledge base cannot be deleted"
            )
        record = self.catalog.get(normalized)
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
        if errors:
            raise KnowledgeBaseError(
                f"Failed to finalize {len(errors)} knowledge-base instance(s)"
            ) from errors[0]


def storage_profiles_path_from_env() -> str | None:
    return os.getenv("LIGHTRAG_STORAGE_PROFILES_FILE")
