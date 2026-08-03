"""Deployment-mode contract for the multi-workspace API server.

The local JSON catalog and in-process coordinator are intentionally supported
only by a single worker.  This module keeps that safety decision independent
from server construction so startup validation and tests use one rule.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import os
from typing import Mapping


class WorkspaceDeploymentError(ValueError):
    """Raised when the configured workspace deployment is unsupported."""


class MultiWorkspaceMode(str, Enum):
    LEGACY = "legacy"
    MULTI = "multi"


class CatalogProviderKind(str, Enum):
    LOCAL = "local"
    POSTGRES = "postgres"


class CoordinatorProviderKind(str, Enum):
    LOCAL = "local"
    MANAGER = "manager"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class WorkspaceDeploymentConfig:
    """Resolved, immutable support-matrix selection for one server process."""

    mode: MultiWorkspaceMode
    catalog_provider: CatalogProviderKind
    coordinator_provider: CoordinatorProviderKind
    workers: int

    @property
    def multi_workspace_enabled(self) -> bool:
        return self.mode is MultiWorkspaceMode.MULTI

    def public_dict(self) -> dict[str, str | int | bool]:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        payload["catalog_provider"] = self.catalog_provider.value
        payload["coordinator_provider"] = self.coordinator_provider.value
        payload["multi_workspace_enabled"] = self.multi_workspace_enabled
        return payload


def _enum_setting(
    environment: Mapping[str, str],
    name: str,
    enum_type: type[Enum],
    default: Enum,
) -> Enum:
    raw_value = environment.get(name, default.value).strip().lower()
    try:
        return enum_type(raw_value)
    except ValueError as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise WorkspaceDeploymentError(
            f"{name} must be one of: {choices}; got {raw_value!r}"
        ) from exc


def resolve_workspace_deployment(
    *,
    workers: int,
    environment: Mapping[str, str] | None = None,
) -> WorkspaceDeploymentConfig:
    """Resolve and validate the current multi-workspace support matrix.

    Phase 0 intentionally exposes only the combinations whose correctness can
    be proved by the current implementation.  Later phases add the PostgreSQL
    catalog and same-host coordinator behind the same contract.
    """

    if workers < 1:
        raise WorkspaceDeploymentError("workers must be at least 1")

    environ = os.environ if environment is None else environment
    mode = _enum_setting(
        environ,
        "LIGHTRAG_MULTI_WORKSPACE_MODE",
        MultiWorkspaceMode,
        MultiWorkspaceMode.LEGACY,
    )
    catalog_provider = _enum_setting(
        environ,
        "LIGHTRAG_KNOWLEDGE_BASE_CATALOG_PROVIDER",
        CatalogProviderKind,
        CatalogProviderKind.LOCAL,
    )
    coordinator_provider = _enum_setting(
        environ,
        "LIGHTRAG_WORKSPACE_COORDINATOR_PROVIDER",
        CoordinatorProviderKind,
        CoordinatorProviderKind.LOCAL,
    )

    config = WorkspaceDeploymentConfig(
        mode=mode,  # type: ignore[arg-type]
        catalog_provider=catalog_provider,  # type: ignore[arg-type]
        coordinator_provider=coordinator_provider,  # type: ignore[arg-type]
        workers=workers,
    )

    if not config.multi_workspace_enabled:
        if config.catalog_provider is not CatalogProviderKind.LOCAL:
            raise WorkspaceDeploymentError(
                "Legacy mode supports only the local catalog provider"
            )
        if config.coordinator_provider is not CoordinatorProviderKind.LOCAL:
            raise WorkspaceDeploymentError(
                "Legacy mode supports only the local coordinator provider"
            )
        return config

    if config.coordinator_provider is not CoordinatorProviderKind.LOCAL:
        raise WorkspaceDeploymentError(
            f"Workspace coordinator provider "
            f"{config.coordinator_provider.value!r} is planned but not available "
            "in this implementation phase"
        )
    if config.workers > 1:
        raise WorkspaceDeploymentError(
            "Multi-workspace mode with workers > 1 requires the Phase 5 shared "
            "coordinator and fault-test gate; the current provider combinations "
            "are supported only with workers=1"
        )
    return config
