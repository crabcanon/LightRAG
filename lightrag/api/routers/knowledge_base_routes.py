"""Knowledge-base management API routes."""

import os
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator

from lightrag.api.catalog import (
    CatalogCASConflict,
    CatalogError,
    CatalogIdempotencyConflict,
    CatalogOperationNotFound,
    CatalogOperationState,
    WorkspaceLifecycleState,
)
from lightrag.api.knowledge_bases import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeBaseConflictError,
    KnowledgeBaseError,
    KnowledgeBaseManager,
    KnowledgeBaseNotFoundError,
    StorageProfileError,
)
from lightrag.api.utils_api import get_combined_auth_dependency


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    isolation_level: Literal["logical", "physical"] = "logical"
    storage_profile_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_profile(self):
        if self.isolation_level == "physical" and not self.storage_profile_id:
            raise ValueError("Physical isolation requires storage_profile_id")
        if self.isolation_level == "logical" and self.storage_profile_id:
            raise ValueError("storage_profile_id is only valid for physical isolation")
        return self


class KnowledgeBaseUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CatalogOperationNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (CatalogCASConflict, CatalogIdempotencyConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, KnowledgeBaseNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, KnowledgeBaseConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StorageProfileError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (KnowledgeBaseError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, CatalogError):
        return HTTPException(status_code=503, detail="Catalog operation failed")
    return HTTPException(status_code=500, detail="Knowledge-base operation failed")


def _admin_dependency(admin_api_key: str | None, *, required: bool):
    async def require_admin(
        provided: str | None = Header(
            default=None,
            alias="X-LightRAG-Admin-Key",
            description="Dedicated key required for knowledge-base mutations",
        ),
    ) -> None:
        if not admin_api_key:
            if required:
                raise HTTPException(
                    status_code=503,
                    detail="Knowledge-base admin API key is not configured",
                )
            return
        if provided is None or not secrets.compare_digest(provided, admin_api_key):
            raise HTTPException(status_code=403, detail="Invalid admin API key")

    return require_admin


def _prefer_wait_seconds(prefer: str | None) -> float:
    if not prefer:
        return 0
    configured_max = float(
        os.getenv("LIGHTRAG_LIFECYCLE_MAX_WAIT_SECONDS", "10") or "10"
    )
    configured_max = max(0.0, min(configured_max, 60.0))
    for item in prefer.split(","):
        name, separator, value = item.strip().partition("=")
        if name.lower() != "wait" or not separator:
            continue
        try:
            requested = float(value.strip().strip('"'))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Prefer wait must be a non-negative number"
            ) from exc
        if requested < 0:
            raise HTTPException(
                status_code=400, detail="Prefer wait must be a non-negative number"
            )
        return min(requested, configured_max)
    return 0


