# LightRAG 独立知识库隔离与本地全栈部署任务

> 本文件同时承担四个职责：仓库代码模型、可直接交给 Codex GPT-5.6 Sol 的主 Prompt、验收清单、按时间追加的执行日志。
>
> 当前状态：第一轮多知识库隔离与本地全栈部署、第二轮“上传目标、OpenAPI 请求头、全存储隔离”优化均已完成并通过自动化与本地运行时验收；服务已按现有 `data/local-multikb` 数据目录运行。

## 1. 文档维护约定

- 时区统一使用 Asia/Shanghai，时间格式使用 ISO 8601，例如 2026-07-20T21:53:37+08:00。
- “仓库代码模型”和“主 Prompt”是当前任务基线；需求变化时新增“决策变更记录”，不要无痕改写历史结论。
- 每完成一个阶段、发生一次重要决策、遇到阻塞或完成一次验证，都必须在“时间线日志”末尾追加记录。
- 每条时间线至少记录：阶段、状态、目标、实际动作、改动文件、验证命令与结果、风险/阻塞、下一步。
- 状态只使用：待开始、进行中、已完成、已阻塞。
- 不在本文件中写入 API Key、数据库密码、访问令牌或其他秘密；只记录所需变量名和“已由用户提供/尚未提供”。
- 任务执行期间同步维护“阶段状态总览”；历史时间线只追加，不删除。

## 2. 仓库快照

| 项目 | 当前值 |
| --- | --- |
| 仓库 | LightRAG |
| 分支 | main |
| 基线提交 | 57ac3d3c24a1fc06d96168e6a5f9f1eb96b4e791 |
| Core 版本 | 1.5.5 |
| API 版本 | 0320 |
| 分析时间 | 2026-07-20T21:53:37+08:00 |
| 当前工作区已有用户改动 | 未跟踪的 .codex/；本文件原为空且未跟踪 |
| 本轮允许写入 | lightrag-tasks.md |

## 3. 代码模型

### 3.1 系统分层

~~~mermaid
flowchart TD
    UI["React 19 WebUI<br/>lightrag_webui/src"] --> API["FastAPI API Server<br/>lightrag/api/lightrag_server.py"]
    API --> DR["Document Routes"]
    API --> QR["Query Routes"]
    API --> GR["Graph Routes"]
    DR --> RAG["LightRAG 实例"]
    QR --> RAG
    GR --> RAG
    RAG --> Roles["_RoleLLMMixin<br/>角色化 LLM/VLM"]
    RAG --> Migrations["_StorageMigrationMixin<br/>存储迁移"]
    RAG --> Pipeline["_PipelineMixin<br/>解析/分析/入库流水线"]
    Pipeline --> Parsers["legacy/native/mineru/docling"]
    Pipeline --> Chunkers["F/R/V/P 分块"]
    Pipeline --> Operate["operate.py<br/>实体关系抽取、合并、检索"]
    RAG --> KV["KV × 7"]
    RAG --> Vector["Vector × 3"]
    RAG --> Graph["Graph × 1"]
    RAG --> Status["DocStatus × 1"]
~~~

### 3.2 核心对象与职责

- lightrag/lightrag.py
  - LightRAG 是最终编排对象，组合 _RoleLLMMixin、_StorageMigrationMixin、_PipelineMixin。
  - 构造阶段验证四类存储，建立角色化 LLM 包装器，并创建 12 个存储对象。
  - initialize_storages() 依次初始化存储和当前 workspace 的 pipeline_status。
  - aquery_llm()/aquery_data() 按 local、global、hybrid、mix、naive、bypass 分派检索。
  - 删除、清缓存、实体/关系编辑和导出也全部绑定到当前实例的 workspace。
- lightrag/pipeline.py
  - apipeline_enqueue_documents() 负责 ID、文件名/内容哈希去重、full_docs 与 doc_status 写入。
  - 写入顺序是 full_docs 先、doc_status 后，避免处理器看到缺少正文的幽灵状态。
  - apipeline_process_enqueue_documents() 在 workspace 级 busy 锁下循环处理 PENDING/中间状态文档。
  - 批处理是三段有界队列：内容解析 → 多模态分析 → 分块/实体关系抽取与合并。
  - 最终先刷新派生 KV/Vector/Graph，再把 doc_status 写成 PROCESSED；状态是提交记录。
- lightrag/operate.py
  - 负责实体/关系抽取、描述合并、图与向量写入、关键词提取和查询上下文组装。
  - local/global/hybrid/mix 走 kg_query；naive 只走 chunks_vdb；最终可进入 QUERY 角色 LLM。
- lightrag/parser 与 lightrag/chunker
  - parser/routing.py 统一解析环境规则、文件名 hint、解析器参数与 process_options。
  - 解析器包括 legacy、native、MinerU、Docling；分块包括 F/R/V/P。
  - 上传文件、解析 sidecar、归档和缓存均与输入目录布局有关，因此也必须纳入知识库隔离。
- lightrag/kg
  - base.py 定义 KV、Vector、Graph、DocStatus 抽象。
  - factory.py 和 kg/__init__.py 负责存储驱动注册与延迟加载。
  - shared_storage.py 提供 workspace 级共享状态、锁、预约、后台任务和多进程协作。
- lightrag/api
  - lightrag_server.py 当前只在 create_app() 内创建一个固定 LightRAG 和一个固定 DocumentManager。
  - document_routes.py、query_routes.py、graph_routes.py 的路由工厂闭包都捕获这个固定实例。
  - LIGHTRAG-WORKSPACE 请求头当前仅被 /health 用于读取 pipeline_status，并不会选择上传、查询或图操作的数据实例。
- lightrag_webui
  - api/lightrag.ts 是统一 Axios/流式 fetch 客户端。
  - stores/settings.ts 持久化 UI、图和检索设置；stores/state.ts 保存健康状态；stores/graph.ts 保存图视图缓存。
  - UploadDocumentsDialog.tsx 当前只上传文件，不携带知识库选择或隔离模式。
  - 文档、图、检索三个页面共享同一后端数据域，切换知识库后必须一起失效和刷新。

### 3.3 当前一个 LightRAG 实例创建的存储集合

| 类型 | 实例/命名空间 |
| --- | --- |
| KV | llm_response_cache、text_chunks、full_docs、full_entities、full_relations、entity_chunks、relation_chunks |
| Vector | entities、relationships、chunks |
| Graph | chunk_entity_relation |
| DocStatus | doc_status |

结论：要实现真正不串库，不能只隔离图或文档列表；以上 12 个存储、输入文件、解析产物、pipeline_status、锁和后台任务都必须使用同一个不可变隔离键。

### 3.4 当前 workspace 的实际隔离语义

| 后端 | 当前 workspace 落点 | 准确定性 |
| --- | --- | --- |
| JSON/NanoVector/NetworkX | working_dir/workspace 子目录 | 文件物理分目录 |
| 上传与解析文件 | input_dir/workspace 子目录 | 文件物理分目录 |
| Redis KV/DocStatus | final_namespace 前缀 | 同一 Redis 内的逻辑键空间隔离 |
| PostgreSQL KV/Vector/DocStatus | 共享表的 workspace 列 | 同一数据库内的行级逻辑隔离 |
| PostgreSQL AGE Graph | workspace_namespace 图名 | 同一 PostgreSQL 内的独立 AGE graph/schema |
| Neo4j Graph | workspace 标签和对应索引 | 同一 Neo4j database 内的标签级逻辑隔离 |

重要限制：

- POSTGRES_WORKSPACE、NEO4J_WORKSPACE、REDIS_WORKSPACE 等驱动专属覆盖变量会把动态实例强制压回同一 workspace；多知识库模式必须禁止或启动时失败。
- “不同前缀/行/标签”不能宣传为独占数据库或独占服务。
- 如果用户选择严格物理隔离，必须使用独立存储连接配置/资源，并在资源不支持时明确拒绝，不能静默降级为逻辑隔离。

### 3.5 当前文档写入与查询链路

文档写入：

~~~text
WebUI/API 上传或文本插入
→ DocumentManager 保存到固定 input_dir/workspace
→ 文档路由预约当前 workspace 的 enqueue slot
→ full_docs + doc_status(PENDING)
→ parse worker
→ analyze worker
→ chunker
→ text_chunks + chunks_vdb
→ entity/relation extraction
→ graph + entity/relation KV/VDB
→ 刷新全部派生存储
→ doc_status(PROCESSED)
~~~

查询：

~~~text
WebUI/API 请求
→ 固定 LightRAG
→ local/global/hybrid/mix: KG + entity/relation/chunk 检索
→ naive: chunk vector 检索
→ rerank/token budget/reference 组装
→ query LLM
→ 当前 LightRAG 的 llm_response_cache
~~~

### 3.6 任务 1 的真正改造点

当前缺口不是底层 workspace 完全不存在，而是缺少：

1. 持久化的“知识库”领域对象与生命周期。
2. 请求级知识库选择。
3. 并发安全、可复用、可关闭的 LightRAG/DocumentManager 实例管理器。
4. 所有文档、查询、图、状态、缓存接口的一致路由。
5. WebUI 的全局知识库选择、上传目标模式和切库后的状态失效。
6. 对默认 workspace 现有数据的无迁移破坏兼容。
7. 对逻辑隔离与严格物理隔离的真实能力声明。

## 4. 阶段状态总览

| 阶段 | 状态 | 完成标准 |
| --- | --- | --- |
| A. 架构确认与 ADR | 已完成 | 隔离语义、API 契约、兼容策略和物理模式边界明确 |
| B. 后端知识库目录与实例管理 | 已完成 | 并发安全创建/选择/初始化/关闭实例 |
| C. API 全链路改造 | 已完成 | 文档、查询、图、健康状态均按知识库路由 |
| D. WebUI 控制 | 已完成 | 可新建独立库、增量写入、切换并查看正确数据 |
| E. 测试与回归 | 已完成 | 后端、前端、并发和隔离回归通过 |
| F. 本地基础设施 | 已完成 | PostgreSQL、Neo4j、Redis Compose 健康 |
| G. 前后端启动与 E2E | 已完成 | 服务、WebUI、四类存储和真实最小流程验证通过 |
| H. 文档与交付 | 已完成 | 配置、运行手册、限制、验证证据齐全 |
| I. 第二轮 Prompt 与全量审计 | 进行中 | Prompt 冻结；上传、OpenAPI、全部注册存储的差距矩阵有代码证据 |
| J. 上传目标体验优化 | 待开始 | “新建独立知识库”置顶；所有存量库以名称和 ID 可选并正确上传 |
| K. OpenAPI 请求头完整发布 | 待开始 | 所有知识库数据面 API 均在 OpenAPI/Swagger 中显式提供知识库请求头 |
| L. 全存储隔离补齐 | 待开始 | 每个注册后端的 logical/physical 能力明确、实现完整且不静默降级 |
| M. 第二轮自动化与浏览器验收 | 待开始 | 后端、前端、构建、OpenAPI、逐后端和真实 WebUI 验证通过 |
| N. 第二轮文档与交付 | 待开始 | 隔离矩阵、限制、证据和最终时间线完整 |

## 5. 可直接交给 Codex GPT-5.6 Sol 的完整主 Prompt

下面内容可作为一个新的 Codex 任务直接使用。建议使用 GPT-5.6 Sol，High 或 Extra High reasoning。该 Prompt 按“目标、上下文、约束、完成条件”组织，并要求先计划、持续验证和回顾。

~~~text
你正在 D:\code\codex\LightRAG 仓库中完成一个高价值、跨后端与前端的长期任务。请把自己当作该仓库的资深维护者：先基于实际代码建立证据，再设计、实现、测试、启动并验证。不要只给方案；除非遇到明确的秘密或外部权限阻塞，否则持续推进到完成条件全部满足。

一、最终目标

完成两项连续任务：

1. 为 LightRAG 新增“多知识库隔离”能力。用户上传文件或插入文本时可以：
   - 增量写入当前已有知识库；
   - 创建一套新的独立知识库并写入其中。

   每个知识库必须拥有一致且完整的数据边界：独立 LightRAG 实例生命周期、Graph、全部 KV（包括 LLM/query/extraction cache）、全部 Vector、DocStatus、输入文件、解析产物、pipeline_status、并发锁和后台任务。API 和 WebUI 都必须能创建、选择和使用知识库；查询、图浏览、文档管理、删除、清空、清缓存和状态读取不能串库。

2. 完成任务 1 后，阅读 README.md、README-zh.md、docs/LightRAG-API-Server*.md、docs/DockerDeployment.md、docs/InteractiveSetup.md、docs/FrontendBuildGuide.md 及相关配置，实际在本机启动完整服务：
   - 后端与 WebUI；
   - 四类存储；
   - KV 使用 RedisKVStorage；
   - Vector 使用 PGVectorStorage；
   - Graph 使用 Neo4JStorage；
   - DocStatus 使用 PGDocStatusStorage；
   - PostgreSQL、Neo4j、Redis 优先用额外的本地 Docker Compose 基础设施部署；
   - LLM 和 Embedding 使用 OpenAI-Compatible API。

二、必须先确认的仓库事实

开始前重新检查当前代码，不要仅依赖本 Prompt。重点确认：

- LightRAG 由 _RoleLLMMixin、_StorageMigrationMixin、_PipelineMixin 组成。
- 一个实例持有 7 个 KV、3 个 Vector、1 个 Graph、1 个 DocStatus 存储。
- pipeline.py 的 enqueue/processing 并发契约、full_docs 与 doc_status 写入顺序、busy/scanning/destructive_busy/pending_enqueues/request_pending 语义不能被破坏。
- lightrag_server.py 当前只创建一个固定 rag 和 DocumentManager；三个路由工厂捕获固定 rag。
- LIGHTRAG-WORKSPACE 头当前不等于完整请求级实例路由。
- 文件型存储按 workspace 分目录；PostgreSQL 多数存储按 workspace 列；Neo4j 按 workspace 标签；Redis 按 key prefix。
- POSTGRES_WORKSPACE、NEO4J_WORKSPACE、REDIS_WORKSPACE 等覆盖变量会破坏动态知识库隔离。
- 默认知识库必须继续访问 args.workspace 当前已有数据，不能把现有数据悄悄迁移到一个新组合键。

三、执行方式

1. 先检查 AGENTS.md、git status、相关实现和测试。保护用户已有改动，不覆盖无关文件。
2. 先写一个简洁 ADR/实施计划，再编码。ADR 至少比较：
   - 直接修改全局 rag.workspace；
   - 每请求临时构造 LightRAG；
   - 按知识库缓存独立 LightRAG 的实例管理器。
   默认应选择“不可变知识库隔离键 + 并发安全实例管理器”，并说明原因。
3. 维护根目录 lightrag-tasks.md：
   - 更新阶段状态总览；
   - 每完成一个阶段就在时间线末尾追加时间、改动、验证、风险和下一步；
   - 不删除历史日志；
   - 不写秘密。
4. 做出小而可审查的改动；每个 bug fix 都增加回归测试。
5. 先跑相关测试，再跑更广测试；失败要定位并修复，不要只报告。
6. 实现后检查 diff、兼容性、安全性、资源释放和错误路径。

四、领域模型与隔离语义

新增明确的 KnowledgeBase（知识库）领域对象，不要把“服务器部署 workspace”和“用户可切换知识库”混成同一个概念。

KnowledgeBase 至少包含：

- id：不可变、安全、唯一，建议 UUID 或严格校验的短 ID；
- name：用户可读名称；
- base_workspace：服务器原有 workspace；
- effective_workspace：实际传给 LightRAG/存储的不可变隔离键；
- isolation_level：logical 或 physical；
- storage_profile_id：严格物理模式需要，逻辑模式为空；
- created_at、updated_at；
- is_default；
- 可选 description。

兼容规则：

- 无知识库选择信息的所有旧 API 请求继续落到默认知识库。
- 默认知识库的 effective_workspace 必须精确等于现有 args.workspace；空 workspace 也要保持原行为。
- 新逻辑隔离知识库的 effective_workspace 使用确定、可验证、无碰撞的组合规则，例如 base workspace + kb marker + immutable id。
- 展示名称绝不能直接作为文件路径、SQL 标识、Neo4j 标签或 Redis 前缀。
- 不能通过修改一个共享 LightRAG 对象的 workspace 来切库；实例构造后隔离键不可变。

隔离级别必须诚实：

- logical：本次必须完整实现。每个知识库有独立 LightRAG 对象和独立 effective_workspace；文件与解析产物物理分目录，外部共享服务用各自可靠的 workspace/namespace 机制隔离。
- physical：表示独占的存储连接配置/数据库/服务资源，不是换一个 prefix。通过受控的 storage profile 提供。至少为目标栈 PostgreSQL、Neo4j、Redis 设计并实现显式配置入口与校验；不要求运行时自动创建 Docker 容器。
- 当 physical 没有配置 storage_profile_id、Neo4j Community 不支持目标数据库能力、或连接配置与默认资源实际相同时，必须返回清晰错误，禁止静默降级。
- 如果严格物理模式需要大幅重构现有驱动的全局环境变量读取，先在 ADR 中列出最小可行范围和后续范围；逻辑隔离与本地共享服务部署不得因此停滞。

五、后端实现要求

1. 知识库目录

- 新增持久化的知识库 catalog。
- catalog 不得放在某个用户知识库自己的 full_docs/KV 中。
- 支持 list/get/create/rename/delete 或归档；默认库不可被误删。
- 写入使用仓库已有原子写/锁模式，兼容单进程与 Gunicorn 多 worker。
- 服务重启后知识库列表和 id 保持稳定。
- 删除知识库必须是显式危险操作：检查该库 pipeline 空闲、要求确认、先停止/释放实例，再仅删除目标库的数据；失败要可诊断，不能影响其他库。

2. 实例管理器

- 从 lightrag_server.py 当前超长的 LightRAG 构造逻辑中抽取可复用 factory。
- 新增并发安全的 KnowledgeBaseManager/RAGInstanceManager：
  - 以 knowledge_base_id 缓存一个初始化完成的 LightRAG 和对应 DocumentManager；
  - 同一知识库并发首次访问只初始化一次；
  - 初始化失败不留下半初始化缓存；
  - 不同知识库可并行；
  - 服务关闭时取消/排空受管后台任务并 finalize 所有实例；
  - 防止无限实例增长，提供合理的容量/空闲回收策略或说明为何当前部署采用有界常驻；
  - 复用安全的 provider 配置，但不能共享会造成跨库污染的存储对象或可变运行状态。
- 检查 PostgreSQL/Redis 连接池和 Neo4j driver 生命周期，避免每次请求创建连接或过早关闭共享连接。
- 检查多 worker 下 catalog、实例初始化、pipeline_status 和后台任务的行为。

3. 请求级上下文

- 建立统一 FastAPI dependency/resolver，一次解析并校验知识库选择，然后给路由返回 KnowledgeBaseContext（metadata、rag、doc_manager）。
- 推荐使用清晰的新请求头 LIGHTRAG-KNOWLEDGE-BASE；也可使用显式 path，但必须全 API 一致。
- 不要继续让文档/查询/图路由直接闭包捕获单个 rag。
- 错误语义至少覆盖：知识库不存在、ID 非法、知识库正在删除/初始化失败、物理 profile 不可用。

4. API

新增知识库管理 API，至少包含：

- GET /knowledge-bases；
- POST /knowledge-bases；
- GET /knowledge-bases/{id}；
- PATCH /knowledge-bases/{id}；
- DELETE /knowledge-bases/{id}，带明确确认。

数据面 API 必须全部按选中知识库路由：

- /documents 下的 upload、text、texts、scan、list、paginated、status_counts、track_status、pipeline_status、reprocess、delete、clear、clear_cache、cancel、recovery reset；
- /query、/query/stream、/query/data；
- /graphs 和所有 graph entity/relation CRUD；
- /health 中与 pipeline、workspace、storage workspace 有关的字段；
- Ollama 兼容查询接口也要明确其知识库选择和默认行为。

上传控制：

