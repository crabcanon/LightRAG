# 多租知识库 Docker 部署与验收指南

本文提供两套可直接构建的完整 LightRAG 镜像部署方式。两者都使用仓库根目录的 `Dockerfile`：镜像内已构建 WebUI、安装 API 与离线存储依赖，并包含当前的知识库目录、请求头路由和实例池实现。不要改用 `Dockerfile.lite`，它不适合含 PostgreSQL、Neo4j、Redis 驱动的部署。

| 部署文件 | 存储组合 | 适用场景 |
| --- | --- | --- |
| `docker-compose.multitenant-standalone.yml` | `JsonKVStorage`、`NanoVectorDBStorage`、`NetworkXStorage`、`JsonDocStatusStorage` | 小规模、单机、无需外部数据库 |
| `docker-compose.multitenant-services.yml` | Redis KV、PostgreSQL/pgvector 向量与文档状态、Neo4j 图 | 需要独立数据库服务、较高可靠性和可运维性的单机部署 |

两套配置都应以**单个 LightRAG 应用副本**运行。当前文件型存储不支持横向扩容；外部存储版本虽然使用共享数据库，但多进程/多节点的管线互斥和目录目录一致性仍不应通过简单增加容器副本来实现。

## 1. 隔离模型与边界

每个通过 `POST /knowledge-bases` 创建的知识库都有一个不可变的 effective workspace。客户端使用 `LIGHTRAG-KNOWLEDGE-BASE` 请求头选择它；缺失该头时仍访问兼容旧部署的默认知识库。

- 独立文件部署会把 KV、三类向量、GraphML、文档状态分别写到同一挂载卷中的 `rag_storage/<workspace>/`，上传原文件也写到 `inputs/<workspace>/`。因此相同内容在两个知识库中得到相同的内容哈希也不会覆盖。
- 外部服务部署中，四个存储族都必须从 `LightRAG` 实例获取同一个 workspace。Compose 会将 `POSTGRES_WORKSPACE`、`NEO4J_WORKSPACE` 和 `REDIS_WORKSPACE` 显式设为空，避免进程级覆盖把所有请求压到同一命名空间。
- 两种方案都提供的是**数据平面的逻辑隔离**；独立文件方案有按目录的落盘分隔，但所有目录仍在同一 Docker volume，外部方案的数据库实例也仍共享。它们不是租户授权边界：拥有 API Key、容器 root、数据库管理员权限的主体仍可能访问多租数据。对不互信租户，应在网关实现鉴权/授权，或为每个租户部署独立服务和独立存储资源。

## 2. 前置条件

1. 安装 Docker Engine / Docker Desktop 和 Docker Compose v2，并确认 `docker version` 能同时显示 Client 与 Server。
2. 准备 OpenAI-Compatible Chat Completions 和 Embeddings API。若模型服务部署在宿主机，Docker Desktop 与此 Compose 均支持使用 `http://host.docker.internal:<port>/v1`；Linux Docker Engine 需在 Compose 中保留 `host-gateway` 映射，或使用可路由的主机 IP。
3. 确认 Embedding 模型输出维度，并将其写入 `EMBEDDING_DIM`。一个知识库写入向量后不得更改模型或维度；变更时应清除并重新构建该知识库。
4. 生产环境把 API 放在 TLS 反向代理之后。默认仅映射到 `127.0.0.1`，不要在未设置边界鉴权前直接暴露到公网。

生成密码时建议使用 URL-safe 的十六进制随机值，例如：

```powershell
openssl rand -hex 32
```

## 3. 方案 A：不使用外挂数据库

### 3.1 配置与构建

```powershell
Copy-Item .env.multitenant-standalone.example .env.multitenant-standalone
```

在 `.env.multitenant-standalone` 中替换所有 `CHANGE_ME...`，至少配置：

- `LIGHTRAG_API_KEY` 和 `TOKEN_SECRET`；
- `LLM_BINDING_HOST`、`LLM_BINDING_API_KEY`、`LLM_MODEL`；
- `EMBEDDING_BINDING_HOST`、`EMBEDDING_BINDING_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIM`。

下面命令会构建前后端一体镜像并启动服务。`--env-file` 是必需的，它同时向 Compose 提供端口/镜像变量，并向容器提供模型配置和密钥。