def create_knowledge_base_routes(
    manager: KnowledgeBaseManager,
    api_key: str | None = None,
    admin_api_key: str | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
    combined_auth = get_combined_auth_dependency(api_key)
    authenticated = [Depends(combined_auth)]
    admin_auth = _admin_dependency(
        admin_api_key, required=manager.multi_workspace_enabled
    )
    admin_authenticated = [Depends(combined_auth), Depends(admin_auth)]

    @router.get("", dependencies=authenticated)
    async def list_knowledge_bases(
        limit: int = Query(default=100, ge=1, le=1000),
        cursor: str | None = Query(default=None, max_length=64),
        state: list[WorkspaceLifecycleState] | None = Query(default=None),
    ):
        page = await manager.alist_record_page(limit=limit, cursor=cursor, states=state)
        return {
            "default_id": DEFAULT_KNOWLEDGE_BASE_ID,
            "knowledge_bases": [record.public_dict() for record in page.records],
            "next_cursor": page.next_cursor,
            "storage_profiles": manager.list_storage_profiles(),
            "multi_workspace_enabled": manager.multi_workspace_enabled,
            "admin_key_required": manager.multi_workspace_enabled
            and bool(admin_api_key),
        }

    @router.post("", status_code=202, dependencies=admin_authenticated)
    async def create_knowledge_base(
        request: KnowledgeBaseCreateRequest,
        response: Response,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", max_length=128
        ),
        prefer: str | None = Header(default=None, alias="Prefer"),
    ):
        try:
            record, operation, created = await manager.create_lifecycle(
                name=request.name,
                isolation_level=request.isolation_level,
                storage_profile_id=request.storage_profile_id,
                idempotency_key=idempotency_key,
            )
            wait_seconds = _prefer_wait_seconds(prefer)
            if wait_seconds:
                operation = await manager.wait_for_operation(
                    operation.operation_id, timeout=wait_seconds
                )
                record = await manager.catalog_provider.get_record(
                    operation.workspace_id, include_tombstoned=True
                )
                if operation.state is CatalogOperationState.SUCCEEDED:
                    response.status_code = 201
                elif operation.state is CatalogOperationState.FAILED:
                    response.status_code = 500
            response.headers["Location"] = (
                f"/knowledge-bases/operations/{operation.operation_id}"
            )
            if operation.state not in {
                CatalogOperationState.SUCCEEDED,
                CatalogOperationState.FAILED,
            }:
                response.headers["Retry-After"] = "1"
            return {
                "created": created,
                "knowledge_base": record.public_dict(),
                "operation": operation.public_dict(),
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/operations/{operation_id}", dependencies=authenticated)
    async def get_knowledge_base_operation(operation_id: str):
        try:
            operation = await manager.get_operation(operation_id)
            record = await manager.catalog_provider.get_record(
                operation.workspace_id, include_tombstoned=True
            )
            return {
                "knowledge_base": record.public_dict(),
                "operation": operation.public_dict(),
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/{knowledge_base_id}", dependencies=authenticated)
    async def get_knowledge_base(knowledge_base_id: str):
        try:
            return (await manager.aget_record(knowledge_base_id)).public_dict()
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.patch("/{knowledge_base_id}", dependencies=admin_authenticated)
    async def update_knowledge_base(
        knowledge_base_id: str, request: KnowledgeBaseUpdateRequest
    ):
        try:
            return (
                await manager.arename(knowledge_base_id, request.name)
            ).public_dict()
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.delete(
        "/{knowledge_base_id}", status_code=202, dependencies=admin_authenticated
    )
    async def delete_knowledge_base(
        knowledge_base_id: str,
        response: Response,
        confirm: bool = Query(
            False,
            description="Must be true because deletion drops all isolated data",
        ),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", max_length=128
        ),
        prefer: str | None = Header(default=None, alias="Prefer"),
    ):
        if not confirm:
            raise HTTPException(
                status_code=400,
                detail="Set confirm=true to delete a knowledge base and all its data",
            )
        try:
            record, operation, created = await manager.delete_lifecycle(
                knowledge_base_id, idempotency_key=idempotency_key
            )
            wait_seconds = _prefer_wait_seconds(prefer)
            if wait_seconds:
                operation = await manager.wait_for_operation(
                    operation.operation_id, timeout=wait_seconds
                )
                record = await manager.catalog_provider.get_record(
                    operation.workspace_id, include_tombstoned=True
                )
                if operation.state is CatalogOperationState.SUCCEEDED:
                    response.status_code = 200
                elif operation.state is CatalogOperationState.FAILED:
                    response.status_code = 500
            response.headers["Location"] = (
                f"/knowledge-bases/operations/{operation.operation_id}"
            )
            if operation.state not in {
                CatalogOperationState.SUCCEEDED,
                CatalogOperationState.FAILED,
            }:
                response.headers["Retry-After"] = "1"
            return {
                "created": created,
                "knowledge_base": record.public_dict(),
                "operation": operation.public_dict(),
            }
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
