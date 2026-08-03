"""Deterministic service-level admission, fairness, and overload tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from lightrag.admission import AdmissionRejectedError, ResourceAdmissionController
from lightrag import LightRAG, ROLES
from lightrag.pipeline import _PipelineMixin
from lightrag.kg import shared_storage as ss
from lightrag.utils import EmbeddingFunc, Tokenizer
from lightrag.workspace_scope import (
    bind_workspace_execution_scope,
    reset_workspace_execution_scope,
)


pytestmark = pytest.mark.offline


class _Tokenizer:
    def encode(self, content: str) -> list[str]:
        return content.split()

    def decode(self, tokens: list[str]) -> str:
        return " ".join(tokens)


class _SharedCoordinator:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.leases: set[str] = set()
        self.sequence = 0
        self.peak = 0
        self.lock = asyncio.Lock()

    def is_limited(self, _group: str) -> bool:
        return True

    async def try_acquire(self, _group: str) -> tuple[str | None, bool]:
        async with self.lock:
            if len(self.leases) >= self.limit:
                return None, True
            self.sequence += 1
            lease = f"lease-{self.sequence}"
            self.leases.add(lease)
            self.peak = max(self.peak, len(self.leases))
            return lease, True

    async def release(self, _group: str, lease_id: str) -> None:
        async with self.lock:
            self.leases.discard(lease_id)

    async def renew(self, _group: str, lease_ids: tuple[str, ...]) -> None:
        assert set(lease_ids) <= self.leases


async def _scoped_call(workspace_id: str, func, *args, operation_kind: str = "query"):
    token = bind_workspace_execution_scope(workspace_id, operation_kind)
    try:
        return await func(*args)
    finally:
        reset_workspace_execution_scope(token)


def test_lightrag_routes_every_provider_family_through_shared_controller(
    tmp_path: Path,
) -> None:
    wrapped_groups: list[tuple[str, str]] = []

    class SpyController:
        def wrap(
            self,
            func,
            *,
            group,
            workspace_id,
            default_operation_kind,
            cost_hint=None,
        ):
            _ = cost_hint
            wrapped_groups.append((group, workspace_id))
            return func

    async def llm(*_args, **_kwargs) -> str:
        return "ok"

    async def embedding(_texts):
        return []

    rag = LightRAG(
        working_dir=str(tmp_path),
        workspace="tenant-provider-routing",
        llm_model_func=llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=4,
            max_token_size=128,
            func=embedding,
        ),
        tokenizer=Tokenizer("test", _Tokenizer()),
        resource_admission_controller=SpyController(),
        resource_admission_workspace_id="kb-public-id",
    )

    assert rag._resource_admission_workspace_id == "kb-public-id"
    assert set(wrapped_groups) == {
        ("embedding", "kb-public-id"),
        *((f"llm:{role.name}", "kb-public-id") for role in ROLES),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["llm:query", "embedding", "rerank"])
async def test_shared_controller_caps_multiple_workspace_wrappers(group: str) -> None:
    controller = ResourceAdmissionController({group: 2}, default_queue_timeout=2)
    release = asyncio.Event()
    started: asyncio.Queue[str] = asyncio.Queue()
    running = 0
    peak = 0

    async def provider(value: str) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        started.put_nowait(value)
        try:
            await release.wait()
            return value
        finally:
            running -= 1

    wrapped_a = controller.wrap(
        provider,
        group=group,
        workspace_id="alpha",
        default_operation_kind="query",
    )
    wrapped_b = controller.wrap(
        provider,
        group=group,
        workspace_id="beta",
        default_operation_kind="query",
    )
    tasks = [
        asyncio.create_task(
            _scoped_call(
                "alpha" if index % 2 == 0 else "beta",
                wrapped_a if index % 2 == 0 else wrapped_b,
                str(index),
            )
        )
        for index in range(6)
    ]

    await started.get()
    await started.get()
    snapshot = await controller.snapshot()
    assert peak == 2
    assert snapshot["groups"][group]["active"] == 2
    assert snapshot["groups"][group]["pending"] == 4

    release.set()
    assert await asyncio.gather(*tasks) == [str(index) for index in range(6)]
    snapshot = await controller.snapshot()
    assert snapshot["groups"][group]["peak_active"] == 2
    assert snapshot["groups"][group]["active"] == 0


@pytest.mark.asyncio
async def test_drr_gives_waiting_workspace_a_turn_under_bulk_load() -> None:
    controller = ResourceAdmissionController({"embedding": 1}, default_queue_timeout=2)
    gates = {name: asyncio.Event() for name in ("a1", "a2", "a3", "b1")}
    started: asyncio.Queue[str] = asyncio.Queue()

    async def provider(name: str) -> str:
        started.put_nowait(name)
        await gates[name].wait()
        return name

    wrapped_alpha = controller.wrap(
        provider,
        group="embedding",
        workspace_id="alpha",
        default_operation_kind="ingestion",
    )
    wrapped_beta = controller.wrap(
        provider,
        group="embedding",
        workspace_id="beta",
        default_operation_kind="ingestion",
    )
    tasks = {
        name: asyncio.create_task(
            _scoped_call(
                "beta" if name == "b1" else "alpha",
                wrapped_beta if name == "b1" else wrapped_alpha,
                name,
            )
        )
        for name in ("a1", "a2", "a3", "b1")
    }

    assert await started.get() == "a1"
    gates["a1"].set()
    assert await started.get() == "a2"
    gates["a2"].set()
    assert await started.get() == "b1"
    gates["b1"].set()
    assert await started.get() == "a3"
    gates["a3"].set()
    assert set(await asyncio.gather(*tasks.values())) == set(tasks)


@pytest.mark.asyncio
async def test_priority_aging_prevents_ingestion_starvation() -> None:
    clock = [0.0]
    controller = ResourceAdmissionController(
        {"llm:query": 1},
        aging_seconds=1,
        default_queue_timeout=2,
        clock=lambda: clock[0],
    )
    gates = {name: asyncio.Event() for name in ("active", "old", "new")}
    started: asyncio.Queue[str] = asyncio.Queue()

    async def provider(name: str) -> str:
        started.put_nowait(name)
        await gates[name].wait()
        return name

    wrapped = controller.wrap(
        provider,
        group="llm:query",
        workspace_id="alpha",
        default_operation_kind="query",
    )
    active = asyncio.create_task(_scoped_call("alpha", wrapped, "active"))
    assert await started.get() == "active"
    old = asyncio.create_task(
        _scoped_call("alpha", wrapped, "old", operation_kind="recovery")
    )
    await asyncio.sleep(0)
    assert (await controller.snapshot())["groups"]["llm:query"]["pending"] == 1

    clock[0] = 10.0
    new = asyncio.create_task(_scoped_call("alpha", wrapped, "new"))
    await asyncio.sleep(0)
    gates["active"].set()
    assert await started.get() == "old"
    gates["old"].set()
    assert await started.get() == "new"
    gates["new"].set()
    assert await asyncio.gather(active, old, new) == ["active", "old", "new"]


@pytest.mark.asyncio
async def test_pipeline_mixin_uses_shared_active_pipeline_cap() -> None:
    controller = ResourceAdmissionController({"pipeline": 1}, default_queue_timeout=2)
    started: asyncio.Queue[str] = asyncio.Queue()
    release = asyncio.Event()

    class DummyPipeline:
        apipeline_process_enqueue_documents = (
            _PipelineMixin.apipeline_process_enqueue_documents
        )

        def __init__(self, workspace_id: str) -> None:
            self._resource_admission_controller = controller
            self._resource_admission_workspace_id = workspace_id

        async def _apipeline_process_enqueue_documents_unadmitted(
            self, _holding_busy: bool = False, token: str | None = None
        ) -> None:
            _ = _holding_busy, token
            started.put_nowait(self._resource_admission_workspace_id)
            await release.wait()

    alpha = DummyPipeline("alpha")
    beta = DummyPipeline("beta")
    tasks = [
        asyncio.create_task(
            _scoped_call("alpha", alpha.apipeline_process_enqueue_documents)
        ),
        asyncio.create_task(
            _scoped_call("beta", beta.apipeline_process_enqueue_documents)
        ),
    ]
    assert await started.get() == "alpha"
    snapshot = await controller.snapshot()
    assert snapshot["groups"]["pipeline"]["active"] == 1
    assert snapshot["groups"]["pipeline"]["pending"] == 1

    release.set()
    await asyncio.gather(*tasks)
    snapshot = await controller.snapshot()
    assert snapshot["groups"]["pipeline"]["peak_active"] == 1
    assert snapshot["groups"]["pipeline"]["active"] == 0


@pytest.mark.asyncio
async def test_pending_limit_rejects_without_growing_queue() -> None:
    controller = ResourceAdmissionController(
        {"rerank": 1},
        global_pending_limit=2,
        per_workspace_pending_limit=1,
        default_queue_timeout=2,
    )
    release = asyncio.Event()
    started = asyncio.Event()

    async def provider() -> None:
        started.set()
        await release.wait()

    wrapped = controller.wrap(
        provider,
        group="rerank",
        workspace_id="alpha",
        default_operation_kind="query",
    )
    active = asyncio.create_task(wrapped())
    await started.wait()
    pending = asyncio.create_task(wrapped())
    await asyncio.sleep(0)

    with pytest.raises(AdmissionRejectedError) as rejected:
        await wrapped()
    assert rejected.value.code == "service_admission_queue_full"
    assert (await controller.snapshot())["groups"]["rerank"]["pending"] == 1

    release.set()
    await asyncio.gather(active, pending)


@pytest.mark.asyncio
async def test_two_worker_controllers_share_deployment_slot_cap() -> None:
    coordinator = _SharedCoordinator(limit=2)
    first = ResourceAdmissionController(
        {"llm:query": 2}, coordinator=coordinator, default_queue_timeout=2
    )
    second = ResourceAdmissionController(
        {"llm:query": 2}, coordinator=coordinator, default_queue_timeout=2
    )
    release = asyncio.Event()
    started: asyncio.Queue[None] = asyncio.Queue()

    async def provider() -> None:
        started.put_nowait(None)
        await release.wait()

    wrappers = [
        controller.wrap(
            provider,
            group="llm:query",
            workspace_id=f"workspace-{index}",
            default_operation_kind="query",
        )
        for index, controller in enumerate((first, second))
    ]
    tasks = [asyncio.create_task(wrappers[index % 2]()) for index in range(6)]
    await started.get()
    await started.get()
    assert coordinator.peak == 2
    assert len(coordinator.leases) == 2

    release.set()
    await asyncio.gather(*tasks)
    assert coordinator.peak == 2
    assert coordinator.leases == set()


@pytest.mark.asyncio
async def test_real_manager_adapter_caps_two_worker_local_controllers() -> None:
    ss.finalize_share_data()
    ss.initialize_share_data(2, {"pipeline": 1})
    try:
        first = ResourceAdmissionController({"pipeline": 1}, default_queue_timeout=2)
        second = ResourceAdmissionController({"pipeline": 1}, default_queue_timeout=2)
        release = asyncio.Event()
        started: asyncio.Queue[None] = asyncio.Queue()
        running = 0
        peak = 0

        async def pipeline() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            started.put_nowait(None)
            try:
                await release.wait()
            finally:
                running -= 1

        wrappers = [
            controller.wrap(
                pipeline,
                group="pipeline",
                workspace_id=f"worker-{index}",
                default_operation_kind="ingestion",
            )
            for index, controller in enumerate((first, second))
        ]
        tasks = [asyncio.create_task(wrapper()) for wrapper in wrappers]
        await started.get()
        await asyncio.sleep(0.05)
        assert peak == 1
        assert await ss.global_concurrency_in_use("pipeline") == 1

        release.set()
        await asyncio.gather(*tasks)
        assert peak == 1
        assert await ss.global_concurrency_in_use("pipeline") == 0
    finally:
        ss.finalize_share_data()


def test_admission_rejection_preserves_retryable_http_contract() -> None:
    original_argv = sys.argv[:]
    try:
        sys.argv = ["lightrag-server"]
        from lightrag.api.utils_api import internal_server_error
    finally:
        sys.argv = original_argv

    http_error = internal_server_error(
        AdmissionRejectedError(
            code="service_admission_queue_full",
            resource_group="embedding",
            retry_after=3,
        )
    )

    assert http_error.status_code == 429
    assert http_error.headers == {"Retry-After": "3"}
    assert http_error.detail == {
        "code": "service_admission_queue_full",
        "resource_group": "embedding",
        "message": "Service capacity is temporarily unavailable",
    }
