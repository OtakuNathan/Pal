# Pal Migration Map

> 目标：对当前实现做迁移归类，明确哪些重写、哪些包装迁移、哪些删除。

## 归类标准

- `重写`：当前职责和新骨架冲突，继续包裹只会加重耦合
- `包装迁移`：核心逻辑仍有价值，但边界需要重挂
- `保留`：基本已经符合新骨架，可直接迁入
- `删除`：语义已废弃或与新架构正面冲突

## 重写

## `src/pal/runtime/pal/app.py`

- 当前职责：总装配、主循环、能力调用、memory 编排、scheduler 编排、worker 编排、diagnostics 编排
- 当前问题：主体编排与领域协调混在一个超级入口里，边界不清
- 目标归属：`core` + `bootstrap` + 若干 domain orchestrators
- 迁移方式：拆成 `PalCore`、turn runner、runtime wiring
- 是否保留语义：保留主循环语义，不保留当前文件形状

## `src/pal/runtime/pal/capabilities.py`

- 当前职责：单点注册 memory、task、work order、proactive、introspection 等能力
- 当前问题：所有领域 capability 杂糅在一个模块，无法体现插件化和子系统分权
- 目标归属：各子系统 provider 自注册到 `Execution`
- 迁移方式：拆成 per-domain capability providers
- 是否保留语义：保留 capability family 方向，不保留集中注册模式

## `src/pal/storage/sqlite.py`

- 当前职责：全局数据库入口、迁移、memory、proactive、tasking、diagnostics、channel route 全部读写
- 当前问题：单文件承担所有领域持久化，已经成为事实上的系统核心
- 目标归属：`foundation/persistence` + per-domain repositories
- 迁移方式：拆成 database infra 和领域 repository
- 是否保留语义：保留 SQLite 作为存储后端，不保留全局 store 形状

## `src/pal/runtime/pal/review_flow.py`

- 当前职责：work order review 编排
- 当前问题：属于 tasking 领域，却挂在 pal runtime 私有目录
- 目标归属：`tasking`
- 迁移方式：按旧语义直接迁移，重挂到 tasking plugin family
- 是否保留语义：保留 review 语义，不重定义 review contract

## `src/pal/runtime/pal/worker_manager.py`

- 当前职责：worker spawn、worker listener、closeout、failure handling
- 当前问题：属于 tasking/execution world，不应挂在 pal runtime 私有入口下
- 目标归属：`tasking`
- 迁移方式：按旧语义直接迁移，拆为 worker orchestration service
- 是否保留语义：保留 worker continuity、checkpoint、approval handoff 语义

## 包装迁移

## `src/pal/memory/*`

- 当前职责：memory models、manager、compact、render、repository
- 当前问题：已经有一定抽象，但仍混着旧 bucket 模型和 conversation-scoped durable host 假设
- 目标归属：`memory`
- 迁移方式：保留可复用 leaf logic，重写 `L1/L2/L3` contract 和 `MemoryService`；`L3` truth model 从统一 `pal_memories` 拆成 `memory_facts` / `memory_cases`，topic 索引收口为 `memory_topics`
- 是否保留语义：保留 compact/recall/commit/correct/retire 主概念，不保留旧 page_fault 与 durable L2 语义；不保留 `tags + memory_tags + topic_tags_blob` 三轨并存语义

## `src/pal/execution/*`

- 当前职责：tool registry、tool executor、policy、skills、runtime factory
- 当前问题：底层执行面比较干净，但 capability/plugin/introspection 还不够一等
- 目标归属：`execution`
- 迁移方式：保留底层工具执行设施，在其上补 capability routing、plugin runtime、introspection index
- 是否保留语义：保留 `Tool` 作为唯一执行原语

## `src/pal/channel/*`

- 当前职责：channel adapter、normalize、route、mailbox、slash command
- 当前问题：主方向正确，但与 route durability、conversation identity 还有旧绑定
- 目标归属：`channel`
- 迁移方式：保留 I/O 和 normalize 核心，改为依赖 `EndpointConfig` 和 `ResponseHandle`
- 是否保留语义：保留 receive / send / segmented send / reply route 主路径

