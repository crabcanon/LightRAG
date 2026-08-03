"""Support-matrix tests for multi-workspace server deployment modes."""

from pathlib import Path
import sys

import pytest

from lightrag.api.workspace_config import (
    CatalogProviderKind,
    CoordinatorProviderKind,
    MultiWorkspaceMode,
    WorkspaceDeploymentError,
    resolve_workspace_deployment,
)
from lightrag.kg.storage_profiles import StorageWorkspaceConsistencyError


pytestmark = pytest.mark.offline


def test_default_deployment_preserves_legacy_mode() -> None:
    config = resolve_workspace_deployment(workers=4, environment={})

    assert config.mode is MultiWorkspaceMode.LEGACY
    assert config.catalog_provider is CatalogProviderKind.LOCAL
    assert config.coordinator_provider is CoordinatorProviderKind.LOCAL
    assert config.multi_workspace_enabled is False


def test_single_worker_local_multi_workspace_is_supported() -> None:
    config = resolve_workspace_deployment(
        workers=1,
        environment={
            "LIGHTRAG_MULTI_WORKSPACE_MODE": "multi",
            "LIGHTRAG_KNOWLEDGE_BASE_CATALOG_PROVIDER": "local",
            "LIGHTRAG_WORKSPACE_COORDINATOR_PROVIDER": "local",
        },
    )

    assert config.multi_workspace_enabled is True
    assert config.public_dict() == {
        "mode": "multi",
        "catalog_provider": "local",
        "coordinator_provider": "local",
        "workers": 1,
        "multi_workspace_enabled": True,
    }


def test_local_multi_workspace_fails_closed_with_multiple_workers() -> None:
    with pytest.raises(
        WorkspaceDeploymentError,
        match="workers > 1 requires the Phase 5 shared coordinator",
    ):
        resolve_workspace_deployment(
            workers=2,
            environment={"LIGHTRAG_MULTI_WORKSPACE_MODE": "multi"},
        )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (
            {
                "LIGHTRAG_MULTI_WORKSPACE_MODE": "multi",
                "LIGHTRAG_WORKSPACE_COORDINATOR_PROVIDER": "manager",
            },
            "coordinator provider 'manager' is planned",
        ),
        (
            {"LIGHTRAG_MULTI_WORKSPACE_MODE": "unexpected"},
            "LIGHTRAG_MULTI_WORKSPACE_MODE must be one of",
        ),
    ],
)
def test_unavailable_or_invalid_modes_fail_closed(
    environment: dict[str, str], message: str
) -> None:
    with pytest.raises(WorkspaceDeploymentError, match=message):
        resolve_workspace_deployment(workers=1, environment=environment)


def test_postgres_catalog_is_available_for_single_worker_multi_mode() -> None:
    config = resolve_workspace_deployment(
        workers=1,
        environment={
            "LIGHTRAG_MULTI_WORKSPACE_MODE": "multi",
            "LIGHTRAG_KNOWLEDGE_BASE_CATALOG_PROVIDER": "postgres",
        },
    )

    assert config.catalog_provider is CatalogProviderKind.POSTGRES
    assert config.coordinator_provider is CoordinatorProviderKind.LOCAL


def test_legacy_mode_rejects_stray_shared_provider_configuration() -> None:
    with pytest.raises(
        WorkspaceDeploymentError,
        match="Legacy mode supports only the local catalog provider",
    ):
        resolve_workspace_deployment(
            workers=1,
            environment={"LIGHTRAG_KNOWLEDGE_BASE_CATALOG_PROVIDER": "postgres"},
        )


def test_worker_count_must_be_positive() -> None:
    with pytest.raises(WorkspaceDeploymentError, match="workers must be at least 1"):
        resolve_workspace_deployment(workers=0, environment={})


def test_server_rejects_override_before_creating_catalog_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lightrag.api.config import parse_args

    original_argv = sys.argv[:]
    try:
        sys.argv = ["lightrag-server"]
        args = parse_args()
    finally:
        sys.argv = original_argv

    working_dir = tmp_path / "must-not-exist"
    args.working_dir = str(working_dir)
    args.input_dir = str(tmp_path / "inputs")
    args.kv_storage = "RedisKVStorage"
    args.workers = 1
    monkeypatch.setenv("LIGHTRAG_MULTI_WORKSPACE_MODE", "multi")
    monkeypatch.setenv("LIGHTRAG_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("REDIS_WORKSPACE", "collapsed")

    from lightrag.api.lightrag_server import create_app

    with pytest.raises(StorageWorkspaceConsistencyError, match="REDIS_WORKSPACE"):
        create_app(args)

    assert not (working_dir / "knowledge_bases.json").exists()
