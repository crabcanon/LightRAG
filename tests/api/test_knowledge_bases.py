"""Regression tests for request-scoped knowledge-base isolation."""

import asyncio
from dataclasses import replace
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse, StreamingResponse
import pytest

from lightrag.api.catalog import CatalogCASConflict, CatalogOperationState
from lightrag.api.knowledge_bases import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KNOWLEDGE_BASE_HEADER,
    KnowledgeBaseCatalog,
    KnowledgeBaseConflictError,
    KnowledgeBaseError,
    KnowledgeBaseManager,
    OllamaSelectorError,
    StorageProfileError,
    resolve_ollama_model_alias,
)
from lightrag.base import OllamaServerInfos
from lightrag.kg.shared_storage import start_reserved_background_task
from lightrag.workspace import (
    LEGACY_DEFAULT_CANONICAL_KEY,
    LEGACY_NAMESPACE_CODEC,
    NAMED_NAMESPACE_CODEC,
    WorkspaceBindingError,
    WorkspaceKind,
)

_original_argv = sys.argv[:]
sys.argv = [sys.argv[0]]
_graph_routes = importlib.import_module("lightrag.api.routers.graph_routes")
_document_routes = importlib.import_module("lightrag.api.routers.document_routes")
_query_routes = importlib.import_module("lightrag.api.routers.query_routes")
_ollama_routes = importlib.import_module("lightrag.api.routers.ollama_api")
_knowledge_base_routes = importlib.import_module(
    "lightrag.api.routers.knowledge_base_routes"
)
sys.argv = _original_argv

create_graph_routes = _graph_routes.create_graph_routes
create_document_routes = _document_routes.create_document_routes
create_query_routes = _query_routes.create_query_routes
OllamaAPI = _ollama_routes.OllamaAPI
create_knowledge_base_routes = _knowledge_base_routes.create_knowledge_base_routes

pytestmark = pytest.mark.offline


class _FakeStorage:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.dropped = False

    async def drop(self) -> None:
        self.dropped = True


class _FakeRag:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.initialized = False
        self.finalized = False
        self.pipeline_recoveries = 0
        self.ollama_server_infos = OllamaServerInfos()
        self.role_llm_kwargs = {"query": {}}
        self.llm_model_kwargs = {}

        async def query_llm(*_args, **_kwargs):
            return self.workspace

        self.role_llm_funcs = {"query": query_llm}
        for attribute in (
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
        ):
            setattr(self, attribute, _FakeStorage(workspace))

    async def initialize_storages(self) -> None:
        self.initialized = True

    async def check_and_migrate_data(self) -> None:
        return None

    async def finalize_storages(self) -> None:
        self.finalized = True

    async def apipeline_process_enqueue_documents(self) -> None:
        self.pipeline_recoveries += 1

    async def get_graph_labels(self) -> list[str]:
        return [self.workspace]

    async def aquery(self, *_args, **_kwargs) -> str:
        return self.workspace


def _document_manager(root: Path, workspace: str):
    input_dir = root / workspace if workspace else root
    input_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        workspace=workspace,
        input_dir=input_dir,
        base_input_dir=root,
    )


def _manager(
    tmp_path: Path,
    *,
    default_workspace: str = "legacy",
    profiles=None,
    default_storage_profile=None,
    active_storage_implementations=None,
    multi_workspace_enabled: bool = True,
    rag_factory=None,
    max_loaded_instances: int = 32,
    allow_non_default_writes: bool = True,
    ollama_model_name: str = "lightrag:latest",
) -> KnowledgeBaseManager:
    catalog = KnowledgeBaseCatalog(
        tmp_path / "rag" / "knowledge_bases.json", default_workspace
    )
    return KnowledgeBaseManager(
        catalog=catalog,
        default_rag=_FakeRag(default_workspace),
        default_document_manager=_document_manager(
            tmp_path / "inputs", default_workspace
        ),
        rag_factory=(
            rag_factory
            if rag_factory is not None
            else lambda record, _profile: _FakeRag(record.effective_workspace)
        ),
        document_manager_factory=lambda record, _profile: _document_manager(
            tmp_path / "inputs", record.effective_workspace
        ),
        storage_profiles=profiles,
        default_storage_profile=default_storage_profile,
        active_storage_implementations=(
            active_storage_implementations
            if active_storage_implementations is not None
            else (
                "RedisKVStorage",
                "PGVectorStorage",
                "Neo4JStorage",
                "PGDocStatusStorage",
            )
        ),
        multi_workspace_enabled=multi_workspace_enabled,
        max_loaded_instances=max_loaded_instances,
        allow_non_default_writes=allow_non_default_writes,
        ollama_model_name=ollama_model_name,
    )


def test_catalog_preserves_default_workspace_and_survives_reload(tmp_path: Path):
    path = tmp_path / "knowledge_bases.json"
    catalog = KnowledgeBaseCatalog(path, "legacy_workspace")
    created = catalog.create(
        name="Independent",
        isolation_level="logical",
        storage_profile_id=None,
    )

    reloaded = KnowledgeBaseCatalog(path, "legacy_workspace")

    assert reloaded.get(DEFAULT_KNOWLEDGE_BASE_ID).effective_workspace == (
        "legacy_workspace"
    )
    assert reloaded.get(created.id) == created
    assert created.effective_workspace == created.id
    assert (
        reloaded.get(DEFAULT_KNOWLEDGE_BASE_ID).canonical_workspace_key
        == LEGACY_DEFAULT_CANONICAL_KEY
    )
    assert (
        reloaded.get(DEFAULT_KNOWLEDGE_BASE_ID).namespace_codec_version
        == LEGACY_NAMESPACE_CODEC
    )
    assert created.workspace_kind == WorkspaceKind.NAMED.value
    assert created.namespace_codec_version == NAMED_NAMESPACE_CODEC
    assert created.to_workspace_binding().canonical_key == created.id

    with pytest.raises(KnowledgeBaseError, match="refusing to remap"):
        KnowledgeBaseCatalog(path, "different_workspace")