- API 必须能表达“写入现有库”和“创建独立库后写入”。
- 推荐把创建知识库作为显式、可重试的 API，再把上传绑定到返回的 id；如果同时提供单请求 isolated 模式，必须处理创建成功但上传失败的补偿和幂等。
- 多文件批次在“创建独立库”模式下默认进入同一个新知识库；在 API 文档和 UI 中写清楚。
- InsertResponse/相关响应返回 knowledge_base_id，便于后续跟踪。

5. 安全和配置

- knowledge_base_id、effective_workspace、路径和存储标识必须严格验证，拒绝路径穿越和标识注入。
- catalog/API 不能返回 storage profile 密码、API Key 或完整秘密连接串。
- 多知识库开启时检测并拒绝 POSTGRES_WORKSPACE、NEO4J_WORKSPACE、REDIS_WORKSPACE 等会压平隔离的配置。
- 任何删除/清空只影响选中知识库，并有跨库负向测试。
- 保持现有认证依赖；知识库管理接口不能意外绕过认证。

六、WebUI 实现要求

1. 增加知识库 store：

- 拉取知识库列表；
- 保存当前 knowledge_base_id；
- 默认选中默认库；
- 本地持久化时处理已删除/无权限库的回退；
- 所有 Axios 请求和流式 fetch 请求都携带同一知识库头。

2. 在全局可见位置提供知识库选择器，至少显示名称、隔离级别和当前状态。

3. 上传对话框提供清晰选择：

- 增量加入当前知识库；
- 新建独立知识库后上传；
- 新建时输入名称并选择 logical/physical；physical 需要选择可用 storage profile。

4. 切换知识库时：

- 终止或禁止遗留的进行中查询；
- 清空/重置图 store、标签缓存、文档分页与状态轮询；
- 检索历史应按知识库分区，或明确切换时清空，禁止把 A 库对话上下文发送到 B 库；
- 立即重新读取 health、documents、pipeline status、graph labels；
- 避免旧请求晚到后覆盖新知识库 UI，可使用请求版本或 AbortController。

5. 更新 lightrag_webui/src/locales 下所有现有语言键；至少确保英文和简体中文文案完整，其他语言可先使用一致英文回退，但不能造成构建缺键。

6. 保持 React 19、Zustand、Axios、Bun 测试和现有样式约定。

七、测试与验收要求

后端至少增加：

- catalog CRUD、持久化、校验、默认库保护测试；
- 实例管理器并发首次初始化一次、失败清理、shutdown finalize 测试；
- 默认库向后兼容测试；
- 两个逻辑知识库分别写入同名文件、同内容、同实体，互不判重且互不可见；
- A 库上传后，B 库的文档列表、查询、图、缓存、状态均看不到 A；
- A 库 clear/delete/cache clear 不影响 B；
- pipeline busy/scanning/delete 只阻塞目标库，不错误阻塞其他库；
- 请求头非法/缺失/不存在的错误测试；
- DB 专属 workspace 覆盖变量的 fail-fast 测试；
- physical profile 缺失或不支持时不降级测试；
- query/graph/document 路由解析到正确实例的 API 测试；
- Gunicorn/多进程相关逻辑能用 mock 或现有 shared_storage 测试覆盖的部分。

前端至少增加：

- Axios interceptor 和 queryTextStream fetch 都带知识库头；
- knowledge base store 选择、回退、持久化迁移测试；
- 上传模式请求负载测试；
- 切库时图/检索/文档状态失效的纯逻辑测试；
- bun test、bun run lint、bun run build 通过。

遵循仓库命令：

- 后端优先使用 ./scripts/test.sh 指定测试文件，再逐步扩大。
- 运行 ruff check . 或至少运行所有改动 Python 文件。
- 前端使用 Bun 内置测试，不引入 Vitest/Jest。
- 涉及真实 DOM 行为时启动开发服务，用仓库指定的浏览器/Playwright 工作流验证。
- 报告精确通过/失败/跳过数量；不能只写“测试通过”。

八、本地部署与启动

任务 1 全部相关测试通过后再进入本阶段。

1. 先阅读并遵守仓库启动文档和 Setup Wizard 约束。不要直接调用 scripts/setup/setup.sh；优先使用 make env-*，或在 Windows 不便使用 make 时手工生成等价、可审查配置。

2. 新增一个只部署基础设施的 Compose 文件，建议命名 docker-compose.local-infra.yml：

- PostgreSQL 使用带 pgvector 的稳定镜像；本任务 Graph 使用 Neo4j，因此无需为 PGGraphStorage 强制 Apache AGE。
- Neo4j 使用与仓库文档兼容的版本。
- Redis 使用配置文件和持久卷。
- 三个服务都有 healthcheck、持久卷、明确端口和 restart 策略。
- 使用独立的项目/卷命名，避免碰撞用户其他容器。
- 不把 LightRAG 应用塞进该 Compose，除非额外提供第二个可选全栈 Compose；默认让后端和前端在宿主机开发环境运行。
- 不执行 docker compose down -v，不删除已有卷。

3. 生成安全的示例配置和本地运行说明：

- LIGHTRAG_KV_STORAGE=RedisKVStorage
- LIGHTRAG_VECTOR_STORAGE=PGVectorStorage
- LIGHTRAG_GRAPH_STORAGE=Neo4JStorage
- LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
- REDIS_URI、POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE、NEO4J_URI/USERNAME/PASSWORD/DATABASE
- LLM_BINDING=openai
- EMBEDDING_BINDING=openai
- LLM/Embedding 的 model、host、key 和 EMBEDDING_DIM
- 不设置 POSTGRES_WORKSPACE、NEO4J_WORKSPACE、REDIS_WORKSPACE
- VLM、MinerU、Docling、reranker 若本次不用则显式关闭，缩小启动面。

4. 秘密闸门：

- 不猜测、伪造或提交 API Key。
- 在真正启动前，如果缺少 OpenAI-Compatible 配置，只向用户集中询问一次：
  - LLM base URL；
  - LLM model；
  - LLM API Key；
  - Embedding base URL（若相同可复用）；
  - Embedding model；
  - Embedding dimension；
  - Embedding API Key（若可复用需用户确认）。
- 将真实秘密只写入 gitignored 的本地 .env；示例文件只保留占位符。

5. 实际启动：

- 检查 Docker、uv、Python、Bun 版本。
- 启动基础设施并等待三个 healthcheck 健康。
- 使用 uv sync --extra test --extra offline 或仓库文档认可的等价方式安装依赖。
- 在 lightrag_webui 执行 bun install --frozen-lockfile、bun test、bun run lint、bun run build。
- 启动 lightrag-server；生产构建由后端在 /webui 提供。
- 如需前端热更新，再单独运行 bun run dev，并按 Vite proxy 配置访问。

6. 真实 E2E 验证：

- GET /health 返回 healthy，且四类 storage 名称与配置一致。
- 打开 /webui，控制台无未处理错误。
- 创建知识库 A 和 B。
- 在 A 上传一个小型测试文档并等待 PROCESSED。
- 在 A 查询得到该文档内容和引用，图接口能看到 A 的节点/关系。
- 切到 B，确认文档、查询、图和缓存看不到 A。
- 在 B 上传相同文件名/相同内容，应允许并只存在于 B。
- 回到 A，数据保持不变。
- 重启后端，再次确认 catalog、A/B 选择与数据仍然正确。
- 记录使用的命令、端口、容器健康状态、测试结果和必要截图/响应摘要；不得记录秘密。

九、建议改动落点

实际文件名可根据仓库风格调整，但优先保持职责清晰：

- lightrag/api/knowledge_bases.py：领域模型、catalog、校验；
- lightrag/api/rag_manager.py：factory、实例生命周期、请求 context；
- lightrag/api/routers/knowledge_base_routes.py：管理 API；
- 重构 lightrag/api/lightrag_server.py；
- 重构 document_routes.py、query_routes.py、graph_routes.py 和 Ollama API，使其使用 resolver；
- lightrag_webui/src/api/lightrag.ts；
- 新增 knowledge base Zustand store 和选择器组件；
- 修改 UploadDocumentsDialog、DocumentManager、GraphViewer/RetrievalView 的切库行为；
- tests/api、tests/workspace、tests/pipeline 下对应回归测试；
- lightrag_webui 对应 Bun 测试；
- docker-compose.local-infra.yml；
- 本地部署说明和不含秘密的 env 示例；
- lightrag-tasks.md 时间线。

十、禁止事项

- 不要用一个全局可变 rag.workspace 在请求间切换。
- 不要只改上传接口而遗漏查询、图、缓存、删除、状态和 Ollama API。
- 不要把 Redis prefix、PostgreSQL workspace 行或 Neo4j label 宣传成独占物理服务。
- 不要设置任何驱动专属 WORKSPACE 覆盖变量。
- 不要破坏默认知识库对旧数据和旧 API 的兼容。
- 不要提交真实 Key/密码。
- 不要依赖真实外部数据库做单元测试；单元测试使用 mock。
- 不要在未经明确确认时删除数据库、容器卷或用户数据。
- 不要在测试失败、只完成后端或只完成 UI 时宣称任务完成。

十一、完成条件

只有同时满足以下条件才能宣告完成：

1. 知识库 catalog、实例管理、请求路由、API、WebUI 全部完成。
2. 默认知识库保持现有数据和无 header 客户端兼容。
3. A/B 两库在文档、图、Vector、全部 KV/Cache、DocStatus、文件、解析产物和 pipeline 状态上有自动化隔离证据。
4. logical/physical 的能力和限制真实、可验证；physical 不可用时明确失败。
5. 后端相关测试、前端测试、lint、build 通过，并报告数量。
6. PostgreSQL、Neo4j、Redis 本地基础设施健康。
7. 使用 OpenAI-Compatible API 实际完成一次上传、处理、查询、图查看和跨库负向验证。
8. 服务重启后知识库 catalog 与数据仍可用。
9. 文档包含从零启动、停止、重启、排障和数据位置说明。
10. lightrag-tasks.md 阶段状态和时间线已更新到最终结果。

十二、最终汇报格式

最终回复先给结果，再给：

- 实现摘要；
- 关键架构决策；
- 主要改动文件；
- API/UI 使用方式；
- logical/physical 隔离矩阵；
- 测试命令与精确结果；
- 本地运行地址、容器健康状态和 E2E 证据；
- 未解决风险或明确没有遗留项；
- lightrag-tasks.md 的最新时间线位置。

现在开始：先读取 AGENTS.md、git status 和上述关键代码，更新 lightrag-tasks.md 的阶段 A 为进行中，写 ADR 与可执行计划，然后持续实施。除非到达秘密闸门或出现需要用户决定且会实质改变架构的事项，不要停在问题列表或纯计划上。
~~~

## 6. 预期 API 契约草案

这是任务执行时的默认契约，不替代实现阶段的 ADR。

| 行为 | 方法与路径 | 选择方式 |
| --- | --- | --- |
| 列出知识库 | GET /knowledge-bases | 管理面，不依赖当前库 |
| 创建知识库 | POST /knowledge-bases | name + isolation_level + 可选 storage_profile_id |
| 查看/改名 | GET/PATCH /knowledge-bases/{id} | path id |
| 删除知识库 | DELETE /knowledge-bases/{id} | path id + confirm |
| 数据面请求 | 现有文档/查询/图 API | LIGHTRAG-KNOWLEDGE-BASE header |
| 兼容旧客户端 | 现有 API 不带 header | 默认知识库 |

建议 KnowledgeBaseContext：

~~~text
KnowledgeBaseContext
├─ metadata
├─ rag
├─ document_manager
├─ effective_workspace
└─ isolation/storage profile summary（不含秘密）
~~~

## 7. 本地目标部署拓扑

~~~mermaid
flowchart LR
    Browser["Browser / WebUI"] --> Vite["可选 Vite :5173"]
    Browser --> API["LightRAG FastAPI :9621"]
    Vite --> API
    API --> Redis["Redis :6379<br/>KV"]
    API --> PG["PostgreSQL + pgvector :5432<br/>Vector + DocStatus"]
    API --> Neo4j["Neo4j :7687/:7474<br/>Graph"]
    API --> OA["OpenAI-Compatible API<br/>LLM + Embedding"]
~~~

四类存储目标映射：

| 存储类型 | 实现 |
| --- | --- |
| KV_STORAGE | RedisKVStorage |
| VECTOR_STORAGE | PGVectorStorage |
| GRAPH_STORAGE | Neo4JStorage |
| DOC_STATUS_STORAGE | PGDocStatusStorage |

## 8. 需要用户提供的信息（到秘密闸门时再询问）

- OpenAI-Compatible LLM base URL
- LLM model
- LLM API Key
- Embedding base URL
- Embedding model
- Embedding dimension
- Embedding API Key 或是否确认复用 LLM Key

当前状态：尚未请求，尚未写入任何秘密。

## 9. 时间线日志（只追加）

### 2026-07-20T21:53:37+08:00 — 仓库分析与主 Prompt 建立

- 阶段：任务准备
- 状态：已完成
- 目标：理解完整代码主链路，识别隔离缺口，生成可长期执行和持续记录的中文 Prompt。
- 实际动作：
  - 检查 git 状态、仓库结构、版本和现有空任务文件。
  - 追踪 LightRAG 构造、12 个存储实例、initialize/finalize、查询分派。
  - 追踪 pipeline enqueue、三段 worker、提交顺序和 workspace 并发契约。
  - 追踪 JSON/Nano/NetworkX、PostgreSQL、Neo4j、Redis 的 workspace 实现。
  - 追踪 FastAPI 的固定 rag/DocumentManager 路由方式和 /health header 行为。
  - 追踪 WebUI API 客户端、Zustand stores、上传、文档、图和检索页面。
  - 阅读 README/API Server/Docker/Interactive Setup/Frontend Build 相关入口和 Compose 模板。
  - 依据 OpenAI Codex 官方 Best practices 与 Prompting 指南，把主 Prompt 按目标、上下文、约束、完成条件、计划、测试和回顾组织。
- 关键结论：
  - 底层 workspace 已能隔离大部分数据，但 API 服务器仍是单固定实例，当前不具备用户请求级多知识库能力。
  - 新功能必须引入持久化知识库 catalog、实例管理器和统一请求上下文，不能只给 upload 增加参数。
  - 外部共享服务的 prefix/row/label 属于逻辑隔离；严格物理隔离必须用独立 storage profile 且不能静默降级。
  - 默认知识库必须继续使用原 args.workspace，保证旧数据与旧客户端兼容。
- 改动文件：
  - lightrag-tasks.md
- 验证：
  - 基线提交：57ac3d3c24a1fc06d96168e6a5f9f1eb96b4e791。
  - Core/API 版本：1.5.5 / 0320。
  - 本轮未运行代码测试，因为只创建任务分析与 Prompt 文档，未修改生产代码。
- 风险/阻塞：
  - “物理隔离”需要区分共享服务内的存储命名空间与独占数据库/服务资源；主 Prompt 已要求 ADR 和真实能力声明。
  - 实际启动阶段需要用户提供 OpenAI-Compatible LLM/Embedding 配置和秘密。
- 下一步：
  - 使用第 5 节主 Prompt 启动实施，先完成阶段 A 的 ADR 和测试计划。

### 2026-07-20T22:08:10+08:00 — 阶段 A：隔离架构 ADR 与实施启动

- 阶段：A（架构决策与测试设计）
- 状态：进行中
- ADR-001 请求路由：新增持久化 Knowledge Base Catalog，并通过 `LIGHTRAG-KNOWLEDGE-BASE` 请求头选择知识库；不带请求头时严格回退到 `default`，其 effective workspace 保持现有 `args.workspace`，保证存量数据和旧客户端兼容。
- ADR-002 实例边界：每个知识库拥有独立 `LightRAG`、`DocumentManager`、pipeline namespace 和输入目录；API 路由通过 request-scoped context 解析实例，后台任务继承创建时上下文，避免只隔离上传而查询/图仍串库。
- ADR-003 生命周期：默认实例随服务启动；其他实例按需初始化并执行 migration；服务关闭统一等待后台任务后再 finalize 全部已加载实例。实例数设置上限，超过上限明确失败，不在活跃请求期间隐式淘汰。
- ADR-004 隔离等级：`logical` 使用唯一 immutable effective workspace 隔离共享 PostgreSQL/Neo4j/Redis 内的数据；`physical` 只接受显式配置且能证明独占资源的 storage profile。当前进程级存储连接配置不能安全地为并存实例切换不同连接，因此未配置可验证 profile 时必须拒绝创建，绝不静默降级为 logical。
- ADR-005 管理面：新增 `/knowledge-bases` CRUD；删除要求 `confirm=true`、禁止删除默认库、拒绝忙碌/活跃中的库，并清理该实例全部存储与输入文件。
- 自动化测试计划：catalog 原子持久化/重启、ID 与 workspace 校验、默认回退、未知库拒绝、A/B 路由、并发上下文、12 类存储 workspace 一致性、CRUD 删除保护、physical fail-closed、前端请求头与选择器状态、构建/lint。
- 改动文件：
  - lightrag-tasks.md
- 下一步：
  - 实现 catalog、manager、request context 和后端回归测试，再接入现有 document/query/graph/Ollama/health 路由。

### 2026-07-20T23:35:46+08:00 — 阶段 A-D：多知识库实现、UI、存储与本地部署验证

- 阶段：A（ADR）、B（后端）、C（WebUI）、D（本地基础设施）
- 状态：已完成；阶段 E 的真实模型解析/查询 E2E 等待用户提供 OpenAI-Compatible 配置。
- 后端实现：
  - 新增 crash-safe `KnowledgeBaseCatalog`、immutable effective workspace、请求级 `ContextVar`、独立 `LightRAG`/`DocumentManager` 生命周期管理与最多加载实例限制。
  - 新增 `/knowledge-bases` CRUD；删除必须 `confirm=true`，默认库不可删除，忙碌/活动/并发删除明确拒绝，成功时 drop 12 类存储、finalize 实例并删除独立输入目录。
  - document/query/graph/Ollama 全部通过统一 router dependency 绑定 `LIGHTRAG-KNOWLEDGE-BASE`；无 header 继续落到原 `args.workspace` 对应的 default，未知库返回 404。
  - `/health` 支持所选知识库并在鉴权后返回 selected workspace、四类 storage workspace 与所选目录；真实 HTTP 验收发现并修复 unknown KB 误报 500 的问题。
  - physical 模式通过管理员 storage profile 为 PostgreSQL、Neo4j、Redis 和文件目录注入每实例连接；profile 必须 `dedicated=true`、结构完整、只绑定一个知识库，否则 fail closed，catalog/health 不输出秘密。
  - Redis URL 日志脱敏；PostgreSQL profile 使用独立共享 pool/refcount；Neo4j profile 使用独立 URI/认证/database；驱动级全局 `*_WORKSPACE` 与动态 logical KB 冲突时拒绝创建。
  - catalog 额外拒绝重复 ID/effective workspace；并发删除由原子 reservation 串行化。
- WebUI 实现：
  - 顶栏新增持久化知识库选择器，切库时完整 remount 以清理 polling、WebGL graph 与流式查询；API key 更新后自动重载目录，已删除选择自动回退 default。
  - Axios 与 NDJSON streaming fetch 都统一注入非 default 的知识库 header。
  - 上传对话框提供“增量加入当前知识库”和“新建独立知识库”；新建后本批文件写入新的 RAG、Graph、Cache、文档目录和 pipeline namespace。
  - 使用 React best-practices 检查异步 Effect、状态边界和可访问性；刷新按钮补充 accessible name。
- 部署与文档：
  - 新增 `docker-compose.local-multikb.yml`；默认只启动 PostgreSQL 18 + pgvector、Neo4j 5.26、Redis 7.4，`app` profile 可选构建前后端一体镜像。
  - 数据库端口只绑定 `127.0.0.1`；三个服务均有 healthcheck、restart 策略和持久卷；Redis 使用 `deploy/local-multikb/redis.conf`。
  - 新增无秘密 env/profile 示例和 `docs/LocalMultiKnowledgeBaseDeployment-zh.md`，包含宿主/容器启动、停止、重启、logical/physical、排障和数据位置。
