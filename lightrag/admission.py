"""Service-scoped fair admission for provider calls and pipelines."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
import math
import os
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from lightrag.utils import logger
from lightrag.workspace_scope import current_workspace_execution_scope


class AdmissionRejectedError(RuntimeError):
    """A bounded service queue refused or timed out a work item."""

    def __init__(
        self,
        *,
        code: str,
        resource_group: str,
        retry_after: int = 1,
        status_code: int = 429,
    ) -> None:
        self.code = code
        self.resource_group = resource_group
        self.retry_after = retry_after
        self.status_code = status_code
        super().__init__(f"Service admission refused work for {resource_group!r}")


class AdmissionCoordinator(Protocol):
    """Deployment-scope slot provider used after local fair selection."""

    def is_limited(self, group: str) -> bool: ...

    async def try_acquire(self, group: str) -> tuple[str | None, bool]: ...

    async def release(self, group: str, lease_id: str) -> None: ...

    async def renew(self, group: str, lease_ids: tuple[str, ...]) -> None: ...


class SharedStorageAdmissionCoordinator:
    """Adapter over the existing single-host global-slot implementation."""

    def is_limited(self, group: str) -> bool:
        from lightrag.kg import shared_storage

        return shared_storage.is_global_concurrency_limited(group)

    async def try_acquire(self, group: str) -> tuple[str | None, bool]:
        from lightrag.kg import shared_storage

        return await shared_storage.try_acquire_global_slot_tracked(group)

    async def release(self, group: str, lease_id: str) -> None:
        from lightrag.kg import shared_storage

        await shared_storage.release_global_slot(group, lease_id)

    async def renew(self, group: str, lease_ids: tuple[str, ...]) -> None:
        from lightrag.kg import shared_storage

        await shared_storage.renew_global_slots(group, lease_ids)


@dataclass(slots=True)
class _AdmissionTicket:
    workspace_id: str
    operation_kind: str
    priority: int
    cost: int
    sequence: int
    enqueued_at: float
    future: asyncio.Future[None]
    granted: bool = False


@dataclass(slots=True)
class _AdmissionGroup:
    limit: int
    active: int = 0
    pending: int = 0
    queues: dict[str, list[_AdmissionTicket]] = field(default_factory=dict)
    workspace_order: deque[str] = field(default_factory=deque)
    deficits: dict[str, int] = field(default_factory=dict)
    admitted_total: int = 0
    completed_total: int = 0
    cancelled_total: int = 0
    rejected_total: int = 0
    peak_active: int = 0
    max_wait_seconds: float = 0.0


def build_service_admission_limits(args: Any) -> dict[str, int]:
    """Resolve deployment-total provider and pipeline limits from server args."""

    from lightrag.llm_roles import ROLES

    limits: dict[str, int] = {}
    for spec in ROLES:
        limit = getattr(args, f"{spec.name}_llm_max_async", None)
        if limit is None:
            limit = getattr(args, "max_async", None)
        if limit is not None and int(limit) > 0:
            limits[f"llm:{spec.name}"] = int(limit)
    embedding_limit = getattr(args, "embedding_func_max_async", None)
    if embedding_limit is not None and int(embedding_limit) > 0:
        limits["embedding"] = int(embedding_limit)
    rerank_limit = getattr(args, "rerank_max_async", None)
    if rerank_limit is not None and int(rerank_limit) > 0:
        limits["rerank"] = int(rerank_limit)
    pipeline_limit = int(os.getenv("LIGHTRAG_MAX_ACTIVE_PIPELINES", "2") or "2")
    if pipeline_limit < 1:
        raise ValueError("LIGHTRAG_MAX_ACTIVE_PIPELINES must be at least 1")
    limits["pipeline"] = pipeline_limit
    return limits


class ResourceAdmissionController:
    """One fair, bounded controller shared by every local RAG instance.

    Local selection uses deficit round-robin across workspace queues. The
    selected call then acquires an optional deployment-wide coordinator slot,
    preserving the existing Gunicorn heartbeat/reaping behavior without
    letting every instance mint its own concurrency budget.
    """

    def __init__(
        self,
        limits: Mapping[str, int],
        *,
        coordinator: AdmissionCoordinator | None = None,
        global_pending_limit: int = 1000,
        per_workspace_pending_limit: int = 100,
        aging_seconds: float = 5.0,
        quantum: int = 1,
        default_queue_timeout: float | None = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized = {str(key): int(value) for key, value in limits.items()}
        if not normalized or any(value < 1 for value in normalized.values()):
            raise ValueError("Every service admission limit must be positive")
        if global_pending_limit < 1 or per_workspace_pending_limit < 1:
            raise ValueError("Service admission pending limits must be positive")
        if aging_seconds <= 0 or quantum < 1:
            raise ValueError("Admission aging and quantum must be positive")
        if default_queue_timeout is not None and default_queue_timeout <= 0:
            raise ValueError("Admission queue timeout must be positive or None")
        self._groups = {
            group: _AdmissionGroup(limit=limit) for group, limit in normalized.items()
        }
        self._coordinator = coordinator or SharedStorageAdmissionCoordinator()
        self._global_pending_limit = global_pending_limit
        self._per_workspace_pending_limit = per_workspace_pending_limit
        self._aging_seconds = aging_seconds
        self._quantum = quantum
        self._default_queue_timeout = default_queue_timeout
        self._clock = clock
        self._lock = asyncio.Lock()
        self._sequence = 0

    @classmethod
    def from_environment(
        cls,
        limits: Mapping[str, int],
        *,
        coordinator: AdmissionCoordinator | None = None,
    ) -> "ResourceAdmissionController":
        timeout_raw = os.getenv("LIGHTRAG_ADMISSION_QUEUE_TIMEOUT", "30").strip()
        timeout = (
            None
            if timeout_raw.lower() in {"", "none", "null", "0"}
            else float(timeout_raw)
        )
        return cls(
            limits,
            coordinator=coordinator,
            global_pending_limit=int(
                os.getenv("LIGHTRAG_ADMISSION_GLOBAL_PENDING", "1000") or "1000"
            ),
            per_workspace_pending_limit=int(
                os.getenv("LIGHTRAG_ADMISSION_PER_WORKSPACE_PENDING", "100") or "100"
            ),
            aging_seconds=float(
                os.getenv("LIGHTRAG_ADMISSION_AGING_SECONDS", "5") or "5"
            ),
            quantum=int(os.getenv("LIGHTRAG_ADMISSION_DRR_QUANTUM", "1") or "1"),
            default_queue_timeout=timeout,
        )

    @staticmethod
    def priority_for(operation_kind: str) -> int:
        return {
            "query": 1,
            "management": 3,
            "ingestion": 5,
            "recovery": 7,
        }.get(operation_kind, 5)

    def _group(self, group: str) -> _AdmissionGroup:
        try:
            return self._groups[group]
        except KeyError as exc:
            raise ValueError(f"Unknown service admission group {group!r}") from exc

    def _effective_priority(self, ticket: _AdmissionTicket, now: float) -> int:
        age_bonus = int((now - ticket.enqueued_at) / self._aging_seconds)
        return ticket.priority - age_bonus

    def _remove_workspace(self, state: _AdmissionGroup, workspace_id: str) -> None:
        state.queues.pop(workspace_id, None)
        state.deficits.pop(workspace_id, None)
        try:
            state.workspace_order.remove(workspace_id)
        except ValueError:
            pass

    def _dispatch_locked(self, state: _AdmissionGroup) -> None:
        while state.active < state.limit and state.workspace_order:
            max_visits = max(1, len(state.workspace_order) * 65)
            granted = False
            for _ in range(max_visits):
                workspace_id = state.workspace_order[0]
                queue = state.queues.get(workspace_id, [])
                original_size = len(queue)
                queue[:] = [ticket for ticket in queue if not ticket.future.cancelled()]
                state.pending -= original_size - len(queue)
                if not queue:
                    self._remove_workspace(state, workspace_id)
                    if not state.workspace_order:
                        return
                    continue
                now = self._clock()
                ticket = min(
                    queue,
                    key=lambda item: (
                        self._effective_priority(item, now),
                        item.sequence,
                    ),
                )
                deficit = state.deficits.get(workspace_id, 0) + self._quantum
                state.deficits[workspace_id] = deficit
                state.workspace_order.rotate(-1)
                if deficit < ticket.cost:
                    continue
                queue.remove(ticket)
                state.deficits[workspace_id] = deficit - ticket.cost
                state.pending -= 1
                state.active += 1
                state.peak_active = max(state.peak_active, state.active)
                state.admitted_total += 1
                state.max_wait_seconds = max(
                    state.max_wait_seconds, now - ticket.enqueued_at
                )
                ticket.granted = True
                if not ticket.future.done():
                    ticket.future.set_result(None)
                if not queue:
                    self._remove_workspace(state, workspace_id)
                granted = True
                break
            if not granted:
                return

    async def _acquire_local(
        self,
        group: str,
        *,
        workspace_id: str,
        operation_kind: str,
        priority: int,
        cost_hint: float,
        queue_timeout: float | None,
    ) -> _AdmissionTicket:
        state = self._group(group)
        loop = asyncio.get_running_loop()
        ticket = _AdmissionTicket(
            workspace_id=workspace_id,
            operation_kind=operation_kind,
            priority=priority,
            cost=max(1, min(64, math.ceil(cost_hint))),
            sequence=0,
            enqueued_at=self._clock(),
            future=loop.create_future(),
        )
        async with self._lock:
            workspace_pending = len(state.queues.get(workspace_id, ()))
            if (
                state.pending >= self._global_pending_limit
                or workspace_pending >= self._per_workspace_pending_limit
            ):
                state.rejected_total += 1
                raise AdmissionRejectedError(
                    code="service_admission_queue_full",
                    resource_group=group,
                )
            self._sequence += 1
            ticket.sequence = self._sequence
            if workspace_id not in state.queues:
                state.queues[workspace_id] = []
                state.workspace_order.append(workspace_id)
                state.deficits[workspace_id] = 0
            state.queues[workspace_id].append(ticket)
            state.pending += 1
            self._dispatch_locked(state)

        timeout = (
            self._default_queue_timeout if queue_timeout is None else queue_timeout
        )
        try:
            if timeout is None:
                await asyncio.shield(ticket.future)
            else:
                await asyncio.wait_for(asyncio.shield(ticket.future), timeout=timeout)
            return ticket
        except BaseException as exc:
            async with self._lock:
                if ticket.granted:
                    state.active -= 1
                    state.cancelled_total += 1
                else:
                    queue = state.queues.get(workspace_id)
                    if queue is not None and ticket in queue:
                        queue.remove(ticket)
                        state.pending -= 1
                        if not queue:
                            self._remove_workspace(state, workspace_id)
                    state.cancelled_total += 1
                self._dispatch_locked(state)
            if isinstance(exc, asyncio.TimeoutError):
                state.rejected_total += 1
                raise AdmissionRejectedError(
                    code="service_admission_wait_timeout",
                    resource_group=group,
                    status_code=503,
                ) from exc
            raise

    async def _release_local(self, group: str) -> None:
        async with self._lock:
            state = self._group(group)
            if state.active < 1:
                raise RuntimeError(f"Admission group {group!r} released below zero")
            state.active -= 1
            state.completed_total += 1
            self._dispatch_locked(state)

    async def _acquire_coordinator_slot(
        self,
        group: str,
        *,
        deadline: float | None,
    ) -> str | None:
        if not self._coordinator.is_limited(group):
            return None
        delay = 0.005
        while True:
            lease_id, favored = await self._coordinator.try_acquire(group)
            if lease_id is not None:
                return lease_id
            if deadline is not None and self._clock() >= deadline:
                raise AdmissionRejectedError(
                    code="deployment_admission_wait_timeout",
                    resource_group=group,
                    status_code=503,
                )
            await asyncio.sleep(delay)
            delay = 0.005 if favored else min(0.1, delay * 1.5)

    async def _heartbeat(self, group: str, lease_id: str) -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                await self._coordinator.renew(group, (lease_id,))
            except Exception as exc:
                logger.warning(
                    "Service admission heartbeat failed for %s: %s",
                    group,
                    type(exc).__name__,
                )

    @asynccontextmanager
    async def admit(
        self,
        group: str,
        *,
        workspace_id: str,
        operation_kind: str,
        priority: int | None = None,
        cost_hint: float = 1,
        queue_timeout: float | None = None,
    ) -> AsyncIterator[None]:
        started_at = self._clock()
        ticket = await self._acquire_local(
            group,
            workspace_id=workspace_id,
            operation_kind=operation_kind,
            priority=(
                self.priority_for(operation_kind) if priority is None else priority
            ),
            cost_hint=cost_hint,
            queue_timeout=queue_timeout,
        )
        timeout = (
            self._default_queue_timeout if queue_timeout is None else queue_timeout
        )
        deadline = None if timeout is None else started_at + timeout
        lease_id: str | None = None
        heartbeat: asyncio.Task[None] | None = None
        try:
            lease_id = await self._acquire_coordinator_slot(group, deadline=deadline)
            if lease_id is not None:
                heartbeat = asyncio.create_task(
                    self._heartbeat(group, lease_id),
                    name=f"admission-heartbeat:{group}",
                )
            yield
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if lease_id is not None:
                try:
                    await self._coordinator.release(group, lease_id)
                except Exception as exc:
                    logger.warning(
                        "Service admission release failed for %s: %s",
                        group,
                        type(exc).__name__,
                    )
            if ticket.granted:
                await self._release_local(group)

    def wrap(
        self,
        func: Callable[..., Awaitable[Any]],
        *,
        group: str,
        workspace_id: str,
        default_operation_kind: str,
        cost_hint: Callable[[tuple[Any, ...], dict[str, Any]], float] | None = None,
    ) -> Callable[..., Awaitable[Any]]:
        """Wrap one raw provider function with service-level admission."""

        @wraps(func)
        async def admitted(*args: Any, **kwargs: Any) -> Any:
            scope = current_workspace_execution_scope()
            if scope is not None and scope.workspace_id != workspace_id:
                raise RuntimeError(
                    "Provider admission scope does not match the immutable "
                    "LightRAG workspace binding"
                )
            effective_workspace = scope.workspace_id if scope else workspace_id
            operation_kind = scope.operation_kind if scope else default_operation_kind
            cost = cost_hint(args, kwargs) if cost_hint is not None else 1
            async with self.admit(
                group,
                workspace_id=effective_workspace,
                operation_kind=operation_kind,
                cost_hint=cost,
            ):
                return await func(*args, **kwargs)

        return admitted

    async def snapshot(self) -> dict[str, Any]:
        """Return a bounded, side-effect-free service admission snapshot."""

        async with self._lock:
            return {
                "groups": {
                    group: {
                        "limit": state.limit,
                        "active": state.active,
                        "pending": state.pending,
                        "workspaces_pending": len(state.queues),
                        "peak_active": state.peak_active,
                        "admitted_total": state.admitted_total,
                        "completed_total": state.completed_total,
                        "cancelled_total": state.cancelled_total,
                        "rejected_total": state.rejected_total,
                        "max_wait_seconds": round(state.max_wait_seconds, 6),
                    }
                    for group, state in self._groups.items()
                },
                "global_pending_limit": self._global_pending_limit,
                "per_workspace_pending_limit": self._per_workspace_pending_limit,
                "aging_seconds": self._aging_seconds,
                "drr_quantum": self._quantum,
            }