def test_catalog_bootstraps_binding_tags_from_pre_phase_one_file(tmp_path: Path):
    path = tmp_path / "knowledge_bases.json"
    catalog = KnowledgeBaseCatalog(path, "legacy_workspace")
    created = catalog.create(
        name="Independent",
        isolation_level="logical",
        storage_profile_id=None,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    for record in payload["knowledge_bases"]:
        record.pop("workspace_kind")
        record.pop("canonical_workspace_key")
        record.pop("namespace_codec_version")
    path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = KnowledgeBaseCatalog(path, "legacy_workspace")

    assert reloaded.get("default").workspace_kind == "legacy_default"
    assert (
        reloaded.get("default").canonical_workspace_key == LEGACY_DEFAULT_CANONICAL_KEY
    )
    assert reloaded.get(created.id).workspace_kind == "named"
    assert reloaded.get(created.id).canonical_workspace_key == created.id


def test_catalog_rejects_workspace_aliases(tmp_path: Path):
    path = tmp_path / "knowledge_bases.json"
    KnowledgeBaseCatalog(path, "legacy_workspace")
    payload = json.loads(path.read_text(encoding="utf-8"))
    alias = {
        **payload["knowledge_bases"][0],
        "id": "alias",
        "name": "Unsafe alias",
    }
    payload["knowledge_bases"].append(alias)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkspaceBindingError, match="public ID 'default'"):
        KnowledgeBaseCatalog(path, "legacy_workspace")


def test_all_twelve_storages_receive_the_effective_workspace(tmp_path: Path):
    manager = _manager(tmp_path)
    record = manager.create(
        name="Isolated", isolation_level="logical", storage_profile_id=None
    )

    context = asyncio.run(manager.get_context(record.id))

    storage_workspaces = {
        value.workspace
        for name, value in vars(context.rag).items()
        if name != "workspace" and isinstance(value, _FakeStorage)
    }
    assert storage_workspaces == {record.effective_workspace}
    assert context.document_manager.workspace == record.effective_workspace


@pytest.mark.asyncio
async def test_contextvar_keeps_concurrent_requests_isolated(tmp_path: Path):
    manager = _manager(tmp_path)
    first = manager.create(
        name="First", isolation_level="logical", storage_profile_id=None
    )
    second = manager.create(
        name="Second", isolation_level="logical", storage_profile_id=None
    )
    barrier = asyncio.Event()

    async def selected_workspace(knowledge_base_id: str) -> str:
        async with manager.bind_request(knowledge_base_id):
            barrier.set()
            await asyncio.sleep(0)
            await barrier.wait()
            return manager.rag_proxy.workspace

    assert await asyncio.gather(
        selected_workspace(first.id), selected_workspace(second.id)
    ) == [first.effective_workspace, second.effective_workspace]


def test_graph_route_uses_header_and_defaults_without_header(tmp_path: Path):
    manager = _manager(tmp_path)
    isolated = manager.create(
        name="Graph B", isolation_level="logical", storage_profile_id=None
    )
    app = FastAPI()
    app.include_router(
        create_graph_routes(
            manager.rag_proxy,
            context_dependency=manager.request_dependency,
        )
    )
    client = TestClient(app)

    assert client.get("/graph/label/list").json() == ["legacy"]
    assert client.get(
        "/graph/label/list",
        headers={KNOWLEDGE_BASE_HEADER: isolated.id},
    ).json() == [isolated.effective_workspace]
    assert (
        client.get(
            "/graph/label/list",
            headers={KNOWLEDGE_BASE_HEADER: "missing"},
        ).status_code
        == 404
    )


def test_selector_matrix_is_fail_closed_and_never_falls_back(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    app = FastAPI()
    app.include_router(
        create_graph_routes(
            manager.rag_proxy,
            context_dependency=manager.request_dependency,
        )
    )
    client = TestClient(app)

    assert client.get("/graph/label/list").status_code == 200
    for selector in ("", "   ", "bad/value", "@legacy-default"):
        response = client.get(
            "/graph/label/list", headers={KNOWLEDGE_BASE_HEADER: selector}
        )
        assert response.status_code == 400, (selector, response.text)
    assert (
        client.get(
            "/graph/label/list",
            headers={KNOWLEDGE_BASE_HEADER: "kb_unknown"},
        ).status_code
        == 404
    )

    expected_status = {
        "CREATING": 503,
        "MIGRATING": 503,
        "ERROR": 503,
        "DELETING": 409,
        "TOMBSTONED": 404,
    }
    for state, status_code in expected_status.items():
        record = manager.create(
            name=state, isolation_level="logical", storage_profile_id=None
        )
        manager.catalog._records[record.id] = replace(record, lifecycle_state=state)
        response = client.get(
            "/graph/label/list",
            headers={KNOWLEDGE_BASE_HEADER: record.id},
        )
        assert response.status_code == status_code, (state, response.text)
        if status_code == 503:
            assert response.headers["retry-after"] == "1"


def test_non_default_write_is_feature_gated_before_instance_load(tmp_path: Path):
    manager = _manager(tmp_path, allow_non_default_writes=False)
    isolated = manager.create(
        name="Write gated", isolation_level="logical", storage_profile_id=None
    )
    app = FastAPI()

    @app.post("/test-write", name="clear_cache")
    async def gated_write(_: None = Depends(manager.request_dependency)):
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/test-write").status_code == 200
    before = manager.side_effect_counters.snapshot()
    response = client.post("/test-write", headers={KNOWLEDGE_BASE_HEADER: isolated.id})

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]
    assert "retry-after" not in response.headers
    assert manager.side_effect_counters.snapshot() == before