- 自动化验证：
  - Ruff：全部改动 Python 文件通过。
  - Backend：相关 API/路由/PostgreSQL/Redis/Neo4j 回归 `158 passed, 6 skipped`；skip 为显式数据库集成标记，单元测试不依赖外部服务。
  - WebUI：Bun 全量 `67 pass, 0 fail`；ESLint 0 error/0 warning；Vite production build 成功，5263 modules transformed。
  - Compose：默认基础设施模式与 `--profile app` 两种 `config --quiet` 均通过。
- 真实本地启动证据（未调用模型）：
  - PostgreSQL `pg_isready` accepting connections；Redis `PONG`；Neo4j Cypher `RETURN 1` 成功，三个容器最终均为 healthy，端口 5432/6379/7474/7687。
  - 宿主 `lightrag-server` 在 9621 启动成功，生产 WebUI 由 `/webui` 提供；默认存储完成 Redis 连接、PG 表/pgvector/HNSW 初始化、Neo4j 全文索引初始化。
  - 真实 HTTP 创建 logical KB `kb_2c661b43b295` 后，health 显示 KV/Vector/Graph/DocStatus workspace 全部等于该 ID，输入目录独立；unknown header 为 404。
  - 浏览器验收确认顶部 Default/独立库切换、上传目标两种模式、独立隔离说明、Connected 状态和 0 console errors。
  - 真实删除验收：default 删除返回 409；测试库 12 个 storage 执行 drop/finalize，输入目录移除，catalog 仅剩 default，旧 header 返回 404。
- 环境事件：
  - 可选 app 镜像首次构建两次被外部镜像网络阻断：Debian mirror HTTP 500/EOF、Docker Hub TLS handshake timeout；因此改用 README 支持的宿主后端 + Docker 基础设施完成前后端/存储启动验证。该事件不属于代码或 Compose 语法失败。
- 秘密状态：
  - 未提交、未记录、未猜测真实 API Key；本次只用明确标记为 startup validation 的占位值，且没有发起任何 LLM/Embedding 请求。
- 下一步（秘密闸门）：
  - 用户提供 LLM base URL/model/key 与 Embedding base URL/model/dimension/key 后，写入 gitignored 本地 `.env`，执行 A/B 同名同内容文档解析、查询/引用、Graph、Cache 负向隔离和服务重启持久化验证，再追加阶段 E 最终时间线。

### 2026-07-21T00:26:00+08:00 — 阶段 E：真实模型 E2E 首轮与集成缺陷修复

- 阶段：E（真实 OpenAI-Compatible API 与 A/B 隔离验收）
- 状态：进行中；代码侧集成缺陷已修复，当前由外部 API 凭据返回 401 阻塞。
- 已完成：
  - 验证用户配置的 LLM/Embedding 地址、模型、维度与两个 key 变量均已设置；全过程未输出或记录 key 内容。
  - 以 RedisKVStorage、PGVectorStorage、Neo4JStorage、PGDocStatusStorage 启动服务，`GET /health` 返回 healthy；三个基础设施容器保持 healthy。
  - 创建两个 logical 知识库并向 A 上传隔离验证文档。真实后台任务发现解析器仍使用进程级 `INPUT_DIR`，导致多知识库实例的文件保存目录与解析查找目录不一致。
  - 修复方式：为 `LightRAG` 增加实例级 `input_dir`，API factory 按默认/physical profile 注入对应输入根目录，pipeline 优先使用实例配置并继续应用 immutable workspace 子目录。
  - 新增回归测试覆盖“全局 INPUT_DIR 指向错误目录，但实例输入目录中的 workspace 文件仍可解析”；相关 source resolver 测试 `3 passed`。
  - 修复后同一文档已完成文件解析和 chunking（1 chunk），不再出现 source file not found，证明上传目录与解析目录隔离链路已贯通。
  - 3072 维 embedding 超过 pgvector 普通 HNSW 的 2000 维索引限制；本地配置已切换为仓库支持的 `HNSW_HALFVEC`，避免仅建表不建索引。
  - 首次模型调用在受限网络环境返回连接失败；获批切换到联网执行后成功到达 OpenRouter，服务端明确返回 HTTP 401 `User not found`，确认网络、路由和 OpenAI 客户端调用链均已贯通，当前 key 未被 OpenRouter 接受。
- 当前测试知识库：A=`kb_5f9176ac67ae`，B=`kb_76f808860a2e`；均为本轮验收数据，不含用户业务数据。
- 阻塞与下一步：
  - 请用户在 gitignored `.env` 中替换有效的 OpenRouter key（LLM 与 Embedding 当前复用同一 key），不要在对话中发送秘密；更新后原地 reprocess A，再完成 A/B 文档、查询、Graph、Redis cache、PostgreSQL workspace、Neo4j label 和服务重启持久化的最终验收。

### 2026-07-21T00:48:54+08:00 — 阶段 E-H：真实 A/B 隔离 E2E、重启持久化与最终交付

- 阶段：E（测试与回归）、F（本地基础设施）、G（全栈 E2E）、H（文档与交付）
- 状态：已完成。
- 模型与本地运行配置：
  - LLM 使用 OpenRouter OpenAI-Compatible API 与 `deepseek/deepseek-v4-flash`；Embedding 最终使用同一 OpenAI-Compatible API 的 `baai/bge-m3`（1024 维）。真实 key 仅存在于 gitignored `.env`，本文件和代码均未记录。
  - `google/gemini-embedding-2` 与 `openai/text-embedding-3-small` 在当前 OpenRouter 账户/提供方策略下返回 403；最小 BGE-M3 探针返回 HTTP 200 和 1024 维，因此选择仓库 Compose 示例已采用的 BGE-M3，并使用普通 HNSW。
  - 服务地址为 `http://127.0.0.1:9621`，生产 WebUI 为 `http://127.0.0.1:9621/webui`；最终服务保持运行。
- 真实 A/B E2E：
  - A=`kb_5f9176ac67ae`，B=`kb_76f808860a2e`。相同的 `isolation-proof.txt`、相同内容和相同 `doc-940fa2fdb38dfcf92a780441486a3a29` 分别在两个 immutable workspace 中处理成功；两库均为 `processed`、各 1 chunk、各 1 个正确文件引用。
  - 上传 B 之前的负向证据：B 的 DocStatus `all=0`、Graph label=0、naive context 查询不包含唯一代码、Redis 前缀键=0、PostgreSQL 活动向量/状态行=0、Neo4j workspace 节点/关系=0；A 同期为 processed、Graph label=9、查询命中唯一代码、Redis 前缀键=24。
  - B 独立处理期间，A 查询仍命中 `QUARTZ-LANTERN-7319`，A pipeline idle 而 B pipeline busy，证明 pipeline 状态与锁按知识库隔离；B 未复用 A 的缓存，单独产生自己的 extraction cache。
  - 两库完成后的 API 证据：A/B 均可通过 header 查询到唯一代码并各返回 `isolation-proof.txt` 引用；Graph API 分别返回 9/8 个标签。
  - Redis 证据：A/B 分别 24/20 个 workspace 前缀键，各有 2 个 `llm_response_cache` 键和 1 个 text chunk；至少一个内部 cache ID 相同但完整 Redis key 前缀不同，直接证明相同计算结果仍物理落在不同 key namespace。
  - PostgreSQL 证据：活动 BGE-M3 表中 A 为 DocStatus/chunk/entity/relation=`1/1/9/9`，B 为 `1/1/8/6`；所有记录通过 `workspace` 列分区。entity/relation/chunk 三张活动表均存在 HNSW cosine 索引。
  - Neo4j 证据：workspace label `kb_5f9176ac67ae` 为 9 节点/9 关系，`kb_76f808860a2e` 为 8 节点/6 关系；没有跨 workspace 边。
  - 文件证据：两个输入目录分别保留 `inputs/<kb-id>/__parsed__/isolation-proof.txt`，没有共享源文件或解析产物目录。
  - 真实生成式查询（非 only-context）在 7.3 秒完成，同时命中唯一代码、创建者 `Lumen Finch`、地点 `Harbor Quill`，并返回 1 个正确引用。
- 重启持久化：
  - 明确停止服务进程并重新启动；重启后 `/health` 为 healthy，catalog 仍包含同 ID 的 A/B，四类存储仍为 Redis/PGVector/Neo4j/PGDocStatus。
  - 重启后 A/B 仍为 processed、doc ID 与 chunk 数不变、Graph label 分别为 9/8、两库向量查询均命中唯一代码并返回引用。
- 集成修复：
  - 真实 E2E 发现上传文件保存在知识库目录但 parser 仍读取进程级 `INPUT_DIR`。为 `LightRAG` 增加实例级 `input_dir`，server factory 按默认或 physical profile 注入根目录，pipeline 优先使用实例配置后再应用 workspace；新增错误全局 INPUT_DIR 下仍解析实例 workspace 文件的回归测试。
  - 修复 Windows MinerU 测试将 URL 编码的 `file://` sidecar 路径与原始路径直接比较的问题：先 URL decode，再比较 resolve 后路径；生产路径格式未改变。
- 自动化验证最终结果：
  - 本轮修复后 Ruff：`All checks passed`。
  - 最终相关后端回归：`125 passed in 3.96s`；此前完整功能相关回归：`158 passed, 6 skipped`。
  - WebUI 全量：`67 pass, 0 fail`；ESLint 0 error/0 warning；Vite production build 成功，5263 modules transformed。
  - Compose 默认基础设施与可选 app profile 配置校验通过；PostgreSQL、Neo4j、Redis 最终均为 healthy，端口仅绑定 `127.0.0.1`。
  - 浏览器生产 WebUI 验收：知识库选择、两种上传目标、切库、Connected 状态正常，0 console errors。
- 交付与限制：
  - logical 模式已完成共享服务内 Redis key、PostgreSQL row、Neo4j label 与文件目录的全链路隔离；physical 模式必须绑定管理员预配置且 `dedicated=true` 的独占 storage profile，缺失/复用/不支持时 fail closed，不会降级为 logical。
  - 两个 E2E 知识库和本地容器卷被保留，便于用户直接在 WebUI 查看；它们仅包含合成验证数据，可后续通过带 `confirm=true` 的知识库删除 API 清理。
  - 本轮创建的 pytest 临时目录已删除；用户原有未跟踪 `.codex/` 未修改。
- 未解决项：无代码功能阻塞；OpenRouter 提供方策略属于外部账户/模型约束，已通过可用的 BGE-M3 配置规避并完成全部验收。

## 10. 第二轮优化：可直接交给 Codex GPT-5.6 Sol 的完整主 Prompt

本 Prompt 基于 2026-07-21 检查到的 OpenAI GPT-5.6 官方模型指引整理。它只保留一次性、结果导向的指令，明确本地自治边界、所需证据和完成条件；执行时应以仓库当前代码为事实源。建议使用 GPT-5.6 Sol，并以 High 或 XHigh reasoning 作为此类跨前后端、跨存储任务的起始配置，再以实际结果评估。

~~~text
<role_and_outcome>
你正在 D:\code\codex\LightRAG 仓库中维护一套已经实现多知识库隔离的 LightRAG 服务。请以资深维护者身份，完成第二轮优化：改善上传目标选择；让知识库请求头在 WebUI API/Swagger 中完整可配置；审计并补齐除 PostgreSQL、Neo4j、Redis 之外所有已注册存储后端的逻辑与严格物理隔离。

不要只输出方案。先用实际代码和生成的 OpenAPI 建立证据，再设计、实现、测试和运行时验证，持续推进到本文的完成条件全部满足。保持现有 PostgreSQL + Neo4j + Redis 隔离、默认知识库兼容、API 路径、数据和用户无关改动不受破坏。
</role_and_outcome>

<current_context>
- 第一轮已经引入 KnowledgeBase catalog、不可变 effective_workspace、KnowledgeBaseManager、每库独立 LightRAG/DocumentManager、LIGHTRAG-KNOWLEDGE-BASE 请求头、WebUI 全局选择器、logical/physical 模式和管理员 storage profile。
- 当前 production WebUI 由 FastAPI 提供，服务通常运行在 http://127.0.0.1:9621/webui；本地 PostgreSQL、Neo4j、Redis 已能正常隔离。
- 已知风险：若 FastAPI dependency 只从原始 Request 读取 header，运行时可能有效，但 OpenAPI 不会生成可编辑的 header parameter；必须以生成的 /openapi.json 和 Swagger/API 页实际行为判断，不能只看运行时代码。
- 仓库的存储注册表和实际构造路径是完整清单的唯一事实源。预期会涉及 JSON、NetworkX、NanoVectorDB、Faiss、PostgreSQL、Redis、Neo4j、MongoDB、Milvus、Qdrant、Memgraph、OpenSearch 等实现，但必须从 STORAGE_IMPLEMENTATIONS/STORAGES、factory 和 LightRAG 四类存储校验逻辑重新枚举，不能遗漏新增或别名后端。
- 不要暴露或记录 .env 中的 API Key、数据库密码、令牌或完整私密连接串。
</current_context>

<autonomy_and_safety>
- 已授权：读取仓库和本地日志；编辑本任务范围内代码、测试、文档和本地部署配置；运行非破坏性的后端/前端测试、lint、build、OpenAPI 检查、浏览器检查和本地服务验证。
- 未授权：删除用户业务数据、重置工作区、覆盖无关改动、提交或推送 Git、写入外部系统、改变外部基础设施资源。涉及这些动作时先请求确认。
- 保留用户已有改动和未跟踪 .codex/。只清理本轮可确认的临时测试产物。
- 外部存储单元测试使用 mock/fake，不以可用的真实 MongoDB、Milvus、Qdrant、Memgraph 或 OpenSearch 作为完成前提；现有 PostgreSQL、Neo4j、Redis 本地 E2E 应保持可用。
</autonomy_and_safety>

<workflow>
1. 先阅读 AGENTS.md、lightrag-tasks.md、git status、存储注册表、知识库管理器、API 路由依赖、OpenAPI 配置、WebUI API 页和上传对话框。保存当前 /openapi.json 统计结果，建立实现前基线。
2. 在编码前写出简洁 ADR/差距矩阵，至少包含：每个存储类可承担的 KV/Vector/Graph/DocStatus 类型、logical 分区键、physical 资源边界、实例级配置入口、全局覆盖风险、连接/客户端缓存、drop/delete 边界、当前结论和所需改动。
3. 按“上传 UI → OpenAPI/API → 存储公共配置契约 → 各后端”分层实现。每修复一个缺陷就加入回归测试；不要通过大范围重写替换已经工作的多知识库路由。
4. 先跑最相关测试，修复后再扩大到完整相关回归。完成实现后检查 diff、错误路径、并发、资源释放、敏感信息和向后兼容。
5. 持续维护 lightrag-tasks.md：每个阶段或重要决策完成后追加 ISO 8601（Asia/Shanghai）时间线，包含改动、验证结果、限制和下一步；历史记录只追加，不删除。
</workflow>

<requirements_upload>
重构上传目标选择，使一个批次明确写入一个目标知识库：
- “新建独立知识库”必须是列表/选项中的第一项，并有清楚的新建语义；不要将其与某个已有库混淆。
- 其后列出 catalog 中所有允许写入的存量知识库，包括当前库；不能只列当前库，也不能只显示 ID。
- 每个存量选项至少同时显示人类可读 name 和不可变 id；必要时补充 isolation/status，但 name + id 是硬性要求。重名时用户仍可凭 id 区分。
- 选择存量库时向该库增量上传；选择新建时输入名称、选择 logical/physical，以及 physical 所需 profile，然后先创建库，再将整批文件写入返回的 id。失败状态、重试边界和“库已创建但上传失败”的提示必须诚实。
- 上传成功后，当前库切换、文档刷新和提示消息必须指向实际目标；不能让旧请求覆盖新选择。
- 更新全部现有 locale 键与前端单元测试，并在真实 production WebUI 中验证选项顺序、名称+ID、全部库可见和两种上传路径。
</requirements_upload>

<requirements_openapi>
全量审计知识库选择在 API 契约中的发布方式：
- 运行时统一使用 canonical header `LIGHTRAG-KNOWLEDGE-BASE`；HTTP 头大小写不敏感。未提供时保持回退 default 的兼容行为，非法或未知 id 保持清晰的 4xx。
- 对所有读取或修改某个知识库数据的 data-plane operation，在 FastAPI/OpenAPI 中显式声明可选 header parameter，使 Swagger/WebUI API 页能直接填写；description 要说明默认行为、值为知识库 id、可通过 GET /knowledge-bases 获取。
- 至少全量检查 documents 下所有上传、插入、扫描、列表、状态、重处理、删除、清空、清缓存、取消/恢复操作，query/query stream/query data，graphs 及实体关系 CRUD，Ollama 兼容查询，以及 health 中依赖当前知识库的数据。以实际路由表为准，不局限于此枚举。
- `/knowledge-bases` catalog 管理面和真正与知识库无关的系统接口不应被错误要求选择 header；在审计表中明确“需要/不需要”及原因。
- 用一个可复用、类型明确的 FastAPI Header dependency 生成 schema，并继续复用现有 request-scoped context；不要为每个 handler 手工复制解析逻辑。
- 增加 OpenAPI schema 回归测试：枚举全部 operations，断言所有 data-plane operations 恰有一个 header parameter、名称/required/schema/description 正确；管理面无误注入。对 streaming fetch 和 Axios 的实际 header 注入也保留回归。
- 在运行中的 `/openapi.json` 和 WebUI API/Swagger 页面验证 header 输入框可见且请求确实路由到所选库。
</requirements_openapi>

<requirements_storage_isolation>
对存储隔离采用可验证、无静默降级的统一语义：

1. logical
- 每个 KnowledgeBase 使用不可变 effective_workspace。
- 文件型后端使用独立 canonical 根目录/子目录；表、集合、索引、图、key 或 payload 型后端必须在所有读、写、查询、去重、索引、drop、迁移和缓存路径中包含可靠的 workspace/namespace。
- 后端专属 `*_WORKSPACE` 或类似全局配置不得把动态知识库压回相同命名空间；冲突时启动或实例创建 fail fast。

2. physical
- physical 表示该知识库使用专属存储资源配置，不等同于只换 workspace/prefix/label。
- 文件型后端至少使用专属、规范化且受允许根约束的存储根目录；外部后端必须使用 profile 中专属的连接端点、database/schema、collection/index/graph 或等价资源边界，并确保实例级连接配置不会通过 os.environ 或进程全局可变状态串到并存知识库。
- storage profile 必须按当前启用的四类 storage 及其具体实现验证所需字段、`dedicated=true`、资源指纹非默认且不被其他知识库复用。配置缺失、后端能力不成立或资源相同必须 fail closed，不得降级 logical。
- catalog、health、日志和异常只能输出脱敏资源标识；不得返回密码、token 或完整含密连接串。

3. exhaustive backend work
- 从注册表生成能力矩阵，逐一检查每个实现，而不是只检查当前部署的 PostgreSQL、Neo4j、Redis。
- 检查后端构造参数如何从 LightRAG global_config 到达驱动；将需要的 profile override 设计成实例级、不可变、后端命名空间化的配置契约，避免不同驱动使用相同字段互相污染。
- 逐后端审计连接池/driver/client 的缓存键是否包含物理资源指纹与认证边界，finalize 是否引用计数安全，drop 是否只作用于目标知识库/专属资源。
- 对已经正确隔离的后端保留实现并补证据；对缺失路径逐个实现。若某后端的上游服务本身不支持单服务内数据库级资源，也必须通过独立端点/profile 实现严格物理隔离，不能标记为“已支持”后仅使用 prefix。
- 对每个注册后端至少有参数化或后端专属单元测试，覆盖两个 logical 知识库互不可见、两个 physical profile 配置不串用、profile 复用拒绝、全局 workspace 冲突拒绝、目标 drop 不影响另一个库、敏感配置不泄漏。外部 SDK 用 mock/fake 断言实际 namespace/query/filter/collection/index/database/URI 参数。
</requirements_storage_isolation>

