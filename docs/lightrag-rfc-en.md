# RFC: Safe Multi-Workspace Architecture for LightRAG

- Status: Draft for community discussion
- Author: @crabcanon
- Related discussion: [Issue #2527](https://github.com/HKUDS/LightRAG/issues/2527)
- Related prior implementation review: [PR #3397](https://github.com/HKUDS/LightRAG/pull/3397)
- Last updated: 2026-07-29

## 1. Summary

This RFC proposes an opt-in multi-workspace server architecture for LightRAG. It is intentionally design-first: it defines usage scenarios, safety invariants, lifecycle rules, resource limits, compatibility behavior, and a phased delivery plan before implementation PRs are opened.

This revision incorporates the follow-up review focus on storage-level effective-workspace consistency (especially doc-status), legacy-default representation, side-effect-free endpoint behavior, migration ownership, and deployment-wide provider limits.

The central decisions are:

1. A `LightRAG` instance is permanently bound to one immutable canonical workspace key. It never switches workspaces during its lifetime.
2. A workspace is created only by an authenticated management operation. A data-plane selector never creates a workspace.
3. A durable shared workspace catalog is the control-plane source of truth. Each worker has a bounded local instance pool, but no worker-local catalog is authoritative.
4. Workspace identity is resolved once in the server/core layer and passed unchanged to every KV, vector, graph, and doc-status storage. Storage backends cannot independently override it in multi-workspace mode.
5. All storage objects must prove that they use the expected canonical workspace key and namespace-encoding policy before migration or data access begins. A mismatch is a hard initialization failure.
6. The unnamed legacy workspace remains usable without moving data. It receives an unambiguous tagged canonical identity, while a versioned compatibility codec preserves historical backend-specific physical names. Reserved names prevent a new workspace from colliding with legacy data.
7. Request, streaming, pipeline, scan, recovery, destructive, and background work hold an explicit workspace context and a lifetime lease. Missing context fails closed; it never falls back to a process-global default.
8. Provider concurrency is governed by shared service-level LLM, embedding, and reranker admission controllers. Creating N workspace instances does not create N independent concurrency budgets.
9. Health and readiness endpoints are side-effect free. Storage migration never runs on an arbitrary first data-plane request.
10. The first implementation phase is logical isolation, catalog, bounded instance pooling, request routing, and the minimum pipeline/resource changes required to make that path safe. Strict physical isolation and WebUI support follow in later phases.

## 2. Motivation and problem statement

LightRAG already accepts a `workspace` value and many storage backends implement a form of namespacing. That is sufficient for separately constructed single-workspace instances, but it is not yet a complete multi-workspace server contract.

The main failure modes are silent rather than immediate:

- A backend-specific environment variable can override the workspace selected by the server and collapse multiple instances into one namespace.
- A mixed backend configuration can resolve KV, vector, graph, and doc-status to different workspaces.
- `doc_status` is not merely metadata. It is the durable ingestion work queue used for PENDING/PROCESSING discovery, `track_id` lookup, and restart recovery. Since document IDs are content hashes, the same document in two workspaces can have the same ID. A collapsed doc-status namespace can therefore overwrite one workspace's queue record with another's.
- Empty/default workspace representations differ by backend. PostgreSQL historically uses `default`, Redis has used an unprefixed namespace and `_` in some workspace fields, while file storage uses the working-directory root. A literal workspace named `default` can collide with unnamed legacy data in PostgreSQL.
- Lazy instance creation can make an unknown or mistyped selector allocate resources, migrate data, or create a new namespace.
- Per-instance LLM and embedding wrappers multiply configured concurrency by the number of active workspaces.
- Worker-local catalogs and locks are insufficient under Gunicorn and leave no safe path to a future multi-node deployment.

This RFC treats workspace isolation as an end-to-end invariant spanning routing, object lifetime, all four storage families, pipeline state, resource scheduling, and recovery.

## 3. Terminology

| Term | Meaning |
| --- | --- |
| Knowledge-base ID | Stable public selector used by REST, Ollama aliases, and management APIs. It is opaque and is not a storage name. |
| Display name | User-editable label. It is never interpolated into a table, collection, index, graph, key prefix, or directory. |
| Canonical workspace key | Immutable internal identity resolved by the control plane and passed to all four storage families. It is a tagged value, not an ambiguous raw string. |
| Effective workspace | The canonical workspace key actually bound to a `LightRAG` instance and its storage objects. In this RFC, “effective” always refers to the canonical key, not a backend-specific string. |
| Physical namespace | Backend-specific table filter, collection/index name, graph label/property, key prefix, or directory derived by a versioned namespace codec. |
| Legacy default | The existing unnamed/single-workspace data namespace. Its canonical key is a reserved tagged value, not the user string `default`. |
| Storage profile | Immutable reference to storage connection/resources. In the first phase it is the only per-workspace configuration override. It does not choose workspace identity. |
| Instance lease | Reference held for the full lifetime of a request, stream, or background task. A leased instance cannot be finalized or evicted. |
| Coordination provider | Interface for leases, mutual exclusion, admission, fencing, and shared state. Implementations may be local, same-host multi-process, or external. |

## 4. Goals

- Prevent cross-workspace reads, writes, queue corruption, cache reuse, graph mutation, and background-task leakage.
- Preserve existing single-workspace behavior, including installations where `WORKSPACE` is unset or empty.
- Make unknown-workspace handling explicit and fail closed.
- Bound memory, connections, active pipelines, and model-provider concurrency.
- Support multiple Gunicorn workers without sticky-session assumptions.
- Define coordination contracts that can later be backed by an external service for multi-node deployment.
- Keep the initial PRs small enough to review independently.
- Provide deterministic tests for isolation, races, recovery, fairness, and compatibility.

## 5. Non-goals

- Per-workspace authentication or ACLs in phase one. Isolation is not authorization.
- Runtime mutation of an instance's workspace.
- Per-workspace LLM, embedding, parser, prompt, chunking, or scheduler configuration in phase one.
- Strict physical isolation for every backend in the first core PR.
- A complete multi-node implementation in the first phase.
- Re-embedding, copying, or renaming existing default-workspace data as part of enabling the feature.
- Unrelated provider, HTTP transport, parser, or storage performance changes.

## 6. Usage scenarios

### 6.1 Existing server with no workspace configured

An existing deployment has no `WORKSPACE` value and clients send no selector header. After upgrade:

- the public `default` catalog record maps to the tagged legacy-default key;
- the same historical physical data is used without movement or re-embedding;
- no-header REST and Ollama requests continue to work;
- the server does not reject an empty legacy server configuration;
- a present-but-empty selector is still invalid, because it is not the same as an absent selector.

### 6.2 Existing server with a configured workspace

An existing deployment uses a non-empty server-level `WORKSPACE`. The bootstrap catalog record maps public ID `default` to that exact canonical named workspace. Existing clients still omit the selector. Startup fails rather than silently remapping if persisted catalog metadata later disagrees with the configured legacy value.

### 6.3 Explicit creation

An authenticated administrator creates a workspace with a display name and optional storage-profile reference. The server allocates an opaque portable ID and canonical key, validates the full storage contract, runs required initialization/migration under a lifecycle lease, and marks the record ACTIVE only after success.

### 6.4 Mistyped selector

A client sends a syntactically valid but unknown ID to an upload or query endpoint. The server returns `404`; it does not add a catalog record, instantiate storage, create a directory/collection, or run migration.

### 6.5 Same document in two workspaces

Two workspaces upload identical content and therefore compute the same document ID. Each workspace has an independent doc-status record and pipeline lifecycle. Processing, deletion, retry, and restart recovery in one workspace cannot observe or overwrite the other record.

### 6.6 Parallel ingestion

Two workspaces may ingest concurrently if global pipeline and provider admission allow it. A bulk job in one workspace cannot consume all scheduling opportunities indefinitely; waiting workspaces receive fair turns with priority aging.

### 6.7 Worker loss and full restart

After a Gunicorn worker exits or the whole service restarts, a bounded recovery coordinator enumerates ACTIVE catalog records, examines each workspace's own doc-status queue, fences stale owners, and requeues recoverable work without needing a user to touch each workspace first.

### 6.8 Pool pressure

When a worker reaches its instance/connection budget, it evicts only a safe idle entry. If there is no safe victim, the request receives bounded backpressure (`503` with retry guidance); the server never finalizes an instance with in-flight or background work.

### 6.9 Ollama client without custom headers

An Ollama-compatible client selects a workspace through a model alias. It does not need custom-header support. Model and header selectors, if both supplied, must resolve to the same catalog record.

## 7. Normative safety invariants

The implementation must maintain all of the following invariants:

1. **Fixed binding:** one live `LightRAG` instance has exactly one immutable canonical workspace key.
2. **Single resolution:** workspace identity is resolved once outside storage backends.
3. **Four-family agreement:** every KV, vector, graph, and doc-status object attached to an instance reports the same canonical workspace key and expected namespace-encoding version.
4. **No storage override in multi-workspace mode:** backend environment/config overrides cannot replace the instance key.
5. **Explicit creation:** only the management plane creates catalog records or storage namespaces.
6. **Fail-closed context:** a multi-workspace data or background operation without a concrete workspace context is rejected; no proxy falls back to default.
7. **Lease before use:** every request, stream, pipeline, scan, migration, recovery, deletion, and spawned task owns a lease until all workspace-bound work is complete.
8. **No unsafe eviction/deletion:** a leased, busy, migrating, deleting, or recoverable instance cannot be evicted or finalized.
9. **Shared limits:** configured provider concurrency is a service budget, not an instance budget.
10. **Side-effect-free observation:** liveness/readiness and pool/catalog inspection cannot instantiate or migrate a workspace.
11. **No first-request migration:** a data-plane request never becomes the migration owner.
12. **No namespace reuse:** deleted/tombstoned IDs and physical namespace identities are not silently reused.

Any failure to prove an invariant during creation or initialization is a hard error. The implementation must not “repair” a mismatch by choosing one backend's result.

## 8. Workspace identity and validation

### 8.1 Separate public names from storage identity

The catalog stores at least:

```text
knowledge_base_id       opaque public ID, for example kb_7f3a9c2d1e04
display_name            user-editable Unicode label
workspace_key           tagged immutable canonical identity
namespace_codec_version immutable encoding policy
storage_profile_id      immutable optional reference
lifecycle_state         CREATING | MIGRATING | ACTIVE | DELETING | ERROR | TOMBSTONED
schema_version           storage migration version
revision                 compare-and-swap revision
created_at / updated_at
```

The generated ID and canonical named key use a conservative backend-portable alphabet such as lowercase ASCII letters, digits, and underscore. They have a bounded length chosen for the strictest supported backend after namespace suffixes are included. User display names are not subject to storage naming rules because they never become physical identifiers.

### 8.2 Reserved identities

The following values are reserved and cannot be allocated as a new named workspace key: empty string, `default`, `_`, any legacy-default sentinel spelling, and internal prefixes used by the namespace codec. The public catalog ID `default` is reserved exclusively for the compatibility record.

A user may use “Default” as a display label because display labels are not namespace identifiers.

### 8.3 Missing, empty, invalid, and unknown selectors

These cases have distinct semantics:

| Selector state | Result |
| --- | --- |
| Header omitted | Select the reserved public `default` record. |
| Header present but empty/whitespace | `400 Bad Request`; do not fall back. |
| Invalid syntax/length | `400 Bad Request`. |
| Valid but absent from catalog | `404 Not Found`; do not create or instantiate. |
| Record not ACTIVE | `409` for lifecycle conflict or `503` for temporary migration/recovery unavailability, with a stable error code. |

Framework parameter parsing must preserve the distinction between an absent header and a present empty value.

## 9. One effective-workspace rule for all storage families

### 9.1 Central resolver

Before constructing storage objects, the server creates an immutable `WorkspaceBinding` containing:

```text
catalog_id
catalog_revision
canonical_workspace_key
namespace_codec_version
storage_profile_id
server_mode
```

Every storage constructor receives this binding or the canonical key derived from it. A backend may encode the key for its own physical namespace, but it cannot choose a different key.

No KV, vector, graph, or doc-status backend reads a `*_WORKSPACE` variable while constructing a multi-workspace instance. Connection settings may still come from global configuration or the selected storage profile; logical workspace identity may not.

### 9.2 Precedence and legacy override variables

The legacy variables are:

```text
POSTGRES_WORKSPACE
MONGODB_WORKSPACE
REDIS_WORKSPACE
NEO4J_WORKSPACE
MILVUS_WORKSPACE
QDRANT_WORKSPACE
MEMGRAPH_WORKSPACE
OPENSEARCH_WORKSPACE
```

Their semantics are mode-dependent:

| Mode | Resolution rule |
| --- | --- |
| Legacy single-workspace mode | Preserve historical precedence for compatibility, then run the four-family consistency check. A consistent existing configuration continues to work. An inconsistent configuration fails with diagnostics because silently splitting doc-status from data storage is unsafe. |
| Multi-workspace mode | The catalog/instance canonical key always wins. If any workspace override applicable to an active backend is non-empty, startup fails and lists every conflicting variable/config field. The values are not silently ignored. |

The same prohibition applies to equivalent `config.ini` fields and to storage-profile fields that attempt to supply logical workspace identity. A storage profile may select resources and connection settings, but not the canonical workspace.

An explicit feature switch enables multi-workspace mode. The server must not infer the mode from a request header, because inference would make a typo change configuration semantics.

### 9.3 Creation-time consistency check

Each storage object implements a common, side-effect-free namespace descriptor:

```text
storage_family          kv | vector | graph | doc_status
storage_role            full_docs | text_chunks | llm_cache | entities | ...
implementation
canonical_workspace_key
namespace_codec_version
physical_namespace_fingerprint   non-secret diagnostic value
```

Initialization uses three gates:

1. **Preflight:** validate mode, override variables, catalog record, storage profile, reserved names, and expected namespace codec before opening data-plane access.
2. **Construction check:** inspect every storage object, not one representative per family. All canonical keys and codec versions must equal the immutable binding, and every derived physical namespace must match the backend's registered codec for its storage role.
3. **Post-connect check:** after backend connection initialization, verify any server-resolved database/schema/collection information still matches the descriptor. This catches clients that apply a connection-level workspace after construction.

Migration and pipeline initialization occur only after all checks pass. A doc-status mismatch is never downgraded to a warning in multi-workspace mode.

The validation error names the catalog ID, expected canonical key, storage family/role, implementation, override source, and a redacted physical fingerprint. It must not print credentials or connection URIs.

### 9.4 Canonical default versus physical compatibility

“One effective workspace” means one canonical identity, not that every backend must use the same literal table or key syntax.

The unnamed legacy workspace is represented internally by a tagged value such as `LegacyDefault`, which cannot equal the named string `default`. All storage objects expose that same canonical value. A versioned `legacy-v1` namespace codec maps it to the backend's historical physical layout:

- PostgreSQL may retain its historical `default` workspace value;
- Redis may retain its historical unprefixed keys;
- file stores may retain files directly under the working directory;
- other backends retain their already deployed legacy representation.

This compatibility mapping is explicit metadata, not each backend independently inventing a fallback. It allows zero-copy upgrade while making the core identity unambiguous. New named workspaces use an encoded `namespace-v1` policy and cannot use reserved legacy aliases. Consequently a literal new workspace named `default` cannot collide with unnamed PostgreSQL data.

The catalog persists the codec version. All four storage families for a workspace must use the same codec policy generation. Changing it is a future explicit data-migration operation, never a configuration side effect.

## 10. Catalog and lifecycle

### 10.1 Shared durable source of truth

The workspace catalog must be visible to all workers and survive restart. It provides atomic create-if-absent and revision-based compare-and-swap updates. A JSON file plus per-process locks is not sufficient for Gunicorn because each worker can hold a stale snapshot and overwrite another worker's management change.

A local catalog implementation may be supported only for an explicitly single-worker development mode. Startup with `workers > 1` requires a shared catalog provider.

Worker memory may cache catalog records, but cache entries carry revisions and are invalidated/refetched on mismatch. No request depends on sticky routing to the worker that created a workspace.

### 10.2 Explicit lifecycle

```text
ABSENT
  -> CREATING
  -> MIGRATING
  -> ACTIVE
  -> DELETING
  -> TOMBSTONED

CREATING or MIGRATING or DELETING
  -> ERROR (on terminal failure, with retry metadata)
```

- Only authenticated management APIs can transition ABSENT to CREATING.
- Data-plane requests are accepted only for ACTIVE records.
- Duplicate create requests with the same idempotency key return the original operation; conflicting payloads return `409`.
- Failed initialization never publishes ACTIVE.
- Deletion creates a tombstone and retains the identity needed to prevent accidental namespace reuse.
- Catalog revisions and lifecycle operations use fencing tokens so a stale worker cannot publish a late result.

## 11. Fixed-instance and bounded-pool design

### 11.1 Fixed binding

An instance is constructed from one immutable catalog snapshot and binding. The binding is read-only after construction. Storage objects retain the same canonical key. Switching is implemented by acquiring another instance, never by mutating an existing one.

This avoids races where an in-flight request, async generator, deferred embedding buffer, or background task observes a workspace change halfway through execution.

### 11.2 Per-worker pool

Each process maintains a local pool because `LightRAG` instances contain event-loop objects, clients, buffers, and in-memory state that should not be shared through a process manager.

The pool supports:

- lazy construction only after an ACTIVE catalog lookup;
- per-key single-flight construction within a worker;
- a maximum instance count;
- a connection/resource-weight budget in addition to count;
- explicit states: INITIALIZING, READY, DRAINING, FINALIZING, FAILED;
- separate foreground and background lease counts;
- idle LRU bookkeeping;
- bounded failure caching/backoff so repeated bad initialization does not create a retry storm.

Different workers may load the same workspace, which is expected. Shared catalog state, migration leases, pipeline exclusion, and provider admission prevent unsafe duplicate ownership.

### 11.3 Safe eviction

An entry is evictable only when all are true:

- foreground lease count is zero;
- background/stream lease count is zero;
- no initialization, migration, scan, pipeline, recovery, deletion, or finalization is active;
- no buffered write or retryable deferred work remains;
- the coordination provider confirms no worker-local responsibility that requires the instance;
- the entry is not pinned by compatibility/startup policy.

Eviction atomically changes READY to DRAINING before finalization, preventing new leases. Finalization failure leaves the entry quarantined/FAILED rather than making it appear safely absent.

If capacity is full and no safe victim exists, admission returns `503 workspace_capacity_exhausted` with `Retry-After`; it does not cancel useful work or exceed the configured budget.

## 12. Request and background context

### 12.1 Explicit context object

The selected instance is exposed through an immutable `WorkspaceContext` containing the catalog identity, binding, instance lease, and request correlation data. Router dependencies create it after authentication/authorization and release it only after the complete response or stream ends.

`ContextVar` may be used as a convenience inside a request, but it is not the authority and has no default fallback in multi-workspace code. Core operations and background jobs accept an explicit context/binding. Access without one raises a typed error.

### 12.2 Streams and spawned tasks

- A streaming response holds its lease until the generator closes or is cancelled.
- A route may not hand a request-scoped proxy to a background task.
- Spawning background work requires an explicit handoff that creates a background lease before the request lease is released.
- Cancellation releases admission tokens and leases exactly once.
- Logs and traces include the public workspace ID and catalog revision, but never credentials.

## 13. Pipeline and workspace binding

Every pipeline operation carries a concrete workspace binding. This includes:

- enqueue and deduplication;
- `pipeline_status` and all namespace locks;
- `track_id` creation and lookup;
- input-directory selection and scanning;
- PENDING/PROCESSING/FAILED queries;
- parse and multimodal work;
- extraction, embedding, graph updates, and cache writes;
- clear/delete/destructive jobs;
- retry and restart recovery.

There is no `workspace=None` or process-global-default fallback on the multi-workspace path. Durable keys that are not already physically partitioned include the canonical workspace key, for example `(workspace_key, track_id)`.

### 13.1 Input scanning

Each workspace has an unambiguous input root derived from its binding/profile. A scan request selects exactly one ACTIVE workspace. A process-wide directory scan may exist only as an administrative coordinator that enumerates catalog records and invokes separate workspace-scoped scans; it cannot classify files against a global doc-status store.

### 13.2 Restart recovery

Recovery is catalog-driven, not first-access-driven:

1. Enumerate ACTIVE workspaces from the shared catalog in deterministic pages.
2. Acquire a per-workspace recovery/pipeline lease with owner ID and fencing token.
3. Inspect that workspace's doc-status queue.
4. Reclaim stale PROCESSING records according to heartbeat/lease policy and transition them idempotently to a recoverable state.
5. Submit work through the global fair pipeline scheduler.
6. Commit state only while the owner fencing token remains current.

Recovery has bounded parallelism and checkpoints its scan cursor. One broken workspace cannot prevent other catalog records from being reconciled. Failed workspaces expose error state and explicit retry controls.

## 14. Parallel pipelines and global resource governance

Different workspaces may run pipelines concurrently, but concurrency is admitted at service level.

### 14.1 Shared admission controller

The server constructs one logical `ResourceAdmissionController`, not one budget per `LightRAG` instance. Every provider call submits an admission request containing:

```text
workspace_id
resource_kind        llm | embedding | rerank
operation_kind       query | ingestion | recovery | management
cost_hint
priority
cancellation_token
```

The configured LLM, embedding, and reranker maximum concurrency values are totals for the server deployment scope supported by the coordinator. Activating N workspaces cannot raise a limit from C to N×C.

Provider wrappers are injected with or call this shared controller. A per-instance semaphore may be retained only as a smaller local guard; it cannot mint additional global capacity.

### 14.2 Deployment modes

| Deployment | Admission implementation |
| --- | --- |
| One process | In-process shared scheduler used by every instance. |
| Same-host Gunicorn | Same-host coordination provider supplies process-safe global tokens and queue arbitration. |
| Future multi-node | External provider supplies leases/tokens with TTL, heartbeat, and fencing. |

Configuration validation rejects a multi-worker mode that cannot enforce the documented global limit; it must not silently reinterpret the limit as “per worker.”

### 14.3 Fairness and overload

- A server-global cap limits simultaneously active ingestion pipelines.
- The ready queue is partitioned by workspace and scheduled with weighted round-robin or deficit round-robin.
- Priority aging prevents indefinite starvation.
- Queries and small interactive jobs may receive a bounded reserved share, without allowing ingestion to starve forever.
- Per-workspace pending queue limits and server-global queue limits provide backpressure.
- Overload returns stable `429`/`503` errors with retry guidance rather than accepting unbounded work.

Fairness is measured by bounded wait time and service share under a documented workload, not inferred from semaphore counts.

## 15. Multi-process and future clustering

### 15.1 Same-host Gunicorn

- Instance pools and provider clients remain per worker.
- Catalog, lifecycle revisions, migration ownership, pipeline exclusion, recovery leases, and global admission are shared.
- Expected connection cost is approximately workers × workspaces loaded per worker × backend client cost; pool budgets and metrics must make this visible.
- Requests may reach any worker. A cache miss reloads the catalog record and acquires/constructs locally.
- Management changes are observed through catalog revisions, not sticky sessions or process-local mutation.

### 15.2 Coordination abstraction

The core depends on a provider contract for:

- lease acquire/renew/release;
- owner identity and monotonic fencing tokens;
- compare-and-swap state updates;
- fair resource admission;
- optional wakeup/notification, with polling fallback;
- TTL and abandoned-owner recovery.

The initial local/same-host implementation may use existing shared-storage infrastructure, but business logic cannot depend directly on a Python `Manager` dictionary. This allows a later Redis/database/etcd-like coordinator without changing routing or pipeline semantics.

### 15.3 No premature multi-node claim

Phase one does not claim multi-node safety. It only prevents design choices that require sticky sessions or unfenced process memory. The support matrix must state exactly which combinations of worker count, catalog provider, and coordinator provider are supported.

## 16. API contract and endpoint instantiation policy

### 16.1 REST selector

The proposed data-plane header is:

```http
LIGHTRAG-KNOWLEDGE-BASE: <knowledge-base-id>
```

The header selects a catalog ID, not a raw backend workspace string. Header omission selects the reserved `default` record for backward compatibility. Successful data-plane responses expose the resolved public ID in a response header or stable response field for auditing.

Management endpoints identify the record in their path/body and do not use the routing header.

### 16.2 Endpoint policy

Every route is registered in exactly one policy class, and an OpenAPI/router test fails if a new route is unclassified.

| Endpoint class | Catalog lookup | May load an existing instance | May create a catalog record/namespace | May migrate | Examples |
| --- | --- | --- | --- | --- | --- |
| Liveness/version | No workspace lookup required | No | No | No | `/health`, version/auth-mode liveness |
| Readiness/control-plane observation | Read shared catalog/coordinator only | No | No | No | `/ready`, pool/catalog lifecycle status |
| Workspace management read | Catalog only | No | No | No | list/get workspace metadata |
| Workspace create | Explicit management target | Yes, through lifecycle worker | Yes | Yes, before ACTIVE | management `POST` |
| Workspace update | Catalog only for mutable metadata | No | No | No | display-name update |
| Workspace delete | Explicit lifecycle operation | May acquire a maintenance instance | No new identity | Only cleanup-specific steps | management `DELETE` |
| Data read | Resolve existing ACTIVE record | Yes | No | No | query, document list/status/count, graph/cache reads |
| Data write | Resolve existing ACTIVE record | Yes | No | No | upload/text/scan, graph/document mutation |
| Workspace runtime observation | Catalog/coordinator or pool `peek` | No if unloaded | No | No | pipeline/pool status; reports UNLOADED rather than loading |

`/health` is process liveness and remains side-effect free regardless of selector input. It cannot call the instance pool's constructing acquire path, initialize storage, create pipeline state, or run migration. Authenticated detailed diagnostics use existing catalog/pool snapshots only.

### 16.3 Affected route families

The common selector applies to all workspace data operations, including:

- native query and streaming query routes;
- document upload, text insertion, scan, list, status, retry, clear, and delete routes;
- graph/entity/relation read and mutation routes;
- cache read/clear routes;
- workspace-bound pipeline operations;
- Ollama-compatible generate/chat routes after model-alias resolution.

Ollama metadata routes such as version, tags, and process listing are observational and cannot instantiate every workspace.

### 16.4 Ollama-compatible selection

Because many Ollama clients cannot add custom headers, generate/chat accept workspace-aware model aliases:

```text
lightrag:latest          -> public default record
lightrag:default         -> public default record
lightrag:<workspace-id>  -> explicit catalog record
```

The alias is resolved through the same catalog and validation rules as the REST header. Unknown aliases return an Ollama-compatible not-found response without creation. If both a custom header and model alias are present and disagree, return `400 selector_conflict` rather than choosing precedence.

Exact alias spelling remains open to maintainer preference, but model-only selection is a requirement.

## 17. Migration timing and ownership

Migration is a control-plane lifecycle operation. It does not run during arbitrary data access.

### 17.1 Bootstrap categories

| Category | Migration timing |
| --- | --- |
| Catalog schema/bootstrap | Idempotently at process/service startup under shared ownership. Does not instantiate every RAG workspace. |
| Existing legacy default storage | At startup before default-workspace readiness, preserving current single-workspace expectations. |
| Newly created workspace | In the explicit create lifecycle before record becomes ACTIVE. |
| Existing non-default workspace after software upgrade | Startup reconciliation enumerates catalog records and runs bounded background migrations under per-workspace leases. The record is MIGRATING and its data plane returns `503` until ready. |
| Retry after failure | Explicit management retry or controlled startup policy; never an ordinary query/upload side effect. |

Global liveness may become healthy while bounded non-default migrations continue, but readiness must report partial/degraded state and per-workspace lifecycle. The default workspace must satisfy its compatibility readiness gate before legacy traffic is reported ready.

### 17.2 Multi-worker safety

Migration ownership uses a shared lease and fencing token. Only the current owner may update schema version or mark ACTIVE. Other workers observe catalog state and do not repeat migration. A crashed owner's lease expires; a new owner resumes an idempotent migration from persisted version/state.

Migration code must be safe to retry and must record enough state to distinguish not-started, in-progress, succeeded, and failed. Expensive migrations have explicit operator controls, progress, and rollback/backup documentation.

## 18. Default-workspace compatibility and upgrade path

### 18.1 Bootstrap behavior

On first upgraded startup, the server idempotently creates the reserved public catalog record `default`:

- if server-level `WORKSPACE` is unset or empty, it maps to `LegacyDefault` and the `legacy-v1` codec;
- if `WORKSPACE` is non-empty, it maps to that validated named key using the existing layout contract;
- no existing storage data is moved, renamed, re-embedded, or copied;
- no-header clients continue selecting this record.

If a persisted default record later disagrees with server configuration, startup fails with a migration/configuration diagnostic rather than choosing one value silently.

### 18.2 Backend override upgrade audit

Before multi-workspace mode can be enabled, an operator-facing preflight reports:

- every active storage implementation and family;
- every legacy workspace override source;
- each computed canonical key and physical fingerprint;
- whether all four families agree;
- reserved-name/default collisions;
- required configuration changes.

In multi-workspace mode any active override is a startup error. In legacy mode a consistent override configuration remains supported, but a cross-family mismatch fails because the existing state is already unsafe. Documentation provides a dry-run command and backup steps before changing an override.

### 18.3 Rollback

The initial logical-isolation phase does not rewrite default data. Rollback therefore consists of disabling multi-workspace routing and using the unchanged default namespace, provided no non-default workspace must remain reachable. Catalog schema changes are backward-compatible/additive within a release window. Operators must back up catalog metadata before destructive lifecycle operations.

## 19. Security boundary

Workspace isolation is not authorization. The selector header/model alias is untrusted routing input, not proof that a caller may access that workspace.

Phase one retains the existing server-wide authentication boundary. Unless a trusted reverse proxy applies policy, any authenticated caller can select any ACTIVE catalog record. This limitation must be explicit in release notes and API documentation.

Routing is structured so a future `WorkspaceAuthorizer(principal, action, catalog_record)` can run after authentication and catalog resolution but before instance acquisition. Management create/delete requires administrative authorization even in phase one.

Workspace IDs are opaque but not secrets. Error responses avoid leaking storage connection details. Rate limits apply to unknown-selector probes and management creation to reduce abuse.

## 20. Per-workspace configuration

In the first phase, the only per-workspace override is an immutable `storage_profile_id`.

- The profile selects connection/resources and is validated before creation.
- It cannot override the canonical workspace key.
- It cannot be mutated while the workspace is ACTIVE. A profile change is a future explicit migration/copy operation.
- LLM, embedding, reranker, parser, chunking, prompts, and admission limits remain server-global.

Strict physical isolation profiles are deferred. Logical isolation must work without requiring a separate database/service per workspace.

## 21. Deletion semantics

Deletion is two-phase and fail closed:

1. CAS ACTIVE to DELETING and deny new foreground/background leases.
2. Drain existing leases with a bounded operator-visible policy.
3. Acquire an exclusive deletion lease/fencing token.
4. Drop only namespaces proven by the catalog binding and storage descriptors for all four families.
5. Remove workspace-scoped input/artifact files.
6. Persist TOMBSTONED; retain identity and audit metadata.

Partial failure leaves ERROR/DELETING with a resumable operation record. The system never marks deletion complete after dropping only doc-status or only data stores. A backend override or descriptor mismatch blocks destructive execution.

## 22. Observability

Required signals include:

- catalog lifecycle counts and revision lag;
- per-worker pool entries, state, leases, idle age, and resource weight;
- instance construction/finalization failures;
- effective-workspace consistency failures by storage family/implementation;
- migration queue, duration, owner, retry, and failure state;
- global and per-workspace admission wait, active count, rejection, and queue depth;
- pipeline scheduling delay and fairness metrics;
- recovery backlog and stale-owner reclamation;
- configured versus enforced deployment-wide provider limits.

Metrics must control workspace-label cardinality; detailed IDs can live in structured logs/traces while aggregate metrics use bounded labels. Health/ready handlers read snapshots and never initialize a workspace.

## 23. Failure semantics

| Failure | Required behavior |
| --- | --- |
| Unknown workspace | `404`; no catalog/storage side effect. |
| Present empty/invalid selector | `400`; no default fallback. |
| Backend override in multi-workspace mode | Startup failure naming the conflicting non-secret settings. |
| Four-family workspace mismatch | Initialization failure before migration/data access. |
| Workspace migrating/recovering | `503` with stable code and retry hint. |
| Pool full, no safe victim | `503`; bounded backpressure. |
| Admission queue full | `429` or `503` by operation type; never unbounded queue growth. |
| Instance construction failure | Single-flight callers receive the same failure; entry enters bounded backoff. |
| Worker dies with lease | TTL/heartbeat recovery and fencing prevent stale commits. |
| Migration fails | Record remains MIGRATING/ERROR; no data-plane access or automatic query retry. |
| Deletion partially fails | Record remains non-ACTIVE and resumable; no ID reuse. |
| Missing task context | Typed internal failure; no fallback to default. |

## 24. Phased MVP and PR sequence

The implementation branch reviewed in one large PR mixes core routing, storage isolation, physical profiles, WebUI, and unrelated changes. The proposed review sequence is:

### PR 1: Canonical workspace contract and legacy safety

Scope:

- tagged canonical workspace key and namespace codec contract;
- central resolution and reserved-name rules;
- standardized storage descriptors for all storage families;
- startup checks for override variables and four-family consistency;
- zero-copy legacy-default bootstrap behavior and tests.

Non-goals: catalog management API, dynamic instance pool, WebUI, physical isolation.

Rollback: no default data layout rewrite; revert code/config after preflight.

### PR 2: Shared catalog and management lifecycle

Scope:

- durable catalog provider contract and a supported shared implementation;
- default-record bootstrap, revisions/CAS, idempotent create, lifecycle states, tombstones;
- management APIs and side-effect-free catalog observation;
- single-worker versus Gunicorn configuration validation.

Non-goals: data-plane routing and physical profiles.

### PR 3: Fixed instance pool and request routing

Scope:

- fixed-bound instances, per-worker single-flight pool, leases, capacity/backpressure, safe idle eviction;
- REST selector rules and route-classification test;
- no fallback context proxy;
- side-effect-free health/readiness;
- query/read routing for existing ACTIVE workspaces.

Non-goals: enabling unsafe multi-workspace ingestion before pipeline PR 4.

### PR 4: Pipeline context, migration, recovery, and shared admission

Scope:

- explicit pipeline/background context and lease handoff;
- doc-status isolation sentinel tests;
- workspace-scoped scan/track/status/destructive paths;
- control-plane migration coordinator and full-restart recovery;
- service-level LLM/embedding/rerank admission, active-pipeline cap, fairness, and Gunicorn enforcement.

This PR enables multi-workspace write/ingestion routes. Before it, those routes remain feature-gated to the compatibility default.

### PR 5: Ollama-compatible selection

Scope: model aliases, conflicts, unknown behavior, tags/ps observation, compatibility tests.

### PR 6: WebUI

Scope: management UI and explicit upload/query selection. “Create independent knowledge base” is presented before existing choices; selectors show both display name and ID.

### PR 7+: Strict physical isolation by backend

Scope: small backend-focused PRs with explicit resource ownership, creation/deletion, migration, and integration tests. Logical workspace identity remains centrally supplied.

The physical-resource lifecycle is intentionally least-privilege: endpoints,
databases, clusters, and volumes are operator-owned, pre-provisioned,
operator-backed-up resources. LightRAG may initialize, migrate, and drop only
the workspace namespaces it owns; it never interprets namespace deletion as
permission to destroy an endpoint or database service. The catalog stores an
immutable, credential-free fingerprint for the complete profile binding and
each active resource section. Credential rotation is allowed, but changing a
bound profile ID to a different host, endpoint, database, or directory fails
before client construction, migration, or any destructive storage call.
Pre-existing physical records without this snapshot are bound only by a fenced
startup migration after the operator has verified and backed up the configured
profile.

Offline contract/mock coverage is not sufficient to advertise a physical
backend as production-verified. Each backend support statement requires a real
service create/migrate/delete test, proof that an unrelated profile is
unaffected, and an operator backup/restore exercise covering the catalog and
all four storage families at one consistent recovery point.

### Later: external coordinator and multi-node support

Scope: implement the existing lease/fencing/admission contract through external shared infrastructure, then publish a tested multi-node support matrix.

## 25. Validation plan

### 25.1 Workspace identity and storage matrix

For every supported backend and meaningful mixed-family combination:

- assert every storage object reports the same canonical key and codec version;
- inject each legacy override and verify multi-workspace startup fails;
- verify consistent legacy single-workspace overrides remain compatible;
- verify mixed overrides fail before data access;
- verify empty legacy, public `default`, reserved `_`, and named workspace identities cannot collide;
- verify physical namespace descriptors are deterministic and credential-free.

Unit tests use fakes/mocks; backend integration suites verify actual namespace/filter behavior for PostgreSQL, Redis, Neo4j, MongoDB, Milvus, Qdrant, Memgraph, OpenSearch, and file-based stores.

### 25.2 Doc-status sentinel tests

Insert identical content into workspace A and B so both produce the same document ID. Independently drive PENDING, PROCESSING, PROCESSED, FAILED, retry, track lookup, delete, and restart recovery. Assert that every status read/write and resulting KV/vector/graph change remains in its workspace.

### 25.3 Deterministic concurrency tests

Use barriers/events rather than timing sleeps to test:

- concurrent first access in one worker constructs once;
- concurrent first access across workers performs only one migration;
- a stream/background task prevents eviction and deletion;
- deletion blocks new leases and stale owners cannot commit;
- pool capacity returns backpressure when all entries are leased;
- cancellation releases one and only one lease/admission token;
- catalog CAS prevents lost updates.

### 25.4 Migration and recovery tests

- `/health` and readiness inspection cause zero instance constructions and migrations;
- an arbitrary first query never invokes migration;
- default startup migration gates compatibility readiness;
- non-default migrations are bounded and visible;
- worker kill transfers ownership after TTL and rejects stale fencing tokens;
- full restart enumerates all ACTIVE workspaces without user touch;
- one failed workspace does not block others;
- repeated migration/recovery is idempotent.

### 25.5 Resource and fairness tests

- with N loaded workspaces, observed LLM/embedding/rerank concurrency never exceeds the configured deployment total;
- the same assertion holds in one-process and Gunicorn modes;
- active ingestion pipelines never exceed the global cap;
- under sustained workspace-A load, workspace B receives service within the documented bound;
- interactive reservations do not permanently starve ingestion;
- queue saturation produces bounded errors and no memory growth.

### 25.6 API and compatibility tests

- absent, empty, invalid, unknown, inactive, and conflicting selectors;
- complete route classification and OpenAPI header presence on every data route;
- management/health routes do not accept or act on the data selector;
- Ollama model-only selection and model/header conflict;
- existing no-header/no-`WORKSPACE` deployment uses unchanged data;
- existing configured default uses unchanged data;
- rollback leaves default data readable.

### 25.7 Security tests

- selector is never treated as authorization;
- management operations require administrative auth;
- errors/logs do not expose connection secrets;
- unknown selectors and create attempts are rate-limited;
- path/collection injection is impossible because display names never become namespaces.

## 26. Alternatives considered

### 26.1 Switch one instance between workspaces

Rejected. Mutable instance fields cannot protect in-flight coroutines, streams, storage buffers, provider callbacks, or background tasks. Proving safe switching would require propagating immutable state through every call anyway, while retaining more shared mutable risk.

### 26.2 Let unknown selectors auto-create

Rejected. A typo becomes a durable data hazard and an abuse vector for directories, collections, migrations, connections, and model traffic.

### 26.3 Let backend overrides win

Rejected in multi-workspace mode. Independent backend precedence makes four-family consistency unprovable and can corrupt doc-status silently. Ignoring the variables is also unsafe because it hides an operator mistake, so startup fails explicitly.

### 26.4 Rewrite every legacy physical namespace to one literal token

Rejected for initial upgrade. It would violate zero-copy compatibility and require risky cross-backend migration. A canonical tagged identity plus versioned physical codec provides a uniform core contract without moving existing data.

### 26.5 One global instance pool shared through `multiprocessing.Manager`

Rejected. Live clients, event-loop primitives, buffers, and `LightRAG` instances are not safe process-shared objects. Share catalog and coordination state; keep instances per worker.

### 26.6 Independent per-instance provider semaphores

Rejected. They multiply configured concurrency by active workspace count and cannot provide workspace fairness.

### 26.7 Migrate on first request

Rejected. It creates unpredictable latency, lets observability acquire side effects, races across workers, and leaves untouched workspaces unrecovered after restart.

## 27. Open questions for maintainers

1. Is `LIGHTRAG-KNOWLEDGE-BASE` acceptable as the public selector, or should the community API standardize on a workspace-named header such as `X-LightRAG-Workspace`? The semantic requirement is that it selects an opaque catalog ID, not a backend namespace.
2. Is failing legacy startup on an actual four-family mismatch acceptable, while preserving no-workspace and consistent-override deployments? The alternative is a one-release warning-only mode, but that continues a known silent-corruption risk.
3. Should same-host Gunicorn support be required before multi-workspace ingestion is enabled, or may the first routing PR explicitly support one worker until the pipeline/admission PR lands?
4. Are the proposed Ollama aliases (`lightrag:default`, `lightrag:<id>`) compatible with expected clients?
5. Which shared catalog implementation should be the first supported one? The provider contract and correctness requirements are independent of that selection.

## 28. Acceptance criteria for the RFC

This RFC is ready to translate into implementation PRs when maintainers agree on:

- fixed instance binding and explicit creation;
- the canonical workspace/legacy codec model;
- override-variable behavior and four-family fail-fast validation;
- shared catalog and lifecycle ownership;
- per-worker lease pool and side-effect-free endpoint policy;
- migration/recovery timing;
- service-level provider admission and fairness;
- REST/Ollama selection semantics;
- the phase-one boundary and compatibility contract.