def test_request_proxy_does_not_fallback_without_a_multi_workspace_context(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    with pytest.raises(Exception, match="explicit leased context"):
        _ = manager.rag_proxy.workspace


def test_stream_lease_is_held_until_response_body_finishes(tmp_path: Path):
    manager = _manager(tmp_path)
    observed_stream_leases: list[int] = []
    app = FastAPI()

    @app.get("/query/stream", dependencies=[Depends(manager.request_dependency)])
    async def stream_probe():
        async def body():
            entry = manager.instance_pool.peek("default")["entries"][0]
            observed_stream_leases.append(entry["stream_leases"])
            yield b"ok\n"

        return StreamingResponse(body(), media_type="application/x-ndjson")

    response = TestClient(app).get("/query/stream")

    assert response.status_code == 200
    assert observed_stream_leases == [1]
    assert manager.instance_pool.peek("default")["entries"][0]["stream_leases"] == 0


def test_openapi_publishes_header_on_every_data_plane_operation(tmp_path: Path):
    manager = _manager(tmp_path)
    app = FastAPI()
    app.include_router(
        create_document_routes(
            manager.rag_proxy,
            manager.document_manager_proxy,
            context_dependency=manager.request_dependency,
        )
    )
    app.include_router(
        create_query_routes(
            manager.rag_proxy,
            context_dependency=manager.request_dependency,
        )
    )
    app.include_router(
        create_graph_routes(
            manager.rag_proxy,
            context_dependency=manager.request_dependency,
        )
    )
    ollama = OllamaAPI(
        manager.rag_proxy,
        context_dependency=manager.request_dependency,
    )
    app.include_router(ollama.router, prefix="/api")
    app.include_router(create_knowledge_base_routes(manager))

    schema = app.openapi()
    data_plane_paths = {
        path
        for path in schema["paths"]
        if path.startswith(("/documents", "/query", "/graph"))
        or path in {"/graphs", "/api/chat", "/api/generate"}
    }
    assert data_plane_paths
    for path in data_plane_paths:
        for operation in schema["paths"][path].values():
            parameters = [
                parameter
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "header"
                and parameter.get("name") == KNOWLEDGE_BASE_HEADER
            ]
            assert len(parameters) == 1, f"{path} must publish exactly one KB header"
            assert parameters[0]["required"] is False
            assert "GET /knowledge-bases" in parameters[0]["description"]

    for path in (
        "/api/version",
        "/api/tags",
        "/api/ps",
        "/knowledge-bases",
        "/knowledge-bases/{knowledge_base_id}",
    ):
        for operation in schema["paths"][path].values():
            assert all(
                parameter.get("name") != KNOWLEDGE_BASE_HEADER
                for parameter in operation.get("parameters", [])
            ), f"{path} must not publish a KB header"


def _ollama_test_app(manager: KnowledgeBaseManager) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(OllamaSelectorError)
    async def ollama_selector_error(
        _request: Request, exc: OllamaSelectorError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message, "code": exc.code},
            headers=exc.headers,
        )

    ollama = OllamaAPI(
        manager.rag_proxy,
        context_dependency=manager.request_dependency,
        model_alias_provider=manager.list_ollama_workspace_ids,
    )
    app.include_router(ollama.router, prefix="/api")
    return app


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("lightrag:latest", DEFAULT_KNOWLEDGE_BASE_ID),
        ("lightrag:default", DEFAULT_KNOWLEDGE_BASE_ID),
        ("lightrag:project_1", "project_1"),
    ],
)
def test_ollama_model_alias_resolution(model: str, expected: str):
    assert resolve_ollama_model_alias(model, "lightrag:latest") == expected


def test_multi_workspace_rejects_ambiguous_emulated_ollama_model(tmp_path: Path):
    with pytest.raises(ValueError, match="reserved"):
        _manager(tmp_path, ollama_model_name="lightrag:project_1")

    legacy = _manager(
        tmp_path / "legacy",
        multi_workspace_enabled=False,
        ollama_model_name="lightrag:project_1",
    )
    assert legacy.ollama_model_name == "lightrag:project_1"