<compatibility_and_quality>
- 不破坏默认知识库继续访问原 args.workspace 和已有数据的行为；旧客户端不带 header 仍可工作。
- 不改变 pipeline 的 busy/scanning/destructive_busy/pending_enqueues/request_pending 并发契约，不共享会跨库污染的 storage 对象、锁、后台任务或解析目录。
- 不声称“物理隔离”而只验证命名空间。最终能力矩阵必须将“共享服务内逻辑分区”和“专属资源配置”分开报告。
- 后端代码、注释、日志和测试命名使用英文；前端遵守 React 19、TypeScript、i18next、Zustand、Axios 和 Bun 现有约定。
- 避免额外依赖；若确需新增，先说明理由并验证 lockfile/build。
</compatibility_and_quality>

<validation>
至少完成并记录以下证据：
- Ruff 检查改动 Python；相关 pytest，随后覆盖 API、workspace、全部 kg 后端隔离测试；报告 passed/skipped/fail 数。
- WebUI Bun 全量测试、ESLint、production build。
- 程序化检查生成的 OpenAPI：路由总数、data-plane operation 数、缺少/重复/错误 header parameter 必须为 0。
- 浏览器验收 production WebUI：上传目标第一项为新建；所有库显示 name + id；Swagger/API data-plane operations 可填写 header；用非默认库执行至少一个只读请求并确认返回对应库。
- 现有 PostgreSQL/Neo4j/Redis 服务健康和 A/B 隔离 smoke 不回退；其他外部后端以 mock 单元测试作为主要证据。
- 必要时重启后端，再确认 catalog、选择和 OpenAPI 行为持久稳定；不得调用真实 LLM 完成与本轮无关的高成本测试。
</validation>

<completion_contract>
只有同时满足以下条件才能宣布完成：
1. 上传 UI 的首项、全库枚举、name + id 展示、增量/新建上传均有代码、自动化和浏览器证据。
2. 所有实际 data-plane API 在 OpenAPI 中准确发布 `LIGHTRAG-KNOWLEDGE-BASE`，无缺失、重复或管理面误注入；运行时路由验证通过。
3. 注册表中的每个存储实现均出现在最终矩阵；logical 和 physical 路径要么已正确实现并有测试，要么修复后有测试。不能留下“已知未实现但仍宣称支持”的后端。
4. 默认知识库、现有 PG/Neo4j/Redis、多库 pipeline 与敏感信息保护无回归。
5. lightrag-tasks.md 的阶段状态、ADR、逐后端矩阵、验证命令与结果、限制和最终时间线已更新。

最终回复先给结论，再简要列出关键改动、全存储能力结论、验证结果和仍存在的外部限制；引用可点击的本地文件。不要重复过程日志，也不要把未运行的验证写成已通过。
</completion_contract>
~~~

Prompt 参考来源：OpenAI 官方 GPT-5.6 Model guidance（`https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6`）与本地 OpenAI Docs skill 的 GPT-5.6 Sol 升级参考。关键采用点为：减少重复、明确自治/审批边界、给出目标/约束/证据/成功标准，并在代表性任务上真实验证。

## 11. 第二轮决策、能力矩阵与时间线（只追加）

### 2026-07-21T14:48:15+08:00 — 第二轮 Prompt 冻结与执行启动

- 阶段：I（Prompt 与全量审计）
- 状态：进行中。
- 目标：在任何第二轮设计或代码改动前，先按 GPT-5.6 Sol 官方实践冻结一份结果导向、边界和验收条件完整的中文主 Prompt。
- 实际动作：
  - 完整读取本地 OpenAI Docs skill 的 GPT-5.6 Sol 参考，并检查 OpenAI 官方 GPT-5.6 Model guidance 的 `Prompting best practices`。
  - 将上传目标、OpenAPI header 发布、全注册存储 logical/physical 隔离、自治边界、验证证据和完成条件写入第 10 节。
  - 第二轮阶段 I-N 已加入阶段状态总览；后续 ADR、逐后端矩阵和验证结果将只追加到本节。
- 改动文件：`lightrag-tasks.md`。
- 验证：确认 Prompt 包含三个用户事项、data-plane API 审计范围、逐注册后端审计规则、测试层次、兼容/安全约束和完成停止条件；尚未在本节点修改生产代码或宣称功能通过。
- 环境事件：尝试安装官方 OpenAI Developer Docs MCP 时本机 `codex.exe` 返回 Access denied；已按 skill 的后备路径读取官方 `developers.openai.com` 页面，不影响 Prompt 冻结。
- 风险/阻塞：暂无；物理隔离的逐后端真实语义必须由下一步代码审计确认，不以当前 PG/Neo4j/Redis 结论外推。
- 下一步：生成当前 OpenAPI 基线；审计上传组件、FastAPI dependency、存储注册表和全部后端，追加 ADR 与差距矩阵后再编码。

### 2026-07-21T14:58:51+08:00 — 第二轮 ADR-006/007/008 与实现前差距矩阵

- 阶段：I（Prompt 与全量审计）
- 状态：已完成。
- 实现前证据：
  - 上传框只有 `current/new` 两个目标，顺序为“当前库”后“新建库”；当前库只显示 `selectedKnowledgeBaseId`，组件不读取 catalog，因此无法选择其余存量库或显示其名称。
  - 运行中 `/openapi.json` 共 44 个 operation，带 `LIGHTRAG-KNOWLEDGE-BASE` header parameter 的 operation 为 0。运行时 router dependency 从原始 `Request` 读取 header，所以请求可隔离，但 FastAPI 无法把它发布为 OpenAPI 参数。
  - `STORAGE_IMPLEMENTATIONS` 有 23 个可选实现；`STORAGES` 额外保留不可由当前类型校验选择的 `AGEStorage` 和已注释/弃用的 `ChromaVectorDBStorage` 映射。第二轮完成矩阵以 23 个可选实现为硬范围，同时记录两个非活动映射。
  - 文件型实现已经从实例级 `working_dir` 派生 workspace 子目录，第一轮 physical profile 的专属 `working_dir` 可作为其物理资源边界。
  - PostgreSQL、Neo4j、Redis 已读取实例级 `storage_profile`；MongoDB、Milvus、Qdrant、Memgraph、OpenSearch 仍直接读取进程环境/config.ini，physical profile 无法到达驱动。
  - MongoDB 与 OpenSearch 的 client manager 是单一进程级 singleton；即使新增 profile 读取，不改为按资源指纹分池也会把并存 physical 知识库复用到首个连接。
  - 动态知识库 fail-fast 当前只检查 `POSTGRES_WORKSPACE`、`NEO4J_WORKSPACE`、`REDIS_WORKSPACE`，遗漏 `MONGODB_WORKSPACE`、`MILVUS_WORKSPACE`、`QDRANT_WORKSPACE`、`MEMGRAPH_WORKSPACE`、`OPENSEARCH_WORKSPACE`，这些变量会压平 logical workspace。
- ADR-006（上传目标）：上传对话框在打开时读取 catalog；使用 `new` sentinel 作为首项，其后枚举全部知识库。存量目标值直接使用不可变 id，显示 `name (id)`；上传循环显式携带批次目标 id，而不是依赖异步 Zustand 更新后的全局 interceptor，避免刚新建/跨库上传时写入旧库。
- ADR-007（OpenAPI）：把统一 request dependency 改成带 `Header(alias="LIGHTRAG-KNOWLEDGE-BASE")` 的类型化依赖；document/query/graph 的 router-level dependency自动发布到全部 operation。Ollama 仅 `/chat` 是 RAG data plane；`/generate` 直接调用底层 LLM，`/version`、`/tags`、`/ps` 是系统/模型元数据，不应误注入知识库选择。`/health` 单独发布同一 header。
- ADR-008（存储 profile）：
  - 建立“实现名 → profile section/workspace override/资源类型”的单一能力注册表；physical profile 只要求当前启用四类存储实际需要的 section，不再硬编码必须同时存在 PostgreSQL、Neo4j、Redis。
  - `working_dir`、`input_dir` 始终是 physical profile 的必需资源；外部后端使用实例级 section：`postgres`、`neo4j`、`redis`、`mongo`、`milvus`、`qdrant`、`memgraph`、`opensearch`。
  - 后端有 profile 时忽略对应进程级 workspace override；无 profile 的 dynamic logical 知识库若存在任一活动后端 workspace override，则 manager fail fast。
  - MongoDB/OpenSearch client manager 改为按脱敏资源指纹分池和引用计数；Milvus/Qdrant/Memgraph 本来每实例持有 client/driver，只需从实例 profile 解析连接。资源 profile 单次分配，并拒绝不同 profile 指向同一 required resource fingerprint。

| 可选实现（共 23 个） | 类型 | logical 机制 | 实现前 physical | 第二轮动作 |
| --- | --- | --- | --- | --- |
| `JsonKVStorage`, `JsonDocStatusStorage` | KV, DocStatus | workspace 子目录与共享状态键 | 已由专属 `working_dir` 支持 | 注册能力与路径唯一性测试 |
| `NetworkXStorage` | Graph | workspace 子目录 | 已由专属 `working_dir` 支持 | 注册能力与路径唯一性测试 |
| `NanoVectorDBStorage`, `FaissVectorDBStorage` | Vector | workspace 子目录/文件 | 已由专属 `working_dir` 支持 | 注册能力与路径唯一性测试 |
| `PGKVStorage`, `PGVectorStorage`, `PGGraphStorage`, `PGDocStatusStorage` | 四类 | workspace 行/图命名空间 | 已支持实例级 `postgres` profile | 保留并纳入统一验证 |
| `RedisKVStorage`, `RedisDocStatusStorage` | KV, DocStatus | workspace key prefix | 已支持实例级 `redis` profile | 保留并纳入统一验证 |
| `Neo4JStorage` | Graph | workspace label/index | 已支持实例级 `neo4j` profile | 保留并纳入统一验证 |
| `MongoKVStorage`, `MongoDocStatusStorage`, `MongoGraphStorage`, `MongoVectorDBStorage` | 四类 | workspace collection prefix | 不支持；连接是全局 singleton | 增加 `mongo` profile和资源键分池 |
| `MilvusVectorDBStorage` | Vector | workspace + model collection | 不支持；连接只读环境 | 增加 `milvus` profile |
| `QdrantVectorDBStorage` | Vector | shared collection 的 workspace payload/filter | 不支持；连接只读环境 | 增加 `qdrant` profile；专属 profile 可使用独立端点/collection prefix |
| `MemgraphStorage` | Graph | workspace label | 不支持；连接只读环境 | 增加 `memgraph` profile |
| `OpenSearchKVStorage`, `OpenSearchDocStatusStorage`, `OpenSearchGraphStorage`, `OpenSearchVectorDBStorage` | 四类 | workspace 独立 index | 不支持；连接是全局 singleton | 增加 `opensearch` profile和资源键分池 |

- 非活动映射：`AGEStorage` 不在 `GRAPH_STORAGE.implementations`，`ChromaVectorDBStorage` 在实现列表中被注释且当前实现位于 deprecated 目录；两者不能通过当前 LightRAG 存储类型校验选用，不计入 23 个可选实现的完成统计，最终仍会报告该事实。
- 下一步：按 ADR-006/007 先完成上传和 OpenAPI；随后实现统一 capability/profile 契约及五个缺失外部后端组，并逐组补 mock 回归。

### 2026-07-21T18:12:28+08:00 — 第二轮前端与 OpenAPI 实现完成

- 阶段：J/K（上传目标与 API 契约）。
- 状态：已完成。
- 实际动作：
  - 上传对话框默认选择不可与 catalog ID 冲突的 `__new_knowledge_base__` sentinel；该项固定为第一项，其后按 catalog 顺序列出全部知识库并显示 `name (id)`。
  - 新建目标支持名称、logical/physical 和可用 storage profile；先创建库，再将本批文件显式携带返回的知识库 ID 上传。选择存量库时向该库增量上传；跨库成功后切换选择并刷新，创建成功但无文件入库时给出诚实提示。
  - Axios interceptor 仅在调用方未显式指定时注入全局知识库；上传批次显式 header 优先，避免 Zustand 选择切换时序把文件写入旧库。
  - FastAPI 使用统一 `Annotated[..., Header(alias="LIGHTRAG-KNOWLEDGE-BASE")]` 依赖发布 OpenAPI 参数；documents、query、graph、`/api/chat`、`/api/generate` 和 `/health` 使用同一 request-scoped context，知识库管理面及 `/api/version|tags|ps` 不误注入。
- ADR-007 更正：实现前记录将 `/api/generate` 误写为“直接调用底层 LLM、无需上下文”；实际代码路径会经 RAG proxy 取得角色函数，因此 `/api/generate` 与 `/api/chat` 都是 data plane，最终实现和测试均为两者发布 header。
- 主要改动：`lightrag/api/knowledge_bases.py`、`lightrag/api/lightrag_server.py`、三个 data router、`ollama_api.py`、`lightrag_webui/src/api/lightrag.ts`、`UploadDocumentsDialog.tsx`、上传选项 helper、locale 与对应测试。
- 自动化证据：API 聚焦测试 22 passed；前端聚焦测试 11 passed；后续全量结果见最终验证节点。
- 风险/阻塞：无。
- 下一步：完成统一 storage capability/profile 契约及逐后端修复。

### 2026-07-21T18:12:29+08:00 — 23 个可选存储实现隔离收口

- 阶段：L/M（统一契约与逐后端实现）。
- 状态：已完成。
- 最终能力结论：

| 可选实现 | logical 分区 | strict physical 资源边界 | 最终状态 |
| --- | --- | --- | --- |
| `JsonKVStorage`, `JsonDocStatusStorage`, `NetworkXStorage`, `NanoVectorDBStorage`, `FaissVectorDBStorage` | workspace 子目录/独立文件 | 专属规范化 `working_dir` 与 `input_dir` | 已支持并测试 |
| `PGKVStorage`, `PGVectorStorage`, `PGGraphStorage`, `PGDocStatusStorage` | workspace 行/图 namespace | 独立 endpoint/database profile | 已支持并纳入统一契约 |
| `RedisKVStorage`, `RedisDocStatusStorage` | key prefix | 独立 Redis endpoint；DB 编号不作为严格物理边界 | 已支持并收紧资源指纹 |
| `Neo4JStorage` | workspace label/index | 独立 endpoint/database profile | 已支持并纳入统一契约 |
| `MongoKVStorage`, `MongoDocStatusStorage`, `MongoGraphStorage`, `MongoVectorDBStorage` | collection prefix | 独立 endpoint/database profile | 新增实例 profile；client 按连接资源与认证边界分池/引用计数 |
| `MilvusVectorDBStorage` | workspace + model collection | 独立 endpoint/database profile | 新增实例 profile |
| `QdrantVectorDBStorage` | payload/filter + collection namespace | 独立 endpoint；`collection_prefix` 仅防专属服务内部名称碰撞 | 新增实例 profile并拒绝同 endpoint 伪 physical |
| `MemgraphStorage` | workspace label | 独立 endpoint/database profile | 新增实例 profile |
| `OpenSearchKVStorage`, `OpenSearchDocStatusStorage`, `OpenSearchGraphStorage`, `OpenSearchVectorDBStorage` | workspace index | 独立 hosts endpoint；`index_prefix` 仅防专属服务内部名称碰撞 | 新增实例 profile；client 分池/引用计数；版本能力按 client 隔离 |

- 统一契约：`lightrag/kg/storage_profiles.py` 对 23 个可选实现逐一登记 profile section 和 workspace override；physical 只验证当前活动四类 backend 所需 section，但始终要求独立文件目录。profile 必须 `dedicated=true`、单次分配，资源指纹既不能复用 default，也不能复用其他 physical 库；密码/token 不参与资源独立性判断且不对外输出。
- 全局覆盖防护：活动实现对应的 `POSTGRES_WORKSPACE`、`NEO4J_WORKSPACE`、`REDIS_WORKSPACE`、`MONGODB_WORKSPACE`、`MILVUS_WORKSPACE`、`QDRANT_WORKSPACE`、`MEMGRAPH_WORKSPACE`、`OPENSEARCH_WORKSPACE` 会动态纳入 logical fail-fast；physical profile 激活时后端忽略旧全局 workspace override。
- 连接隔离：MongoDB/OpenSearch 从进程 singleton 改为资源键 client pool；OpenSearch `_shard_doc` 能力从“最后连接覆盖全局”改为按实际 client 查询。Milvus、Qdrant、Memgraph 从实例 `storage_profile` 解析连接。
- 文件可靠性：全存储回归发现 Windows 并发 `os.replace` 会短暂报 sharing violation；`lightrag/file_atomic.py` 增加有界 PermissionError 重试，保持同文件系统原子 old-or-new 语义，新增回归后 JSON/NetworkX 并发写入通过。
- 非活动映射仍为：不可由当前类型校验选择的 `AGEStorage`，以及 deprecated/注释掉的 `ChromaVectorDBStorage`；未将其伪装成 23 个活动实现的一部分。
- 主要改动：`storage_profiles.py`、MongoDB/Milvus/Qdrant/Memgraph/OpenSearch 后端、manager/server profile plumbing、profile 示例与中文部署文档、跨后端/驱动路由/client 生命周期测试。
- 风险/阻塞：MongoDB、Milvus、Qdrant、Memgraph、OpenSearch 未在本机部署真实集群，按任务边界用 SDK mock/fake 断言 URI/database/collection/index/filter/client pool；当前真实 E2E 仍使用 PostgreSQL、Neo4J、Redis。
- 下一步：执行全量自动化、live OpenAPI/API 与 production WebUI 验收。

### 2026-07-21T18:12:30+08:00 — 第二轮最终验证与服务交付

- 阶段：N（全量验证与交付）。
- 状态：已完成。
- 自动化结果：
  - 12 个活动 backend 测试目录：`874 passed, 13 skipped`；覆盖 JSON、NetworkX、Nano、Faiss、PG、Neo4J、Redis、Mongo、Milvus、Qdrant、Memgraph、OpenSearch。
  - strict physical/OpenSearch/API 聚焦：`272 passed`；此前完整存储首轮为 `1065 passed, 26 skipped, 9 failed`，其中 2 个 Windows 原子替换失败已修复。其余失败是根级 Linux PID/zombie 语义与测试源码默认 GBK 解码问题，不属于 backend 隔离；GBK 两项在 `PYTHONUTF8=1` 下通过。
  - 完整 API：`335 passed, 14 skipped`。
  - pipeline/workspace：两次集合运行分别 `373 passed/1 flaky`、`372 passed/2 flaky`；失败均为 pytest 清理 `tmp_path` 时仍有后台 flush 的 Windows 套件级时序，失败用例单独复跑通过。没有知识库 profile/header 路由失败。
  - WebUI：`70 pass, 0 fail`；`eslint .` 通过；Vite production build 通过并写入 `lightrag/api/webui`。
  - Ruff：所有本轮改动 Python 与测试通过。
