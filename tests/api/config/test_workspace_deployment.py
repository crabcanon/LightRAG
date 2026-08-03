"""Support-matrix tests for multi-workspace server deployment modes."""

import pytest

from lightrag.api.workspace_config import (
    CatalogProviderKind,
    CoordinatorProviderKind,
    MultiWorkspaceMode,
    WorkspaceDeploymentError,
    resolve_workspace_deployment,
)


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
        match="workers > 1 requires a shared durable catalog",
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
                "LIGHTRAG_KNOWLEDGE_BASE_CATALOG_PROVIDER": "postgres",
            },
            "PostgreSQL knowledge-base catalog provider is planned",
        ),
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
