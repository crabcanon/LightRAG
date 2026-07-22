"""Storage isolation capability and instance-scoped profile helpers.

The API server can keep several ``LightRAG`` instances alive in one process.
Physical knowledge bases therefore cannot switch connection settings through
``os.environ``: every backend must resolve its connection from the immutable
``global_config['storage_profile']`` attached to that instance.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class StorageIsolationCapability:
    """Isolation metadata for one selectable storage implementation."""

    profile_section: str | None
    workspace_environment_variable: str | None


STORAGE_ISOLATION_CAPABILITIES: dict[str, StorageIsolationCapability] = {
    # File-backed implementations use the profile's dedicated working_dir.
    "JsonKVStorage": StorageIsolationCapability(None, None),
    "JsonDocStatusStorage": StorageIsolationCapability(None, None),
    "NetworkXStorage": StorageIsolationCapability(None, None),
    "NanoVectorDBStorage": StorageIsolationCapability(None, None),
    "FaissVectorDBStorage": StorageIsolationCapability(None, None),
    # External services use an instance-scoped profile section.
    "RedisKVStorage": StorageIsolationCapability("redis", "REDIS_WORKSPACE"),
    "RedisDocStatusStorage": StorageIsolationCapability("redis", "REDIS_WORKSPACE"),
    "PGKVStorage": StorageIsolationCapability("postgres", "POSTGRES_WORKSPACE"),
    "PGVectorStorage": StorageIsolationCapability("postgres", "POSTGRES_WORKSPACE"),
    "PGGraphStorage": StorageIsolationCapability("postgres", "POSTGRES_WORKSPACE"),
    "PGDocStatusStorage": StorageIsolationCapability("postgres", "POSTGRES_WORKSPACE"),
    "Neo4JStorage": StorageIsolationCapability("neo4j", "NEO4J_WORKSPACE"),
    "MongoKVStorage": StorageIsolationCapability("mongo", "MONGODB_WORKSPACE"),
    "MongoDocStatusStorage": StorageIsolationCapability("mongo", "MONGODB_WORKSPACE"),
    "MongoGraphStorage": StorageIsolationCapability("mongo", "MONGODB_WORKSPACE"),
    "MongoVectorDBStorage": StorageIsolationCapability("mongo", "MONGODB_WORKSPACE"),
    "MilvusVectorDBStorage": StorageIsolationCapability("milvus", "MILVUS_WORKSPACE"),
    "QdrantVectorDBStorage": StorageIsolationCapability("qdrant", "QDRANT_WORKSPACE"),
    "MemgraphStorage": StorageIsolationCapability("memgraph", "MEMGRAPH_WORKSPACE"),
    "OpenSearchKVStorage": StorageIsolationCapability(
        "opensearch", "OPENSEARCH_WORKSPACE"
    ),
    "OpenSearchDocStatusStorage": StorageIsolationCapability(
        "opensearch", "OPENSEARCH_WORKSPACE"
    ),
    "OpenSearchGraphStorage": StorageIsolationCapability(
        "opensearch", "OPENSEARCH_WORKSPACE"
    ),
    "OpenSearchVectorDBStorage": StorageIsolationCapability(
        "opensearch", "OPENSEARCH_WORKSPACE"
    ),
}


PROFILE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "postgres": ("host", "port", "user", "password", "database"),
    "neo4j": ("uri", "username", "password", "database"),
    "redis": ("uri",),
    "mongo": ("uri", "database"),
    "milvus": ("uri", "db_name"),
    "qdrant": ("url", "collection_prefix"),
    "memgraph": ("uri", "database"),
    "opensearch": ("hosts", "index_prefix"),
}


PROFILE_RESOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    "postgres": ("host", "port", "database"),
    "neo4j": ("uri", "database"),
    "redis": ("uri",),
    "mongo": ("uri", "database"),
    "milvus": ("uri", "db_name"),
    # Redis logical DBs, Qdrant collections and OpenSearch indices are
    # namespaces inside one service, not strict physical resource boundaries.
    "qdrant": ("url",),
    "memgraph": ("uri", "database"),
    "opensearch": ("hosts",),
}


def get_storage_profile_section(
    global_config: Mapping[str, Any], section: str
) -> Mapping[str, Any]:
    """Return a backend profile section without falling back to global state."""

    profile = global_config.get("storage_profile") or {}
    if not isinstance(profile, Mapping):
        return {}
    value = profile.get(section) or {}
    return value if isinstance(value, Mapping) else {}


def resolve_workspace_override(
    global_config: Mapping[str, Any], section: str, environment_variable: str
) -> str | None:
    """Use legacy workspace overrides only when no physical profile is active."""

    if get_storage_profile_section(global_config, section):
        return None
    value = os.environ.get(environment_variable, "").strip()
    return value or None


def required_profile_sections(
    storage_implementations: Sequence[str],
) -> tuple[str, ...]:
    """Return deterministic profile sections required by active backends."""

    sections = {"working_dir", "input_dir"}
    for implementation in storage_implementations:
        try:
            capability = STORAGE_ISOLATION_CAPABILITIES[implementation]
        except KeyError as exc:
            raise ValueError(
                f"Storage implementation {implementation!r} has no isolation capability"
            ) from exc
        if capability.profile_section:
            sections.add(capability.profile_section)
    return tuple(sorted(sections))


def forced_workspace_variables(
    storage_implementations: Sequence[str],
) -> tuple[str, ...]:
    """Return workspace env overrides that can collapse the active backends."""

    variables: set[str] = set()
    for implementation in storage_implementations:
        try:
            variable = STORAGE_ISOLATION_CAPABILITIES[
                implementation
            ].workspace_environment_variable
        except KeyError as exc:
            raise ValueError(
                f"Storage implementation {implementation!r} has no isolation capability"
            ) from exc
        if variable:
            variables.add(variable)
    return tuple(sorted(variables))


def validate_storage_profile(
    profile_id: str, profile: Mapping[str, Any], required_sections: Sequence[str]
) -> None:
    """Validate the complete physical resource contract for active backends."""

    if profile.get("dedicated") is not True:
        raise ValueError(f"Storage profile {profile_id!r} must declare dedicated=true")
    for section in required_sections:
        value = profile.get(section)
        if section in {"working_dir", "input_dir"}:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Storage profile {profile_id!r} is missing {section!r}"
                )
            continue
        if not isinstance(value, Mapping):
            raise ValueError(
                f"Storage profile {profile_id!r} section {section!r} must be an object"
            )
        missing = [
            field
            for field in PROFILE_REQUIRED_FIELDS[section]
            if value.get(field) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"Storage profile {profile_id!r} section {section!r} is missing: "
                + ", ".join(missing)
            )


def _without_url_credentials(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc.lower(), path, "", ""))


def _resource_url(section: str, value: str) -> str:
    normalized = _without_url_credentials(value)
    if section != "redis":
        return normalized
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return normalized
    if not parsed.scheme or not parsed.netloc:
        return normalized
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _resource_host(value: Any) -> str:
    text = str(value).strip().lower().rstrip("/")
    try:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return text
    if not hostname:
        return text
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}" if port is not None else hostname


def profile_resource_fingerprints(
    profile: Mapping[str, Any], required_sections: Sequence[str]
) -> dict[str, str]:
    """Hash non-secret physical identities for reuse detection.

    Hashes are internal comparison values and are never included in API output.
    Credentials intentionally do not distinguish resources: changing a password
    must not make the same database or endpoint appear physically independent.
    """

    fingerprints: dict[str, str] = {}
    for section in required_sections:
        if section in {"working_dir", "input_dir"}:
            identity: Any = os.path.normcase(
                str(Path(str(profile[section])).expanduser().resolve())
            )
        else:
            config = profile[section]
            identity = {}
            for field in PROFILE_RESOURCE_FIELDS[section]:
                value = config[field]
                if field in {"uri", "url"} and isinstance(value, str):
                    value = _resource_url(section, value)
                if isinstance(value, list):
                    value = sorted(
                        _resource_host(item)
                        if field == "hosts"
                        else str(item).strip().lower()
                        for item in value
                    )
                elif field in {"host", "port"}:
                    value = str(value).strip().lower()
                identity[field] = value
        serialized = json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
        fingerprints[section] = hashlib.sha256(serialized).hexdigest()
    return fingerprints


def build_default_resource_profile(
    *,
    working_dir: str,
    input_dir: str,
    workspace: str,
    required_sections: Sequence[str],
) -> dict[str, Any]:
    """Resolve the default instance's non-secret physical resource identity.

    This mirrors each backend's environment/config.ini fallback sufficiently
    to reject a supposedly dedicated profile that points back at the running
    default resource. Credentials are intentionally omitted because they do
    not create a new physical boundary.
    """

    parser = configparser.ConfigParser()
    parser.read("config.ini", encoding="utf-8")

    def setting(
        environment_variable: str,
        section: str,
        option: str,
        fallback: Any = None,
    ) -> Any:
        return os.environ.get(
            environment_variable,
            parser.get(section, option, fallback=fallback),
        )

    profile: dict[str, Any] = {
        "working_dir": working_dir,
        "input_dir": input_dir,
    }
    for section in required_sections:
        if section in {"working_dir", "input_dir"}:
            continue
        if section == "postgres":
            profile[section] = {
                "host": setting("POSTGRES_HOST", "postgres", "host", "localhost"),
                "port": setting("POSTGRES_PORT", "postgres", "port", 5432),
                "database": setting(
                    "POSTGRES_DATABASE", "postgres", "database", "postgres"
                ),
            }
        elif section == "neo4j":
            profile[section] = {
                "uri": setting("NEO4J_URI", "neo4j", "uri"),
                "database": os.environ.get(
                    "NEO4J_DATABASE", re.sub(r"[^a-zA-Z0-9-]", "-", workspace)
                ),
            }
        elif section == "redis":
            profile[section] = {
                "uri": setting("REDIS_URI", "redis", "uri", "redis://localhost:6379")
            }
        elif section == "mongo":
            profile[section] = {
                "uri": setting(
                    "MONGO_URI",
                    "mongodb",
                    "uri",
                    "mongodb://root:root@localhost:27017/",
                ),
                "database": setting(
                    "MONGO_DATABASE", "mongodb", "database", "LightRAG"
                ),
            }
        elif section == "milvus":
            profile[section] = {
                "uri": setting(
                    "MILVUS_URI",
                    "milvus",
                    "uri",
                    str(Path(working_dir) / "milvus_lite.db"),
                ),
                "db_name": setting("MILVUS_DB_NAME", "milvus", "db_name"),
            }
        elif section == "qdrant":
            profile[section] = {"url": setting("QDRANT_URL", "qdrant", "uri")}
        elif section == "memgraph":
            profile[section] = {
                "uri": setting(
                    "MEMGRAPH_URI", "memgraph", "uri", "bolt://localhost:7687"
                ),
                "database": setting(
                    "MEMGRAPH_DATABASE", "memgraph", "database", "memgraph"
                ),
            }
        elif section == "opensearch":
            hosts = setting("OPENSEARCH_HOSTS", "opensearch", "hosts", "localhost:9200")
            profile[section] = {
                "hosts": (
                    [item.strip() for item in str(hosts).split(",") if item.strip()]
                    if isinstance(hosts, str)
                    else list(hosts)
                )
            }
    return profile