def test_ollama_model_only_routes_named_workspace_and_rejects_unsafe_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def count_tokens(value: str) -> int:
        return len(value)

    monkeypatch.setattr(_ollama_routes, "aestimate_tokens", count_tokens)
    manager = _manager(tmp_path)
    record = manager.create(
        name="Ollama project", isolation_level="logical", storage_profile_id=None
    )
    client = TestClient(_ollama_test_app(manager))
    alias = f"lightrag:{record.id}"
    initial_counters = manager.side_effect_counters.snapshot()
    initial_pool = manager.instance_pool.peek()
    initial_records = [item.id for item in manager.list_records()]

    unknown = client.post(
        "/api/chat",
        json={
            "model": "lightrag:missing",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert unknown.status_code == 404
    assert unknown.json() == {"error": "Model not found", "code": "model_not_found"}

    conflict = client.post(
        "/api/generate",
        headers={KNOWLEDGE_BASE_HEADER: DEFAULT_KNOWLEDGE_BASE_ID},
        json={"model": alias, "prompt": "hello", "stream": False},
    )
    assert conflict.status_code == 400
    assert conflict.json() == {
        "error": "Model and knowledge-base header select different workspaces",
        "code": "selector_conflict",
    }

    malformed = client.post(
        "/api/generate",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 400
    assert malformed.json() == {
        "error": "Invalid JSON request body",
        "code": "invalid_request",
    }
    assert manager.side_effect_counters.snapshot() == initial_counters
    assert manager.instance_pool.peek() == initial_pool
    assert [item.id for item in manager.list_records()] == initial_records

    generated = client.post(
        "/api/generate",
        headers={KNOWLEDGE_BASE_HEADER: record.id},
        json={"model": alias, "prompt": "hello", "stream": False},
    )
    assert generated.status_code == 200
    assert generated.json()["model"] == alias
    assert generated.json()["response"] == record.effective_workspace

    chatted = client.post(
        "/api/chat",
        json={
            "model": alias,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert chatted.status_code == 200
    assert chatted.json()["model"] == alias
    assert chatted.json()["message"]["content"] == record.effective_workspace
    assert manager.side_effect_counters.instance_constructions == 1

    generated_stream = client.post(
        "/api/generate",
        json={"model": alias, "prompt": "hello", "stream": True},
    )
    chat_stream = client.post(
        "/api/chat",
        json={
            "model": alias,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert generated_stream.status_code == 200
    assert chat_stream.status_code == 200
    for response in (generated_stream, chat_stream):
        chunks = [json.loads(line) for line in response.text.splitlines()]
        assert chunks
        assert all(chunk["model"] == alias for chunk in chunks)

    before_ps = manager.side_effect_counters.snapshot()
    running_aliases = {
        model["name"] for model in client.get("/api/ps").json()["models"]
    }
    assert alias in running_aliases
    assert manager.side_effect_counters.snapshot() == before_ps


def test_ollama_metadata_lists_catalog_aliases_without_loading_instances(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    record = manager.create(
        name="Catalog only", isolation_level="logical", storage_profile_id=None
    )
    client = TestClient(_ollama_test_app(manager))
    before_counters = manager.side_effect_counters.snapshot()
    before_pool = manager.instance_pool.peek()

    tags_response = client.get("/api/tags")
    ps_response = client.get("/api/ps")

    assert tags_response.status_code == 200
    assert {model["name"] for model in tags_response.json()["models"]} >= {
        "lightrag:latest",
        "lightrag:default",
        f"lightrag:{record.id}",
    }
    assert ps_response.status_code == 200
    assert ps_response.json() == {"models": []}
    assert manager.side_effect_counters.snapshot() == before_counters
    assert manager.instance_pool.peek() == before_pool


def test_management_api_crud_and_default_delete_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def no_pipeline_status(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "lightrag.api.knowledge_bases.get_namespace_data", no_pipeline_status
    )
    manager = _manager(tmp_path)
    app = FastAPI()
    app.include_router(
        create_knowledge_base_routes(manager, admin_api_key="admin-secret")
    )
    client = TestClient(app)
    admin_headers = {
        "X-LightRAG-Admin-Key": "admin-secret",
        "Prefer": "wait=2",
    }

    created_response = client.post(
        "/knowledge-bases",
        json={"name": "Project B", "isolation_level": "logical"},
        headers=admin_headers,
    )
    assert created_response.status_code == 201
    assert created_response.json()["operation"]["state"] == "SUCCEEDED"
    knowledge_base_id = created_response.json()["knowledge_base"]["id"]
    assert (
        client.patch(
            f"/knowledge-bases/{knowledge_base_id}",
            json={"name": "Renamed"},
            headers=admin_headers,
        ).json()["name"]
        == "Renamed"
    )
    assert (
        client.delete(
            f"/knowledge-bases/{DEFAULT_KNOWLEDGE_BASE_ID}?confirm=true",
            headers=admin_headers,
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/knowledge-bases/{knowledge_base_id}", headers=admin_headers
        ).status_code
        == 400
    )
    deleted = client.delete(
        f"/knowledge-bases/{knowledge_base_id}?confirm=true",
        headers={**admin_headers, "Idempotency-Key": "delete-project-b"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["operation"]["state"] == "SUCCEEDED"
    assert deleted.json()["knowledge_base"]["lifecycle_state"] == "TOMBSTONED"
    assert client.get(f"/knowledge-bases/{knowledge_base_id}").status_code == 404


def test_management_mutation_requires_dedicated_admin_key(tmp_path: Path):
    manager = _manager(tmp_path)
    app = FastAPI()
    app.include_router(
        create_knowledge_base_routes(manager, admin_api_key="admin-secret")
    )
    client = TestClient(app)

    request = {"name": "Protected", "isolation_level": "logical"}
    assert client.post("/knowledge-bases", json=request).status_code == 403
    assert (
        client.post(
            "/knowledge-bases",
            json=request,
            headers={"X-LightRAG-Admin-Key": "wrong"},
        ).status_code
        == 403
    )
    accepted = client.post(
        "/knowledge-bases",
        json=request,
        headers={
            "X-LightRAG-Admin-Key": "admin-secret",
            "Idempotency-Key": "protected-create",
        },
    )
    assert accepted.status_code == 202
    assert accepted.headers["location"].endswith(
        accepted.json()["operation"]["operation_id"]
    )


def test_multi_mode_fails_closed_when_admin_key_is_unconfigured(tmp_path: Path):
    manager = _manager(tmp_path)
    app = FastAPI()
    app.include_router(create_knowledge_base_routes(manager))
    client = TestClient(app)

    assert (
        client.post(
            "/knowledge-bases",
            json={"name": "No admin boundary", "isolation_level": "logical"},
        ).status_code
        == 503
    )


@pytest.mark.asyncio
async def test_create_lifecycle_failure_never_enters_data_plane(
    tmp_path: Path,
) -> None:
    class FailingMigrationRag(_FakeRag):
        async def check_and_migrate_data(self) -> None:
            raise RuntimeError("simulated migration failure with private details")

    manager = _manager(
        tmp_path,
        rag_factory=lambda record, _profile: FailingMigrationRag(
            record.effective_workspace
        ),
    )
    record, operation, created = await manager.create_lifecycle(
        name="Will fail",
        isolation_level="logical",
        storage_profile_id=None,
        idempotency_key="failing-create",
    )
    assert created is True

    terminal = await manager.wait_for_operation(operation.operation_id, timeout=2)
    failed_record = await manager.catalog_provider.get_record(
        record.id, include_tombstoned=True
    )
    assert terminal.state.value == "FAILED"
    assert failed_record.lifecycle_state == "ERROR"
    assert failed_record.error_code == "workspace_initialization_failed"
    assert failed_record.error_message == "RuntimeError"
    assert "private details" not in failed_record.public_dict().values()
    with pytest.raises(KnowledgeBaseConflictError, match="ERROR"):
        await manager.get_context(record.id)


def test_legacy_mode_exposes_only_default_and_rejects_management_create(
    tmp_path: Path,
):
    manager = _manager(tmp_path, multi_workspace_enabled=False)
    hidden = manager.catalog.create(
        name="Hidden until multi mode",
        isolation_level="logical",
        storage_profile_id=None,
    )
    app = FastAPI()
    app.include_router(
        create_knowledge_base_routes(manager, admin_api_key="admin-secret")
    )
    client = TestClient(app)

    listed = client.get("/knowledge-bases")
    assert listed.status_code == 200
    assert [record["id"] for record in listed.json()["knowledge_bases"]] == [
        DEFAULT_KNOWLEDGE_BASE_ID
    ]
    assert client.get(f"/knowledge-bases/{hidden.id}").status_code == 404
    assert (
        client.post(
            "/knowledge-bases",
            json={"name": "Rejected", "isolation_level": "logical"},
            headers={"X-LightRAG-Admin-Key": "admin-secret"},
        ).status_code
        == 409
    )


@pytest.mark.asyncio
async def test_legacy_mode_rejects_non_default_context(tmp_path: Path):
    manager = _manager(tmp_path, multi_workspace_enabled=False)
    hidden = manager.catalog.create(
        name="Hidden until multi mode",
        isolation_level="logical",
        storage_profile_id=None,
    )

    default_context = await manager.get_context(None)
    assert default_context.metadata.id == DEFAULT_KNOWLEDGE_BASE_ID
    with pytest.raises(Exception, match="Multi-workspace mode is disabled"):
        await manager.get_context(hidden.id)


@pytest.mark.asyncio
async def test_side_effect_counters_expose_construction_init_and_migration(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    await manager.initialize()
    default_snapshot = manager.side_effect_counters.snapshot()
    assert default_snapshot == {
        "instance_constructions": 0,
        "storage_initializations": 2,
        "migrations": 1,
    }

    created = manager.create(
        name="Observable lifecycle",
        isolation_level="logical",
        storage_profile_id=None,
    )
    await manager.get_context(created.id)

    assert manager.side_effect_counters.snapshot() == {
        "instance_constructions": 1,
        "storage_initializations": 3,
        "migrations": 1,
    }


@pytest.mark.asyncio
async def test_data_plane_pool_initialization_does_not_run_first_access_migration(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    await manager.initialize()
    created = manager.create(
        name="Read-only load", isolation_level="logical", storage_profile_id=None
    )

    async with manager.bind_request(created.id) as execution:
        assert execution.binding.canonical_key == created.id
        assert manager.rag_proxy.workspace == created.effective_workspace

    assert manager.side_effect_counters.snapshot() == {
        "instance_constructions": 1,
        "storage_initializations": 3,
        "migrations": 1,
    }


@pytest.mark.asyncio
async def test_manager_pool_evicts_only_idle_workspace_and_never_overcommits(
    tmp_path: Path,
):
    manager = _manager(tmp_path, max_loaded_instances=2)
    await manager.initialize()
    first = manager.create(
        name="First pooled", isolation_level="logical", storage_profile_id=None
    )
    second = manager.create(
        name="Second pooled", isolation_level="logical", storage_profile_id=None
    )

    async with manager.bind_request(first.id) as first_context:
        with pytest.raises(Exception, match="no idle victim"):
            async with manager.bind_request(second.id):
                pass
        first_rag = first_context.rag

    async with manager.bind_request(second.id):
        pass
    assert first_rag.finalized is True
    pool = manager.instance_pool.peek()
    assert pool["loaded_entries"] == 2
    assert {entry["workspace_id"] for entry in pool["entries"]} == {
        "default",
        second.id,
    }


@pytest.mark.asyncio
async def test_observation_context_reports_unloaded_without_side_effects(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    await manager.initialize()
    record = manager.create(
        name="Observe only", isolation_level="logical", storage_profile_id=None
    )
    before = manager.side_effect_counters.snapshot()

    async with manager.bind_observation(record.id):
        assert manager.rag_proxy.workspace == record.effective_workspace
        assert manager.rag_proxy.runtime_state == "UNLOADED"

    assert manager.side_effect_counters.snapshot() == before
    assert manager.instance_pool.peek(record.id)["entries"] == []


@pytest.mark.asyncio
async def test_managed_background_handoff_holds_explicit_pool_lease(
    tmp_path: Path,
):
    manager = _manager(tmp_path)
    record = manager.create(
        name="Background handoff", isolation_level="logical", storage_profile_id=None
    )
    release_work = asyncio.Event()
    managed_tasks: set[asyncio.Task] = set()

    async def work(started: asyncio.Event) -> None:
        started.set()
        await release_work.wait()

    async def backstop_release() -> None:
        return None

    async with manager.bind_request(record.id):
        task = await start_reserved_background_task(
            managed_tasks,
            work=work,
            backstop_release=backstop_release,
        )

    entry = manager.instance_pool.peek(record.id)["entries"][0]
    assert entry["foreground_leases"] == 0
    assert entry["background_leases"] == 1
    with pytest.raises(Exception, match="active leases"):
        await manager.instance_pool.reserve_delete(record.id)

    release_work.set()
    await task
    assert manager.instance_pool.peek(record.id)["entries"][0]["background_leases"] == 0


@pytest.mark.asyncio
async def test_startup_reclaims_running_migration_and_fences_old_owner(
    tmp_path: Path,
):
    first = _manager(tmp_path)
    record = first.create(
        name="Interrupted migration", isolation_level="logical", storage_profile_id=None
    )
    migrating, operation, _ = first.catalog.create_migration_operation(
        workspace_id=record.id,
        idempotency_key="interrupted-migration",
        payload={"workspace_id": record.id},
    )
    old_claim = first.catalog.claim_operation(
        operation.operation_id, owner_id="dead-owner"
    )
    assert migrating.lifecycle_state == "MIGRATING"

    restarted = KnowledgeBaseManager(
        catalog=first.catalog,
        default_rag=_FakeRag("legacy"),
        default_document_manager=_document_manager(tmp_path / "inputs", "legacy"),
        rag_factory=lambda item, _profile: _FakeRag(item.effective_workspace),
        document_manager_factory=lambda item, _profile: _document_manager(
            tmp_path / "inputs", item.effective_workspace
        ),
    )
    await restarted.initialize()

    recovered = await restarted.catalog_provider.get_record(record.id)
    terminal = await restarted.catalog_provider.get_operation(operation.operation_id)
    assert recovered.lifecycle_state == "ACTIVE"
    assert terminal.state is CatalogOperationState.SUCCEEDED
    assert terminal.fencing_token > old_claim.fencing_token
    with pytest.raises(CatalogCASConflict, match="fenced out"):
        await restarted.catalog_provider.finish_operation(
            operation.operation_id,
            owner_id="dead-owner",
            fencing_token=old_claim.fencing_token,
            state=CatalogOperationState.SUCCEEDED,
        )


@pytest.mark.asyncio
async def test_startup_recovery_pages_all_active_and_isolates_bad_workspace(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("LIGHTRAG_RECOVERY_PAGE_SIZE", "1")
    recovered_rags: dict[str, _FakeRag] = {}

    class SelectiveMigrationRag(_FakeRag):
        async def check_and_migrate_data(self) -> None:
            if self.workspace.endswith("bad"):
                raise RuntimeError("simulated bad workspace")

    def rag_factory(record, _profile):
        rag = SelectiveMigrationRag(
            "tenant_bad" if record.name == "Bad" else record.effective_workspace
        )
        recovered_rags[record.id] = rag
        return rag

    manager = _manager(
        tmp_path,
        rag_factory=rag_factory,
    )
    good = manager.create(
        name="Good", isolation_level="logical", storage_profile_id=None
    )
    bad = manager.create(name="Bad", isolation_level="logical", storage_profile_id=None)

    await manager.initialize()

    good_record = await manager.catalog_provider.get_record(good.id)
    bad_record = await manager.catalog_provider.get_record(
        bad.id, include_tombstoned=True
    )
    report = manager.recovery_coordinator.last_report
    assert good_record.lifecycle_state == "ACTIVE"
    assert bad_record.lifecycle_state == "ERROR"
    assert report.migrations_started == 3  # default + both named records
    assert report.succeeded == 2
    assert report.failed == 1
    assert report.failures[0]["workspace_id"] == bad.id
    assert recovered_rags[good.id].pipeline_recoveries == 1
    assert recovered_rags[bad.id].pipeline_recoveries == 0


@pytest.mark.asyncio
async def test_delete_cleanup_journal_resumes_only_unfinished_resources(
    tmp_path: Path,
):
    drop_counts: dict[str, int] = {}
    created_rags: list[_FakeRag] = []

    class CountingStorage(_FakeStorage):
        def __init__(self, workspace: str, role: str) -> None:
            super().__init__(workspace)
            self.role = role

        async def drop(self) -> None:
            drop_counts[self.role] = drop_counts.get(self.role, 0) + 1
            if self.role == "full_docs" and drop_counts[self.role] == 1:
                raise RuntimeError("simulated partial drop")
            await super().drop()

    class CountingRag(_FakeRag):
        def __init__(self, workspace: str) -> None:
            super().__init__(workspace)
            for role in (
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
            ):
                setattr(self, role, CountingStorage(workspace, role))
            created_rags.append(self)

    manager = _manager(
        tmp_path,
        rag_factory=lambda record, _profile: CountingRag(record.effective_workspace),
    )
    record = manager.create(
        name="Resumable delete", isolation_level="logical", storage_profile_id=None
    )
    input_dir = Path(manager._document_manager_factory(record, None).input_dir)
    (input_dir / "keep-until-complete.txt").write_text("data", encoding="utf-8")
    _, operation, _ = await manager.catalog_provider.create_delete_operation(
        workspace_id=record.id,
        idempotency_key="resumable-delete",
        payload={"workspace_id": record.id},
    )

    await manager._run_delete_lifecycle(operation.operation_id)

    failed = await manager.catalog_provider.get_operation(operation.operation_id)
    failed_record = await manager.catalog_provider.get_record(
        record.id, include_tombstoned=True
    )
    assert failed.state is CatalogOperationState.FAILED
    assert failed_record.lifecycle_state == "ERROR"
    assert input_dir.exists()
    assert "storage:llm_response_cache" in failed.metadata["cleanup_completed"]
    assert "storage:full_docs" not in failed.metadata["cleanup_completed"]

    await manager._run_delete_lifecycle(operation.operation_id)

    terminal = await manager.catalog_provider.get_operation(operation.operation_id)
    tombstone = await manager.catalog_provider.get_record(
        record.id, include_tombstoned=True
    )
    assert terminal.state is CatalogOperationState.SUCCEEDED
    assert terminal.metadata["cleanup_complete"] is True
    assert tombstone.lifecycle_state == "TOMBSTONED"
    assert not input_dir.exists()
    assert drop_counts["full_docs"] == 2
    assert all(count == 1 for role, count in drop_counts.items() if role != "full_docs")
    assert len(created_rags) == 2
    with pytest.raises(CatalogCASConflict, match="fenced out"):
        await manager.catalog_provider.update_operation_metadata(
            operation.operation_id,
            owner_id=failed.owner_id or "",
            fencing_token=failed.fencing_token,
            metadata={"cleanup_complete": False},
        )


def test_pipeline_observation_returns_unloaded_without_constructing_instance(
    monkeypatch, tmp_path: Path
):
    from lightrag.exceptions import PipelineNotInitializedError

    manager = _manager(tmp_path)
    record = manager.create(
        name="Pipeline observation", isolation_level="logical", storage_profile_id=None
    )

    async def missing_pipeline_status(*_args, **_kwargs):
        raise PipelineNotInitializedError("missing:pipeline_status")

    monkeypatch.setattr(
        "lightrag.kg.shared_storage.get_namespace_data", missing_pipeline_status
    )
    app = FastAPI()
    app.include_router(
        create_document_routes(
            manager.rag_proxy,
            manager.document_manager_proxy,
            context_dependency=manager.request_dependency,
        )
    )
    before = manager.side_effect_counters.snapshot()

    response = TestClient(app).get(
        "/documents/pipeline_status",
        headers={KNOWLEDGE_BASE_HEADER: record.id},
    )

    assert response.status_code == 200
    assert response.json()["runtime_state"] == "UNLOADED"
    assert manager.side_effect_counters.snapshot() == before
    assert manager.instance_pool.peek(record.id)["entries"] == []


@pytest.mark.asyncio
async def test_delete_drops_all_storages_and_removes_catalog_record(
    monkeypatch, tmp_path: Path
):
    async def idle_pipeline_status(*_args, **_kwargs):
        return {"busy": False, "scanning": False, "pending_enqueues": 0}

    monkeypatch.setattr(
        "lightrag.api.knowledge_bases.get_namespace_data", idle_pipeline_status
    )
    manager = _manager(tmp_path)
    record = manager.create(
        name="Disposable", isolation_level="logical", storage_profile_id=None
    )
    context = await manager.get_context(record.id)
    input_dir = Path(context.document_manager.input_dir)
    (input_dir / "document.txt").write_text("content", encoding="utf-8")

    deleted = await manager.delete(record.id)

    assert deleted == record
    assert not input_dir.exists()
    assert context.rag.finalized is True
    assert all(
        storage.dropped
        for name, storage in vars(context.rag).items()
        if name != "workspace" and isinstance(storage, _FakeStorage)
    )
    with pytest.raises(Exception, match="does not exist"):
        manager.catalog.get(record.id)


@pytest.mark.asyncio
async def test_delete_refuses_storage_binding_mismatch_before_drop(
    monkeypatch, tmp_path: Path
):
    async def idle_pipeline_status(*_args, **_kwargs):
        return {"busy": False, "scanning": False, "pending_enqueues": 0}

    monkeypatch.setattr(
        "lightrag.api.knowledge_bases.get_namespace_data", idle_pipeline_status
    )
    manager = _manager(tmp_path)
    record = manager.create(
        name="Protected", isolation_level="logical", storage_profile_id=None
    )
    context = await manager.get_context(record.id)

    def reject_binding(*, stage: str):
        assert stage == "pre-delete"
        raise WorkspaceBindingError("simulated descriptor mismatch")

    context.rag.validate_storage_bindings = reject_binding

    with pytest.raises(KnowledgeBaseConflictError, match="cleanup was refused"):
        await manager.delete(record.id)

    assert manager.catalog.get(record.id) == record
    assert all(
        not storage.dropped
        for storage in vars(context.rag).values()
        if isinstance(storage, _FakeStorage)
    )


@pytest.mark.asyncio
async def test_concurrent_delete_is_rejected_after_reservation(
    monkeypatch, tmp_path: Path
):
    async def idle_pipeline_status(*_args, **_kwargs):
        return {"busy": False, "scanning": False, "pending_enqueues": 0}

    monkeypatch.setattr(
        "lightrag.api.knowledge_bases.get_namespace_data", idle_pipeline_status
    )
    manager = _manager(tmp_path)
    record = manager.create(
        name="Delete once", isolation_level="logical", storage_profile_id=None
    )
    context = await manager.get_context(record.id)
    started = asyncio.Event()
    release = asyncio.Event()
    original_drop = context.rag.llm_response_cache.drop

    async def slow_drop():
        started.set()
        await release.wait()
        await original_drop()

    context.rag.llm_response_cache.drop = slow_drop
    first_delete = asyncio.create_task(manager.delete(record.id))
    await started.wait()
    try:
        with pytest.raises(Exception, match="being deleted"):
            await manager.delete(record.id)
    finally:
        release.set()

    assert await first_delete == record


def test_physical_profile_is_strict_and_single_use(tmp_path: Path):
    profile = {
        "dedicated": True,
        "working_dir": str(tmp_path / "physical-rag"),
        "input_dir": str(tmp_path / "physical-inputs"),
        "postgres": {
            "host": "pg-a",
            "port": 5432,
            "user": "rag",
            "password": "secret",
            "database": "rag_a",
        },
        "neo4j": {
            "uri": "bolt://neo-a",
            "username": "neo4j",
            "password": "secret",
            "database": "neo4j",
        },
        "redis": {"uri": "redis://redis-a:6379/0"},
    }
    manager = _manager(tmp_path, profiles={"dedicated-a": profile})

    with pytest.raises(StorageProfileError, match="not configured"):
        manager.create(
            name="Missing",
            isolation_level="physical",
            storage_profile_id="missing",
        )

    record = manager.create(
        name="Physical",
        isolation_level="physical",
        storage_profile_id="dedicated-a",
    )
    assert record.storage_profile_id == "dedicated-a"

    with pytest.raises(StorageProfileError, match="already assigned"):
        manager.create(
            name="Cannot reuse",
            isolation_level="physical",
            storage_profile_id="dedicated-a",
        )


@pytest.mark.parametrize(
    ("implementation", "workspace_variable"),
    [
        ("PGKVStorage", "POSTGRES_WORKSPACE"),
        ("Neo4JStorage", "NEO4J_WORKSPACE"),
        ("RedisKVStorage", "REDIS_WORKSPACE"),
        ("MongoKVStorage", "MONGODB_WORKSPACE"),
        ("MilvusVectorDBStorage", "MILVUS_WORKSPACE"),
        ("QdrantVectorDBStorage", "QDRANT_WORKSPACE"),
        ("MemgraphStorage", "MEMGRAPH_WORKSPACE"),
        ("OpenSearchKVStorage", "OPENSEARCH_WORKSPACE"),
    ],
)
def test_forced_storage_workspace_rejects_dynamic_library(
    monkeypatch, tmp_path: Path, implementation: str, workspace_variable: str
):
    monkeypatch.setenv(workspace_variable, "forced")
    manager = _manager(tmp_path, active_storage_implementations=(implementation,) * 4)

    with pytest.raises(StorageProfileError, match=workspace_variable):
        manager.create(
            name="Unsafe", isolation_level="logical", storage_profile_id=None
        )


def test_physical_profile_requires_only_the_active_backend_sections(tmp_path: Path):
    profile = {
        "dedicated": True,
        "working_dir": str(tmp_path / "mongo-rag"),
        "input_dir": str(tmp_path / "mongo-inputs"),
        "mongo": {"uri": "mongodb://mongo-a:27017", "database": "rag_a"},
    }
    manager = _manager(
        tmp_path,
        profiles={"mongo-a": profile},
        active_storage_implementations=(
            "MongoKVStorage",
            "MongoVectorDBStorage",
            "MongoGraphStorage",
            "MongoDocStatusStorage",
        ),
    )

    record = manager.create(
        name="Mongo physical",
        isolation_level="physical",
        storage_profile_id="mongo-a",
    )

    assert record.storage_profile_id == "mongo-a"


def test_different_profile_ids_cannot_reuse_the_same_physical_resources(
    tmp_path: Path,
):
    first = {
        "dedicated": True,
        "working_dir": str(tmp_path / "first-rag"),
        "input_dir": str(tmp_path / "first-inputs"),
        "mongo": {"uri": "mongodb://user:one@mongo:27017", "database": "rag"},
    }
    second = {
        "dedicated": True,
        "working_dir": str(tmp_path / "second-rag"),
        "input_dir": str(tmp_path / "second-inputs"),
        "mongo": {"uri": "mongodb://other:two@mongo:27017", "database": "rag"},
    }
    manager = _manager(
        tmp_path,
        profiles={"first": first, "second": second},
        active_storage_implementations=("MongoKVStorage",) * 4,
    )
    manager.create(name="First", isolation_level="physical", storage_profile_id="first")

    with pytest.raises(StorageProfileError, match="mongo"):
        manager.create(
            name="Second", isolation_level="physical", storage_profile_id="second"
        )


def test_physical_profile_cannot_reuse_a_default_resource(tmp_path: Path):
    default_profile = {
        "working_dir": str(tmp_path / "default-rag"),
        "input_dir": str(tmp_path / "default-inputs"),
        "mongo": {"uri": "mongodb://default-mongo:27017", "database": "rag"},
    }
    candidate = {
        "dedicated": True,
        "working_dir": str(tmp_path / "physical-rag"),
        "input_dir": str(tmp_path / "physical-inputs"),
        "mongo": {
            "uri": "mongodb://other:secret@default-mongo:27017/",
            "database": "rag",
        },
    }
    manager = _manager(
        tmp_path,
        profiles={"candidate": candidate},
        default_storage_profile=default_profile,
        active_storage_implementations=("MongoKVStorage",) * 4,
    )

    assert manager.list_storage_profiles() == [
        {"id": "candidate", "available": False, "dedicated": True}
    ]

    with pytest.raises(StorageProfileError, match="default resources: mongo"):
        manager.create(
            name="Not dedicated",
            isolation_level="physical",
            storage_profile_id="candidate",
        )
