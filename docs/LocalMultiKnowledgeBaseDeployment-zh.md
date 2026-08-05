# 本地多知识库部署与验证

本文对应 `docker-compose.local-multikb.yml`，目标存储映射为：

| LightRAG 存储类型 | 实现 | 本地服务 |
| --- | --- | --- |
| KV / LLM Cache | `RedisKVStorage` | Redis 7.4 |
| Vector | `PGVectorStorage` | PostgreSQL 18 + pgvector |
| Graph | `Neo4JStorage` | Neo4j 5.26 Community |
| Document Status | `PGDocStatusStorage` | PostgreSQL 18 + pgvector |

## 1. 配置 OpenAI-Compatible API

复制示例文件，但不要提交包含秘密的副本：

```powershell
Copy-Item .env.local-multikb.example .env.local-multikb
```

至少替换以下值：

- `LLM_BINDING_HOST`、`LLM_MODEL`、`LLM_BINDING_API_KEY`
- `EMBEDDING_BINDING_HOST`、`EMBEDDING_MODEL`、`EMBEDDING_DIM`、`EMBEDDING_BINDING_API_KEY`
- `LIGHTRAG_API_KEY`、`TOKEN_SECRET`

Embedding 模型和维度一旦用于写入向量后不要修改；修改时必须清理对应知识库的向量数据并重新解析文档。

## 2. 启动基础设施、应用与查看状态

默认命令只启动 PostgreSQL、Neo4j、Redis，不构建应用镜像，适合宿主机开发：

```powershell
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml up -d
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml ps
```

宿主机启动后端（Windows PowerShell 需确保终端使用 UTF-8）：

```powershell
Copy-Item .env.local-multikb .env
$env:PYTHONUTF8 = '1'
uv sync --extra api --extra offline-storage
uv run lightrag-server
```

`lightrag-server` 固定读取当前目录的 `.env`；该文件同样已被 Git 忽略。Compose 则继续通过 `--env-file .env.local-multikb` 读取同一组配置，并在应用容器内把数据库主机名覆盖为 Compose 服务名。

也可以启用可选的 `app` profile，构建前后端一体镜像：

```powershell
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml --profile app up -d --build
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml ps
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml logs -f lightrag
```

服务地址：

- WebUI：`http://localhost:9621/webui/`
- OpenAPI：`http://localhost:9621/docs`
- Neo4j Browser：`http://localhost:7474`

停止但保留数据：

```powershell
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml down
```

重启后 `data/local-multikb`、三个 Docker named volumes 和 `knowledge_bases.json` 会继续保留。只有明确确认不再需要所有本地数据时，才使用 `down -v` 并删除 `data/local-multikb`。

## 3. API 使用方式

无 `LIGHTRAG-KNOWLEDGE-BASE` 请求头时始终使用 `default`，它继续绑定服务器原有 `WORKSPACE`，兼容旧客户端与旧数据。

```powershell
$headers = @{ 'X-API-Key' = '<LIGHTRAG_API_KEY>' }
$adminHeaders = @{
  'X-API-Key' = '<LIGHTRAG_API_KEY>'
  'X-LightRAG-Admin-Key' = '<LIGHTRAG_ADMIN_API_KEY>'
  'Idempotency-Key' = 'create-project-a-001'
  'Prefer' = 'wait=10'
}
$catalog = Invoke-RestMethod -Headers $headers -Uri http://localhost:9621/knowledge-bases

$createResult = Invoke-RestMethod -Method Post -Headers $adminHeaders `
  -ContentType 'application/json' -Uri http://localhost:9621/knowledge-bases `
  -Body '{"name":"项目 A","isolation_level":"logical"}'
$newKb = $createResult.knowledge_base