```powershell
docker compose --env-file .env.multitenant-standalone -f docker-compose.multitenant-standalone.yml build --pull lightrag
docker compose --env-file .env.multitenant-standalone -f docker-compose.multitenant-standalone.yml up -d
docker compose --env-file .env.multitenant-standalone -f docker-compose.multitenant-standalone.yml ps
```

浏览器访问：

- WebUI：`http://127.0.0.1:9621/webui/`
- OpenAPI：`http://127.0.0.1:9621/docs`
- Health：`http://127.0.0.1:9621/health`

### 3.2 数据持久化与限制

唯一的 named volume `lightrag_standalone_data` 保存 catalog、输入文件、解析物和所有工作区数据。执行 `down` 不会删数据；`down -v` 会永久删除它，执行前必须完成备份。

```powershell
docker compose --env-file .env.multitenant-standalone -f docker-compose.multitenant-standalone.yml down
docker run --rm -v lightrag-multitenant-standalone_lightrag_standalone_data:/data -v ${PWD}:/backup alpine tar czf /backup/lightrag-standalone-backup.tgz -C /data .
```

该方案适合中小数据量和单实例使用。NanoVectorDB 与 NetworkX 都是本地文件实现，不要为提高吞吐量而启动第二个应用容器共享此 volume。

## 4. 方案 B：PostgreSQL、Neo4j、Redis 外挂服务

### 4.1 配置与构建

```powershell
Copy-Item .env.multitenant-services.example .env.multitenant-services
```

除模型与 LightRAG API 配置外，还必须设置不同的高强度值：

- `POSTGRES_PASSWORD`
- `NEO4J_PASSWORD`
- `REDIS_PASSWORD`

不要在该文件中设置 `POSTGRES_WORKSPACE`、`NEO4J_WORKSPACE` 或 `REDIS_WORKSPACE`。它们是历史上的单实例强制覆盖项，与动态选择知识库不兼容。

```powershell
docker compose --env-file .env.multitenant-services -f docker-compose.multitenant-services.yml build --pull lightrag
docker compose --env-file .env.multitenant-services -f docker-compose.multitenant-services.yml up -d
docker compose --env-file .env.multitenant-services -f docker-compose.multitenant-services.yml ps
docker compose --env-file .env.multitenant-services -f docker-compose.multitenant-services.yml logs -f lightrag
```

只有 LightRAG 的 `9621` 端口映射到宿主机；PostgreSQL、Neo4j、Redis 只在 Compose 内部网络暴露。这样可减少数据库被直接访问的攻击面。

### 4.2 健康检查、备份与恢复原则

```powershell
$compose = 'docker compose --env-file .env.multitenant-services -f docker-compose.multitenant-services.yml'
Invoke-RestMethod http://127.0.0.1:9621/health
Invoke-Expression "$compose exec postgres pg_isready -U lightrag -d lightrag"
Invoke-Expression "$compose exec redis redis-cli -a '<REDIS_PASSWORD>' ping"
Invoke-Expression "$compose exec neo4j cypher-shell -u neo4j -p '<NEO4J_PASSWORD>' 'RETURN 1'"
```

数据分别保存在 `lightrag_services_app_data`、`lightrag_services_postgres_data`、`lightrag_services_neo4j_data` 和 `lightrag_services_redis_data` 四个 named volume。备份必须同时覆盖它们；其中 PostgreSQL 应优先使用 `pg_dump`/`pg_restore`，Neo4j 应使用与当前版本兼容的导出/备份工具，而不是只复制正在运行中的数据库文件。

停机但保留数据：

```powershell
docker compose --env-file .env.multitenant-services -f docker-compose.multitenant-services.yml down
```

只有明确不再需要所有知识库和四类存储数据时，才可执行 `down -v`。

## 5. 创建知识库并验收隔离

先使用默认 API Key 创建两个知识库，再分别给两个请求加入知识库头。下例中的 ID 不需要由客户端猜测，应使用创建响应返回的 `id`。

