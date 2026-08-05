"""Framework-neutral hooks for explicit workspace background handoff."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Awaitable, Callable


BackgroundLeaseFactory = Callable[[], Awaitable[AbstractAsyncContextManager[object]]]

_background_lease_factory: ContextVar[BackgroundLeaseFactory | None] = ContextVar(
    "lightrag_background_workspace_lease_factory", default=None
)


@dataclass(frozen=True, slots=True)
class WorkspaceExecutionScope:
    workspace_id: str
    operation_kind: str


_workspace_execution_scope: ContextVar[WorkspaceExecutionScope | None] = ContextVar(
    "lightrag_workspace_execution_scope", default=None
)


def bind_workspace_execution_scope(
    workspace_id: str, operation_kind: str
) -> Token[WorkspaceExecutionScope | None]:
    return _workspace_execution_scope.set(
        WorkspaceExecutionScope(workspace_id, operation_kind)
    )


def reset_workspace_execution_scope(
    token: Token[WorkspaceExecutionScope | None],
) -> None:
    _workspace_execution_scope.reset(token)


def current_workspace_execution_scope() -> WorkspaceExecutionScope | None:
    return _workspace_execution_scope.get()


def bind_background_lease_factory(
    factory: BackgroundLeaseFactory,
) -> Token[BackgroundLeaseFactory | None]:
    """Bind the request-owned factory used by managed task launchers."""

    return _background_lease_factory.set(factory)


def reset_background_lease_factory(
    token: Token[BackgroundLeaseFactory | None],
) -> None:
    _background_lease_factory.reset(token)


async def capture_background_workspace_lease() -> AbstractAsyncContextManager[object]:
    """Capture a lease before an asyncio task inherits request context.

    Library callers outside the multi-workspace API receive a no-op async
    context, preserving the existing single-instance behavior.
    """

    factory = _background_lease_factory.get()
    if factory is None:
        return _NullAsyncContext()
    return await factory()


class _NullAsyncContext(AbstractAsyncContextManager[object]):
    async def __aenter__(self) -> object:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None
