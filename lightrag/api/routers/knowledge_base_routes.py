"""Knowledge-base management API routes."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

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
    if isinstance(exc, KnowledgeBaseNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, KnowledgeBaseConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, StorageProfileError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (KnowledgeBaseError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Knowledge-base operation failed")


def create_knowledge_base_routes(
    manager: KnowledgeBaseManager, api_key: str | None = None
) -> APIRouter:
    router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])
    combined_auth = get_combined_auth_dependency(api_key)
    authenticated = [Depends(combined_auth)]

    @router.get("", dependencies=authenticated)
    async def list_knowledge_bases():
        return {
            "default_id": DEFAULT_KNOWLEDGE_BASE_ID,
            "knowledge_bases": [
                record.public_dict() for record in manager.catalog.list()
            ],
            "storage_profiles": manager.list_storage_profiles(),
        }

    @router.post("", status_code=201, dependencies=authenticated)
    async def create_knowledge_base(request: KnowledgeBaseCreateRequest):
        try:
            record = manager.create(
                name=request.name,
                isolation_level=request.isolation_level,
                storage_profile_id=request.storage_profile_id,
            )
            return record.public_dict()
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/{knowledge_base_id}", dependencies=authenticated)
    async def get_knowledge_base(knowledge_base_id: str):
        try:
            return manager.catalog.get(knowledge_base_id).public_dict()
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.patch("/{knowledge_base_id}", dependencies=authenticated)
    async def update_knowledge_base(
        knowledge_base_id: str, request: KnowledgeBaseUpdateRequest
    ):
        try:
            return manager.catalog.rename(knowledge_base_id, request.name).public_dict()
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.delete("/{knowledge_base_id}", dependencies=authenticated)
    async def delete_knowledge_base(
        knowledge_base_id: str,
        confirm: bool = Query(
            False,
            description="Must be true because deletion drops all isolated data",
        ),
    ):
        if not confirm:
            raise HTTPException(
                status_code=400,
                detail="Set confirm=true to delete a knowledge base and all its data",
            )
        try:
            record = await manager.delete(knowledge_base_id)
            return {"status": "deleted", "knowledge_base": record.public_dict()}
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