```powershell
$base = 'http://127.0.0.1:9621'
$headers = @{ 'X-API-Key' = '<LIGHTRAG_API_KEY>' }
$adminHeaders = @{
  'X-API-Key' = '<LIGHTRAG_API_KEY>'
  'X-LightRAG-Admin-Key' = '<LIGHTRAG_ADMIN_API_KEY>'
  'Idempotency-Key' = 'create-alpha-001'
  'Prefer' = 'wait=10'
}

$alphaResult = Invoke-RestMethod -Method Post -Headers $adminHeaders -ContentType 'application/json' `
  -Uri "$base/knowledge-bases" -Body '{"name":"Alpha","isolation_level":"logical"}'
$adminHeaders['Idempotency-Key'] = 'create-beta-001'
$betaResult = Invoke-RestMethod -Method Post -Headers $adminHeaders -ContentType 'application/json' `
  -Uri "$base/knowledge-bases" -Body '{"name":"Beta","isolation_level":"logical"}'
$alpha = $alphaResult.knowledge_base
$beta = $betaResult.knowledge_base

$alphaHeaders = @{ 'X-API-Key' = '<LIGHTRAG_API_KEY>'; 'LIGHTRAG-KNOWLEDGE-BASE' = $alpha.id }
$betaHeaders = @{ 'X-API-Key' = '<LIGHTRAG_API_KEY>'; 'LIGHTRAG-KNOWLEDGE-BASE' = $beta.id }

Invoke-RestMethod -Method Post -Headers $alphaHeaders -ContentType 'application/json' `
  -Uri "$base/documents/text" -Body '{"text":"Only Alpha may retrieve this sentence.","file_source":"alpha.txt"}'
Invoke-RestMethod -Method Post -Headers $betaHeaders -ContentType 'application/json' `
  -Uri "$base/documents/text" -Body '{"text":"Only Beta may retrieve this sentence.","file_source":"beta.txt"}'

Invoke-RestMethod -Headers $alphaHeaders -Uri "$base/documents"
Invoke-RestMethod -Headers $betaHeaders -Uri "$base/documents"
```

也可以在 WebUI 顶部切换当前知识库；上传弹窗的第一项是“新建独立知识库”，其后按“名称 (ID)”列出所有已有知识库。用同一个内容分别上传到两个库，并在一个库中删除后确认另一个库仍存在，是最直接的人工隔离验收。

## 6. 当前实现的自动化验证结果

默认文件型存储的回归矩阵覆盖以下场景：

- 两个工作区并发插入完全相同的内容，故意使 document ID 与 chunk ID 相同；
- 断言 7 个 JSON KV namespace、3 个 NanoVectorDB 文件、NetworkX GraphML 文件和 JSON doc-status 文件都落到各自 `<workspace>` 子目录；
- 验证两个图只含本知识库实体；删除 Alpha 的同 ID 文档、chunk 与向量后，Beta 的同 ID 数据仍完整；
- 验证 API 的 catalog、请求头路由、默认库兼容、路径校验、存储 workspace 一致性和管线锁隔离。

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\workspace\test_workspace_isolation.py `
  tests\workspace\test_workspace_path_validation.py `
  tests\workspace\test_file_storage_multitenant_e2e.py `
  tests\api\test_knowledge_bases.py -q --basetemp temp\pytest-multitenant-regression
```

容器启动后还应按第 5 节使用真实模型 API 做一次人工上传、查询和删除验收。这一步依赖你的 API Key、模型服务可达性以及已启动的 Docker Engine，不能由离线单元测试替代。

## 7. 常见问题

- Docker 构建能成功、解析时模型调用失败：检查容器内可达的 `LLM_BINDING_HOST` 和 `EMBEDDING_BINDING_HOST`。宿主机 `localhost` 对容器并不是宿主机本身。
- 新建知识库返回配置错误：检查是否设置了任何 `*_WORKSPACE` 强制覆盖项，并确认没有手工编辑 `knowledge_bases.json`。
- 外部服务启动慢：首次启动时 pgvector 初始化、Neo4j 启动和健康检查可能需要几十秒；使用 `docker compose ... logs -f` 查看服务状态。
- WebUI 无法访问：确认 `LIGHTRAG_BIND_HOST`、防火墙和反向代理。若改为 `0.0.0.0`，必须在网关层加 TLS、网络访问控制和用户/租户授权。