$kbHeaders = @{
  'X-API-Key' = '<LIGHTRAG_API_KEY>'
  'LIGHTRAG-KNOWLEDGE-BASE' = $newKb.id
}
Invoke-RestMethod -Headers $kbHeaders -Uri http://localhost:9621/documents
```

WebUI 顶栏可切换当前知识库；上传对话框可选择：

- 第一项“新建独立知识库”：先创建唯一 effective workspace，再把本批文件写入新实例；可选择 logical，或选择一个可用 profile 创建 physical 库。
- 其后列出所有存量知识库，使用“名称 (ID)”区分；选择任一存量库会向该库增量写入 RAG/Graph/Cache。

## 4. logical 与 physical

`logical` 模式为每个知识库创建独立 `LightRAG`、`DocumentManager`、输入目录、12 个存储 namespace 和 pipeline 状态。文件型后端使用 workspace 子目录；PostgreSQL 使用 workspace 行/图命名空间；Neo4j、Memgraph 使用 workspace label；Redis 使用 workspace key 前缀；MongoDB、Milvus、OpenSearch 使用 workspace 集合/索引；Qdrant 使用 workspace payload 与强制 filter。

`physical` 模式还要求每个知识库独占一份 storage profile。管理员需：

1. 创建 `data/local-multikb/config`，把 `storage-profiles.local.example.json` 复制为其中的 `storage-profiles.json`；
2. 为 profile 部署当前启用四类 storage 所需的专属资源与文件目录；
3. 在 `.env.local-multikb` 设置 `LIGHTRAG_STORAGE_PROFILES_FILE=/app/config/storage-profiles.json`；Compose 已将该 config 目录只读挂载到应用容器；
4. profile 必须声明 `dedicated=true`，且一个 profile 只能绑定一个知识库。不同 profile 不能复用任一活动存储的物理资源指纹。

建议像示例一样显式声明固定 lifecycle；省略时系统也采用相同的安全默认值：

```json
{
  "resource_ownership": "operator",
  "provisioning": "preprovisioned",
  "deletion": "drop_workspace_namespaces",
  "backup": "operator_managed"
}
```

它表示数据库、集群、服务和卷由运维人员预先创建并负责备份/退役；LightRAG 只在
create/migrate/delete operation 中初始化、升级或删除自己拥有的 workspace
namespace，不会删除整个 endpoint/database 服务。配置成其他更强的删除策略会在
启动/创建前被拒绝，避免一个应用 API 获得销毁基础设施的权限。

profile 的必需 section 根据当前四类 storage 动态计算：文件型实现只需专属 `working_dir`/`input_dir`；外部实现分别使用 `postgres`、`neo4j`、`redis`、`mongo`、`milvus`、`qdrant`、`memgraph`、`opensearch`。示例文件展示了全部 section；实际 profile 可以只保留当前启用的 section。若 profile 不存在、缺字段、未声明独占、已占用或资源指纹与另一个 physical 库相同，API 返回明确错误，不会降级为 logical。profile 内密码不会进入 catalog、健康检查或日志输出。

MongoDB 与 OpenSearch 的进程内 client 会按 profile 资源分池和引用计数；Milvus、Qdrant、Memgraph 使用实例级连接。Redis DB 编号、Qdrant `collection_prefix` 和 OpenSearch `index_prefix` 只属于共享服务内的逻辑命名空间，不能单独证明严格物理隔离；这三类 physical profile 必须使用不同于 default 和其他 physical 库的 endpoint，Qdrant/OpenSearch 同时仍要求独立 prefix，防止专属服务内部发生名称碰撞。

创建 physical 知识库时，catalog 会持久化不含密码/token 的逐资源 SHA-256
指纹和整体 profile 指纹。凭据轮换不会改变指纹；修改 host、endpoint、database
或专属目录会在构造 client、迁移或执行任何 `drop()` 之前 fail closed。旧版本已
存在但没有指纹的 physical record 会在启动期 fenced migration 中绑定当前资源；
升级前应先核对 profile 并备份，不能在同一次升级中顺便把 profile ID 改指向新资源。

### 4.1 删除前备份与恢复责任

删除 API 只清理 LightRAG namespace，并在 durable operation journal 中逐项记录
`DELETING_NAMESPACES`、每个 storage role、input directory 和
`NAMESPACES_DELETED`。专属服务/数据库仍由 operator 保留，便于基础设施级恢复。
在调用 `DELETE /knowledge-bases/{id}?confirm=true` 前，至少完成当前活动后端对应的
备份，并把 catalog 文件或 PostgreSQL catalog 表一起纳入同一恢复点：

| 后端 | 建议的 operator backup 边界 |
| --- | --- |
| 文件型 JSON/NetworkX/Nano/Faiss | 归档 profile 的 `working_dir` 与 `input_dir`，保留权限和时间戳 |
| PostgreSQL | 对 profile database 执行 `pg_dump`，同时备份 shared catalog 表 |
| Redis | 对专属 endpoint 执行 RDB/AOF snapshot，并记录 endpoint/profile ID |
| Neo4j / Memgraph | 使用对应版本官方 database dump/backup 工具备份 profile database |
| MongoDB | 对 profile database 执行 `mongodump` |
| Milvus | 使用集群支持的 backup 工具备份 profile database/collection |
| Qdrant | 对专属 endpoint 的 LightRAG collections 创建 snapshot |
| OpenSearch | 使用 snapshot repository 备份 profile indices |

恢复时先停止对应知识库流量，恢复 operator-owned 资源与 catalog 到一致时间点，再
启动服务触发 catalog-driven migration/recovery。不要只恢复 doc-status 或只恢复
向量/图/KV 中的一部分；四个 storage family 必须保持同一有效 workspace。

## 5. 健康检查与排障

```powershell
Invoke-RestMethod -Headers $headers -Uri http://localhost:9621/health
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml exec postgres pg_isready -U rag -d rag
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml exec redis redis-cli ping
docker compose --env-file .env.local-multikb -f docker-compose.local-multikb.yml exec neo4j cypher-shell -u neo4j -p '<password>' 'RETURN 1'
```

常见问题：

- 动态库创建失败并提到 `*_WORKSPACE`：删除当前活动后端对应的 `POSTGRES_WORKSPACE`、`NEO4J_WORKSPACE`、`REDIS_WORKSPACE`、`MONGODB_WORKSPACE`、`MILVUS_WORKSPACE`、`QDRANT_WORKSPACE`、`MEMGRAPH_WORKSPACE` 或 `OPENSEARCH_WORKSPACE`；这些强制覆盖会让多个 logical 库串到同一 namespace。
- PostgreSQL 向量维度错误：确认 `EMBEDDING_DIM` 与服务实际输出一致；已有数据需重建向量。
- Neo4j 认证失败：Compose 的 `NEO4J_PASSWORD` 与 `.env.local-multikb` 必须一致。
- 新库创建较慢：显式 create lifecycle 会在发布 ACTIVE 前初始化并迁移存储；普通首次查询不会拥有 migration 权限。
- catalog 无法启动：不要手工改写 `knowledge_bases.json`；从备份恢复，避免默认 workspace 被重新映射。
