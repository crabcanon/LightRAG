"""Per-worker workspace instance pool with explicit, lease-owned contexts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import Enum
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal
from weakref import WeakValueDictionary

from lightrag.workspace import WorkspaceBinding

if TYPE_CHECKING:
    from lightrag.api.knowledge_bases import KnowledgeBaseRecord


LeaseKind = Literal["foreground", "stream", "background"]


class WorkspacePoolError(RuntimeError):
    """Base error for safe instance-pool admission failures."""


class WorkspacePoolCapacityError(WorkspacePoolError):
    """No safely evictable entry exists within the configured budget."""


class WorkspacePoolBusyError(WorkspacePoolError):
    """An entry has live leases or is in a non-admissible state."""


class WorkspacePoolInitializationError(WorkspacePoolError):
    """Construction is in failure backoff or failed for this acquisition."""


class WorkspaceContextMissingError(WorkspacePoolError):
    """Multi-workspace code accessed a request proxy without an explicit lease."""


class PoolEntryState(str, Enum):
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DRAINING = "DRAINING"
    FINALIZING = "FINALIZING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class WorkspaceExecutionContext:
    """Immutable authority carried by a foreground/stream/background lease."""

    metadata: KnowledgeBaseRecord
    binding: WorkspaceBinding
    rag: Any
    document_manager: Any


@dataclass(slots=True)
class _PoolEntry:
    workspace_id: str
    state: PoolEntryState
    context: WorkspaceExecutionContext | None
    weight: int
    pinned: bool
    foreground_leases: int = 0
    stream_leases: int = 0
    background_leases: int = 0
    last_used: float = 0.0
    failure_count: int = 0
    retry_after: float = 0.0
    error_type: str | None = None

    @property
    def total_leases(self) -> int:
        return self.foreground_leases + self.stream_leases + self.background_leases

    def public_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "state": self.state.value,
            "weight": self.weight,
            "pinned": self.pinned,
            "foreground_leases": self.foreground_leases,
            "stream_leases": self.stream_leases,
            "background_leases": self.background_leases,
            "last_used": self.last_used,
            "failure_count": self.failure_count,
            "retry_after_seconds": max(0.0, self.retry_after - time.monotonic()),
            "error_type": self.error_type,
            "catalog_revision": (
                self.context.metadata.revision if self.context is not None else None
            ),
        }


class WorkspaceLease:
    """Exactly-once release token for one execution context."""

    __slots__ = ("context", "kind", "_pool", "_released")

    def __init__(
        self,
        pool: "WorkspaceInstancePool",
        context: WorkspaceExecutionContext,
        kind: LeaseKind,
    ) -> None:
        self.context = context
        self.kind = kind
        self._pool = pool
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._pool.release(self.context.metadata.id, self.kind)

    async def __aenter__(self) -> WorkspaceExecutionContext:
        return self.context

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


ConstructContext = Callable[[Any], Awaitable[WorkspaceExecutionContext]]
InitializeContext = Callable[[WorkspaceExecutionContext], Awaitable[None]]
FinalizeContext = Callable[[WorkspaceExecutionContext], Awaitable[None]]
CanEvictContext = Callable[[WorkspaceExecutionContext], Awaitable[bool]]
WeightForRecord = Callable[[Any], int]


class WorkspaceInstancePool:
    """Bounded local pool; catalog/coordinator state remains outside the pool."""

    def __init__(
        self,
        *,
        construct: ConstructContext,
        initialize: InitializeContext,
        finalize: FinalizeContext,
        can_evict: CanEvictContext,
        weight_for: WeightForRecord,
        max_entries: int,
        max_weight: int,
        failure_backoff_seconds: float = 1.0,
        failure_backoff_max_seconds: float = 30.0,
    ) -> None:
        if max_entries < 1 or max_weight < 1:
            raise ValueError("Workspace pool capacity and weight must be positive")
        self._construct = construct
        self._initialize = initialize
        self._finalize = finalize
        self._can_evict = can_evict
        self._weight_for = weight_for
        self.max_entries = max_entries
        self.max_weight = max_weight
        self.failure_backoff_seconds = max(0.0, failure_backoff_seconds)
        self.failure_backoff_max_seconds = max(
            self.failure_backoff_seconds, failure_backoff_max_seconds
        )
        self._entries: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        # Idle keys disappear automatically, preventing unknown-workspace or
        # high-cardinality traffic from growing a permanent lock registry.
        self._key_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @staticmethod
    def _binding_signature(record: KnowledgeBaseRecord) -> tuple[Any, ...]:
        binding = record.to_workspace_binding()
        return (
            binding.public_id,
            binding.kind,
            binding.canonical_key,
            binding.codec_version,
            binding.physical_workspace,
            binding.storage_profile_id,
            binding.server_mode,
        )

    @staticmethod
    def _increment(entry: _PoolEntry, kind: LeaseKind) -> None:
        if kind == "foreground":
            entry.foreground_leases += 1
        elif kind == "stream":
            entry.stream_leases += 1
        elif kind == "background":
            entry.background_leases += 1
        else:  # pragma: no cover - Literal callers plus defensive runtime gate
            raise ValueError(f"Unknown workspace lease kind: {kind!r}")

    def add_ready(
        self,
        context: WorkspaceExecutionContext,
        *,
        pinned: bool,
        weight: int | None = None,
    ) -> None:
        """Register a context before concurrent serving begins."""

        resolved_weight = weight or self._weight_for(context.metadata)
        if resolved_weight < 1 or resolved_weight > self.max_weight:
            raise WorkspacePoolCapacityError(
                f"Workspace {context.metadata.id!r} weight exceeds pool budget"
            )
        self._entries[context.metadata.id] = _PoolEntry(
            workspace_id=context.metadata.id,
            state=PoolEntryState.READY,
            context=context,
            weight=resolved_weight,
            pinned=pinned,
            last_used=time.monotonic(),
        )

    async def acquire(
        self, record: KnowledgeBaseRecord, *, kind: LeaseKind = "foreground"
    ) -> WorkspaceLease:
        key_lock = self._key_locks.setdefault(record.id, asyncio.Lock())
        async with key_lock:
            while True:
                victim = await self._reserve(record, kind=kind)
                if isinstance(victim, WorkspaceLease):
                    return victim
                if victim is not None:
                    await self._finalize_victim(victim)
                    continue
                break

            context: WorkspaceExecutionContext | None = None
            try:
                context = await self._construct(record)
                await self._initialize(context)
            except BaseException as exc:
                if context is not None:
                    try:
                        await self._finalize(context)
                    except BaseException:
                        async with self._lock:
                            entry = self._entries.get(record.id)
                            if entry is not None:
                                entry.context = context
                await self._record_failure(record.id, exc)
                raise WorkspacePoolInitializationError(
                    f"Workspace {record.id!r} initialization failed"
                ) from exc

            async with self._lock:
                entry = self._entries.get(record.id)
                if entry is None or entry.state is not PoolEntryState.INITIALIZING:
                    # A destructive reservation won the race while construction
                    # ran. Never publish the newly initialized context.
                    publish = False
                else:
                    entry.context = context
                    entry.state = PoolEntryState.READY
                    entry.last_used = time.monotonic()
                    entry.error_type = None
                    self._increment(entry, kind)
                    publish = True
            if not publish:
                await self._finalize(context)
                raise WorkspacePoolBusyError(
                    f"Workspace {record.id!r} stopped accepting leases"
                )
            return WorkspaceLease(self, context, kind)

    async def _reserve(
        self, record: KnowledgeBaseRecord, *, kind: LeaseKind
    ) -> WorkspaceLease | WorkspaceExecutionContext | None:
        now = time.monotonic()
        async with self._lock:
            prior_failure_count = 0
            entry = self._entries.get(record.id)
            if entry is not None:
                if entry.state is PoolEntryState.READY and entry.context is not None:
                    if self._binding_signature(entry.context.metadata) != (
                        self._binding_signature(record)
                    ):
                        if entry.total_leases or entry.pinned:
                            raise WorkspacePoolBusyError(
                                f"Workspace {record.id!r} has a stale leased binding"
                            )
                        entry.state = PoolEntryState.DRAINING
                        return entry.context
                    if entry.context.metadata.revision != record.revision:
                        entry.context = replace(
                            entry.context,
                            metadata=record,
                            binding=record.to_workspace_binding(),
                        )
                    self._increment(entry, kind)
                    entry.last_used = now
                    return WorkspaceLease(self, entry.context, kind)
                if entry.state is PoolEntryState.FAILED:
                    if now < entry.retry_after:
                        raise WorkspacePoolInitializationError(
                            f"Workspace {record.id!r} initialization is in backoff"
                        )
                    if entry.context is None:
                        prior_failure_count = entry.failure_count
                        self._entries.pop(record.id, None)
                    else:
                        raise WorkspacePoolBusyError(
                            f"Workspace {record.id!r} is quarantined after cleanup failure"
                        )
                else:
                    raise WorkspacePoolBusyError(
                        f"Workspace {record.id!r} pool entry is {entry.state.value}"
                    )

            weight = self._weight_for(record)
            if weight < 1 or weight > self.max_weight:
                raise WorkspacePoolCapacityError(
                    f"Workspace {record.id!r} weight exceeds pool budget"
                )
            current_count = sum(
                1
                for item in self._entries.values()
                if item.state is not PoolEntryState.FAILED or item.context is not None
            )
            current_weight = sum(
                item.weight
                for item in self._entries.values()
                if item.state is not PoolEntryState.FAILED or item.context is not None
            )
            if current_count + 1 > self.max_entries or (
                current_weight + weight > self.max_weight
            ):
                candidates = sorted(
                    (
                        item
                        for item in self._entries.values()
                        if item.state is PoolEntryState.READY
                        and item.context is not None
                        and not item.pinned
                        and item.total_leases == 0
                    ),
                    key=lambda item: (item.last_used, item.workspace_id),
                )
                for candidate in candidates:
                    # The callback is async, so selection is confirmed outside
                    # the lock and the entry remains READY until then.
                    candidate.state = PoolEntryState.DRAINING
                    return candidate.context
                raise WorkspacePoolCapacityError(
                    "Workspace instance pool is full and has no idle victim"
                )
            self._entries[record.id] = _PoolEntry(
                workspace_id=record.id,
                state=PoolEntryState.INITIALIZING,
                context=None,
                weight=weight,
                pinned=False,
                last_used=now,
                failure_count=prior_failure_count,
            )
            return None

    async def _finalize_victim(self, context: WorkspaceExecutionContext) -> None:
        workspace_id = context.metadata.id
        can_evict = False
        try:
            can_evict = await self._can_evict(context)
            if not can_evict:
                raise WorkspacePoolCapacityError(
                    f"Workspace {workspace_id!r} has non-lease work and cannot evict"
                )
            async with self._lock:
                entry = self._entries.get(workspace_id)
                if entry is None or entry.state is not PoolEntryState.DRAINING:
                    raise WorkspacePoolBusyError(
                        f"Workspace {workspace_id!r} eviction reservation was lost"
                    )
                entry.state = PoolEntryState.FINALIZING
            await self._finalize(context)
        except BaseException as exc:
            async with self._lock:
                entry = self._entries.get(workspace_id)
                if entry is not None:
                    if can_evict:
                        entry.state = PoolEntryState.FAILED
                        entry.error_type = type(exc).__name__
                    else:
                        entry.state = PoolEntryState.READY
            raise
        async with self._lock:
            self._entries.pop(workspace_id, None)

    async def _record_failure(self, workspace_id: str, error: BaseException) -> None:
        context: WorkspaceExecutionContext | None = None
        cleanup_failed = False
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return
            context = entry.context
        if context is not None:
            try:
                await self._finalize(context)
            except BaseException:
                cleanup_failed = True
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return
            entry.state = PoolEntryState.FAILED
            entry.failure_count += 1
            delay = min(
                self.failure_backoff_max_seconds,
                self.failure_backoff_seconds * (2 ** (entry.failure_count - 1)),
            )
            entry.retry_after = time.monotonic() + delay
            entry.error_type = type(error).__name__
            if not cleanup_failed:
                entry.context = None
                entry.weight = 0

    async def release(self, workspace_id: str, kind: LeaseKind) -> None:
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return
            attribute = f"{kind}_leases"
            current = getattr(entry, attribute)
            if current <= 0:
                raise WorkspacePoolError(
                    f"Workspace {workspace_id!r} lease underflow for {kind}"
                )
            setattr(entry, attribute, current - 1)
            entry.last_used = time.monotonic()

    async def reserve_delete(
        self, workspace_id: str
    ) -> WorkspaceExecutionContext | None:
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return None
            if entry.state is not PoolEntryState.READY or entry.total_leases:
                raise WorkspacePoolBusyError(
                    f"Workspace {workspace_id!r} has active leases or pool work"
                )
            entry.state = PoolEntryState.DRAINING
            return entry.context

    async def cancel_delete(self, workspace_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None and entry.state is PoolEntryState.DRAINING:
                entry.state = PoolEntryState.READY

    async def forget(self, workspace_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None and entry.total_leases:
                raise WorkspacePoolBusyError(
                    f"Workspace {workspace_id!r} still has active leases"
                )
            self._entries.pop(workspace_id, None)

    def peek(self, workspace_id: str | None = None) -> dict[str, Any]:
        entries = self._entries.values()
        if workspace_id is not None:
            entries = (entry for entry in entries if entry.workspace_id == workspace_id)
        snapshots = sorted(
            (entry.public_dict() for entry in entries),
            key=lambda item: item["workspace_id"],
        )
        return {
            "max_entries": self.max_entries,
            "max_weight": self.max_weight,
            "loaded_entries": sum(
                1 for entry in self._entries.values() if entry.context is not None
            ),
            "reserved_weight": sum(entry.weight for entry in self._entries.values()),
            "entries": snapshots,
        }

    async def finalize_all(self) -> list[BaseException]:
        async with self._lock:
            contexts = [
                entry.context
                for entry in self._entries.values()
                if entry.context is not None
            ]
            self._entries.clear()
        errors: list[BaseException] = []
        for context in contexts:
            try:
                await self._finalize(context)
            except BaseException as exc:
                errors.append(exc)
        return errors
