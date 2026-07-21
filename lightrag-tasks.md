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