- live OpenAPI：当前源码服务共有 44 个 operation；33 个 data-plane operation 均恰有一个可选 `LIGHTRAG-KNOWLEDGE-BASE` header，missing/invalid=0，unexpected=0。知识库管理面和系统模型元数据接口不带该 header。
- live 路由：catalog 共 5 个库。用 header 请求 `kb_5f9176ac67ae` 后，`/health` 返回同一 `knowledge_base_id` 和 selected workspace，KV/DocStatus/Graph/Vector 四类 storage workspace 全为该 ID；`/documents/status_counts` 返回 processed=1、all=1。
- production WebUI：上传对话框第一项为 `Create an isolated knowledge base`；其后可见 `Default (default)` 与全部 4 个非默认库，均显示名称和 ID。WebUI API 页 GET `/documents` 展开后存在可编辑 `LIGHTRAG-KNOWLEDGE-BASE` 文本框，实际填入 `kb_5f9176ac67ae` 后读取值一致，未执行写操作。
- 浏览器说明：内置 browser-use 连接因宿主只读 `process` 属性冲突，两次初始化均失败；按其恢复规则改用官方 Playwright CLI 对同一 production WebUI 完成验收，不降低页面证据范围。
- 运行环境：PostgreSQL、Neo4J、Redis 三个 Compose 容器均 healthy；当前源码已按 `data/local-multikb/rag_storage`、`data/local-multikb/inputs`、workspace=`default` 在 9621 运行，重启后 catalog 保留。
- 文档：`storage-profiles.local.example.json` 已展示八类外部 section；`docs/LocalMultiKnowledgeBaseDeployment-zh.md` 已说明上传行为、全后端 logical/physical 语义、严格 endpoint 边界和八个 workspace 覆盖变量。
- 已知非功能性环境噪声：本机 `.pytest_cache` 无写权限产生 warning；不影响断言和测试结果。未调用真实 LLM，也未写入/删除业务文档。
- 下一步：无必需工作；如要把其他外部后端加入真实部署，再提供对应专属 endpoint/profile 并运行其 integration marker。

### 2026-07-21T18:17:43+08:00 — 最终收口复核

- 状态：已完成。
- 对 OpenSearch `hosts` 增加 endpoint 规范化：忽略协议、主机名大小写与末尾斜杠等文本差异后再生成严格物理资源指纹，避免用等价地址绕过“专属 endpoint”约束。
- 最终聚焦复跑：`tests/kg/test_storage_profile_isolation.py` 与 `tests/api/test_knowledge_bases.py` 共 `60 passed`；全仓 Ruff 与 `git diff --check` 均通过。
- 运行态复核：`http://127.0.0.1:9621/health` 返回 `healthy`，9621 端口保持监听；PostgreSQL、Neo4J、Redis 三个 Compose 容器均为 `healthy`。
- 环境说明：pytest 仍仅报告仓库既有 `.pytest_cache` 无写权限 warning，不影响测试结果；本轮测试使用独立 `--basetemp`，交付前清理该临时目录。

## 12. 社区 RFC 反馈审计与多知识库架构重构（2026-07-23，只追加）

### 12.1 本轮目标、边界与结论