## `proactive` 相关旧 schema / store 路径

- 当前职责：`proactive_definitions`、`proactive_runs`、schedule 计算、proactive run 记账、output channel / reply target 持久化
- 当前问题：旧命名已经退休，durable hot-state 表也不再需要
- 目标归属：`proactive`
- 迁移方式：只保留 `proactive_definitions/proactive_runs` active schema；hot/top-of-mind state 留在 runtime memory
- 是否保留语义：保留 schedule、due trigger、run lifecycle 和 output routing；不保留 `service` 作为子系统名

## 包装迁移

## `src/pal/llm/*`

- 当前职责：provider-neutral IR、wire-shape codec、endpoint fallback
- 当前问题：旧版 provider adapter 与 canonical DTO 已由 immutable IR 和统一 JSON-frame decoder 取代
- 目标归属：`llm`
- 迁移方式：以 OpenAI/Anthropic SDK 承载三个固定 wire shape；本地 `llm_endpoints` 是模型路由与能力真相源；streaming 与 single-shot 均先归一化为 JSON frame
- 是否保留语义：保留 fallback 和本地 tool execution 边界；不保留 provider 行为分支、旧 DTO 或假 streaming

## 保留

## `src/pal/ipc/*`

- 当前职责：peer transport、mailbox、codec
- 当前问题：属于基础设施，不应被高层架构反复改写
- 目标归属：`foundation/io`
- 迁移方式：直接迁入
- 是否保留语义：保留

## `src/pal/io/*`

- 当前职责：process runner、pipe helpers
- 当前问题：基础设施属性强
- 目标归属：`foundation/io`
- 迁移方式：直接迁入
- 是否保留语义：保留

## `src/pal/security/*`

- 当前职责：credentials、keychain
- 当前问题：与新骨架无直接冲突
- 目标归属：基础设施或独立 security family
- 迁移方式：直接迁入
- 是否保留语义：保留

## `src/pal/diagnostics/*`

- 当前职责：失败上报和用户反馈渲染
- 当前问题：目前还是 runtime helper，不是正式 failure constitution
- 目标归属：diagnostics / reporting domain
- 迁移方式：保留当前数据对象方向，升级成正式 failure reporting contract
- 是否保留语义：保留并扩展

## 删除

## `src/pal/retrieval/page_fault.py`

- 当前职责：旧的 page fault candidate selection
- 当前问题：建立在“模型静默决定回忆什么”的旧哲学上
- 目标归属：无
- 迁移方式：删除
- 是否保留语义：不保留

## 当前实现的总体判断

当前最急需拆解的三个耦合点是：

- `app.py`
- `capabilities.py`
- `sqlite.py`

当前最值得保留并重挂的叶子能力是：

- `channel` I/O 处理
- `execution` 底层 tool 执行设施
- `llm` canonical/provider 层
- `ipc` / `io` / `security`

tasking 这条线的判断单独强调如下：

- task subsystem 在旧版本里语义已经比较明确
- 新架构不重发明 task / worker / work_order / approval / checkpoint / ledger 的业务含义
- 这一块的主要动作是 ownership refactor，而不是 domain semantic rewrite

legacy 文档中已证明稳定、并应视为直接迁入新架构的语义还包括：

- `supervisor -> pal -> worker` process model
- `supervisor-pal` 控制面与 `pal-worker` 执行面的 IPC 分层
- `Pal`-owned user-facing channel
- provider-neutral LLM IR + three wire-shape codecs + exact-model hooks
- `skill = manual`、`tool = 唯一执行原语`
- `L1` 作为近无损压缩 transcript，而不是 summary bucket

## Invariants

- 不再保留全局超级 store。
- 不再保留单点 capability 注册器。
- 不再保留 `page_fault`。
- `Tool` 仍是唯一执行原语。
- `L3` 仍是 memory owned durable layer。

## Non-Goals

- 不在本文件定义具体改代码顺序
- 不在本文件定义每个测试如何迁移
- 不在本文件承诺旧 schema 或旧 CLI 兼容
