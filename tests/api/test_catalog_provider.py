"""Catalog provider lifecycle, CAS, idempotency, and fencing tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lightrag.api.catalog import (
    CatalogCASConflict,
    CatalogIdempotencyConflict,
    CatalogOperationState,
    LocalCatalogProvider,
    PostgresCatalogProvider,
    WorkspaceLifecycleState,
)
from lightrag.api.knowledge_bases import KnowledgeBaseCatalog, KnowledgeBaseRecord
from lightrag.workspace import NAMED_NAMESPACE_CODEC


pytestmark = pytest.mark.offline


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeAcquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ScriptedConnection:
    def __init__(self, fetchrow_results) -> None:
        self.fetchrow_results = list(fetchrow_results)
        self.statements: list[str] = []

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, statement, *args):
        self.statements.append(statement)
        return "OK"

    async def fetchrow(self, statement, *args):
        self.statements.append(statement)
        return self.fetchrow_results.pop(0)


class _FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self):
        return _FakeAcquire(self.connection)

    async def close(self):
        self.closed = True


def _named_record(workspace_id: str = "kb_123456789abc") -> KnowledgeBaseRecord:
    now = datetime.now(timezone.utc).isoformat()
    return KnowledgeBaseRecord(
        id=workspace_id,
        name="Catalog test",
        effective_workspace=workspace_id,
        isolation_level="logical",
        storage_profile_id=None,
        created_at=now,
        updated_at=now,
        workspace_kind="named",
        canonical_workspace_key=workspace_id,
        namespace_codec_version=NAMED_NAMESPACE_CODEC,
        lifecycle_state=WorkspaceLifecycleState.CREATING.value,
        revision=1,
    )


@pytest.mark.asyncio
async def test_local_provider_idempotency_revision_and_fencing(
    tmp_path: Path,
) -> None:
    catalog = KnowledgeBaseCatalog(tmp_path / "catalog.json", "")
    provider = LocalCatalogProvider(catalog)
    default_record = catalog.get("default")
    assert await provider.initialize(default_record) == default_record

    payload = {"name": "Catalog test", "isolation_level": "logical"}
    record, operation, created = await provider.create_workspace_operation(
        record=_named_record(),
        idempotency_key="create-1",
        payload=payload,
    )
    assert created is True
    assert record.lifecycle_state == WorkspaceLifecycleState.CREATING.value
    assert operation.state is CatalogOperationState.PENDING

    (
        replay_record,
        replay_operation,
        replay_created,
    ) = await provider.create_workspace_operation(
        record=_named_record("kb_different0000"),
        idempotency_key="create-1",
        payload=payload,
    )
    assert replay_created is False
    assert replay_record == record
    assert replay_operation == operation

    with pytest.raises(CatalogIdempotencyConflict):
        await provider.create_workspace_operation(
            record=_named_record("kb_different1111"),
            idempotency_key="create-1",
            payload={**payload, "name": "different"},
        )

    claim = await provider.claim_operation(operation.operation_id, owner_id="worker-a")
    assert claim.fencing_token > 0
    migrating = await provider.transition_record(
        record.id,
        expected_revision=record.revision,
        expected_states=(WorkspaceLifecycleState.CREATING,),
        target_state=WorkspaceLifecycleState.MIGRATING,
        operation_id=operation.operation_id,
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
    )
    assert migrating.revision == record.revision + 1

    with pytest.raises(CatalogCASConflict):
        await provider.transition_record(
            record.id,
            expected_revision=record.revision,
            expected_states=(WorkspaceLifecycleState.CREATING,),
            target_state=WorkspaceLifecycleState.ACTIVE,
            operation_id=operation.operation_id,
            owner_id="worker-a",
            fencing_token=claim.fencing_token,
        )

    active = await provider.transition_record(
        record.id,
        expected_revision=migrating.revision,
        expected_states=(WorkspaceLifecycleState.MIGRATING,),
        target_state=WorkspaceLifecycleState.ACTIVE,
        operation_id=operation.operation_id,
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
    )
    finished = await provider.finish_operation(
        operation.operation_id,
        owner_id="worker-a",
        fencing_token=claim.fencing_token,
        state=CatalogOperationState.SUCCEEDED,
    )
    assert active.lifecycle_state == WorkspaceLifecycleState.ACTIVE.value
    assert finished.state is CatalogOperationState.SUCCEEDED
    assert await provider.list_unfinished_operations() == ()

    deleting, delete_operation, delete_created = (
        await provider.create_delete_operation(
            workspace_id=active.id,
            idempotency_key="delete-1",
            payload={"workspace_id": active.id},
        )
    )
    assert delete_created is True
    assert deleting.lifecycle_state == WorkspaceLifecycleState.DELETING.value
    delete_claim = await provider.claim_operation(
        delete_operation.operation_id, owner_id="delete-worker"
    )
    deleting = await provider.transition_record(
        deleting.id,
        expected_revision=deleting.revision,
        expected_states=(WorkspaceLifecycleState.DELETING,),
        target_state=WorkspaceLifecycleState.DELETING,
        operation_id=delete_operation.operation_id,
        owner_id="delete-worker",
        fencing_token=delete_claim.fencing_token,
    )
    tombstone = await provider.transition_record(
        deleting.id,
        expected_revision=deleting.revision,
        expected_states=(WorkspaceLifecycleState.DELETING,),
        target_state=WorkspaceLifecycleState.TOMBSTONED,
        operation_id=delete_operation.operation_id,
        owner_id="delete-worker",
        fencing_token=delete_claim.fencing_token,
    )
    await provider.finish_operation(
        delete_operation.operation_id,
        owner_id="delete-worker",
        fencing_token=delete_claim.fencing_token,
        state=CatalogOperationState.SUCCEEDED,
    )
    assert tombstone.tombstoned_at is not None
    replay_record, replay_operation, replay_created = (
        await provider.create_delete_operation(
            workspace_id=active.id,
            idempotency_key="delete-1",
            payload={"workspace_id": active.id},
        )
    )
    assert replay_created is False
    assert replay_record.lifecycle_state == WorkspaceLifecycleState.TOMBSTONED.value
    assert replay_operation.state is CatalogOperationState.SUCCEEDED


@pytest.mark.asyncio
async def test_local_provider_rejects_stale_owner_after_retry(tmp_path: Path) -> None:
    catalog = KnowledgeBaseCatalog(tmp_path / "catalog.json", "")
    provider = LocalCatalogProvider(catalog)
    record, operation, _created = await provider.create_workspace_operation(
        record=_named_record(),
        idempotency_key=None,
        payload={"name": "Catalog test"},
    )
    first = await provider.claim_operation(operation.operation_id, owner_id="old")
    errored = await provider.transition_record(
        record.id,
        expected_revision=record.revision,
        expected_states=(WorkspaceLifecycleState.CREATING,),
        target_state=WorkspaceLifecycleState.ERROR,
        operation_id=operation.operation_id,
        owner_id="old",
        fencing_token=first.fencing_token,
        error_code="simulated",
    )
    await provider.finish_operation(
        operation.operation_id,
        owner_id="old",
        fencing_token=first.fencing_token,
        state=CatalogOperationState.FAILED,
    )

    second = await provider.claim_operation(operation.operation_id, owner_id="new")
    assert second.fencing_token > first.fencing_token
    assert second.retry_count == 1

    with pytest.raises(CatalogCASConflict):
        await provider.transition_record(
            record.id,
            expected_revision=errored.revision,
            expected_states=(WorkspaceLifecycleState.ERROR,),
            target_state=WorkspaceLifecycleState.MIGRATING,
            operation_id=operation.operation_id,
            owner_id="old",
            fencing_token=first.fencing_token,
        )

    recovered = await provider.transition_record(
        record.id,
        expected_revision=errored.revision,
        expected_states=(WorkspaceLifecycleState.ERROR,),
        target_state=WorkspaceLifecycleState.MIGRATING,
        operation_id=operation.operation_id,
        owner_id="new",
        fencing_token=second.fencing_token,
    )
    assert recovered.error_code is None


@pytest.mark.asyncio
async def test_local_provider_persists_operation_and_supports_pagination(
    tmp_path: Path,
) -> None:
    path = tmp_path / "catalog.json"
    provider = LocalCatalogProvider(KnowledgeBaseCatalog(path, ""))
    first, operation, _ = await provider.create_workspace_operation(
        record=_named_record("kb_111111111111"),
        idempotency_key="persisted",
        payload={"ordinal": 1},
    )
    await provider.create_workspace_operation(
        record=_named_record("kb_222222222222"),
        idempotency_key=None,
        payload={"ordinal": 2},
    )

    reloaded = LocalCatalogProvider(KnowledgeBaseCatalog(path, ""))
    assert await reloaded.get_operation(operation.operation_id) == operation
    page = await reloaded.list_records(limit=2)
    assert [record.id for record in page.records] == ["default", first.id]
    assert page.next_cursor == first.id
    second_page = await reloaded.list_records(limit=2, cursor=page.next_cursor)
    assert [record.id for record in second_page.records] == ["kb_222222222222"]


def test_postgres_catalog_configuration_requires_password() -> None:
    with pytest.raises(Exception, match="requires.*PASSWORD"):
        PostgresCatalogProvider.from_environment({})


@pytest.mark.asyncio
async def test_postgres_provider_bootstrap_and_mutations_emit_cas_guards() -> None:
    default = KnowledgeBaseRecord.legacy_default("")
    renamed = KnowledgeBaseRecord.from_dict(
        {**default.public_dict(), "name": "Renamed", "revision": 2}
    )
    connection = _ScriptedConnection(
        [default.public_dict(), renamed.public_dict(), renamed.public_dict()]
    )
    pool = _FakePool(connection)
    provider = PostgresCatalogProvider({})
    provider._pool = pool

    assert await provider.initialize(default) == default
    assert any("CREATE TABLE IF NOT EXISTS" in sql for sql in connection.statements)
    assert any("storage_profile_id" in sql for sql in connection.statements)

    assert (
        await provider.update_name(default.id, expected_revision=1, name="Renamed")
        == renamed
    )
    transitioned = await provider.transition_record(
        default.id,
        expected_revision=2,
        expected_states=(WorkspaceLifecycleState.ACTIVE,),
        target_state=WorkspaceLifecycleState.ERROR,
        operation_id="op_test",
        owner_id="owner",
        fencing_token=7,
        error_code="test",
    )
    assert transitioned == renamed
    mutation_sql = "\n".join(connection.statements)
    assert "c.revision = $2" in mutation_sql
    assert "o.owner_id = $6" in mutation_sql
    assert "o.fencing_token = $7" in mutation_sql
    assert "o.state = 'RUNNING'" in mutation_sql

    await provider.finalize()
    assert pool.closed is True
