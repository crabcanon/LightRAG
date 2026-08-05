from typing import TYPE_CHECKING, Any

from ._version import __version__ as __version__

__all__ = [
    "LightRAG",
    "QueryParam",
    "RoleLLMConfig",
    "RoleSpec",
    "ROLES",
    "WorkspaceBinding",
    "__version__",
]

if TYPE_CHECKING:
    from .lightrag import (
        LightRAG as LightRAG,
        QueryParam as QueryParam,
        ROLES as ROLES,
        RoleLLMConfig as RoleLLMConfig,
        RoleSpec as RoleSpec,
    )
    from .workspace import WorkspaceBinding as WorkspaceBinding


_LAZY_EXPORTS = {
    "LightRAG",
    "QueryParam",
    "RoleLLMConfig",
    "RoleSpec",
    "ROLES",
    "WorkspaceBinding",
}


def __getattr__(name: str) -> Any:
    if name == "WorkspaceBinding":
        from .workspace import WorkspaceBinding

        globals()[name] = WorkspaceBinding
        return WorkspaceBinding
    if name in _LAZY_EXPORTS:
        from .lightrag import LightRAG, QueryParam, RoleLLMConfig, RoleSpec, ROLES

        values = {
            "LightRAG": LightRAG,
            "QueryParam": QueryParam,
            "RoleLLMConfig": RoleLLMConfig,
            "RoleSpec": RoleSpec,
            "ROLES": ROLES,
        }
        value = values[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__author__ = "Zirui Guo"
__url__ = "https://github.com/HKUDS/LightRAG"
