"""Fail-closed tests for the workspace endpoint policy registry."""

from fastapi import FastAPI
import pytest

from lightrag.api.endpoint_policy import (
    ENDPOINT_POLICY_SPECS,
    EndpointPolicy,
    EndpointPolicyError,
    validate_endpoint_policies,
)


pytestmark = pytest.mark.offline


def test_liveness_policy_forbids_workspace_side_effects() -> None:
    spec = ENDPOINT_POLICY_SPECS[EndpointPolicy.LIVENESS_VERSION]

    assert spec.catalog_lookup is False
    assert spec.may_load_instance is False
    assert spec.may_create_namespace is False
    assert spec.may_migrate is False


def test_data_routes_may_load_but_never_create_or_migrate() -> None:
    for policy in (EndpointPolicy.DATA_READ, EndpointPolicy.DATA_WRITE):
        spec = ENDPOINT_POLICY_SPECS[policy]
        assert spec.catalog_lookup is True
        assert spec.may_load_instance is True
        assert spec.may_create_namespace is False
        assert spec.may_migrate is False


def test_known_schema_route_gets_diagnostic_snapshot() -> None:
    app = FastAPI()

    @app.get("/health")
    async def get_status():
        return {"status": "healthy"}

    assert validate_endpoint_policies(app) == {
        "GET /health": EndpointPolicy.LIVENESS_VERSION.value
    }


def test_new_unclassified_route_fails_closed() -> None:
    app = FastAPI()

    @app.get("/future-workspace-route")
    async def future_workspace_route():
        return {}

    with pytest.raises(
        EndpointPolicyError,
        match="GET /future-workspace-route",
    ):
        validate_endpoint_policies(app)


def test_routes_excluded_from_openapi_do_not_need_data_plane_policy() -> None:
    app = FastAPI()

    @app.get("/internal-docs", include_in_schema=False)
    async def internal_docs():
        return {}

    assert validate_endpoint_policies(app) == {}
