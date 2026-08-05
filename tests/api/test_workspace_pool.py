"""Deterministic tests for the per-worker workspace lease pool."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from lightrag.api.knowledge_bases import KnowledgeBaseRecord
from lightrag.api.workspace_pool import (
    WorkspaceExecutionContext,
    WorkspaceInstancePool,
    WorkspacePoolBusyError,
    WorkspacePoolCapacityError,
    WorkspacePoolInitializationError,
)
from lightrag.workspace import NAMED_NAMESPACE_CODEC


pytestmark = pytest.mark.offline


def _record(workspace_id: str, revision: int = 1) -> KnowledgeBaseRecord:
    now = datetime.now(timezone.utc).isoformat()
    return KnowledgeBaseRecord(
        id=workspace_id,
        name=workspace_id,
        effective_workspace=workspace_id,
        isolation_level="logical",
        storage_profile_id=None,
        created_at=now,
        updated_at=now,
        workspace_kind="named",
        canonical_workspace_key=workspace_id,
        namespace_codec_version=NAMED_NAMESPACE_CODEC,
        lifecycle_state="ACTIVE",
        revision=revision,
    )


def _context(record: KnowledgeBaseRecord) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        metadata=record,
        binding=record.to_workspace_binding(),
        rag=SimpleNamespace(workspace=record.effective_workspace),
        document_manager=SimpleNamespace(workspace=record.effective_workspace),
    )


@pytest.mark.asyncio
async def test_pool_single_flight_constructs_once_for_concurrent_leases() -> None:
    construct_count = 0
    initialize_count = 0
    entered = asyncio.Event()
    release_initialize = asyncio.Event()

    async def construct(record):
        nonlocal construct_count
        construct_count += 1
        return _context(record)

    async def initialize(context):
        nonlocal initialize_count
        initialize_count += 1
        entered.set()
        await release_initialize.wait()

    async def finalize(context):
        return None

    async def can_evict(context):
        return True

    pool = WorkspaceInstancePool(
        construct=construct,
        initialize=initialize,
        finalize=finalize,
        can_evict=can_evict,
        weight_for=lambda record: 1,
        max_entries=2,
        max_weight=2,
    )
    record = _record("kb_singleflight")
    first_task = asyncio.create_task(pool.acquire(record))
    await entered.wait()
    second_task = asyncio.create_task(pool.acquire(record, kind="stream"))
    release_initialize.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert construct_count == 1
    assert initialize_count == 1
    assert first.context.rag is second.context.rag
    snapshot = pool.peek(record.id)["entries"][0]
    assert snapshot["foreground_leases"] == 1
    assert snapshot["stream_leases"] == 1
    await first.release()
    await first.release()
    await second.release()
    assert pool.peek(record.id)["entries"][0]["foreground_leases"] == 0


@pytest.mark.asyncio
async def test_pool_refuses_overcommit_then_evicts_idle_lru() -> None:
    finalized: list[str] = []

    async def construct(record):
        return _context(record)

    async def no_op(context):
        return None

    async def finalize(context):
        finalized.append(context.metadata.id)

    async def can_evict(context):
        return True

    pool = WorkspaceInstancePool(
        construct=construct,
        initialize=no_op,
        finalize=finalize,
        can_evict=can_evict,
        weight_for=lambda record: 1,
        max_entries=1,
        max_weight=1,
    )
    first = await pool.acquire(_record("kb_a"), kind="background")
    with pytest.raises(WorkspacePoolCapacityError, match="no idle victim"):
        await pool.acquire(_record("kb_b"))

    await first.release()
    second = await pool.acquire(_record("kb_b"))
    assert finalized == ["kb_a"]
    assert pool.peek()["loaded_entries"] == 1
    await second.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_kind", ["stream", "background"])
async def test_long_lived_lease_blocks_destructive_reservation(
    lease_kind: str,
) -> None:
    async def no_op(context):
        return None

    async def can_evict(context):
        return True

    pool = WorkspaceInstancePool(
        construct=lambda record: asyncio.sleep(0, result=_context(record)),
        initialize=no_op,
        finalize=no_op,
        can_evict=can_evict,
        weight_for=lambda record: 1,
        max_entries=1,
        max_weight=1,
    )
    record = _record(f"kb_{lease_kind}")
    lease = await pool.acquire(record, kind=lease_kind)

    with pytest.raises(WorkspacePoolBusyError, match="active leases"):
        await pool.reserve_delete(record.id)

    await lease.release()
    assert await pool.reserve_delete(record.id) is lease.context
    await pool.cancel_delete(record.id)


@pytest.mark.asyncio
async def test_weight_budget_refuses_physical_overcommit() -> None:
    async def no_op(context):
        return None

    async def can_evict(context):
        return True

    pool = WorkspaceInstancePool(
        construct=lambda record: asyncio.sleep(0, result=_context(record)),
        initialize=no_op,
        finalize=no_op,
        can_evict=can_evict,
        weight_for=lambda record: 2,
        max_entries=2,
        max_weight=2,
    )
    first = await pool.acquire(_record("kb_weight_a"))
    with pytest.raises(WorkspacePoolCapacityError, match="no idle victim"):
        await pool.acquire(_record("kb_weight_b"))
    await first.release()


@pytest.mark.asyncio
async def test_pool_failure_backoff_prevents_retry_storm() -> None:
    attempts = 0

    async def construct(record):
        nonlocal attempts
        attempts += 1
        return _context(record)

    async def initialize(context):
        raise RuntimeError("simulated")

    async def no_op(context):
        return None

    async def can_evict(context):
        return True

    pool = WorkspaceInstancePool(
        construct=construct,
        initialize=initialize,
        finalize=no_op,
        can_evict=can_evict,
        weight_for=lambda record: 1,
        max_entries=1,
        max_weight=1,
        failure_backoff_seconds=60,
        failure_backoff_max_seconds=60,
    )
    record = _record("kb_failure")
    with pytest.raises(WorkspacePoolInitializationError):
        await pool.acquire(record)
    with pytest.raises(WorkspacePoolInitializationError, match="backoff"):
        await pool.acquire(record)
    assert attempts == 1
    snapshot = pool.peek(record.id)["entries"][0]
    assert snapshot["state"] == "FAILED"
    assert snapshot["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_catalog_revision_refresh_does_not_rebuild_fixed_binding() -> None:
    construct_count = 0

    async def construct(record):
        nonlocal construct_count
        construct_count += 1
        return _context(record)

    async def no_op(context):
        return None

    async def can_evict(context):
        return True

    pool = WorkspaceInstancePool(
        construct=construct,
        initialize=no_op,
        finalize=no_op,
        can_evict=can_evict,
        weight_for=lambda record: 1,
        max_entries=1,
        max_weight=1,
    )
    original = _record("kb_revision")
    first = await pool.acquire(original)
    await first.release()
    renamed = replace(original, name="Renamed", revision=2)
    second = await pool.acquire(renamed)
    assert construct_count == 1
    assert second.context.metadata.name == "Renamed"
    assert second.context.binding.canonical_key == original.canonical_workspace_key
    assert second.context.binding.catalog_revision == 2
    await second.release()
