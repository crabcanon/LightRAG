"""Deterministic same-host workspace coordinator tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from lightrag.api.workspace_coordinator import (
    STARTUP_RECOVERY_GROUP,
    LocalWorkspaceCoordinator,
    SameHostManagerWorkspaceCoordinator,
    WorkspaceCoordinationError,
)
from lightrag.kg import shared_storage as ss


pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def clean_shared_storage():
    ss.finalize_share_data()
    yield
    ss.finalize_share_data()


@pytest.mark.asyncio
async def test_local_coordinator_executes_startup_action_once() -> None:
    coordinator = LocalWorkspaceCoordinator()
    calls = 0

    async def action():
        nonlocal calls
        calls += 1
        return {"succeeded": 1}

    first, second = await asyncio.gather(
        coordinator.run_startup_once(action),
        coordinator.run_startup_once(action),
    )

    assert first == second == {"succeeded": 1}
    assert calls == 1
    assert (await coordinator.snapshot())["startup_recovery"]["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_manager_coordinator_shares_one_startup_result() -> None:
    ss.initialize_share_data(2, {STARTUP_RECOVERY_GROUP: 1})
    first = SameHostManagerWorkspaceCoordinator(timeout_seconds=2)
    second = SameHostManagerWorkspaceCoordinator(timeout_seconds=2)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def action():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"succeeded": 4, "failures": []}

    leader = asyncio.create_task(first.run_startup_once(action))
    await entered.wait()
    follower = asyncio.create_task(second.run_startup_once(action))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(leader, follower) == [
        {"succeeded": 4, "failures": []},
        {"succeeded": 4, "failures": []},
    ]
    assert calls == 1
    snapshot = await second.snapshot()
    assert snapshot["startup_recovery"]["status"] == "SUCCEEDED"
    assert "owner_lease_id" not in snapshot["startup_recovery"]
    assert "result" not in snapshot["startup_recovery"]


@pytest.mark.asyncio
async def test_manager_coordinator_reclaims_dead_owner_and_advances_fence() -> None:
    ss.initialize_share_data(2, {STARTUP_RECOVERY_GROUP: 1})
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    dead_pid = process.pid

    lease_namespace = await ss._get_lease_namespace()
    gate = ss._load_gate_state(lease_namespace, STARTUP_RECOVERY_GROUP)
    gate["leases"]["dead-owner"] = {
        "pid": dead_pid,
        "updated_at": time.time(),
    }
    lease_namespace[STARTUP_RECOVERY_GROUP] = gate
    state_namespace = await ss.get_namespace_data("workspace_coordinator", workspace="")
    state_namespace["startup_recovery"] = {
        "status": "RUNNING",
        "owner_pid": dead_pid,
        "owner_lease_id": "dead-owner",
        "fencing_token": 8,
        "updated_at": time.time(),
    }

    coordinator = SameHostManagerWorkspaceCoordinator(timeout_seconds=2)
    result = await coordinator.run_startup_once(
        lambda: asyncio.sleep(0, result={"succeeded": 1})
    )

    assert result == {"succeeded": 1}
    state = dict(state_namespace["startup_recovery"])
    assert state["status"] == "SUCCEEDED"
    assert state["fencing_token"] == 9
    assert state["owner_pid"] == os.getpid()


@pytest.mark.asyncio
async def test_manager_coordinator_failure_is_shared_and_not_retried() -> None:
    ss.initialize_share_data(2, {STARTUP_RECOVERY_GROUP: 1})
    coordinator = SameHostManagerWorkspaceCoordinator(timeout_seconds=2)
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        raise ValueError("synthetic secret-free failure")

    with pytest.raises(ValueError, match="synthetic"):
        await coordinator.run_startup_once(fail)
    with pytest.raises(WorkspaceCoordinationError, match="ValueError"):
        await coordinator.run_startup_once(fail)
    assert calls == 1
