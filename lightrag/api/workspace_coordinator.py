"""Deployment-scoped coordination for workspace control-plane work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import os
import time
from typing import Any, Protocol

from lightrag.admission import SharedStorageAdmissionCoordinator
from lightrag.kg.shared_storage import get_namespace_data, get_namespace_lock


STARTUP_RECOVERY_GROUP = "workspace:startup_recovery"
_COORDINATOR_NAMESPACE = "workspace_coordinator"
_STARTUP_STATE_KEY = "startup_recovery"


class WorkspaceCoordinationError(RuntimeError):
    """A deployment coordinator could not safely complete startup work."""


StartupAction = Callable[[], Awaitable[Mapping[str, Any]]]


class WorkspaceCoordinator(Protocol):
    """Coordination boundary used by the workspace manager."""

    provider_kind: str
    shared: bool

    async def run_startup_once(self, action: StartupAction) -> dict[str, Any]: ...

    async def snapshot(self) -> dict[str, Any]: ...


class LocalWorkspaceCoordinator:
    """Single-process coordinator with no cross-worker claims."""

    provider_kind = "local"
    shared = False

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._result: dict[str, Any] | None = None
        self._failed_error_type: str | None = None

    async def run_startup_once(self, action: StartupAction) -> dict[str, Any]:
        async with self._lock:
            if self._result is not None:
                return dict(self._result)
            if self._failed_error_type is not None:
                raise WorkspaceCoordinationError(
                    "Workspace startup recovery previously failed: "
                    f"{self._failed_error_type}"
                )
            try:
                self._result = dict(await action())
            except BaseException as exc:
                self._failed_error_type = type(exc).__name__
                raise
            return dict(self._result)

    async def snapshot(self) -> dict[str, Any]:
        if self._result is not None:
            status = "SUCCEEDED"
        elif self._failed_error_type is not None:
            status = "FAILED"
        else:
            status = "PENDING"
        return {
            "provider": self.provider_kind,
            "shared": self.shared,
            "startup_recovery": {
                "status": status,
                "error_type": self._failed_error_type,
            },
        }


class SameHostManagerWorkspaceCoordinator:
    """Gunicorn coordinator backed by the master-owned SyncManager.

    A tracked global slot provides kill-safe ownership. A small shared state
    record publishes the terminal result to every worker and carries a
    monotonic fencing token. If an owner dies, the existing global-slot reaper
    admits a successor; an old owner that resumes after lease loss cannot
    publish a terminal result because its fence no longer matches.
    """

    provider_kind = "manager"
    shared = True

    def __init__(
        self,
        *,
        timeout_seconds: float = 300.0,
        heartbeat_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0 or heartbeat_seconds <= 0:
            raise ValueError("Coordinator timeout and heartbeat must be positive")
        self._timeout_seconds = timeout_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._slots = SharedStorageAdmissionCoordinator()

    @classmethod
    def from_environment(cls) -> "SameHostManagerWorkspaceCoordinator":
        return cls(
            timeout_seconds=float(
                os.getenv("LIGHTRAG_COORDINATOR_STARTUP_TIMEOUT", "300") or "300"
            ),
            heartbeat_seconds=float(
                os.getenv("LIGHTRAG_COORDINATOR_HEARTBEAT_SECONDS", "5") or "5"
            ),
        )

    async def _state_namespace(self):
        return await get_namespace_data(_COORDINATOR_NAMESPACE, workspace="")

    async def _read_state(self) -> dict[str, Any]:
        namespace = await self._state_namespace()
        return dict(namespace.get(_STARTUP_STATE_KEY) or {})

    async def _heartbeat(self, lease_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await self._slots.renew(STARTUP_RECOVERY_GROUP, (lease_id,))

    @staticmethod
    def _terminal_result(state: Mapping[str, Any]) -> dict[str, Any] | None:
        status = state.get("status")
        if status == "SUCCEEDED":
            return dict(state.get("result") or {})
        if status == "FAILED":
            raise WorkspaceCoordinationError(
                "Workspace startup recovery failed in coordinator owner: "
                f"{state.get('error_type') or 'unknown'}"
            )
        return None

    async def run_startup_once(self, action: StartupAction) -> dict[str, Any]:
        if not self._slots.is_limited(STARTUP_RECOVERY_GROUP):
            raise WorkspaceCoordinationError(
                f"Shared slot {STARTUP_RECOVERY_GROUP!r} is not configured"
            )

        deadline = time.monotonic() + self._timeout_seconds
        delay = 0.005
        lease_id: str | None = None
        while lease_id is None:
            terminal = self._terminal_result(await self._read_state())
            if terminal is not None:
                return terminal
            lease_id, favored = await self._slots.try_acquire(STARTUP_RECOVERY_GROUP)
            if lease_id is not None:
                break
            if time.monotonic() >= deadline:
                raise WorkspaceCoordinationError(
                    "Timed out waiting for same-host startup recovery owner"
                )
            await asyncio.sleep(delay)
            delay = 0.005 if favored else min(0.1, delay * 1.5)

        heartbeat: asyncio.Task[None] | None = None
        try:
            namespace = await self._state_namespace()
            lock = get_namespace_lock(_COORDINATOR_NAMESPACE, workspace="")
            async with lock:
                state = dict(namespace.get(_STARTUP_STATE_KEY) or {})
                terminal = self._terminal_result(state)
                if terminal is not None:
                    return terminal
                fence = int(state.get("fencing_token", 0)) + 1
                namespace[_STARTUP_STATE_KEY] = {
                    "status": "RUNNING",
                    "owner_pid": os.getpid(),
                    "owner_lease_id": lease_id,
                    "fencing_token": fence,
                    "updated_at": time.time(),
                }

            heartbeat = asyncio.create_task(
                self._heartbeat(lease_id),
                name="workspace-startup-coordinator-heartbeat",
            )
            try:
                result = dict(await action())
            except BaseException as exc:
                async with lock:
                    current = dict(namespace.get(_STARTUP_STATE_KEY) or {})
                    if (
                        current.get("owner_lease_id") == lease_id
                        and current.get("fencing_token") == fence
                    ):
                        namespace[_STARTUP_STATE_KEY] = {
                            **current,
                            "status": "FAILED",
                            "error_type": type(exc).__name__,
                            "updated_at": time.time(),
                        }
                raise

            async with lock:
                current = dict(namespace.get(_STARTUP_STATE_KEY) or {})
                if (
                    current.get("owner_lease_id") != lease_id
                    or current.get("fencing_token") != fence
                ):
                    raise WorkspaceCoordinationError(
                        "Startup recovery owner lost its coordinator fence"
                    )
                namespace[_STARTUP_STATE_KEY] = {
                    **current,
                    "status": "SUCCEEDED",
                    "result": result,
                    "updated_at": time.time(),
                }
            return result
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._slots.release(STARTUP_RECOVERY_GROUP, lease_id)

    async def snapshot(self) -> dict[str, Any]:
        state = await self._read_state()
        return {
            "provider": self.provider_kind,
            "shared": self.shared,
            "startup_recovery": {
                key: value
                for key, value in state.items()
                if key not in {"owner_lease_id", "result"}
            },
        }
