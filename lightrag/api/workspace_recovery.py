"""Catalog-driven startup migration and lifecycle recovery coordination."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from lightrag.api.catalog import (
    CATALOG_SCHEMA_VERSION,
    CatalogOperation,
    CatalogOperationState,
    CatalogOperationType,
    CatalogProvider,
    WorkspaceLifecycleState,
)
from lightrag.utils import logger


RunOperation = Callable[[str, bool], Awaitable[None]]


@dataclass(slots=True)
class WorkspaceRecoveryReport:
    """Bounded, secret-free startup recovery result."""

    resumed_operations: int = 0
    migrations_started: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def record_failure(self, workspace_id: str, error: BaseException) -> None:
        self.failed += 1
        if len(self.failures) < 100:
            self.failures.append(
                {
                    "workspace_id": workspace_id,
                    "error_type": type(error).__name__,
                }
            )

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceRecoveryCoordinator:
    """Single-worker startup owner for durable lifecycle and migrations.

    This coordinator runs before the server accepts requests. Therefore a
    RUNNING operation belongs to a process that is known to be dead and may be
    reclaimed with a fresh fencing token. Multi-worker mode must replace this
    assumption with the external/same-host coordinator provider in Phase 5.
    """

    def __init__(
        self,
        *,
        catalog: CatalogProvider,
        run_create: RunOperation,
        run_migrate: RunOperation,
        run_delete: RunOperation,
        page_size: int = 100,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("Recovery page size must be between 1 and 1000")
        self.catalog = catalog
        self.run_create = run_create
        self.run_migrate = run_migrate
        self.run_delete = run_delete
        self.page_size = page_size
        self.run_id = uuid4().hex
        self.last_report = WorkspaceRecoveryReport()

    async def recover(self) -> WorkspaceRecoveryReport:
        report = WorkspaceRecoveryReport()
        handled_workspaces: set[str] = set()
        operation_cursor: str | None = None
        while True:
            unfinished = await self.catalog.list_unfinished_operations(
                limit=self.page_size,
                cursor=operation_cursor,
            )
            for operation in unfinished:
                handled_workspaces.add(operation.workspace_id)
                report.resumed_operations += 1
                await self._run_one(operation, report, reclaim_running=True)
            if len(unfinished) < self.page_size:
                break
            operation_cursor = unfinished[-1].operation_id

        cursor: str | None = None
        while True:
            page = await self.catalog.list_records(
                limit=self.page_size,
                cursor=cursor,
                states=(WorkspaceLifecycleState.ACTIVE,),
            )
            for record in page.records:
                if record.id in handled_workspaces:
                    continue
                payload = {
                    "workspace_id": record.id,
                    "catalog_schema_version": CATALOG_SCHEMA_VERSION,
                    "startup_run_id": self.run_id,
                }
                try:
                    _, operation, _ = await self.catalog.create_migration_operation(
                        workspace_id=record.id,
                        idempotency_key=f"startup:{self.run_id}:{record.id}",
                        payload=payload,
                    )
                    report.migrations_started += 1
                    await self._run_one(operation, report, reclaim_running=False)
                except Exception as exc:
                    logger.error(
                        "Startup migration preparation failed for workspace %s: %s",
                        record.id,
                        type(exc).__name__,
                    )
                    report.record_failure(record.id, exc)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.last_report = report
        return report

    async def _run_one(
        self,
        operation: CatalogOperation,
        report: WorkspaceRecoveryReport,
        *,
        reclaim_running: bool,
    ) -> None:
        runner = {
            CatalogOperationType.CREATE: self.run_create,
            CatalogOperationType.MIGRATE: self.run_migrate,
            CatalogOperationType.DELETE: self.run_delete,
        }.get(operation.operation_type)
        if runner is None:
            report.record_failure(
                operation.workspace_id,
                ValueError(
                    f"Unsupported recovery operation {operation.operation_type.value}"
                ),
            )
            return
        try:
            await runner(operation.operation_id, reclaim_running)
            terminal = await self.catalog.get_operation(operation.operation_id)
            if terminal.state is CatalogOperationState.SUCCEEDED:
                report.succeeded += 1
            else:
                report.record_failure(
                    operation.workspace_id,
                    RuntimeError(terminal.error_code or "operation_failed"),
                )
        except Exception as exc:
            logger.error(
                "Startup recovery failed for workspace %s operation %s: %s",
                operation.workspace_id,
                operation.operation_id,
                type(exc).__name__,
            )
            report.record_failure(operation.workspace_id, exc)