- 社区上下文：HKUDS/LightRAG [Issue #2527](https://github.com/HKUDS/LightRAG/issues/2527) 的维护者要求先提交独立设计 RFC，再讨论实现 PR；RFC 应聚焦使用场景、约束和设计需求，而不是让维护者直接审阅完整集成分支。
- 审计基线：`dev` 分支提交 `27e41a70`（已合并当时最新社区 `main`）。本轮只审计、形成回复/RFC/方案与 Prompt，不修改业务实现、不提交或发布 GitHub 内容；待 RFC 共识形成后再按阶段改代码。
- 总结判断：当前分支已经证明多知识库 logical/physical 存储隔离、API header 路由和 WebUI 可以工作，但它还不是适合直接提交社区的最终架构。固定实例绑定、显式创建、单进程请求隔离已经具备；共享 catalog、可回收实例池、后台任务生命周期租约、全服务重启恢复、按知识库公平调度、多节点协调和 Ollama 无自定义 header 选库尚未闭环。
- PR 策略调整：接受维护者的建议。停止以“完整 integration branch”为社区评审起点；先发 RFC。第一阶段收敛为 logical isolation + shared catalog + bounded instance pool + request/pipeline routing，physical isolation 与 WebUI 拆到后续阶段。
- 推荐架构选择：一个 `LightRAG` 实例在整个生命周期内只绑定一个不可变 `effective_workspace`，禁止中途切换；请求只负责选择并租用正确实例，不允许修改实例 workspace。未知知识库必须由管理 API 显式创建，任何拼错、空值或无权限选择都不得自动创建，也不得静默回退 default。

### 12.2 当前代码模型与证据

当前数据路径可以概括为：

```text
HTTP request
  -> typed knowledge-base selector
  -> process-local KnowledgeBaseManager
  -> process-local catalog snapshot / lazy context creation
  -> ContextVar proxy
  -> fixed LightRAG + fixed DocumentManager
  -> workspace-scoped KV / Vector / Graph / DocStatus / input directory
  -> workspace-scoped pipeline_status and locks
```

关键证据：

- `lightrag/api/knowledge_bases.py:95-134`：catalog record 已把 `id`、人类可读 `name`、不可变 `effective_workspace`、隔离级别和 storage profile 分开；新库的 workspace 使用服务端生成的 `kb_<12 hex>`，不会直接把展示名称写入目录/表/集合。
- `lightrag/api/knowledge_bases.py:140-321`：catalog 是本地 JSON 文件和进程内 `threading.RLock`；它能原子替换单个文件，但不是跨 worker 的共享一致性目录。
- `lightrag/api/knowledge_bases.py:333-362`：路由依赖 `ContextVar` 选择上下文；代理在上下文缺失时使用 `default_context`，这是多知识库模式下不应存在的静默回退。
- `lightrag/api/knowledge_bases.py:364-609`：每个知识库惰性创建独立 `LightRAG`/`DocumentManager`；进程内首次访问由 manager lock 和实例初始化 lock 去重；池上限可通过 `LIGHTRAG_MAX_LOADED_KNOWLEDGE_BASES` 配置，但满池只拒绝，没有回收。
- `lightrag/api/knowledge_bases.py:611-725`：普通请求有 `active_requests` 计数，删除会检查请求和 pipeline 状态；后台任务没有独立 lease/refcount，删除也没有 catalog 生命周期状态和 drain/fencing 协议。
- `lightrag/api/lightrag_server.py:1292-1312,2197-2242`：default 实例启动时初始化，其他实例惰性初始化；实例构造时传入固定 workspace。
- `lightrag/api/lightrag_server.py:2248-2280`：documents/query/graph/Ollama data plane 都接入同一 header dependency；`/health` 也可选择知识库。
- `lightrag/lightrag.py:1314-1336`：storage 和 `pipeline_status` 按实例的 `self.workspace` 初始化，但仍保留首实例设置进程全局 default workspace 的兼容路径。
- `lightrag/pipeline.py:1055-1289`：文档处理读取 `self.workspace` 对应的 pipeline 状态与 doc status，能够恢复 PENDING/FAILED/异常 PROCESSING 文档；恢复需要某个调用者再次触发处理，不会在服务重启后自动枚举所有知识库。
- `lightrag/kg/shared_storage.py:145-158,2949-2967`：共享状态 API 允许省略 workspace 并回落进程 default；多知识库安全路径不应依赖这一兼容行为。
- `lightrag/kg/shared_storage.py:1418-1528`：Gunicorn preload 使用 `SyncManager` 在同一主机的 worker 间共享锁、pipeline 状态和全局并发 gate；它不是持久目录，也不能跨节点。
- `lightrag/kg/shared_storage.py:2996-3450` 与 `lightrag/utils.py:1366-1440`：社区主线已有跨 Gunicorn worker 的 LLM/embedding/rerank 全局并发 slot 和进程级 soft FIFO。它可以作为资源治理基础，但当前公平单位是进程/本地队列，不是知识库；单 worker 多实例还会各自创建 `max_async` 队列，导致总并发随加载实例数放大。
- `lightrag/api/routers/ollama_api.py:221-299`：`/api/chat` 和 `/api/generate` 支持 header dependency；请求体必填的 `model` 当前没有参与知识库选择，不能解决标准 Ollama 客户端无法发送自定义 header 的问题。
- `tests/api/test_knowledge_bases.py` 已覆盖并发 ContextVar 隔离、12 个 storage workspace 传递、管理 CRUD、header OpenAPI 发布和 physical profile；尚未覆盖跨 worker catalog 一致性、满池回收、后台 lease、重启恢复、租户公平性和 Ollama model 选库。

### 12.3 维护者问题逐项审计矩阵

| 维护者关注点 | 当前状态 | 当前实现/缺口 | RFC 决策草案 |
| --- | --- | --- | --- |
| 实例是否可切换 workspace | 部分满足 | 应用只在构造时传 workspace，正常路径不切换；但对象字段并非架构级不可变，代理缺上下文时回退 default | 实例终身绑定一个 workspace；初始化后禁止修改，并校验所有 storage namespace 与绑定一致 |
| 在途请求/后台任务不串库 | 部分满足，高风险缺口 | 请求 ContextVar 隔离已测；后台 task 依赖创建 task 时隐式复制 ContextVar，没有显式 context/lease；代理可回退 default | route resolver 返回 concrete context lease；请求、stream、后台 task 都持 lease 至完成；多库模式代理缺上下文立即失败 |
| 惰性创建与首次访问竞争 | 基本满足（单进程） | manager lock 防止同一 worker 重复实例化；不同 worker 各自建实例符合 per-worker pool，但 catalog 可能不一致 | per-worker single-flight；共享 catalog 是唯一真相；失败实例进入有界 FAILED/backoff 状态，避免惊群重试 |
| workspace 名称校验 | 部分满足 | public ID 有 64 位 ASCII regex，新库 namespace 是生成 ID；通用 `validate_workspace` 只禁路径分隔符和 `.`/`..`，legacy workspace 未统一满足所有数据库命名限制 | display name 永不用于物理命名；新 namespace 使用服务端生成的小写 ASCII opaque ID；为每个 backend 做长度/保留字适配，legacy default 单独兼容校验 |
| pool 容量与 eviction | 未实现 | 每 worker 默认最多 32 个，满后 409；无 LRU/TTL、无安全回收、无连接预算 | per-worker 有界池；idle LRU；仅当 request lease=0、background lease=0、pipeline 无 owner/pending/recovery 且 catalog ACTIVE 时回收；无安全 victim 返回 503 + Retry-After |
| 未知 workspace 是否自动创建 | 已满足但边界需修正 | syntactically valid unknown ID 为 404，创建只能走管理 API；但显式空 header 会回退 default，非法格式被映射成 404 | header 缺失才兼容 default；header 已出现但为空/非法为 400；合法未知为 404；永不因 data-plane 请求创建 catalog 记录 |
| pipeline/workspace 显式绑定 | 部分满足 | pipeline 主路径使用 `self.workspace`，input 目录与 storage 固定；共享状态 API 和 proxy 仍有 default fallback，track ID 只在选中 storage 内有意义 | 引入不可变 WorkspaceContext；pipeline/job/scan/delete/track 查询显式携带 `(kb_id, workspace)`；多库路径不接受 `None` workspace |
| 多库未完成文档的重启恢复 | 未闭环 | durable doc status 可重跑，手工 `/reprocess_failed` 可恢复；启动只初始化 default，不枚举 catalog，pipeline_status 重启即丢失 | 启动 reconciliation 枚举 ACTIVE catalog，按全局并发上限排队恢复；durable doc status/job intent 是真相，pipeline_status 仅作观测缓存 |
| 不同 workspace 并行 pipeline | 已允许但无全局 admission | workspace lock 允许跨库并行；没有全局 active pipeline cap | 同库单写 pipeline，不同库可并行；增加全局 active-pipeline semaphore 和每库上限，过载排队/背压 |
| LLM/embedding 全局限制与公平 | 部分满足 | Gunicorn 有跨 worker global slot；单 worker 多实例上限会相乘；soft FIFO 按进程而不是租户 | 所有运行模式共享 provider/role 全局预算；按 workspace 做 work-conserving weighted/deficit round-robin，并用 aging 防止低优先级永久饥饿 |
| Gunicorn 多进程一致性 | 未满足 | pool 本就应 per-worker；但 JSON catalog 在 preload 后成为各 worker 独立快照，管理写可能丢更新或其他 worker 404；连接成本为 workers × loaded contexts | catalog 使用共享持久 store + revision/CAS；pool 明确 per-worker；容量和 DB connection budget 按 worker 数计算并暴露指标 |
| 未来多节点与无 sticky session | 未实现 | `SyncManager`、PID 存活判断和内存状态只覆盖一个 Gunicorn master 进程树 | 把 catalog、pipeline mutual exclusion、admission 抽象为 provider；协调协议使用 lease + TTL + heartbeat + fencing token；请求可落到任意节点 |
| API header 与受影响端点 | 大体满足 | documents/query/graph、Ollama chat/generate、health 已发布 header；management 和 Ollama metadata 不带 header | 保持 `LIGHTRAG-KNOWLEDGE-BASE`（HTTP 大小写不敏感）；形成明确 data/control plane 表，错误语义统一且 response 返回 resolved KB ID |
| Ollama 无自定义 header 选库 | 未满足 | `model` 字段被忽略，只能靠 header | `/api/tags` 发布 `lightrag:default`、`lightrag:<kb-id>`；chat/generate 从必填 model 解析；`lightrag:latest` 兼容 default；model 与 header 冲突时 400 |
| 隔离与授权边界 | 未说明/未实现 | 现有 API key/JWT 是服务级认证，任何通过认证的调用者都能选择任意 catalog ID；header 不是权限凭据 | Phase 1 明确“不提供 per-KB authorization”；需要租户安全的部署必须用受信网关/独立实例；保留 principal-to-KB authorizer hook 供后续扩展 |
| 每 workspace 可覆盖配置 | 当前范围基本正确，阶段过大 | 当前只允许 storage profile，不允许覆盖 LLM/parser/chunk/prompt；但同时实现了 strict physical 和全部 WebUI | Phase 1 唯一允许的 per-KB 配置维度是 immutable `storage_profile_id`；LLM、embedding、parser、prompt、chunk、调度策略均为 server-global；physical 语义后移 |
| 单 workspace 升级路径 | 基本满足 | default record 映射旧 `args.workspace`，不搬数据；无 header 继续 default；本地 catalog 不适合多 worker | shared catalog 首次启动幂等导入 legacy default；不移动数据；default workspace 不匹配则 fail fast；升级/回滚前备份 catalog |
| 分阶段 MVP | 与维护者建议不一致 | 当前分支一次包含 logical、physical、WebUI、全后端 profile | 重排为 RFC → logical/core MVP → pipeline/resource/multiprocess hardening → Ollama/SDK → WebUI → physical backends → cluster provider |

### 12.4 必须补齐的架构方案 draft

#### ADR-RFC-001：固定实例绑定与 fail-closed 上下文

1. `KnowledgeBaseRecord.id` 是客户端使用的 opaque ID；`name` 仅展示；`effective_workspace` 是服务端生成、创建后不可修改的内部 namespace。
2. 一个实例只服务一个 record revision。不得提供 `switch_workspace()`，不得在请求中修改 `rag.workspace`，不得复用同一个 storage 对象跨 workspace。
3. data-plane resolver 在进入 handler 前完成“解析 selector → 读取 shared catalog → 校验状态/权限 → 获取 pool lease”。handler 获得 concrete `KnowledgeBaseContext`，而不是会回退 default 的动态 proxy。
4. header 缺失代表 legacy default；header 存在但为空、含非法字符或与另一选择器冲突必须失败。任何异常路径都不能退回 default。
5. streaming response 的 lease 持续到流完成/取消；后台任务在返回响应前把 request lease 转移为 background lease，并直接捕获 concrete `rag`/workspace；任务完成、失败、取消和 shutdown drain 都释放 lease。

#### ADR-RFC-002：共享、持久、显式创建的 catalog

1. catalog 是控制面唯一真相，不能以 worker 内存或未加跨进程一致性控制的 JSON 快照为真相。记录至少包括 `id/name/effective_workspace/state/storage_profile_id/revision/timestamps`。
2. 生命周期建议为 `CREATING -> ACTIVE -> DELETING -> TOMBSTONED`，失败操作保留可诊断/可重试状态。只有 ACTIVE 可承接 data-plane 请求。
3. 创建只能走认证后的管理 API，并支持 idempotency key，避免客户端重试创建两个库；未知 selector 永不 auto-create。
4. catalog store 需要原子 create-if-absent、compare-and-swap revision、list 和 watch/poll revision 语义。worker 可以缓存，但每次 acquire 必须能发现删除/更新 revision。
5. 部署契约：单 worker/dev 可使用 local store；`workers > 1` 必须配置所有 worker 可见的 shared durable store，否则启动 fail fast。后续多节点复用同一接口。
6. 删除采用两阶段：先 CAS 为 DELETING 阻止新 lease，再等待现有 request/background/pipeline lease drain，取得 exclusive fencing lease 后清理数据，最后 tombstone。失败保持 DELETING/ERROR 以便幂等重试，不能先删 catalog 再赌清理成功。

#### ADR-RFC-003：有界 per-worker instance pool

1. pool 必须是 per-worker：Python 对象、event loop、DB client 和本地队列不可放入 `multiprocessing.Manager` 共享。跨 worker 一致性来自 shared catalog/coordination，而不是共享实例对象。
2. entry 状态建议为 `ABSENT/CREATING/READY/DRAINING/FINALIZING/FAILED`；同一 worker 首次访问用 single-flight，所有等待者共享同一个初始化结果。
3. pool 限制按 worker 配置，并在启动时报告理论最大连接成本：`workers × max_loaded_instances_per_worker × backend_connection_budget`。
4. eviction 为 idle LRU/TTL，default 可配置 pinned。安全条件必须同时满足：无 request lease、无 background lease、无 pipeline owner、无 pending enqueue、无 recovery fence、catalog 仍 ACTIVE。
5. eviction 先在锁内将 entry 改为 DRAINING 并禁止新 lease，再在锁外 finalize；成功移除，失败进入 FAILED 并告警。没有安全 victim 时返回 503，而不是取消在途工作。
6. health/liveness 不应因为一个未加载 selector 自动建立昂贵连接；catalog health 和 KB readiness 分离，后者需要认证并显式选择。

#### ADR-RFC-004：显式 pipeline 上下文与重启恢复

1. pipeline、scan、upload、reprocess、clear、delete、custom chunks 和所有 task payload 都携带不可变 `kb_id/effective_workspace/track_id`；track 的唯一键是 `(kb_id, track_id)`。
2. `pipeline_status` 是 workspace-scoped 的运行态/观测缓存，不是持久恢复真相。durable doc status、operation journal 和 input manifest 才决定重启后做什么。
3. 同一 workspace 的 mutation pipeline 仍单写；不同 workspace 可以并行。协调接口必须返回 fencing token，storage commit/状态更新可拒绝过期 owner。
4. 服务重启时，recovery coordinator 先枚举 ACTIVE catalog，轻量检查未完成状态，再在全局 active-pipeline 上限下按公平队列恢复；不能一次加载所有实例并打满连接池。
5. PENDING/FAILED/异常 PROCESSING 文档重新入队；未完成 scan 依据 durable scan intent/input manifest 重跑分类；已完成数据不重复写。破坏性操作中断进入 RECOVERY_REQUIRED，必须由幂等恢复或显式管理员流程解除。
6. 新请求可以提升该知识库 recovery job 的优先级，但不能绕过同库 fencing；liveness 不因后台恢复失败而失败，readiness/metrics 应显示 per-KB recovery 状态。

#### ADR-RFC-005：并行 pipeline、全局资源预算与公平性

1. 跨知识库 pipeline 默认允许并行，以利用 I/O 与 provider 并发；增加 `max_active_pipelines_global`，同时保留每 workspace 单 pipeline 和可选 per-workspace ingest cap。
2. LLM extract/query、embedding、rerank 的预算是 server/cluster global，而不是“每个实例各有一份”。单 worker 和 Gunicorn 都必须走同一个 admission contract。
3. 调度以 workspace 为公平单位。推荐 work-conserving deficit/weighted round-robin；同一 workspace 内保留 query/ingest priority，跨 workspace 加 aging，避免一个 bulk ingest 长期占满全部 slot，也避免低优先级永久饥饿。
4. 过载必须背压：实例池无安全容量返回 503 + Retry-After；pipeline admission 可保持 durable queued；不能无界创建 task、connection 或 provider request。
5. 指标至少包含 loaded/creating/draining contexts、active leases、per-workspace queued/running、global slot in-use/wait、recovery backlog、eviction/failure 数；日志使用 KB ID，不输出 storage profile secrets。

#### ADR-RFC-006：Gunicorn 与未来集群

1. Gunicorn 下每 worker 有独立 pool；shared catalog 和 pipeline coordinator 保证任意 worker 都能解析相同 KB，并确保同库 mutation 不重复执行。不依赖 sticky session。
2. 当前 `SyncManager` 可继续作为单主机协调 provider，但它只覆盖同一个 Gunicorn master 进程树。全服务重启后的恢复依赖 durable 状态，而不是 Manager 内存。
3. coordinator 抽象至少包含 keyed lease、TTL/heartbeat、fencing、global admission 和 owner-death reconciliation。未来可实现 Redis/PostgreSQL/其他外部协调，不让 pipeline 代码直接依赖 PID 或 Manager proxy。
4. 多节点阶段要求 catalog、coordinator 和 storage 都是节点共享资源；pool 仍是节点/worker 本地缓存。节点故障后 lease 过期并由 fencing 阻止旧 owner late commit。
5. Phase 1 必须明确支持矩阵：single worker 必须完整支持；Gunicorn 只有在 shared catalog + same-host coordinator 已配置时支持；未提供 external coordinator 前明确标注不支持 multi-node，而不是隐式依赖 sticky session。

#### ADR-RFC-007：API、Ollama、错误语义和响应可观测性

1. 普通 REST data plane 使用 canonical header `LIGHTRAG-KNOWLEDGE-BASE`。header value 是 KB ID，不是展示名或物理 workspace。
2. 需要 selector：全部 `/documents/**`、`/query*`、`/graph/**`、`/graphs`，以及 KB-specific readiness/status。管理 API `/knowledge-bases/**` 通过 path/body 指定目标，不再要求 selector。
3. 错误语义：显式空/格式非法/selector 冲突为 400；未知为 404；CREATING/DELETING/active conflict 为 409；pool/admission/recovery 临时不可用为 503；不得自动创建或回退 default。
4. 成功响应或响应 header 应暴露 resolved KB ID，便于代理、SDK、审计日志和用户确认实际路由目标。
5. Ollama 客户端通过必填 `model` 选库：`lightrag:latest` 与 `lightrag:default` 映射 default，`lightrag:<kb-id>` 映射非默认库；`/api/tags`/`ps` 返回调用者可见的 aliases。高级客户端仍可带 header；若 model 与 header 同时存在且不同，返回 400。
6. `/api/generate` 虽不做 RAG retrieval，但会使用选中实例的 role queue/cache，因此也必须执行一致的选库、lease 和冲突规则。

#### ADR-RFC-008：安全、配置、升级与范围

1. workspace isolation 只保证数据路由/命名空间隔离，不等于 authorization。Phase 1 沿用 server-wide API key/JWT，不实现每库 ACL；所有通过服务认证的用户原则上可访问所有 KB。
2. 需要真正 tenant security 的部署在 ACL 阶段完成前应使用外部可信网关映射/过滤 KB ID，或每租户独立 LightRAG 服务。selector header 不能被视为身份声明。
3. Phase 1 每库唯一可覆盖配置是 `storage_profile_id`；该字段创建时确定并在首个数据写入后不可变。LLM/embedding/parser/chunker/prompt/并发参数均继承 server-global 配置。
4. logical MVP 先定义 profile/namespace 契约；strict physical 的专属 endpoint/database/index 与逐 backend 能力验证后移，避免首个 PR 同时审阅所有后端连接池和 WebUI。
5. 旧部署升级时幂等创建 `default` record，映射现有 `args.workspace`，不移动/重嵌入数据；不带 header 的旧客户端行为不变。catalog 与现有 workspace 不一致时拒绝启动，不能静默重映射。
6. catalog schema/version、storage profile 引用和 rollback/backup 方法必须写入升级文档；新版本创建的非默认库在回滚旧版本后不可被旧服务访问，但 default 仍保持可用。

### 12.5 建议的 MVP 与社区 PR 拆分

| 阶段/PR | 范围 | 明确不包含 | 验收重点 |
| --- | --- | --- | --- |
| RFC | 场景、约束、不变量、API、进程/恢复模型、开放问题 | 业务实现 | 维护者对固定实例、显式创建、shared catalog、pool/fairness/phasing 达成共识 |
| Core PR 1 | catalog repository contract、shared durable catalog、legacy default migration、管理 API | WebUI、physical backend 改造 | 多 worker create/list/update/delete 一致；无 lost update；unknown 不 auto-create |
| Core PR 2 | fixed-context per-worker pool、lease、single-flight、safe LRU、REST routing | WebUI、strict physical | 并发/stream/background 不串库；满池背压；eviction 不终止在途工作 |
| Core PR 3 | explicit pipeline context、restart reconciliation、global pipeline cap、workspace fairness、Gunicorn hardening | multi-node provider | worker kill/服务重启恢复；单/多 worker 全局预算一致；无租户饥饿 |
| Compatibility PR | Ollama model alias、SDK/OpenAPI 文档与迁移说明 | WebUI | 无 custom header 的 Ollama client 可确定选库；selector 冲突 fail closed |
| WebUI PR | catalog selector、上传新建/增量体验、状态展示 | physical 后端 | name + ID、路由确认、错误/恢复状态清楚 |
| Physical PR 系列 | storage profile + 各 backend 专属资源、连接池、drop 边界 | 与 core pool/routing 混合提交 | 每 backend 独立审阅和 mock/integration 隔离证据 |
| Cluster PR | external catalog/coordinator/admission provider、fencing | Phase 1 承诺 | 多节点无 sticky session、节点故障恢复与 late-commit 防护 |

### 12.6 RFC 验证与可靠性测试计划

- 路由不变量：在两个 KB 放入相同 doc/entity/key ID 和不同 sentinel，覆盖每个读写/删除/cache/graph/vector/doc-status API，断言永远只触达已解析 workspace；显式空、非法、unknown 和冲突 selector 均 fail closed。
- 生命周期并发：用 `asyncio.Event`/barrier（不靠 sleep）覆盖 create single-flight、request 与 delete、background handoff 与 delete、stream 与 eviction、eviction 与新 acquire、初始化失败重试；断言无 finalize-after-use、无 active lease 负数、无默认库回退。
- catalog 多进程：多个真实进程并发 create/rename/delete/list，验证 CAS、revision、idempotency 和 worker cache invalidation；模拟一个 worker 写后另一个立即路由，不得 404 或读旧状态。
- pipeline/restart：两个以上 KB 同时 ingest，杀死一个 worker、重启全部服务、保留 durable storage；验证每库未完成文档只恢复一次、已完成结果不重复、破坏性半提交被 fencing，track 查询必须带同一 KB。
- 资源治理：单 worker、多 Gunicorn worker分别运行多个 KB；记录 provider 函数真实并发峰值不超过 global limit；大量 A 库任务下 B 库在有界调度轮次内获得 slot；active pipeline 不超过全局 cap。
- pool/连接：加载超过容量的 KB，验证只驱逐 safe idle entry；全部 entry active 时返回 503；反复加载/回收后连接、queue worker、task 和文件句柄回到基线。
- Ollama：`tags -> model alias -> chat/generate` 端到端验证 default/非默认；不带 custom header 也能隔离；header/model 不一致返回 400。
- 升级：用旧单 workspace fixture 启动新版本，不搬数据即可查询；重复启动不重复创建 default；catalog/workspace mismatch fail fast；无 header 的既有 REST client 回归通过。
- 安全：API 日志和 catalog response 不暴露 profile secret；未认证管理/data-plane 均遵循现有认证；测试明确证明“认证后可选所有 KB”，避免误宣称已有 per-KB authorization。

### 12.7 可直接回复 Issue #2527 维护者的英文草案（暂未发布）

```markdown
Thanks for the detailed guidance. I agree that the integration branch is too large to be a useful first review, and I will open a dedicated design RFC before proposing implementation PRs.

I also re-audited the current branch against your questions. Some important parts are already aligned: a `LightRAG` instance is created for one immutable effective workspace and is not intentionally switched at runtime; instances are created lazily with a per-workspace first-access guard; and an unknown knowledge-base ID must already exist in the catalog rather than being auto-created by a data-plane request.

However, the audit also found several areas that need to be redesigned before the implementation should be proposed upstream:

- The current catalog is an atomically-written local JSON file, but each Gunicorn worker has its own in-memory snapshot. That is not a valid shared catalog and can produce stale reads or lost management updates.
- The pool is bounded but has no safe eviction policy. Request counts also do not cover detached/background work, so deletion or future eviction needs an explicit context lease that survives streaming and background task hand-off.
- The request proxy currently falls back to the default context when no `ContextVar` is present. Multi-workspace data-plane and background code must instead fail closed and carry an explicit workspace context.
- Pipeline state is workspace-scoped, but restart recovery is currently trigger-based. The server does not enumerate all catalog entries and fairly reconcile unfinished documents after a full restart.
- Existing cross-worker LLM/embedding limits are a useful base, but single-worker multi-instance limits can multiply, and the fairness unit is a process/local queue rather than a workspace. We need a global active-pipeline cap and workspace-aware fair admission.
- The current same-host `SyncManager` coordination does not provide a future multi-node contract, and the pool/connection cost is indeed workers × active workspaces.
- Ollama chat/generate currently require the custom header. The request `model` field is not used for workspace selection, so standard Ollama clients cannot select a non-default knowledge base.
- Current authentication is server-wide. Workspace isolation is not per-workspace authorization, and I will state that explicitly as out of scope for the first phase.

The RFC will propose the following decisions for discussion:

1. A `LightRAG` instance is permanently bound to one server-generated workspace ID. Requests acquire a concrete, reference-counted context; there is no runtime workspace switching or default fallback after explicit selection.
2. Knowledge bases are created only through a management API. A missing selector keeps the legacy default behavior, while an explicitly empty/invalid selector is `400` and a valid but unknown ID is `404`.
3. The catalog is a shared durable control-plane store with revisions/CAS. Instance pools remain per worker, use single-flight lazy creation, and evict only idle entries with no request, background, or pipeline leases. If no safe victim exists, the server applies backpressure rather than cancelling work.
4. Every pipeline/background job carries an explicit knowledge-base/workspace context. Durable document/job state drives bounded restart reconciliation across all active knowledge bases.
5. Pipelines for different workspaces may run concurrently, subject to a global active-pipeline cap and global LLM/embedding/rerank budgets. Admission is workspace-aware and work-conserving so bulk ingest in one workspace cannot starve another.
6. Gunicorn uses per-worker instance pools plus the shared catalog/coordinator, with no sticky-session assumption. Pipeline mutual exclusion is behind a lease/fencing abstraction so an external coordinator can be added for multi-node deployments later.
7. REST uses `LIGHTRAG-KNOWLEDGE-BASE`. For Ollama clients, I propose catalog-backed model aliases such as `lightrag:default` and `lightrag:<knowledge-base-id>`; conflicting model/header selectors fail with `400`.
8. The only per-workspace configuration dimension in the first phase is a storage-profile reference. LLM, embedding, parser, chunking, prompts, and scheduling remain server-global. Per-workspace authorization is explicitly out of scope.
9. Existing single-workspace deployments are imported as the `default` catalog record without moving data, and requests without the selector remain backward compatible.

I will follow your suggested phasing: logical isolation + shared catalog + instance pool + request/pipeline routing first; WebUI and strict physical-isolation backend work in later PRs. The RFC will also include the pool lifecycle, recovery behavior, Gunicorn resource model, failure semantics, test invariants, and a proposed reviewable PR sequence.

Thank you again — this feedback identified several concurrency and lifecycle requirements that a functional isolation demo alone does not prove.
```

### 12.8 独立设计 Issue（RFC）英文 draft（暂未发布）

建议标题：

```text
[RFC] Multi-workspace API server: fixed workspace instances, shared catalog, bounded pools, and workspace-safe pipelines
```

建议正文：

```markdown
## Summary

This RFC proposes a multi-workspace mode for one LightRAG API server. It focuses on usage scenarios, safety constraints, lifecycle semantics, resource governance, and compatibility. It intentionally separates the logical-isolation core from later WebUI and strict physical-isolation work.

## Usage scenarios

1. An existing single-workspace deployment upgrades without moving or re-embedding data. Requests without a selector continue to use the legacy default workspace.
2. An administrator explicitly creates a knowledge base, receives an opaque ID, then uploads, queries, scans, and manages documents by selecting that ID.
3. Multiple knowledge bases ingest and query concurrently without sharing storage namespaces, pipeline state, caches, input directories, or in-memory RAG state.
4. A server with a bounded connection/memory budget lazily loads active knowledge bases and safely evicts only idle instances.
5. After a worker or full-service restart, unfinished work from multiple knowledge bases is reconciled without duplicate processing or cross-workspace recovery.
6. Gunicorn workers can handle any request without sticky sessions. A future multi-node deployment can replace same-host coordination without changing pipeline semantics.
7. Standard Ollama clients that cannot send custom headers can still select a knowledge base through the required `model` field.

## Non-goals for the first phase

- Per-knowledge-base users, roles, ACLs, quotas, billing, or security tenancy.
- Per-workspace LLM, embedding, parser, chunker, prompt, or scheduler configuration.
- Strict physical isolation for every storage backend.
- WebUI management and selection flows.
- Multi-node production support before an external coordination provider exists.

## Safety invariants

1. A LightRAG instance is bound to exactly one immutable effective workspace for its complete lifecycle. It never switches workspaces.
2. Every data-plane request, stream, background job, and pipeline operation owns an explicit knowledge-base context until completion. An explicitly selected operation never falls back to the default workspace.
3. A data-plane request never creates a knowledge base. Creation is explicit and authenticated through the management API.
4. The catalog is shared durable control-plane state, not process memory. Workspace IDs and catalog revisions are consistent across workers.
5. An instance is finalized only after all request, streaming, background, and pipeline leases have drained.
6. One workspace has at most one mutation pipeline owner. Different workspaces may run concurrently under global resource limits and fair admission.
7. Durable document/job state, not in-memory pipeline status, is the source of truth for restart recovery.
8. Workspace isolation is not authorization. The selector identifies a routing target, not a principal or permission.

## Instance and workspace binding

- `name` is display-only. A server-generated opaque knowledge-base ID is used by clients, and a server-generated backend-safe effective workspace is used for storage namespaces.
- Instances are fixed-workspace objects. Request routing selects an instance; it never mutates one.
- The resolver returns a concrete context lease. The lease covers normal responses, streaming, and explicit hand-off to managed background work.
- In multi-workspace paths, a missing internal context is an error. Legacy defaulting occurs only at the external API boundary when the selector header is absent.

## Catalog and creation policy

- Unknown knowledge bases are never auto-created. This prevents a mistyped selector from silently creating data and prevents unbounded resource abuse.
- The management API creates records with a lifecycle such as `CREATING -> ACTIVE -> DELETING -> TOMBSTONED` and supports idempotent retries.
- The shared catalog provides atomic create-if-absent, revision/CAS updates, listing, and cache invalidation or revision checks.
- A local catalog may be supported for single-worker development. Multi-worker startup requires a durable catalog visible to all workers and fails closed otherwise.
- Deletion first prevents new leases, then drains existing leases, obtains an exclusive fenced operation, cleans resources idempotently, and finally tombstones the record.

## Bounded per-worker instance pools

- Pools are per worker because event-loop-bound Python objects and DB clients cannot be shared safely between workers.
- Creation is lazy and single-flight per knowledge-base ID in each worker.
- Capacity is configured per worker, and deployment diagnostics report the workers × loaded-workspaces connection/memory budget.
- Idle LRU/TTL eviction is allowed only when the entry has no request, stream, background, pipeline, pending-enqueue, or recovery ownership. If no safe entry exists, the request receives temporary backpressure rather than terminating work.
- Initialization failures use bounded retry/backoff and are observable; they do not create unbounded retry storms.

## Pipeline context and restart recovery

- Pipeline status, track IDs, input scanning, destructive operations, and managed jobs carry explicit `(knowledge_base_id, effective_workspace)` context. Track lookup is scoped by `(knowledge_base_id, track_id)`.
- In-memory `pipeline_status` is operational state only. Persistent document status, operation journals, and scan intent/input manifests determine recovery.
- On startup, a recovery coordinator enumerates active catalog records, detects unfinished work without eagerly loading every full instance, and schedules bounded workspace recovery.
- Pending, failed, and abnormally interrupted documents are safely re-queued. Interrupted destructive operations remain fenced until an idempotent recovery or explicit administrator action resolves them.

## Parallelism, limits, and fairness

- Different workspaces may run pipelines concurrently; one workspace remains single-writer for mutations.
- A global active-pipeline cap limits loaded processing work.
- LLM roles, embedding, and rerank use global budgets in both single-worker and Gunicorn modes; loading more instances does not multiply provider concurrency.
- Admission is workspace-aware and work-conserving (for example weighted/deficit round-robin with aging). One workspace's bulk ingest must not starve another workspace indefinitely.
- Overload produces bounded queues or explicit `503` backpressure, never unbounded tasks/connections.

## Multi-process and future clustering

- Each Gunicorn worker has its own instance pool. All workers use the same durable catalog and workspace coordination contract; no sticky sessions are required.
- Same-host shared state may remain one coordinator implementation, but pipeline mutual exclusion is expressed as leases with owner identity and fencing semantics.
- A future external coordinator can provide TTL, heartbeat, fencing, and global admission for multiple nodes. Old owners cannot commit after losing a lease.
- The support matrix will explicitly distinguish single worker, same-host Gunicorn, and future multi-node operation.

## API contract

- REST data plane uses optional `LIGHTRAG-KNOWLEDGE-BASE: <knowledge-base-id>`; omission selects the backward-compatible default.
- If the header is present but empty or syntactically invalid, return `400`. If it is valid but unknown, return `404`. Lifecycle conflicts return `409`; temporary pool/admission/recovery unavailability returns `503`.
- All document, query, graph, cache, pipeline, and knowledge-base-specific status/readiness operations use the same resolver. Management operations identify records in their path/body and do not use the routing header.
- Successful responses expose the resolved knowledge-base ID for auditing and client verification.
- Ollama uses model aliases: `lightrag:latest`/`lightrag:default` select default and `lightrag:<knowledge-base-id>` selects another record. If both model and header selectors are supplied and disagree, return `400`.

## Security boundary

The first phase retains server-wide authentication. Workspace isolation prevents accidental storage/state mixing but does not authorize one user for one workspace. Any authenticated caller may access any catalog entry unless an external trusted gateway restricts selectors. A principal-to-workspace authorization hook can be added later without treating the selector itself as identity.

## Per-workspace configuration

The only per-workspace configuration dimension in the first phase is an immutable storage-profile reference. LLM, embedding, parser, chunking, prompts, and scheduling remain server-global. Strict physical-isolation guarantees and backend-specific profile implementations are deferred to later reviewable PRs.

## Upgrade path

- First startup idempotently creates/imports the `default` catalog record mapped to the existing configured workspace.
- Existing data is not moved or re-embedded.
- Existing clients without the header continue to work.
- A mismatch between the catalog's default effective workspace and the server's legacy workspace fails startup rather than remapping data.
- Catalog backup/versioning and rollback limitations are documented before release.

## Proposed phases

1. Shared catalog, default migration, logical namespaces, fixed-instance pool, explicit REST routing, and management API.
2. Explicit pipeline/background context, restart reconciliation, global active-pipeline/resource limits, fairness, and same-host Gunicorn hardening.
3. Ollama model selection and SDK/OpenAPI compatibility.
4. WebUI management and upload/query selection.
5. Strict physical isolation, split into backend-focused PRs.
6. External coordinator and multi-node support.

## Validation requirements

- Cross-workspace sentinel tests for every storage/data-plane operation.
- Deterministic concurrency tests for first access, deletion, streaming/background leases, eviction, and cancellation.
- Real multi-process catalog consistency tests with concurrent management updates.
- Worker-kill and full-restart pipeline recovery tests across multiple workspaces.
- Global concurrency peak and workspace fairness tests in single-worker and Gunicorn modes.
- Ollama model-only routing and header/model conflict tests.
- Legacy single-workspace upgrade and no-header compatibility tests.

## Questions for maintainers

1. Should same-host Gunicorn support be required in the first core phase, or may the first merge explicitly support one worker while the shared coordinator PR follows immediately?
2. Is the proposed distinction between a single-worker local catalog and a required shared durable catalog for `workers > 1` acceptable?
3. Do `lightrag:default` and `lightrag:<knowledge-base-id>` fit the expected Ollama compatibility contract, or would another alias format be preferred?
4. Is safe idle LRU eviction desired in the first pool PR, or should the MVP return backpressure at capacity and add eviction in the next hardening PR?
5. Does limiting per-workspace overrides to `storage_profile_id` match the desired first-phase configuration boundary?
```

### 12.9 Codex GPT-5.6 Sol 架构设计阶段标准 Prompt

以下 Prompt 用于下一轮与 Codex GPT-5.6 Sol 共同完善 RFC。在 RFC 获得用户/社区确认前，它明确禁止进入大范围业务实现。结构采用结果目标、上下文、硬约束、证据、成功标准、输出契约和询问触发条件，避免重复指令与无验证的“已完成”声明。

~~~text
<role>
你是 LightRAG 多知识库架构的 principal engineer 和 RFC 作者。你需要以高性能、高可靠、高可维护、高可扩展为目标，先完成可供 HKUDS/LightRAG 维护者评审的设计共识；在设计决策被明确批准前，不实施完整功能，不提交或发布 GitHub 内容。
</role>

<objective>
基于 HKUDS/LightRAG Issue #2527 的维护者反馈和当前 dev 实现，完成一份以使用场景、约束、不可违反的不变量、失败语义和分阶段范围为中心的 RFC。逐项回答实例/workspace 绑定、实例池、pipeline 上下文、跨库并行与公平、多进程/未来集群、API/Ollama、安全、配置覆盖、升级和 MVP 范围。对当前未考虑或不可靠的部分给出可验证的设计方案，并把所有决策与时间线持续追加到 lightrag-tasks.md。
</objective>

<sources_of_truth>
1. 仓库根目录 AGENTS.md 与 lightrag-tasks.md；后者第 12 节是本轮 RFC 基线。
2. 社区 Issue：https://github.com/HKUDS/LightRAG/issues/2527 ，尤其维护者要求先提交 dedicated design RFC 的最新回复。
3. 当前分支与 upstream/main 的实际代码、测试、OpenAPI 和 Git 历史。任何“已支持”都必须有文件/行号或测试证据。
4. OpenAI 官方 GPT-5.6 model guidance；Prompt 保持目标导向、边界明确、少重复，并在代表性场景上真实验证。
</sources_of_truth>

<current_findings>
- 当前实例在构造时绑定固定 effective_workspace，惰性初始化和单 worker 首次访问去重已经存在；unknown ID 不会由 data plane 自动创建。
- 当前 catalog 是 local JSON + per-process snapshot，Gunicorn 管理写不具备共享一致性。
- pool 有容量上限但无安全 eviction；普通 request 计数不覆盖 stream/background 的完整生命周期。
- ContextVar proxy 在上下文缺失时会回落 default；显式空 header 也会成为 default，均不满足 fail-closed。
- pipeline 主路径使用 self.workspace，但 full restart 不会公平枚举全部 catalog 并自动恢复未完成文档。
- Gunicorn 已有跨 worker LLM/embedding/rerank global slot，可复用；单 worker 多实例会放大本地 max_async，且公平单位不是 workspace。
- Ollama request.model 当前不参与选库；现有认证是 server-wide，不是 per-workspace authorization。
- 当前分支把 logical、physical、WebUI 和全部 storage backend 集成在一起，必须按社区建议重新分期。
</current_findings>

<hard_constraints>
- 架构选择默认采用“一个 LightRAG 实例终身绑定一个不可变 workspace”，不设计运行中切换。
- 知识库只能经认证管理 API 显式创建；data-plane 的拼错、空值、非法值和 unknown ID 不得 auto-create，不得静默回落 default。
- display name 与物理 namespace 分离；用户名称不得直接成为目录、表、collection、index 或 graph namespace。
- catalog 必须是跨 worker 可见的持久控制面真相；worker 内存只能是有 revision 校验的缓存。
- pool 是 per-worker；所有 request、stream 和 background work 必须持有 concrete context lease。没有安全 victim 时背压，不取消在途工作。
- pipeline/job/scan/track/destructive operation 必须携带显式知识库上下文；多库安全路径不接受 workspace=None 或 global default fallback。
- 全局 LLM/embedding/rerank 和 active-pipeline 限制在单 worker、Gunicorn 和未来集群语义一致；公平性以 workspace 为单位且避免饥饿。
- 不依赖 sticky session；协调协议必须能演进为 external lease + TTL + heartbeat + fencing。
- 明确声明 workspace isolation 不是 authorization。第一阶段不虚构 per-KB ACL。
- 第一阶段每 workspace 只允许覆盖 storage_profile_id；其他模型、解析、chunk、prompt 和调度配置保持 server-global。
- 遵循维护者的 phased MVP：logical/core 优先，strict physical 与 WebUI 后移。
- 保留 legacy default：只有 selector header 完全缺失时才选择 default；旧数据不搬迁、不重嵌入。
- 不读取、输出或写入 .env secret；不修改未跟踪 .codex/；不删除业务数据；不执行 GitHub 发布、commit、push 或 PR，除非用户另行明确授权。
</hard_constraints>

<workflow>
1. 重新读取 AGENTS.md、lightrag-tasks.md 第 12 节、Issue 最新回复、git status、upstream/main 与 dev 差异。确认社区主线是否已改变相关基础设施。
2. 建立“维护者问题 -> 当前代码证据 -> 已覆盖/部分/缺失/方向冲突 -> 风险 -> RFC 决策 -> 验证方法”矩阵。重点检查空 header、ContextVar fallback、后台 task handoff、delete/evict race、catalog lost update、单 worker 并发放大和 full restart。
3. 先用场景和不变量完善 RFC，不从类名/函数名开始。至少覆盖：legacy default、显式创建、并发 query/ingest、满池、worker kill、全服务重启、Gunicorn 任意 worker、Ollama model-only selection、删除与恢复。
4. 冻结关键 ADR：固定实例、shared catalog、per-worker lease pool、pipeline durable recovery、global admission/fairness、coordination provider、API/Ollama、安全/配置/升级。
5. 对仍影响产品语义的开放问题给出推荐选项、替代项和权衡；只在答案会显著改变 RFC/PR 边界时询问用户。可以继续完成不依赖该答案的审计与文档。
6. 输出可直接发布的英文 Issue 回复与独立 RFC draft，但不要实际发布。RFC 聚焦 requirements/constraints/scenarios；实现细节只写到能定义安全契约的程度。
7. 给出可审阅的 PR 顺序和每个 PR 的 non-goals、迁移、测试与回滚条件。避免一个 PR 同时包含 core routing、全 storage physical 和 WebUI。
8. 设计确定性验证：用 event/barrier、fake storage/provider、真实多进程与故障注入，不用仅靠 sleep 的脆弱时序。任何性能/公平结论都给出可测指标。
9. 只追加更新 lightrag-tasks.md：记录决策、未决项、变更、验证和 Asia/Shanghai ISO 8601 时间线；不改写历史结论。
</workflow>

<required_design_answers>
- 固定实例为什么优于可切换实例；如何防止字段被修改和 storage namespace 不一致。
- public ID、display name、effective workspace 的生成、校验、最大长度、backend portability 和 legacy 规则。
- catalog store 的共享性、持久性、CAS/revision、幂等创建、生命周期和两阶段删除。
- pool lazy/single-flight、容量单位、连接预算、entry state、lease、safe eviction、失败退避和 backpressure。
- request/stream/background 如何传递 concrete context；任何缺失上下文为何 fail closed。
- pipeline_status、track_id、input scan、managed jobs 和 destructive work 如何显式绑定 workspace。
- 多库未完成文档在 worker kill/full restart 后如何发现、排序、幂等恢复与 fencing。
- 不同库 pipeline 是否并行；global active cap、provider limit、per-workspace fairness、priority aging 与 overload 行为。
- Gunicorn 的 per-worker pool、shared catalog/coordinator、workers × active-workspaces 成本、无 sticky session；未来 multi-node provider 契约。
- REST header 的缺失/空/非法/unknown/冲突语义，受影响 endpoint 清单和 resolved target 可观测性。
- Ollama client 不带 custom header 时如何用 model alias 选库；tags/ps、header/model 冲突和兼容 default。
- isolation 与 authentication/authorization 的边界、Phase 1 non-goals 和后续 authorizer hook。
- Phase 1 唯一 per-workspace override、storage profile 不可变条件、strict physical 后移方式。
- legacy default 的零搬迁升级、catalog mismatch、备份、回滚和 feature/support matrix。
</required_design_answers>

<deliverables>
A. 中文当前实现审计摘要与证据矩阵，明确 covered/partial/missing/direction mismatch。
B. 中文 ADR/架构决策，含状态机、不变量、失败/背压/恢复语义和替代方案权衡。
C. 可直接回复原 Issue 的英文文本。
D. 可直接新建 dedicated design issue 的英文 RFC，重点为 scenarios/constraints/requirements/non-goals/open questions。
E. 分阶段 MVP/PR 序列，每阶段列 scope、non-goals、migration、tests、rollback 和依赖。
F. 完整验证计划，覆盖单进程、多进程、故障恢复、公平、资源释放、Ollama、升级和安全边界。
G. lightrag-tasks.md 追加时间线，记录未决问题和下一步；本阶段不产生业务实现 commit。
</deliverables>

<success_criteria>
1. 维护者反馈中的每一个问题都在矩阵和 RFC 中有明确答案，不用读集成分支猜测语义。
2. 明确选择固定 workspace 实例、显式创建、shared catalog、per-worker bounded pool、explicit pipeline context 和无 sticky session。
3. 空/非法/unknown selector、后台 lease、delete/evict race、full restart、单 worker 并发放大和 workspace fairness 都有 fail-closed 设计与可执行测试。
4. Phase 1 范围与维护者建议一致；physical/WebUI 不再阻塞 core RFC/PR 审阅。
5. RFC 对 authorization 和 multi-node 支持不做超过实现证据的承诺，升级路径不搬迁 default 数据。
6. 文档中的“当前已支持”和“拟议设计”严格区分；未运行的测试不得写成已通过。
</success_criteria>

<output_style>
- 先给结论，再给证据和决策。
- 使用紧凑表格表达逐项映射；复杂并发关系可用小型状态机/时序图。
- 中文用于本地分析/Prompt，社区回复和 RFC 使用清晰专业英文。
- 引用实际文件与行号；外部结论引用 Issue 或官方文档链接。
- 不复述长过程日志，不泄露 secret，不用模糊的“应该没问题”代替验证。
</output_style>

<completion_contract>
本阶段只有在审计矩阵、ADR、英文回复、英文 RFC、PR 分期、验证计划和 lightrag-tasks.md 时间线均完成，并明确列出所有开放问题后才算完成。未获得 RFC 方向确认前停止在设计边界，不开始全量业务重构。
</completion_contract>
~~~

Prompt 依据：OpenAI 官方 [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/model-guidance?model=gpt-5.6) 与本地 OpenAI Docs skill 的 GPT-5.6 Sol 升级/Prompt 参考。采用点包括：提供领域上下文和 hard constraints、明确授权边界与提问触发条件、给出成功标准和输出格式、保持指令单一且避免重复、要求真实证据与验证。

### 2026-07-28T19:48:14+08:00 — 默认存储多租隔离验证与完整 Docker 部署补充

- 阶段：默认文件型存储多租验证、容器化部署交付。
- 新增回归：`tests/workspace/test_file_storage_multitenant_e2e.py`。测试以两个并发 `LightRAG` 实例向不同 workspace 写入完全相同的内容，故意复用 content-hash document ID 与 chunk ID；覆盖 7 个 JSON KV namespace、3 个 NanoVectorDB、NetworkX GraphML 和 JSON doc-status 的 workspace 落盘路径，并验证图实体不串租、删除一个库的同 ID 文档/分块/向量后另一个库仍完整。
- 验证结果：`tests/workspace/test_workspace_isolation.py`、`tests/workspace/test_workspace_path_validation.py`、新增文件型 E2E 测试和 `tests/api/test_knowledge_bases.py` 共 `58 passed`。pytest 仅报告既有 `.pytest_cache` ACL 警告；运行时使用仓库内 `--basetemp`，未修改业务数据。
- 隔离结论：在通过知识库 catalog 与 `LIGHTRAG-KNOWLEDGE-BASE` 请求头访问的前提下，默认 `JsonKVStorage`、`NanoVectorDBStorage`、`NetworkXStorage`、`JsonDocStatusStorage` 可完成数据、图、文档状态、文件路径和 pipeline namespace 的逻辑隔离；同一共享 Docker volume/服务级 API Key 不构成不互信租户的授权或严格物理安全边界。
- 部署产物：新增 `docker-compose.multitenant-standalone.yml`（完整镜像 + 默认文件型存储）和 `docker-compose.multitenant-services.yml`（完整镜像 + PostgreSQL/pgvector、Neo4j、Redis），以及对应的 `.env.multitenant-*.example`。外部服务方案显式禁止 `POSTGRES_WORKSPACE`、`NEO4J_WORKSPACE`、`REDIS_WORKSPACE` 强制覆盖，确保四类存储使用实例 workspace。
- 文档：新增 `docs/MultiTenantDockerDeployment-zh.md`，覆盖镜像构建、OpenAI-Compatible API 配置、启动/停止、持久化/备份、API/WebUI 多租验收、隔离边界和故障排查。
- Compose 验证：两份 Compose 均已使用各自 example 环境文件完成 `docker compose config --quiet` 静态解析。当前工作站 Docker CLI 存在但 Docker Engine 未启动（`npipe://./pipe/docker_engine` 不存在），因此未执行镜像 build、容器启动或带真实模型 API 的容器级 E2E；Docker Desktop/Engine 启动后应按新增部署文档执行 build、up 和第 5 节双知识库上传/查询/删除验收。

### 2026-07-23T14:04:59+08:00 — 维护者反馈审计与 RFC/Prompt draft 完成

- 阶段：社区 RFC 设计准备。
- 状态：设计文档已完成，等待用户共同评审；尚未发布社区回复/RFC，尚未按新架构改业务代码。
- 审计结论：固定实例、显式管理创建、logical storage namespace、request header 和单进程首次访问已经有可复用基础；shared catalog、安全 eviction/background lease、自动多库重启恢复、workspace 公平、multi-node coordinator、Ollama model 选库和授权边界尚未完整实现。
- 新发现的高风险边界：显式空 header 当前落到 default；ContextVar proxy 缺上下文落到 default；catalog 在 Gunicorn worker 间是过期快照；后台 task handoff 与 delete/future eviction 无共同 lease；单 worker 多实例会放大全局 provider 并发。
- 决策草案：实例终身固定 workspace；data plane 永不 auto-create；只有 header 缺失兼容 default；shared durable catalog + CAS；per-worker lease pool + safe idle LRU；pipeline 显式 context + durable bounded recovery；全运行模式 global admission + workspace fairness；无 sticky session；Ollama 使用 model aliases；Phase 1 不含 per-KB authorization。
- 社区材料：第 12.7 节为原 Issue 英文回复草案，第 12.8 节为 dedicated design RFC draft；均仅写入本地，未代表用户发布。
- Prompt：第 12.9 节已冻结下一轮 Codex GPT-5.6 Sol 设计阶段 Prompt；明确在 RFC 共识前不得开始大范围实现。
- 下一步：与用户逐项确认第 12.8 节的五个开放问题，必要时调整 RFC；用户认可后再发布 dedicated design issue，并依据维护者反馈冻结 Core PR 1 的最小范围。

### 2026-07-23T14:22:32+08:00 — RFC 文档与当前实现证据复核

- 文档检查：第 12 节共追加 458 行；Markdown backtick/tilde fence 数均为偶数，UTF-8 中文读取正常，`git diff --check` 通过（仅有仓库 Windows line-ending 提示）。历史章节未改写。
- 聚焦回归：`.venv\Scripts\python.exe -m pytest tests/api/test_knowledge_bases.py -q --basetemp .test-temp-rfc -p no:cacheprovider` 结果为 `21 passed in 0.85s`。验证了当前 catalog/default 兼容、ContextVar 并发隔离、12 storage workspace 传递、data-plane OpenAPI header、管理 CRUD、删除保护和 storage profile 的既有行为。
- 环境说明：首次 `uv run pytest` 因运行中的服务占用 `.venv\Scripts\lightrag-server.exe`，uv 无法替换入口文件；直接 pytest 首次又因系统默认 `pytest-of-hy` 和既有 `.pytest_cache` ACL 被拒绝而停在 fixture setup。两者都没有产生用例断言失败；改用独立可写 basetemp 后全部通过。
- 清理：已核验并删除本轮专用 `D:\code\codex\LightRAG\.test-temp-rfc`；未停止当前服务，未触碰业务数据或未跟踪 `.codex/`。
- 限制：本轮没有改业务代码，因此未重跑全仓、真实 LLM、外部 backend integration 或浏览器 E2E；第 12.6 节的新架构验证均是后续实现必须新增的测试，未写成已通过。

### 2026-07-23T21:01:00+08:00 — 维护者二次反馈与完整 RFC 冻结

- 新增输入：维护者基于已关闭的 PR #3397 补充了 storage-level effective-workspace 一致性、无默认 workspace 的兼容语义、端点实例化权限、迁移时机，以及跨 workspace 共享 LLM/embedding limiter 五组要求。
- 代码证据：当前 PostgreSQL 的实际优先级为 backend override > instance workspace > `default`；Redis KV 对空 workspace 保留空值而 Redis doc-status 暴露 `_`；Qdrant 将空值映射为 `_`。各 backend 独立解析覆盖变量，确实可能让四类 storage 的 effective workspace 分裂或坍缩。
- 高风险确认：`doc_status` 同时承担 PENDING/PROCESSING 扫描、`track_id` 查询与重启恢复队列；相同内容在不同知识库生成相同 content-hash doc ID，因此 doc-status 坍缩会发生静默覆盖，而不仅是展示或诊断错误。
- 当前实现缺口：动态 logical 知识库创建已能针对 active backend 的 forced workspace variable 拒绝部分危险配置，但默认知识库、混合 backend、实际 storage object 的四族一致性仍未 fail-fast；`/health` 当前会走 `get_context()`，非默认实例仍在首次访问时执行 `initialize_storages()` 和 `check_and_migrate_data()`。
- 架构决策：multi-workspace 模式下由 catalog/instance 统一解析不可变 canonical workspace key，所有 active backend workspace override 均导致启动失败；legacy single-workspace 保留一致配置的历史优先级，但四族实际不一致仍 fail-fast。storage profile 只能选择资源，不能改变 workspace identity。
- 默认兼容决策：公共 ID `default` 映射到带类型的 `LegacyDefault` canonical key；后端通过版本化 `legacy-v1` codec 继续读取 PostgreSQL `default`、Redis 无前缀、文件根目录等历史物理布局。新建 workspace 禁止空值、`default`、`_` 和内部保留前缀，从身份层消除碰撞，且不搬迁旧数据。
- 生命周期决策：`/health`、`/ready`、catalog/pool status 只读快照且不得创建实例；数据端点只能 lazy-load 已存在且 ACTIVE 的记录；仅管理 create/delete 可显式产生生命周期副作用。迁移由 startup/control-plane coordinator 执行，普通首次 query/upload 永不成为 migration owner。
- 资源决策：每个 RAG 实例不再拥有可叠加的 provider 总预算；单进程、Gunicorn 和未来集群统一通过 service-level LLM/embedding/rerank admission controller 计数，并以 workspace 为公平调度单元。N 个 active workspace 的上限仍为配置值 C，而不是 N×C。
- 文档产物：已将可直接用于 dedicated design issue 的完整英文 RFC 写入根目录 `lightrag-rfc.md`，覆盖 28 节、usage scenarios、12 条不变量、endpoint policy matrix、migration/recovery、失败语义、7 组分拆 PR 与完整验证计划；尚未发布 GitHub、commit 或 push。
- 待维护者确认：公共 header 命名、legacy 四族不一致是否立即 fail-fast、Gunicorn 进入支持矩阵的阶段、Ollama alias 格式，以及首个 shared catalog provider 的选择。

### 2026-07-23T21:39:28+08:00 — Audit Gap 中文深度分析文档完成

- 产物：新增根目录 `lightrag-audit-gaps.md`，基于 `dev@27e41a703f4f` 解释当前 header → context → instance pool → 12 storage objects → pipeline 的端到端代码模型。
- 覆盖范围：逐项分析 shared catalog、pool/lease、default fallback、全 catalog 重启恢复、四族 effective-workspace 一致性、legacy empty/default 编码、health/migration side effect、provider concurrency 放大和 Ollama model routing 共九个 Gap。
- 分析格式：每个 Gap 均包含当前代码路径、通俗类比、生产案例、最坏故障、现有局部保护、目标设计和确定性测试方法；另给出九个 Gap 叠加时的复合事故链和按依赖排序的实现阶段。
- 关键补充：确认 `active_requests` 与 deleting reservation 非原子，managed background task 可在 HTTP request lease 结束后继续持有复制的 ContextVar；当前 32 实例上限没有 eviction，因此长期运行会出现“只能入住不能退房”的容量耗尽。
- 测试边界：现有 fake-storage 测试证明 12 个对象收到初始 workspace，但没有执行真实 backend override 解析；现有 ContextVar 测试证明普通并发请求隔离，但没有证明 background/stream/delete/eviction 的完整生命周期安全。
- 变更范围：仅新增/更新 Markdown 文档，没有修改业务代码、配置、存储数据或 `.codex/`，未发布 GitHub、commit 或 push。

## 13. 2026-08-03 上游同步后的 RFC 实现优化任务

### 13.1 本轮目标与执行约束

本轮以 `docs/lightrag-rfc-en.md` 为规范基线，以 `dev@64713519`（包含 `upstream/main@301e715c`）为代码基线。完整差距、证据、目标架构、ADR、失败语义和验证策略见 `docs/lightrag-rfc-impl.md`。

当前阶段只完成设计冻结和任务拆分，不修改多知识库业务代码。用户评审 `docs/lightrag-rfc-impl.md` 并确认关键决策后，才按下表顺序逐项实现。每项任务必须满足自己的验收标准并记录真实验证结果，不能用后续阶段的计划替代当前完成证据。

共同约束：

- default workspace 升级不搬迁、不重嵌入、不重命名现有数据；
- 一个 `LightRAG` 实例终身绑定一个 immutable workspace binding；
- data plane 不 auto-create，不在 first request 中迁移；
- 只有 header 完全缺失才兼容选择 default；present-empty/invalid 不回退；
- multi-workspace mode 禁止 active backend workspace override；
- non-default ingestion/write 在 pipeline context、recovery 和 shared admission 完成前保持 feature-gated；
- local JSON catalog 只可作为单 worker provider；未证明安全前不得宣称 Gunicorn/multi-node 支持；
- isolation 不等于 authorization，management mutation 必须有明确 admin boundary；
- 不修改或提交未跟踪的 `.codex/`。

### 13.2 可逐项实施的任务清单

| ID | 阶段 | 任务 | 依赖 | 状态 | 完成定义 |
| --- | --- | --- | --- | --- | --- |
| RFC-I00 | 设计 Gate | 评审 `lightrag-rfc-impl.md` 第 13 节八项决策，冻结 selector、catalog provider、lifecycle API、admin 和支持矩阵 | 无 | 已完成 | 决策写入 ADR；不再存在会改变 Phase 1～4 边界的未决项 |
| RFC-I01 | Phase 0 | 增加 legacy/multi-workspace feature mode、deployment support matrix 与 fail-closed 启动校验 | RFC-I00 | 已完成 | 不支持的 workers/catalog/coordinator 组合启动失败；default 行为不变 |
| RFC-I02 | Phase 0 | 建立 endpoint policy registry、OpenAPI route classification 和 side-effect counter 测试骨架 | RFC-I00 | 已完成 | 每个 route 唯一分类；新增未分类 route 测试失败；health/ready 的零构造断言可执行 |
| RFC-I03 | Phase 1 | 实现 `WorkspaceBinding` tagged identity、`legacy-v1`/`namespace-v1` codec、reserved name 规则 | RFC-I01 | 已完成 | empty/default/_/named 不碰撞；default 原物理布局可读；binding 构造后不可变 |
| RFC-I04 | Phase 1 | 为 KV/vector/graph/doc-status 建立统一 `StorageNamespaceDescriptor` 和四族一致性 preflight | RFC-I03 | 已完成 | 每个 active backend 报告 canonical key/codec/fingerprint；mismatch 在数据访问前失败 |
| RFC-I05 | Phase 1 | 统一 workspace override 规则并覆盖全部 storage backend | RFC-I04 | 已完成 | multi-workspace 任一 override 启动失败；legacy consistent 可兼容、mixed family 失败；真实 backend 回归通过 |
| RFC-I06 | Phase 2 | 抽象 `CatalogProvider`，保留 single-worker local provider，实现首个 PostgreSQL shared provider | RFC-I03、RFC-I00 catalog 决策 | 未开始 | revision/CAS、分页、唯一约束、cache invalidation；local+workers>1 fail startup |
| RFC-I07 | Phase 2 | 实现 catalog lifecycle、幂等 management operation、fencing 与 tombstone | RFC-I06、RFC-I04 | 未开始 | CREATING/MIGRATING/ACTIVE/DELETING/TOMBSTONED/ERROR 可恢复；kill owner 与 stale commit 测试通过 |
| RFC-I08 | Phase 3 | 重构为 explicit `WorkspaceExecutionContext` 与 fail-closed ContextVar adapter，修正 selector/response contract | RFC-I06、RFC-I07 | 未开始 | absent/empty/invalid/unknown/inactive 全矩阵通过；缺 context typed failure；成功响应暴露 resolved ID |
| RFC-I09 | Phase 3 | 实现 per-worker lease pool：entry state、single-flight、resource weight、safe LRU、backoff、503 背压 | RFC-I08 | 未开始 | stream/background lease 阻止 eviction/delete；满池无安全 victim 返回 503；取消 exactly-once release |
| RFC-I10 | Phase 3 | 拆分 side-effect-free `/health`、`/ready`、catalog/pool peek；移除 first-access migration | RFC-I07、RFC-I09、RFC-I02 | 未开始 | health/ready 产生零实例构造、零 storage init、零 migration；runtime observation 可返回 UNLOADED |
| RFC-I11 | Phase 4 | 建立 control-plane migration/recovery coordinator，分页枚举全 catalog 并使用 lease/fencing | RFC-I07、RFC-I10 | 未开始 | full restart 无需用户访问即可恢复全部 ACTIVE workspace；一个坏库不阻塞其他库；migration 非 request-owned |
| RFC-I12 | Phase 4 | 把 request/stream/background/pipeline/delete 全部绑定 explicit context 和 lease handoff，接入现有 ingress/fence | RFC-I08、RFC-I09、RFC-I11 | 未开始 | background handoff 原子；相同 doc hash 跨库状态全生命周期不串；worker kill 后旧 owner 不能提交 |
| RFC-I13 | Phase 4 | 实现 fenced 两阶段删除和 durable cleanup journal | RFC-I04、RFC-I07、RFC-I09、RFC-I12 | 未开始 | ACTIVE→DELETING 拒绝新 lease；partial drop 可续跑；descriptor mismatch 禁止 destructive action；最终 tombstone |
| RFC-I14 | Phase 5 | 将现有 Gunicorn global slot 提升为单进程/Gunicorn 共用的 service-level provider admission | RFC-I09、RFC-I12 | 未开始 | N workspace 的 LLM/embedding/rerank observed peak 不超过 deployment total C |
| RFC-I15 | Phase 5 | 增加 global active-pipeline cap、workspace DRR/aging、公平和 bounded overload | RFC-I11、RFC-I14 | 未开始 | A 持续 bulk ingest 时 B 在约定上限内获得服务；queue 饱和返回 429/503 且内存有界 |
| RFC-I16 | Phase 5 | 将 same-host Manager 能力封装为 coordinator provider，完成 Gunicorn support matrix | RFC-I06、RFC-I11、RFC-I15 | 未开始 | 任意 worker 路由、catalog revision、worker kill、provider cap 和 pipeline owner 测试通过；无 sticky session |
| RFC-I17 | Phase 6 | 实现 Ollama model alias、header/model conflict 和 metadata side-effect-free | RFC-I08、RFC-I10 | 未开始 | 标准 client 仅用 model 可选非默认库；unknown 不创建；conflict 400；tags/ps 不 load instance |
| RFC-I18 | Phase 7 | 基于新 contract 重接 WebUI 与 API selector，修正 stale `LIGHTRAG-WORKSPACE` 文案/header | RFC-I08、RFC-I10、RFC-I17 | 未开始 | UI 创建项置顶、全库 name+ID 可选；Bun 全测/build；API Vary/header 一致 |
| RFC-I19 | Phase 7+ | 按 backend 分拆 strict physical profile 的 provision/migration/delete/backup 硬化 | RFC-I04、RFC-I07、RFC-I13 | 未开始 | 每个 backend 独立 PR、真实服务 integration、resource ownership 与恢复文档，不混入 core PR |
| RFC-I20 | Later | 实现 external coordinator 并验证多节点 | RFC-I16 | 未开始 | TTL/heartbeat/fencing、网络故障、node kill、global admission 和无 sticky session 全部通过后才更新支持声明 |

### 13.3 每项任务的统一交付模板

每完成一个 RFC-I 任务，在本文件时间线追加：

1. Asia/Shanghai ISO 8601 时间、任务 ID、代码基线与 commit；
2. 实际修改文件和不变量变化；
3. 执行的 unit/integration/multiprocess/fault-injection/UI 测试命令；
4. 明确 pass/skip/fail 数量及环境限制；
5. 兼容、迁移、回滚和 feature flag 状态；
6. 新发现的 Gap 和后续任务依赖调整；
7. 未运行的真实外部服务或多节点测试必须明确标为未验证。

### 13.4 实施顺序 Gate

- RFC-I00 未完成：不得开始业务实现；
- RFC-I03～I05 未完成：不得发布动态 catalog/data routing；
- RFC-I06～I10 未完成：不得宣称 Gunicorn catalog 一致或 side-effect-free health；
- RFC-I11～I15 未完成：non-default ingestion/write 保持 feature-gated；
- RFC-I16 未完成：同机 Gunicorn 不进入 supported matrix；
- RFC-I20 未完成：不得宣称 multi-node production safety。

### 2026-08-03T14:56:45+08:00 — 上游同步、合并验证与新一轮 RFC 实现设计完成

- Git 整理：当前工作按类别形成 `b58b7edb test(workspace): cover standalone tenant isolation`、`6e4ad39c feat(docker): add multi-tenant deployment stacks`、`81606bed docs(rfc): organize multi-workspace design artifacts`，已推送 `origin/dev`。
- 上游同步：将 `upstream/main@301e715c` 合并到 fork `main`，解决 server、MongoDB、OpenSearch、WebUI stream test 与 PostgreSQL manager test 冲突，形成 `b9ce4baa Merge upstream/main into main` 并推送 `origin/main`；再以 `64713519 Merge branch 'main' into dev` 合并并推送 `origin/dev`。
- 冲突处理原则：保留多知识库 `build_rag` factory 和 selected-RAG health capability，同时吸收上游 pipeline scheduling、strict storage reads、pending admission、新 storage client 管理和测试 mock 改进；未修改 `.codex/`。
- 已完成验证：冲突相关 backend/API/storage 聚焦回归 `355 passed`；多知识库/默认文件存储隔离回归 `58 passed`；WebUI targeted stream `21 passed`、全量 Bun `94 passed, 0 failed`，production build 成功；相关 ruff/pre-commit 通过。
- API 全量环境边界：Windows 不提供 Gunicorn 所需 POSIX `fcntl`；一次排除 Gunicorn 的 API sweep 暴露 5 项，其中 tokenizer cache 配置后两项 Ollama input-limit 在对应 13-test 文件中通过。剩余观察项为一个 Windows-only Gunicorn import，以及两个既有 auth contract 断言期望 401、当前 API-key-only 路径实际返回 403；它们未被伪报为本轮全量通过，也未在本设计文档提交中修改无关业务语义。
- 新审计产物：新增 `docs/lightrag-rfc-impl.md`。结论是最新上游的 workspace ingress、bounded scheduling、recovery fence、scan job store 和 Gunicorn provider global slot 可复用，但 shared catalog/lifecycle、canonical descriptor、lease pool、side-effect-free health、catalog-driven recovery、单进程 shared admission、workspace fairness、fenced deletion 与 Ollama alias 仍需按 Phase 1～7 实现。
- 任务状态：RFC-I00 等待用户评审；RFC-I01～I20 均未开始。用户确认设计前，本轮停止在文档和任务边界，不实施业务重构。

### 2026-08-03T19:49:46+08:00 — RFC-I00～I02 Phase 0 决策冻结与安全支架

- 决策：用户确认 `docs/lightrag-rfc-impl.md` 第 13 节全部推荐项；PostgreSQL shared catalog、override fail-fast、异步 lifecycle、canonical header/Ollama alias、read→query→write、Admin API Key 和单 worker 首个 MVP 成为后续实现基线。
- 模式与支持矩阵：新增 `lightrag/api/workspace_config.py`，`LIGHTRAG_MULTI_WORKSPACE_MODE` 默认 `legacy`；显式 `multi + local catalog + local coordinator + workers=1` 作为当前唯一支持的多 workspace 组合。local provider 配置多 worker、未实现的 PostgreSQL/Manager provider 或非法枚举均启动失败，不静默降低安全语义。
- legacy Gate：API server 把 mode 注入 `KnowledgeBaseManager`；legacy 仅暴露/路由 default，拒绝动态 create/delete 和已存在非默认 record，同时保留真正 unknown ID 的既有 404 语义。直接构造 manager 的 library/API 单元测试默认继续启用多 workspace，避免破坏显式调用方。
- Endpoint policy：新增 `lightrag/api/endpoint_policy.py`，为 schema-visible route 定义 liveness、control observation、management lifecycle、data read/write 和 runtime observation 等唯一类别；app 构造结束时 fail-closed 校验，任何新 route 未登记会阻止启动。policy 明确 health/version 不得 catalog lookup、load、create 或 migrate。
- 可观测测试支架：manager 新增 construction/storage-init/migration attempt counter；后续 Phase 3 可用调用前后 snapshot 证明 health/ready 零副作用，而不靠 mock 调用路径猜测。
- 部署配置：三个 multi-tenant Compose 及对应 example 显式设置 `multi/local/local`；通用 `env.example` 说明 opt-in 和当前单 worker 限制。三份 Compose 均完成 `docker compose config --quiet`；Docker 仅报告用户级 config ACL warning，解析退出码为 0。
- 验证：新增 deployment matrix、endpoint fail-closed 和 legacy gate/counter 测试；聚焦 suite `48 passed`，包含 health 回归。与默认文件存储隔离组合后的 Phase 0 suite 为 `84 passed`。ruff check 通过。
- 已知非本轮问题：额外 path-prefix sweep 的三个剩余失败仍是 Windows 无 POSIX `fcntl`，以及两个既有 API-key-only 401/403 断言差异；Phase 0 曾引入的一项 unknown-ID 文案回归已修复并由 health 测试覆盖。
- 状态：RFC-I00、RFC-I01、RFC-I02 完成；下一项为 RFC-I03 canonical `WorkspaceBinding` 与 versioned codec。

### 2026-08-03T20:12:25+08:00 — RFC-I03～I05 Phase 1 canonical binding 与四族一致性

- 代码基线与提交：基于 `dev@2a73ee97` 实施，形成 `3d79281c feat(workspace): enforce canonical storage bindings`。未跟踪的 `.codex/` 未修改、未暂存、未提交。
- Tagged identity：新增 `lightrag/workspace.py` 和 public `WorkspaceBinding` export。默认库固定为 `LegacyDefault + @legacy-default + legacy-v1`，named record 固定为 `Named + namespace-v1`；binding 为 frozen dataclass。空值、`default`、`_`、路径穿越及内部前缀不能成为 named key，display name 不参与物理命名。
- 零拷贝兼容：catalog 在不提升 JSON version 的情况下为旧 record 补齐 binding tags；默认 record 的 `effective_workspace` 和既有目录/表/label/payload 不重命名、不复制、不重嵌入。23 个可选 backend 均在 legacy codec registry 中显式登记，PostgreSQL `default`、Qdrant `_`、Neo4j/Memgraph `base` 与无前缀/根目录被统一映射到同一个 canonical identity。
- 四族 descriptor gate：`StorageNameSpace` 统一提供 secret-free `StorageNamespaceDescriptor`；`LightRAG` 对全部 12 个 storage object 在 construction 和 post-connect 两次验证 family/role/implementation/canonical key/codec/profile/fingerprint。named workspace 必须精确使用 canonical physical key；legacy family 解析不一致直接失败。删除前再次验证，mismatch 时在任何 `drop()` 前拒绝 destructive cleanup。
- Override 启动审计：API 在创建 `DocumentManager`、catalog 文件和 RAG 实例前审计四个 active storage family。multi mode 下八类 `*_WORKSPACE` 环境变量及 PostgreSQL `config.ini` override 任一非空即失败；legacy mode 只有四族解析到同一逻辑 workspace 才兼容，mixed family fail-closed。storage profile 的 backend section 明确禁止 `workspace` 字段。Redis KV/doc-status 新增实际 key partition 报告，避免内部 lock fallback 掩盖真实 namespace。
- 严格验证结论：Phase 1 聚焦与 workspace 扩展 suite `165 passed`；PostgreSQL、Redis、MongoDB、Neo4j、Milvus、Qdrant、Memgraph、OpenSearch 八类 backend 离线目录 `1023 passed, 12 deselected`；显式 named binding 的默认文件后端真实同 hash 插入/删除 E2E 通过，并验证 12 个 descriptor 一致；ruff 通过。
- 扩大回归边界：排除一个会在线下载 tiktoken cache 的 batch 文件后，`tests/kg` 得到 `1470 passed, 8 skipped, 8 failed, 139 deselected`。8 项均为既有 Windows/POSIX 环境差异（PID 1、`SIGKILL`、Windows `os.kill` fallback）或测试源码使用系统 GBK 解码 UTF-8；包含 batch 文件的 fail-fast 运行在 `1147 passed, 1 skipped` 后因沙箱禁止下载 `o200k_base.tiktoken` 停止。API 扩展回归 `82 passed, 3 failed`，仍是 Windows 缺 `fcntl` 与既有 401/403 断言差异，和 Phase 0 记录一致。
- 未验证边界：本机 Docker daemon 未运行，故本阶段没有把 PostgreSQL/Redis/Neo4j 等真实服务 integration 伪报为已通过；其 mock/offline backend 回归已全绿。多进程 shared catalog、lifecycle、lease/fencing、真实服务 mixed override 与 multi-node 仍属于 Phase 2+。
- 兼容与回滚：`LIGHTRAG_MULTI_WORKSPACE_MODE` 默认仍为 `legacy`；直接 library 调用未提供 binding 时自动使用 `legacy-v1`。回滚代码不会搬迁数据；含新增 tag 的 version-1 catalog 仍保留原字段和物理 workspace。状态：RFC-I03、I04、I05 完成，下一项为 RFC-I06 PostgreSQL shared `CatalogProvider`。
