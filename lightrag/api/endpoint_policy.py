"""Explicit endpoint policy registry for workspace lifecycle side effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi.routing import APIRoute


class EndpointPolicyError(RuntimeError):
    """Raised when a schema-visible API route has no endpoint policy."""


class EndpointPolicy(str, Enum):
    NON_WORKSPACE = "non_workspace"
    LIVENESS_VERSION = "liveness_version"
    CONTROL_PLANE_OBSERVATION = "control_plane_observation"
    WORKSPACE_MANAGEMENT_READ = "workspace_management_read"
    WORKSPACE_CREATE = "workspace_create"
    WORKSPACE_UPDATE = "workspace_update"
    WORKSPACE_DELETE = "workspace_delete"
    DATA_READ = "data_read"
    DATA_WRITE = "data_write"
    WORKSPACE_RUNTIME_OBSERVATION = "workspace_runtime_observation"


@dataclass(frozen=True, slots=True)
class EndpointPolicySpec:
    catalog_lookup: bool
    may_load_instance: bool
    may_create_namespace: bool
    may_migrate: bool


ENDPOINT_POLICY_SPECS: dict[EndpointPolicy, EndpointPolicySpec] = {
    EndpointPolicy.NON_WORKSPACE: EndpointPolicySpec(False, False, False, False),
    EndpointPolicy.LIVENESS_VERSION: EndpointPolicySpec(False, False, False, False),
    EndpointPolicy.CONTROL_PLANE_OBSERVATION: EndpointPolicySpec(
        True, False, False, False
    ),
    EndpointPolicy.WORKSPACE_MANAGEMENT_READ: EndpointPolicySpec(
        True, False, False, False
    ),
    EndpointPolicy.WORKSPACE_CREATE: EndpointPolicySpec(True, True, True, True),
    EndpointPolicy.WORKSPACE_UPDATE: EndpointPolicySpec(True, False, False, False),
    EndpointPolicy.WORKSPACE_DELETE: EndpointPolicySpec(True, True, False, False),
    EndpointPolicy.DATA_READ: EndpointPolicySpec(True, True, False, False),
    EndpointPolicy.DATA_WRITE: EndpointPolicySpec(True, True, False, False),
    EndpointPolicy.WORKSPACE_RUNTIME_OBSERVATION: EndpointPolicySpec(
        True, False, False, False
    ),
}


def _policies() -> dict[str, EndpointPolicy]:
    non_workspace = {
        "redirect_to_webui",
        "get_auth_status",
        "login",
        "webui_redirect_to_docs",
    }
    liveness = {"get_status", "get_version"}
    control_observation = {"get_tags", "get_running_models"}
    management_read = {"list_knowledge_bases", "get_knowledge_base"}
    data_read = {
        "get_scan_job_status",
        "list_source_conflicts",
        "documents",
        "get_track_status",
        "get_documents_paginated",
        "get_document_status_counts",
        "get_supported_file_types",
        "query_text",
        "query_text_stream",
        "query_data",
        "get_graph_labels",
        "get_popular_labels",
        "search_labels",
        "get_knowledge_graph",
        "check_entity_exists",
        "generate",
        "chat",
    }
    runtime_observation = {"get_pipeline_status"}
    data_write = {
        "scan_for_new_documents",
        "repair_source_conflict",
        "upload_to_input_dir",
        "insert_text",
        "insert_texts",
        "clear_documents",
        "delete_document",
        "clear_cache",
        "reprocess_failed_documents",
        "force_reset_recovery",
        "cancel_pipeline",
        "update_entity",
        "update_relation",
        "create_entity",
        "create_relation",
        "merge_entities",
        "delete_entity",
        "delete_relation",
    }

    result: dict[str, EndpointPolicy] = {}
    for names, policy in (
        (non_workspace, EndpointPolicy.NON_WORKSPACE),
        (liveness, EndpointPolicy.LIVENESS_VERSION),
        (control_observation, EndpointPolicy.CONTROL_PLANE_OBSERVATION),
        (management_read, EndpointPolicy.WORKSPACE_MANAGEMENT_READ),
        (data_read, EndpointPolicy.DATA_READ),
        (runtime_observation, EndpointPolicy.WORKSPACE_RUNTIME_OBSERVATION),
        (data_write, EndpointPolicy.DATA_WRITE),
    ):
        for name in names:
            if name in result:
                raise AssertionError(f"Duplicate endpoint policy for {name!r}")
            result[name] = policy
    result.update(
        {
            "create_knowledge_base": EndpointPolicy.WORKSPACE_CREATE,
            "update_knowledge_base": EndpointPolicy.WORKSPACE_UPDATE,
            "delete_knowledge_base": EndpointPolicy.WORKSPACE_DELETE,
        }
    )
    return result


ENDPOINT_POLICIES = _policies()


def validate_endpoint_policies(app: Any) -> dict[str, str]:
    """Fail startup when any schema-visible API route is unclassified.

    The returned snapshot is safe for diagnostics and tests.  It is keyed by
    ``METHOD path`` because one endpoint function can be registered for more
    than one path.
    """

    missing: list[str] = []
    snapshot: dict[str, str] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        policy = ENDPOINT_POLICIES.get(route.name)
        methods = sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
        if policy is None:
            missing.extend(f"{method} {route.path}" for method in methods)
            continue
        for method in methods:
            key = f"{method} {route.path}"
            if key in snapshot:
                raise EndpointPolicyError(
                    f"API endpoint policy key is registered more than once: {key}"
                )
            snapshot[key] = policy.value

    if missing:
        raise EndpointPolicyError(
            "Schema-visible API routes are missing endpoint policies: "
            + ", ".join(sorted(missing))
        )
    return snapshot
