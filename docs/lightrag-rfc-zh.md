# RFC：LightRAG 安全的多工作空间架构

- 状态：用于社区讨论的草案
- 作者：@crabcanon
- 对应讨论：[Issue #2527](https://github.com/HKUDS/LightRAG/issues/2527)
- 对此前实现审查的关联：[PR #3397](https://github.com/HKUDS/LightRAG/pull/3397)
- 最后更新：2026-07-26
- 英文原文：lightrag-rfc.md

> 本文件是同目录英文 RFC 的中文完整翻译。术语、API 名称和配置项尽量保留英文标识；如中英文在未来修改中产生语义差异，以英文原文为准，并应在同一次修改中同步更新两份文件。

## 1. 摘要

本 RFC 提出一套由显式开关启用的 LightRAG 多工作空间服务端架构。它刻意先讨论设计：在提交实现型 PR 前，先定义使用场景、安全不变量、生命周期规则、资源上限、兼容性行为和分阶段交付计划。

本次修订纳入了后续审查中关于存储层 effective workspace 一致性（尤其是 doc-status）、遗留默认库的表示方式、无副作用端点、迁移所有权和部署范围模型限流的重点意见。

核心决策如下：

1. 一个 LightRAG 实例在其整个生命周期内永久绑定一个不可变、规范化的工作空间键，绝不在运行中切换工作空间。
2. 工作空间只能由经过认证的管理面操作创建；数据面选择器不能创建工作空间。
3. 持久化且跨进程共享的工作空间目录是控制面的唯一事实来源。每个 worker 可以有受上限约束的本地实例池，但不存在任何 worker 本地目录可作为权威。
4. 工作空间身份只在 server/core 层解析一次，并原样传给 KV、向量、图和文档状态四类存储。在多工作空间模式下，存储后端不能再自行覆盖它。
5. 在迁移或数据访问开始前，所有存储对象都必须证明自己使用预期的规范化工作空间键和 namespace 编码策略。任何不匹配都必须导致初始化硬失败。
6. 未命名的遗留工作空间无需搬迁即可继续使用。它拥有无歧义、带标签的规范化身份，同时版本化兼容 codec 保留不同后端历史上的物理命名；保留名称可防止新工作空间碰撞遗留数据。
7. 请求、流式响应、pipeline、扫描、恢复、破坏性操作和后台任务都持有明确的工作空间上下文及生命周期 lease。上下文缺失必须失败关闭，绝不能回退到进程全局默认值。
8. LLM、embedding 和 reranker 并发由服务级共享准入控制器管理。创建 N 个工作空间实例不能制造 N 份独立并发预算。
9. 健康和就绪端点必须无副作用。存储迁移绝不能由任意第一次数据面请求触发。
10. 第一阶段实现逻辑隔离、目录、受限实例池、请求路由，以及使这条路径安全所必需的 pipeline/资源改造。严格物理隔离和 WebUI 支持放在后续阶段。

## 2. 动机与问题陈述

LightRAG 已经接受 workspace 值，且许多存储后端实现了某种命名空间隔离。对于分别构造的单工作空间实例，这已足够；但它尚不是一份完整的多工作空间服务端契约。

主要故障往往是静默发生的，而非立刻报错：

- 某个后端专属环境变量可以覆盖服务端选择的 workspace，使多个实例坍缩到同一命名空间。
- 混合后端配置可能让 KV、向量、图和 doc-status 解析到不同工作空间。
- doc_status 不只是元数据；它是用于 PENDING/PROCESSING 发现、track_id 查询和重启恢复的持久化摄入工作队列。文档 ID 是内容哈希，两个工作空间的相同文档会有相同 ID；一旦 doc-status 命名空间坍缩，一个工作空间的队列记录会静默覆盖另一个工作空间。
- 空/default 工作空间在不同后端中的表示不一致。PostgreSQL 历史上使用 default，Redis 曾使用无前缀命名空间，并在某些 workspace 字段使用 _；文件存储则使用 working-directory 根目录。一个字面量为 default 的工作空间可能与 PostgreSQL 中未命名遗留数据碰撞。
- 惰性实例创建可能让未知或拼写错误的选择器分配资源、迁移数据或创建命名空间。
- 每实例 LLM/embedding wrapper 会把配置的并发度乘以活跃工作空间数量。
- worker 本地目录和锁不足以支持 Gunicorn，也无法为未来多节点部署提供安全路径。

本 RFC 将工作空间隔离视为一项端到端不变量，覆盖路由、对象生命周期、全部四类存储、pipeline 状态、资源调度和恢复。

## 3. 术语

| 术语 | 含义 |
| --- | --- |
| Knowledge-base ID | REST、Ollama alias 和管理 API 使用的稳定公开选择器。它是不可推导的标识，不是存储名称。 |
| Display name | 用户可编辑的展示标签。绝不插入表名、collection、index、graph、key prefix 或目录名。 |
| Canonical workspace key | 由控制面解析、传递给四类存储的不可变内部身份。它是带标签的值，而不是有歧义的原始字符串。 |
| Effective workspace | 实际绑定到某个 LightRAG 实例及其存储对象的 canonical workspace key。本 RFC 中 effective 始终指这一规范化键，而非后端专属字符串。 |
| Physical namespace | 由版本化 namespace codec 从工作空间身份导出的后端专属表过滤条件、collection/index 名、图 label/property、key prefix 或目录。 |
| Legacy default | 既有的未命名/单工作空间数据命名空间。其 canonical key 是保留的带标签值，而不是用户字符串 default。 |
| Storage profile | 指向存储连接/资源的不可变引用。第一阶段它是唯一允许的按工作空间配置覆盖项，不能选择工作空间身份。 |
| Instance lease | 在完整请求、流或后台任务存续期间持有的实例引用。仍被 lease 的实例不能被 finalization 或 eviction。 |
| Coordination provider | 为 lease、互斥、准入、fencing 和共享状态提供接口的组件。它可以有本地、同主机多进程或外部实现。 |

## 4. 目标

- 防止跨工作空间读取、写入、队列损坏、缓存复用、图变更和后台任务泄漏。
- 保持现有单工作空间行为，包括 WORKSPACE 未设置或为空的部署。
- 使未知工作空间的处理显式且失败关闭。
- 限制内存、连接数、活跃 pipeline 数和模型提供方并发。
- 支持多 Gunicorn worker，不依赖 sticky session。
- 定义可由外部服务实现的协调契约，为未来多节点部署留出空间。
- 使初始 PR 足够小，可被独立审查。
- 为隔离、竞态、恢复、公平性和兼容性提供确定性测试。

## 5. 非目标

- 第一阶段不实现按工作空间的认证或 ACL；隔离不等于授权。
- 不支持运行时修改一个实例的 workspace。
- 第一阶段不支持按工作空间覆盖 LLM、embedding、parser、prompt、chunking 或 scheduler 配置。
- 第一阶段不为每一个后端实现严格物理隔离。
- 第一阶段不声称已完整实现多节点部署。
- 启用该特性时，不重嵌入、复制或重命名既有默认工作空间数据。
- 不顺带修改无关的 provider、HTTP transport、parser 或存储性能行为。

## 6. 使用场景

### 6.1 没有配置 workspace 的既有服务

已有部署未设置 WORKSPACE，客户端也不发送选择器 header。升级后：

- 公开 default 目录记录映射到带标签的 legacy-default 键；
- 继续使用同一份历史物理数据，无需迁移或重嵌入；
- 不带 header 的 REST 和 Ollama 请求继续工作；
- 服务不因遗留的空 workspace 配置而拒绝启动；
- 已提供但为空的选择器仍然无效，因为它不能等价于缺失的选择器。

### 6.2 配置了 workspace 的既有服务

已有部署使用了非空的服务级 WORKSPACE。bootstrap 目录记录将公开 ID default 映射到该精确的规范化命名工作空间。既有客户端继续省略选择器。如果持久化目录元数据随后与配置的遗留值不一致，启动必须失败，而不是静默重映射。

### 6.3 显式创建

经过认证的管理员以 display name 和可选 storage-profile reference 创建工作空间。服务生成不透明、可跨后端使用的 ID 和 canonical key，校验完整存储契约，在生命周期 lease 内执行所需初始化/迁移；只有全部成功才将记录标记为 ACTIVE。

### 6.4 拼写错误的选择器

客户端向上传或查询端点发送语法合法但目录中不存在的 ID。服务返回 404；不能新增目录记录、实例化存储、创建目录/collection 或执行迁移。

### 6.5 两个工作空间中的同一文档

两个工作空间上传相同内容，因此计算出同一个文档 ID。二者各自拥有独立的 doc-status 记录和 pipeline 生命周期。一个工作空间的处理、删除、重试和重启恢复不能观察或覆盖另一个工作空间的记录。

### 6.6 并行摄入

两个工作空间可以并行摄入，只要全局 pipeline 和 provider 准入允许。一个租户的大批量任务不能无限期占满调度机会；等待中的工作空间应通过优先级老化获得公平轮次。

### 6.7 worker 丢失和完整重启

Gunicorn worker 退出或整个服务重启后，受上限约束的恢复协调器枚举 ACTIVE 目录记录，检查各工作空间自己的 doc-status 队列，fence 过期 owner，并重新排入可恢复工作；无需用户逐个访问工作空间。

### 6.8 实例池压力

worker 达到实例/连接预算时，只能驱逐安全的空闲条目。若不存在安全候选，请求收到有界背压（503 和重试指引）；服务绝不 finalize 仍有进行中请求或后台任务的实例。

### 6.9 无法添加自定义 header 的 Ollama 客户端

Ollama-compatible 客户端通过 model alias 选择工作空间，无需自定义 header。若 model 和 header 都提供，它们必须解析到同一目录记录。

## 7. 规范性安全不变量

实现必须始终维持下列不变量：

1. **固定绑定：** 一个存活的 LightRAG 实例恰好拥有一个不可变 canonical workspace key。
2. **单次解析：** 工作空间身份只在存储后端之外解析一次。
3. **四类一致：** 连接到某实例的每个 KV、向量、图和 doc-status 对象都报告相同 canonical key 与预期 namespace 编码版本。
4. **多工作空间模式不允许存储覆盖：** 后端环境变量/配置不能替换实例键。
5. **显式创建：** 只有管理面可创建目录记录或存储命名空间。
6. **上下文失败关闭：** 多工作空间的数据或后台操作若没有具体工作空间上下文，必须拒绝；任何 proxy 都不能回退 default。
7. **先获取 lease 再使用：** 每个请求、流、pipeline、扫描、迁移、恢复、删除和派生任务都要持有 lease，直至所有绑定该工作空间的工作完成。
8. **禁止不安全驱逐/删除：** 已 lease、忙碌、迁移、删除或需要恢复的实例不能被驱逐或 finalize。
9. **共享上限：** 配置的 provider 并发是服务预算，而不是实例预算。
10. **观察无副作用：** liveness/readiness 以及 pool/catalog 检查不能实例化或迁移工作空间。
11. **禁止首次请求迁移：** 数据面请求不能成为迁移 owner。
12. **禁止命名空间复用：** 已删除/tombstoned 的 ID 和物理命名空间身份不能被静默复用。

创建或初始化期间，任何无法证明不变量的情况都属于硬错误。实现不能通过任选一个后端的结果来“修复”不匹配。

## 8. 工作空间身份与校验

### 8.1 将公开名称与存储身份分离

目录至少保存：

~~~text
knowledge_base_id       不透明的公开 ID，例如 kb_7f3a9c2d1e04
display_name            用户可编辑的 Unicode 标签
workspace_key           带标签、不可变的规范化身份
namespace_codec_version 不可变的编码策略
storage_profile_id      不可变的可选引用
lifecycle_state         CREATING | MIGRATING | ACTIVE | DELETING | ERROR | TOMBSTONED
schema_version          存储迁移版本
revision                compare-and-swap revision
created_at / updated_at
~~~

生成的 ID 和 canonical named key 使用保守且可跨后端的字符集，例如小写 ASCII 字母、数字和下划线。长度必须在加入 namespace 后缀后仍满足最严格后端的限制。用户 display name 不受存储命名规则限制，因为它永远不会成为物理标识。

### 8.2 保留身份

以下值是保留值，不能分配为新的命名工作空间键：空字符串、default、_、任何 legacy-default sentinel 拼写，以及 namespace codec 使用的内部前缀。公开目录 ID default 专属于兼容记录。

用户可以把“Default”作为 display label，因为 display label 不是命名空间标识。

### 8.3 缺失、空、无效和未知选择器

这些情形具有不同语义：

| 选择器状态 | 结果 |
| --- | --- |
| Header 缺失 | 选择保留的公开 default 记录。 |
| Header 已提供但为空白 | 400 Bad Request；不能回退。 |
| 语法或长度无效 | 400 Bad Request。 |
| 语法合法但目录不存在 | 404 Not Found；不能创建或实例化。 |
| 记录不是 ACTIVE | 生命周期冲突返回 409；临时迁移/恢复不可用返回 503，并带稳定错误码。 |

框架参数解析必须保留“header 缺失”与“header 存在但为空”之间的区别。

## 9. 四类存储统一 effective-workspace 规则

### 9.1 中央解析器

在构造存储对象之前，服务创建不可变的 WorkspaceBinding：

~~~text
catalog_id
catalog_revision
canonical_workspace_key
namespace_codec_version
storage_profile_id
server_mode
~~~

每个存储构造器接收此 binding 或由其派生的 canonical key。后端可将该键编码为自己的物理命名空间，但不能选择另一个键。

多工作空间实例构造时，任何 KV、向量、图或 doc-status 后端都不能读取 *_WORKSPACE 变量。连接设置仍可来自全局配置或选定 storage profile；逻辑工作空间身份不能来自它们。

### 9.2 优先级与遗留覆盖变量

遗留变量包括：

~~~text
POSTGRES_WORKSPACE
MONGODB_WORKSPACE
REDIS_WORKSPACE
NEO4J_WORKSPACE
MILVUS_WORKSPACE
QDRANT_WORKSPACE
MEMGRAPH_WORKSPACE
OPENSEARCH_WORKSPACE
~~~

其语义依模式而不同：

| 模式 | 解析规则 |
| --- | --- |
| 遗留单工作空间模式 | 为兼容性保留历史优先级，随后执行四类一致性检查。配置一致的既有部署继续工作；配置不一致必须带诊断信息失败，因为静默分裂 doc-status 与数据存储不安全。 |
| 多工作空间模式 | 目录/实例 canonical key 永远优先。任何活跃后端适用的 workspace override 只要非空，启动必须失败，并列出每个冲突变量/配置字段；不能静默忽略。 |

同一禁令适用于等价的 config.ini 字段，以及试图提供逻辑工作空间身份的 storage-profile 字段。storage profile 可以选择资源与连接设置，但不能选择 canonical workspace。

多工作空间模式必须由显式 feature switch 开启。服务不能根据某次请求的 header 推断模式，因为推断会让拼写错误改变配置语义。

### 9.3 创建时一致性检查

每个存储对象实现一个统一、无副作用的 namespace descriptor：

~~~text
storage_family          kv | vector | graph | doc_status
storage_role            full_docs | text_chunks | llm_cache | entities | ...
implementation
canonical_workspace_key
namespace_codec_version
physical_namespace_fingerprint   不含敏感信息的诊断值
~~~

初始化依次通过三道关：

1. **预检：** 校验模式、override 变量、目录记录、storage profile、保留名称和预期 namespace codec，再打开数据面访问。
2. **构造检查：** 检查每个存储对象，而非每个 family 的一个代表。所有 canonical key 和 codec version 必须等于不可变 binding；每个派生的物理命名空间必须匹配该后端为该 storage role 注册的 codec。
3. **连接后检查：** 后端连接初始化后，验证服务端解析出的 database/schema/collection 信息仍与 descriptor 相符，以捕获在构造后再应用连接级 workspace 的 client。

只有全部检查通过，才允许迁移和 pipeline 初始化。在多工作空间模式下，doc-status 不匹配绝不能降级为 warning。

校验错误应说明 catalog ID、预期 canonical key、storage family/role、实现、override 来源和脱敏 physical fingerprint；绝不能打印凭据或连接 URI。

### 9.4 规范化默认库与物理兼容

“一个 effective workspace”指一个 canonical identity，而不是要求每个后端采用同样的字面量表名或 key 语法。

未命名遗留工作空间在内部表示为类似 LegacyDefault 的带标签值，不能等于命名字符串 default。所有存储对象暴露相同 canonical value。版本化 legacy-v1 namespace codec 将其映射到各后端历史物理布局：

- PostgreSQL 可保留历史 default workspace 值；
- Redis 可保留历史无前缀 key；
- 文件存储可继续把文件放在 working directory 下；
- 其他后端保留已部署的遗留表示。

该兼容映射是显式元数据，不是让每个后端独立发明 fallback。它能实现零拷贝升级，同时使核心身份无歧义。新的命名工作空间使用 encoded named-v1 策略，且不能使用保留 legacy alias。因此，新建的字面量 default 工作空间不会和未命名 PostgreSQL 数据碰撞。

目录持久化 codec version。同一工作空间的四类存储必须使用相同 codec policy generation。更改它是未来显式数据迁移操作，绝不能是配置副作用。

## 10. 目录与生命周期

### 10.1 跨进程共享的持久化事实来源

工作空间目录必须对所有 worker 可见且在重启后仍存在。它需要提供原子 create-if-absent 和基于 revision 的 compare-and-swap 更新。JSON 文件加进程内锁不足以支持 Gunicorn，因为各 worker 可能持有过期快照并覆盖其他 worker 的管理变更。

本地目录实现只能用于显式的单 worker 开发模式。workers 大于 1 时，启动必须要求共享目录 provider。

worker 内存可缓存目录记录，但缓存项携带 revision，失配时必须失效或重新拉取。任何请求都不能依赖路由到创建工作空间的同一 worker。

### 10.2 显式生命周期

~~~text
ABSENT
  -> CREATING
  -> MIGRATING
  -> ACTIVE
  -> DELETING
  -> TOMBSTONED

CREATING 或 MIGRATING 或 DELETING
  -> ERROR（发生终止性失败时，保留重试元数据）
~~~

- 只有经过认证的管理 API 可将 ABSENT 转为 CREATING。
- 数据面请求只接受 ACTIVE 记录。
- 使用相同 idempotency key 的重复创建请求返回原操作；payload 冲突时返回 409。
- 初始化失败绝不能发布 ACTIVE。
- 删除产生 tombstone，并保留阻止误复用命名空间的身份信息。
- 目录 revision 和生命周期操作使用 fencing token，过期 worker 不能发布迟到结果。

## 11. 固定实例与受限实例池设计

### 11.1 固定绑定

实例依据一份不可变目录快照和 binding 构造。构造后 binding 只读，存储对象保留相同 canonical key。切换工作空间应通过获取另一实例实现，而不是修改既有实例。

这样可避免进行中的请求、async generator、延迟 embedding buffer 或后台任务在中途观察到 workspace 改变。

### 11.2 每 worker 实例池

每个进程维护本地 pool，因为 LightRAG 实例包含 event-loop 对象、client、buffer 和内存状态，不应通过 process manager 共享。

pool 支持：

- 仅在 ACTIVE 目录查询后惰性构造；
- 同一 worker 内按 key single-flight 构造；
- 最大实例数；
- 除计数外的连接/资源权重预算；
- 显式状态：INITIALIZING、READY、DRAINING、FINALIZING、FAILED；
- 独立的前台和后台 lease 计数；
- idle LRU 记录；
- 受限的失败缓存/退避，避免反复初始化失败引发重试风暴。

不同 worker 可以加载同一工作空间，这是预期行为。共享目录状态、迁移 lease、pipeline 排他和 provider 准入共同防止不安全的重复 owner。

### 11.3 安全驱逐

一个条目只有同时满足以下条件才可驱逐：

- 前台 lease 计数为零；
- 后台/流式 lease 计数为零；
- 没有初始化、迁移、扫描、pipeline、恢复、删除或 finalization 进行中；
- 没有缓冲写入或可重试的延迟工作；
- coordination provider 确认没有要求该实例承担的 worker 本地责任；
- 未被兼容性/启动策略 pin。

驱逐先原子地将 READY 改为 DRAINING，再 finalization，以阻止新 lease。finalization 失败应使条目停留在 quarantined/FAILED，而不能伪装成安全缺席。

若容量已满且没有安全候选，准入返回 503 workspace_capacity_exhausted 和 Retry-After；不能取消有用工作或突破预算。

## 12. 请求与后台上下文

### 12.1 显式上下文对象

选定实例通过不可变 WorkspaceContext 暴露，其中包含目录身份、binding、instance lease 和请求关联数据。router dependency 在认证/授权之后创建它，只有在完整 response 或 stream 结束后才释放。

ContextVar 可作为请求内部便利手段，但不是权威；在多工作空间代码中没有默认 fallback。核心操作和后台任务接受显式 context/binding，缺少它时抛出 typed error。

### 12.2 流与派生任务

- 流式响应一直持有 lease，直至 generator 关闭或取消。
- route 不能把 request-scoped proxy 交给后台任务。
- 创建后台工作必须进行显式 handoff：在释放请求 lease 之前先创建 background lease。
- 取消必须恰好一次释放 admission token 和 lease。
- 日志与 trace 包含公开 workspace ID 和 catalog revision，但不包含凭据。

## 13. Pipeline 与工作空间绑定

每项 pipeline 操作携带具体 workspace binding，包括：

- enqueue 和去重；
- pipeline_status 和所有 namespace lock；
- track_id 创建与查询；
- input directory 选择与扫描；
- PENDING/PROCESSING/FAILED 查询；
- parse 和多模态工作；
- extraction、embedding、graph 更新和 cache 写入；
- clear/delete 等破坏性任务；
- retry 和重启恢复。

多工作空间路径不存在 workspace=None 或进程全局默认 fallback。凡尚未物理分区的持久化 key 都应包含 canonical workspace key，例如 (workspace_key, track_id)。

### 13.1 输入目录扫描

每个工作空间拥有从其 binding/profile 无歧义导出的 input root。一次 scan 请求只选择一个 ACTIVE 工作空间。进程范围目录扫描只能作为管理协调器存在：它枚举目录记录并发起彼此独立、工作空间范围内的 scan；它不能相对全局 doc-status store 分类文件。

### 13.2 重启恢复

恢复由目录驱动，而不是由首次访问驱动：

1. 按确定性分页枚举共享目录中的 ACTIVE 工作空间。
2. 获取带 owner ID 和 fencing token 的按工作空间 recovery/pipeline lease。
3. 检查该工作空间自己的 doc-status 队列。
4. 依照 heartbeat/lease 策略回收陈旧 PROCESSING 记录，并幂等转为可恢复状态。
5. 通过全局公平 pipeline scheduler 提交工作。
6. 仅当 owner fencing token 仍有效时提交状态。

恢复有有界并发，并记录扫描 cursor checkpoint。一个损坏工作空间不能阻止其他目录记录被协调；失败工作空间暴露 error state 和显式 retry control。

## 14. 并行 pipeline 与全局资源治理

不同工作空间可以并行运行 pipeline，但并发必须在服务层获得准入。

### 14.1 共享准入控制器

服务构造一个逻辑上的 ResourceAdmissionController，而不是每个 LightRAG 实例一份预算。每个 provider 调用提交一个包含以下内容的 admission request：

~~~text
workspace_id
resource_kind        llm | embedding | rerank
operation_kind       query | ingestion | recovery | management
cost_hint
priority
cancellation_token
~~~

配置的 LLM、embedding 和 reranker 最大并发是 coordinator 所支持服务部署范围内的总量。激活 N 个工作空间不能将上限从 C 提升为 N×C。

provider wrapper 应注入或调用该共享控制器。允许保留每实例 semaphore 作为更小的本地保护，但它不能凭空增加全局容量。

### 14.2 部署模式

| 部署 | 准入实现 |
| --- | --- |
| 单进程 | 每个实例共用同一个进程内 scheduler。 |
| 同主机 Gunicorn | 同主机 coordination provider 提供进程安全的全局 token 和队列仲裁。 |
| 未来多节点 | 外部 provider 提供带 TTL、heartbeat 和 fencing 的 lease/token。 |

配置校验必须拒绝无法实施文档化全局上限的多 worker 模式；绝不能把该上限静默解释成“每 worker”。

### 14.3 公平性与过载

- 服务全局 cap 限制同时活跃的 ingestion pipeline 数。
- ready queue 按工作空间分区，以 weighted round-robin 或 deficit round-robin 调度。
- priority aging 防止无限饥饿。
- query 和小型交互任务可获得有界保留份额，但不能使 ingestion 永久饥饿。
- 按工作空间 pending queue 上限和服务全局 queue 上限共同提供背压。
- 过载返回稳定的 429/503 和重试指引，不能接受无上限工作。

公平性应通过已文档化 workload 下的有界等待时间与服务份额衡量，而非由 semaphore 数量推测。

## 15. 多进程与未来集群

### 15.1 同主机 Gunicorn

- 实例池与 provider client 保持每 worker 独立。
- catalog、lifecycle revision、migration ownership、pipeline exclusion、recovery lease 和全局 admission 必须共享。
- 预期连接成本约为：worker 数 × 每个 worker 加载的 workspace 数 × 每个 backend client 的成本。pool 预算与指标必须将其可视化。
- 请求可到达任意 worker。cache miss 时重新加载 catalog record，并在本地获取/构造实例。
- 管理变更通过 catalog revision 被观测，而不是依赖 sticky session 或进程本地修改。

### 15.2 协调抽象

核心逻辑依赖 provider contract 提供：

- lease 获取、续约和释放；
- owner identity 与单调递增 fencing token；
- compare-and-swap 状态更新；
- 公平的资源准入；
- 可选 wakeup/notification，以及 polling fallback；
- TTL 与 abandoned-owner 恢复。

初始本地/同主机实现可以使用现有 shared-storage 基础设施，但业务逻辑不能直接依赖 Python Manager dictionary。这样未来可替换为 Redis、数据库或 etcd 类 coordinator，而无需改变路由和 pipeline 语义。

### 15.3 不提前宣称多节点支持

第一阶段不声称多节点安全。它只避免作出必须依赖 sticky session 或无 fencing 进程内存的设计选择。支持矩阵必须准确说明 worker 数、catalog provider 和 coordinator provider 的哪些组合受到支持。

## 16. API 契约与端点实例化策略

### 16.1 REST 选择器

建议数据面 header：

~~~http
LIGHTRAG-KNOWLEDGE-BASE: <knowledge-base-id>
~~~

该 header 选择 catalog ID，而不是原始后端 workspace 字符串。header 缺失时，为兼容性选择保留的 default 记录。成功的数据面响应应在 response header 或稳定 response field 中暴露已解析的公开 ID，便于审计。

管理端点在 path/body 中指定记录，不使用路由 header。

### 16.2 端点策略

每一条 route 必须恰好归属一种策略类别；新增 route 若未分类，OpenAPI/router test 必须失败。

| 端点类别 | Catalog 查询 | 可加载已有实例 | 可创建 catalog record/namespace | 可迁移 | 示例 |
| --- | --- | --- | --- | --- | --- |
| Liveness/version | 无需 workspace 查询 | 否 | 否 | 否 | /health、version/auth-mode liveness |
| Readiness/control-plane observation | 仅读取共享 catalog/coordinator | 否 | 否 | 否 | /ready、pool/catalog lifecycle status |
| Workspace management read | 仅 catalog | 否 | 否 | 否 | 列表/读取 workspace metadata |
| Workspace create | 显式管理目标 | 是，仅经 lifecycle worker | 是 | 是，在 ACTIVE 前 | management POST |
| Workspace update | 仅 catalog 中可变 metadata | 否 | 否 | 否 | 更新 display name |
| Workspace delete | 显式 lifecycle operation | 可获取 maintenance instance | 不创建新 identity | 仅限清理步骤 | management DELETE |
| Data read | 解析已有 ACTIVE record | 是 | 否 | 否 | query、document list/status/count、graph/cache read |
| Data write | 解析已有 ACTIVE record | 是 | 否 | 否 | upload/text/scan、graph/document mutation |
| Workspace runtime observation | catalog/coordinator 或 pool peek | 未加载时否 | 否 | 否 | pipeline/pool status；报告 UNLOADED 而不加载 |

/health 是进程 liveness；无论 selector 输入为何，它都必须保持无副作用。它不能调用实例池的 constructing acquire path，不能初始化 storage、创建 pipeline state 或执行 migration。经过认证的详细诊断只能读取已有 catalog/pool snapshot。

### 16.3 受影响的 route 家族

公共选择器适用于所有 workspace 数据操作，包括：

- 原生 query 和 streaming query route；
- 文档 upload、text insertion、scan、list、status、retry、clear 和 delete；
- graph/entity/relation 读取与修改；
- cache 读取/清空；
- 与 workspace 绑定的 pipeline 操作；
- 在 model-alias 解析后的 Ollama-compatible generate/chat route。

Ollama metadata route，例如 version、tags 和 process listing，仅用于观察，不能实例化全部工作空间。

### 16.4 Ollama-compatible 选择

许多 Ollama 客户端无法添加 custom header，因此 generate/chat 接受 workspace-aware model alias：

~~~text
lightrag:latest          -> 公开 default 记录
lightrag:default         -> 公开 default 记录
lightrag:<workspace-id>  -> 显式 catalog 记录
~~~

alias 通过和 REST header 相同的 catalog 与校验规则解析。未知 alias 返回 Ollama-compatible not-found response，不创建任何内容。若 custom header 与 model alias 同时存在但不同，返回 400 selector_conflict，不能任选一个优先。

alias 的精确拼写仍由 maintainer 决定，但仅通过 model 完成选择是必须具备的能力。

## 17. 迁移时机与所有权

迁移是控制面生命周期操作，不在任意数据访问期间运行。

### 17.1 Bootstrap 分类

| 类别 | 迁移时机 |
| --- | --- |
| Catalog schema/bootstrap | 在进程/服务启动时由共享 owner 幂等执行；不实例化每个 RAG workspace。 |
| 既有 legacy default storage | 在 default-workspace readiness 前的启动阶段执行，保留当前单工作空间预期。 |
| 新创建的 workspace | 显式 create lifecycle 中执行，记录变为 ACTIVE 前完成。 |
| 软件升级后的既有非默认 workspace | 启动 reconciliation 枚举 catalog record，在每 workspace lease 下以有界后台迁移执行。记录处于 MIGRATING，数据面返回 503，直至就绪。 |
| 失败后的重试 | 显式管理 retry 或受控启动策略；不能是普通 query/upload 的副作用。 |

有界的非默认迁移继续时，全局 liveness 可以健康，但 readiness 必须报告部分/降级状态和每个 workspace lifecycle。default workspace 必须先通过其兼容性 readiness gate，才可把遗留流量报告为 ready。

### 17.2 多 worker 安全

migration ownership 使用共享 lease 和 fencing token。只有当前 owner 可以更新 schema version 或标记 ACTIVE。其他 worker 只观察 catalog state，不能重复迁移。已崩溃 owner 的 lease 过期后，新 owner 从持久化 version/state 恢复幂等迁移。

migration code 必须可安全重试，并记录足以区分未开始、进行中、成功和失败的状态。昂贵迁移应提供显式 operator control、进度和 rollback/backup 文档。

## 18. 默认工作空间兼容性与升级路径

### 18.1 Bootstrap 行为

第一次升级启动时，服务幂等创建保留公开 catalog record default：

- 若服务级 WORKSPACE 未设置或为空，映射到 LegacyDefault 和 legacy-v1 codec；
- 若 WORKSPACE 非空，映射到该通过校验的 named key，并使用既有 layout contract；
- 不移动、重命名、重嵌入或复制任何既有存储数据；
- 无 header 客户端继续选择该记录。

若之后持久化 default record 与服务配置不一致，启动必须带 migration/configuration diagnostic 失败，不能静默选择任一方。

### 18.2 后端覆盖变量升级审计

多工作空间模式启用前，面向 operator 的 preflight 报告：

- 每个活跃 storage implementation 和 family；
- 每个 legacy workspace override source；
- 每个计算出的 canonical key 与 physical fingerprint；
- 四类 family 是否一致；
- 保留名称/default collision；
- 必要的配置修改。

多工作空间模式下，任何活跃 override 都是启动错误。遗留模式下，配置一致的 override 仍受支持；但跨 family 不一致必须失败，因为已有状态本就不安全。文档应提供 dry-run command 和修改 override 前的 backup 步骤。

### 18.3 回滚

初始逻辑隔离阶段不重写 default 数据布局。因此，只要不需要继续访问非默认 workspace，回滚就是禁用多工作空间路由并使用未改变的 default namespace。catalog schema 变更应在一个 release window 内保持向后兼容/仅追加。operator 必须在破坏性 lifecycle 操作前备份 catalog metadata。

## 19. 安全边界

工作空间隔离不是授权。selector header/model alias 是不可信的路由输入，不是调用者有权访问该工作空间的证明。

第一阶段保留既有服务级认证边界。除非可信 reverse proxy 应用了策略，任意已认证调用者都可选择任何 ACTIVE catalog record。此限制必须在 release note 和 API 文档中明确说明。

路由应被组织为未来的 WorkspaceAuthorizer(principal, action, catalog_record) 可以在认证、catalog 解析之后、实例获取之前运行。即使在第一阶段，管理 create/delete 仍必须要求管理员授权。

workspace ID 不透明但不是 secret。错误响应不得泄漏存储连接细节。对未知-selector probe 和 management creation 应施加 rate limit，以降低滥用。

## 20. 按工作空间配置

第一阶段唯一的按工作空间覆盖项是不可变 storage_profile_id。

- profile 选择连接/资源，并在创建前校验。
- 它不能覆盖 canonical workspace key。
- workspace 为 ACTIVE 时不能修改。profile change 属于未来显式 migration/copy 操作。
- LLM、embedding、reranker、parser、chunking、prompt 和 admission limit 保持服务全局。

严格物理隔离 profile 延后实现；逻辑工作空间隔离必须不要求每个 workspace 使用一套独立数据库/服务。

## 21. 删除语义

删除采用两阶段且失败关闭：

1. 以 CAS 将 ACTIVE 变为 DELETING，并拒绝新的前台/后台 lease。
2. 按有界且 operator 可见的策略 drain 既有 lease。
3. 获取排他的 deletion lease/fencing token。
4. 只删除已被 catalog binding 和四类存储 descriptor 证明归属该 workspace 的 namespace。
5. 删除 workspace 范围的 input/artifact 文件。
6. 持久化 TOMBSTONED，并保留 identity 和审计 metadata。

部分失败使记录留在 ERROR/DELETING 并可恢复。系统绝不能只删除 doc-status 或只删除数据存储后就标记删除成功。backend override 或 descriptor mismatch 必须阻止破坏性操作。

## 22. 可观测性

要求的信号包括：

- catalog lifecycle 计数与 revision lag；
- 每 worker pool entry、state、lease、idle age 和 resource weight；
- 实例 construction/finalization failure；
- 按 storage family/implementation 统计的 effective-workspace consistency failure；
- migration queue、duration、owner、retry 和 failure state；
- 全局及每 workspace admission wait、active count、rejection 和 queue depth；
- pipeline scheduling delay 与 fairness metric；
- recovery backlog 与 stale-owner reclamation；
- 已配置与实际执行的部署范围 provider limit。

metric 必须控制 workspace label cardinality；详细 ID 应留在 structured log/trace 中，聚合 metric 只使用有界 label。health/ready handler 读取 snapshot，绝不初始化 workspace。

## 23. 失败语义

| 失败 | 必需行为 |
| --- | --- |
| Unknown workspace | 404；不产生 catalog/storage 副作用。 |
| Present 但 empty/invalid selector | 400；不回退 default。 |
| 多工作空间模式下 backend override | 启动失败，并指出冲突的非敏感设置。 |
| 四类 workspace 不匹配 | 在 migration/data access 前初始化失败。 |
| Workspace 正在 migrating/recovering | 503，带稳定 code 和 retry hint。 |
| Pool 满且没有安全 victim | 503；有界背压。 |
| Admission queue 满 | 按操作类型返回 429 或 503；绝不无界队列增长。 |
| Instance construction failure | Single-flight 调用者得到同一 failure；entry 进入有界 backoff。 |
| Worker 持 lease 时死亡 | TTL/heartbeat recovery 与 fencing 阻止过期 commit。 |
| Migration failure | 记录保持 MIGRATING/ERROR；不允许数据面访问或自动 query retry。 |
| Deletion 部分失败 | 记录保持非 ACTIVE 且可恢复；不复用 ID。 |
| Task context 缺失 | Typed internal failure；不回退 default。 |

## 24. 分阶段 MVP 与 PR 顺序

此前在一个大型 PR 中审查的实现分支混合了核心路由、存储隔离、物理 profile、WebUI 和无关修改。建议的可审查顺序如下。

### PR 1：Canonical workspace 契约与遗留安全

范围：

- 带标签 canonical workspace key 与 namespace codec contract；
- 中央解析与保留名称规则；
- 所有 storage family 的标准 descriptor；
- workspace override variable 与四类一致性的启动检查；
- 零拷贝 legacy-default bootstrap 行为与测试。

非目标：catalog management API、dynamic instance pool、WebUI、physical isolation。

回滚：不重写 default data layout；preflight 后可回退 code/config。

### PR 2：共享 catalog 与管理生命周期

范围：

- durable catalog provider contract 和一个受支持的 shared implementation；
- default-record bootstrap、revision/CAS、idempotent create、lifecycle state、tombstone；
- management API 与无副作用 catalog observation；
- single-worker 与 Gunicorn configuration validation。

非目标：data-plane routing 和 physical profile。

### PR 3：固定实例池与请求路由

范围：

- 固定绑定实例、每 worker single-flight pool、lease、capacity/backpressure、安全 idle eviction；
- REST selector rule 与 route-classification test；
- 无 fallback context proxy；
- 无副作用 health/readiness；
- 对既有 ACTIVE workspace 的 query/read routing。

非目标：在 pipeline PR 4 前启用不安全的多工作空间 ingestion。

### PR 4：Pipeline 上下文、迁移、恢复与共享准入

范围：

- 显式 pipeline/background context 与 lease handoff；
- doc-status isolation sentinel test；
- workspace-scoped scan/track/status/destructive path；
- control-plane migration coordinator 和完整重启恢复；
- 服务级 LLM/embedding/rerank admission、active-pipeline cap、公平性和 Gunicorn enforcement。

该 PR 才启用多工作空间 write/ingestion route。在此之前，此类 route 仍 feature-gated 到兼容 default。

### PR 5：Ollama-compatible 选择

范围：model alias、冲突、unknown 行为、tags/ps observation、compatibility test。

### PR 6：WebUI

范围：management UI 和显式 upload/query 选择。“Create independent knowledge base”应排在既有选择之前；selector 同时显示 display name 和 ID。

### PR 7+：按后端实现严格物理隔离

范围：小粒度、面向具体后端的 PR；明确资源所有权、创建/删除、迁移与 integration test。逻辑 workspace identity 仍由中心提供。

物理资源生命周期遵循最小权限原则：endpoint、database、cluster 与 volume 由
operator 拥有、预创建并负责备份。LightRAG 只能初始化、迁移和删除自己拥有的
workspace namespace；namespace 删除绝不代表可以销毁整个 endpoint 或 database
服务。catalog 为完整 profile binding 及每个活动资源 section 持久化不可变且不含
凭据的指纹。允许轮换凭据，但若把已经绑定的 profile ID 改指向另一 host、endpoint、
database 或目录，系统必须在构造 client、执行 migration 或任何破坏性 storage
调用前失败。旧版本中没有该 snapshot 的 physical record，只能在 operator 核对并
备份当前 profile 后，由 fenced startup migration 完成首次绑定。

离线 contract/mock 覆盖不足以把 physical backend 宣称为 production-verified。
每个后端的支持声明都必须有真实服务 create/migrate/delete 测试，证明无关 profile
不受影响，并完成覆盖 catalog 与四类 storage family 同一一致恢复点的 operator
backup/restore 演练。

### 后续：外部 coordinator 与多节点支持

范围：用外部共享基础设施实现已有 lease/fencing/admission contract，再发布经过测试的多节点支持矩阵。

## 25. 验证计划

### 25.1 工作空间身份与存储矩阵

对每个受支持后端和有意义的混合 family 组合：

- 断言每个存储对象报告相同 canonical key 和 codec version；
- 注入每一种 legacy override，验证多工作空间启动失败；
- 验证配置一致的 legacy single-workspace override 保持兼容；
- 验证 mixed override 在数据访问前失败；
- 验证空 legacy、公开 default、保留 _ 和 named workspace identity 无法碰撞；
- 验证 physical namespace descriptor 确定且不含凭据。

unit test 使用 fake/mock；backend integration suite 验证 PostgreSQL、Redis、Neo4j、MongoDB、Milvus、Qdrant、Memgraph、OpenSearch 和 file-based store 的实际 namespace/filter 行为。

### 25.2 Doc-status sentinel 测试

向 workspace A 和 B 插入相同内容，使二者产生同一 document ID。独立推进 PENDING、PROCESSING、PROCESSED、FAILED、retry、track lookup、delete 和 restart recovery。断言每个 status read/write 以及产生的 KV/vector/graph change 始终位于各自工作空间。

### 25.3 确定性并发测试

使用 barrier/event，而非依赖时间 sleep，测试：

- 单 worker 内并发首次访问只构造一次；
- 跨 worker 并发首次访问只执行一次 migration；
- stream/background task 阻止 eviction 和 deletion；
- deletion 阻止新 lease，stale owner 不能 commit；
- 所有 entry 都已 lease 时，pool capacity 返回 backpressure；
- cancellation 恰好一次释放 lease/admission token；
- catalog CAS 防止 lost update。

### 25.4 迁移与恢复测试

- /health 和 readiness inspection 产生零 instance construction 和 migration；
- 任意第一次 query 不调用 migration；
- default startup migration gate 兼容性 readiness；
- 非默认 migration 有界且可见；
- worker kill 后，TTL 过期转移 owner，并拒绝 stale fencing token；
- 完整重启无需用户访问即可枚举全部 ACTIVE workspace；
- 一个失败 workspace 不阻塞其他 workspace；
- 重复 migration/recovery 幂等。

### 25.5 资源与公平性测试

- 加载 N 个 workspace 时，观测到的 LLM/embedding/rerank 并发永不超过配置的部署总量；
- 该断言在单进程和 Gunicorn 模式均成立；
- 活跃 ingestion pipeline 永不超过全局 cap；
- 在 workspace A 持续负载下，workspace B 在已文档化边界内获得服务；
- 交互式 reservation 不永久饿死 ingestion；
- queue saturation 产生有界错误且内存不增长。

### 25.6 API 与兼容性测试

- 缺失、空、无效、未知、inactive 和冲突 selector；
- complete route classification，以及每条 data route 在 OpenAPI 中包含 header；
- management/health route 不接受或不处理 data selector；
- Ollama model-only 选择和 model/header conflict；
- 既有 no-header/no-WORKSPACE 部署使用未改变数据；
- 既有 configured default 使用未改变数据；
- rollback 后 default data 仍可读。

### 25.7 安全测试

- selector 永不被当成 authorization；
- management operation 要求管理员 auth；
- error/log 不暴露 connection secret；
- unknown selector 与 create attempt 被 rate-limit；
- display name 从不成为 namespace，故无法发生 path/collection injection。

## 26. 已考虑的替代方案

### 26.1 在一个实例上切换工作空间

拒绝。可变实例字段无法保护进行中的 coroutine、stream、storage buffer、provider callback 或 background task。证明这种切换安全，最终仍需要把不可变状态传遍每个调用点，却保留更多共享可变风险。

### 26.2 让未知选择器自动创建

拒绝。一次拼写错误会变成持久数据风险和目录、collection、migration、connection、模型流量的滥用向量。

### 26.3 让后端 override 获胜

在多工作空间模式下拒绝。独立后端优先级使四类一致性无法证明，并可静默损坏 doc-status。静默忽略变量同样不安全，因为它隐藏 operator 错误；因此必须显式启动失败。

### 26.4 将每个 legacy physical namespace 重写为一个字面 token

初始升级阶段拒绝。它违反零拷贝兼容性，并要求高风险跨后端迁移。带标签 canonical identity 加版本化 physical codec 可在不移动既有数据的情况下提供统一核心契约。

### 26.5 用 multiprocessing.Manager 共享一个全局实例池

拒绝。live client、event-loop primitive、buffer 和 LightRAG 实例都不是安全的跨进程共享对象。应共享 catalog 与 coordination state，实例仍保持每 worker 独立。

### 26.6 独立的每实例 provider semaphore

拒绝。它会将配置并发乘以 active workspace 数，且无法提供 workspace 公平性。

### 26.7 在首次请求时迁移

拒绝。它引入不可预测延迟，使 observability 获得副作用，在 worker 间产生竞态，并使重启后未被访问的 workspace 无法恢复。

## 27. 待 maintainer 决策的问题

1. LIGHTRAG-KNOWLEDGE-BASE 是否适合作为公开 selector，还是社区应标准化为类似 X-LightRAG-Workspace 的 workspace-named header？语义要求是它选择不透明 catalog ID，而不是后端 namespace。
2. 在保留无 workspace 和配置一致 override 部署的前提下，是否接受 legacy startup 在真实四类不一致时失败？替代方案是一个 release 的 warning-only 模式，但这会继续保留已知的静默损坏风险。
3. 多工作空间 ingestion 启用前，是否必须支持同主机 Gunicorn，还是第一个 routing PR 可以明确仅支持一个 worker，直至 pipeline/admission PR 落地？
4. 提议的 Ollama alias（lightrag:default、lightrag:<id>）是否兼容预期客户端？
5. 第一个受支持的共享 catalog implementation 应选什么？provider contract 和正确性要求不依赖该选择。

## 28. RFC 的验收标准

当 maintainer 对以下内容达成一致时，本 RFC 才可以转化为实现 PR：

- 固定实例绑定和显式创建；
- canonical workspace/legacy codec 模型；
- override-variable 行为与四类 fail-fast validation；
- 共享 catalog 和 lifecycle ownership；
- 每 worker lease pool 与无副作用 endpoint policy；
- migration/recovery 时机；
- 服务级 provider admission 与公平性；
- REST/Ollama 选择语义；
- 第一阶段边界与兼容性契约。

在这些决策被接受前，实现不应再次将全部存储后端、物理隔离、WebUI 和路由合并成一个 integration PR。
