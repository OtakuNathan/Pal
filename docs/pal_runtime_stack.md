# Pal Runtime Stack

> 目标：定义目标模块骨架、拥有边界和对外接口。

## 总览

```mermaid
flowchart TD
    IO["foundation/io"]
    PERSIST["foundation/persistence"]
    CH["channel"]
    LLM["llm"]
    MEM["memory"]
    EX["execution"]
    CTRL["control"]
    CORE["core"]
    TASK["tasking"]
    PRO["proactive"]

    IO --> CH
    IO --> CORE
    PERSIST --> CH
    PERSIST --> MEM
    PERSIST --> EX
    PERSIST --> TASK
    PERSIST --> PRO

    CH --> CORE
    CTRL --> CORE
    MEM --> CORE
    EX --> CORE
    LLM --> CORE

    EX --> TASK
    EX --> PRO
    MEM --> TASK
```

## foundation/io

### Owns

- async transports
- subprocess / stdio / pipe
- timers
- streaming primitives
- socket and network watchers
- raw message envelopes
- IPC framing and codecs

### Does Not Own

- business routing
- capability semantics
- memory logic
- plugin lifecycle

### Exposes

- `EventEnvelope`
- transport adapters
- async mailbox primitives
- stream / process runners
- IPC envelope / peer primitives

## foundation/persistence

### Owns

- database connection lifecycle
- transaction boundary
- migration runner
- base repository utilities

### Does Not Own

- memory semantics
- channel routing policy
- execution governance
- tasking rules

### Exposes

- `Database`
- transaction scope
- migration runner
- repository base abstractions

## channel

### Owns

- inbound receive
- outbound send
- segmented send
- ingress acknowledgement UX
- typing / status feedback
- endpoint lookup from DB
- endpoint identity
- normalize
- reply routing
- envelope creation
- `Pal`-owned user-facing channel runtime

### Does Not Own

- agent reasoning
- memory persistence
- capability execution
- approval policy

### Exposes

- `EndpointConfig`
- `ResponseHandle`
- `ChannelAdapter`
- `ChannelNormalizer`
- `ChannelEnvelope`

### Identity Rule

channel identity 固定为：

- `channel_kind + endpoint_id`

它不等于 conversation identity，也不等于 memory identity。

### Legacy Alignment

旧版 `DeliveryRoute` 语义在新架构中拆成：

- `EndpointConfig`
- `ResponseHandle`

保留“哪里来的，哪里回去”的回复路由原则，但不再让 route 成为 memory ownership 的核心抽象。

### IM UX Rule

IM channel 默认采用三阶段反馈：

1. ingress accepted
2. typing / processing visible
3. final reply delivered

对于 Telegram，推荐默认实现为：

- 消息入队后立即给用户消息打 `eyes` 标记
- 随后切换 typing
- 最终再发送正式回复

## llm

### Owns

- canonical request / outcome / stream event
- provider transport normalization
- prompt assembly inputs
- request and response canonicalization
- structured parse boundary
- model selection signals
- endpoint candidate resolution
- fallback candidate building
- streaming assembly and forwarding
- native tool-calling wire adaptation

### Does Not Own

- durable state
- side effects
- tasking state
- scheduler state

### Exposes

- canonical request and response models
- `LLMStreamEvent`
- `LLMEndpointConfig`
- provider registry
- structured parse helpers
- canonical tool call / result models

### Canonical Shape Rule

`llm` 内部必须维持 vendor-agnostic canonical shape。

至少包括：

- canonical request
- canonical outcome
- canonical tool definition
- canonical tool call
- canonical tool result

外部 provider 只负责把这些 canonical objects 变形成各自 wire shape。

### Transport Strategy

`llm` 默认保留旧版 canonical/provider 分层，但 transport 层优先通过 `LiteLLM` 统一。

也就是说：

- `Pal` 自己定义 canonical protocol
- `LiteLLM` 负责尽量统一 provider wire shape
- provider native tool calling 仍然不能绕过本地 `Execution`

### Registry Rule

模型能力与路由信息应收敛在本地数据库中，而不是完全依赖 provider 在线发现。

推荐继续使用并扩展 `llm_endpoints` 作为：

- 本地模型能力表
- endpoint candidate registry
- fallback priority source

## memory

### Owns

- `L1`
- `L2`
- `L3` provider contract
- compact
- recall
- retire
- memory pack projection

### Does Not Own

- channel state
- execution policy
- minions lifecycle
- proactive lifecycle

### Exposes

- `MemoryService`
- `L3Provider`
- `MemoryPack`
- memory lifecycle contracts

## execution

### Owns

- capability registry
- capability routing table
- tool registry
- fallback-capable provider registries
- introspection index
- validator pipeline
- side-effect dispatch
- plugin runtime
- skill runtime

### Does Not Own

- conversation state
- durable memory truth
- control decisions
- minions planning policy

### Exposes

- `CapabilityDescriptor`
- `CapabilityCall`
- `CapabilityResult`
- `ToolBinding`
- `PluginManifest`
- `ExecutionRuntime`

### Built-in Tooling Rule

`Pal` 可以拥有少量内建工具，但应保持极简。

推荐原则：

- 常见 UTF-8 文件读写改优先通过结构化文件能力，例如 `op_file_read`、`op_file_edit`、`op_file_write`、`op_file_state`
- 命令、测试、构建、脚本执行通过 `op_exec_shell`
- `web_search` 保留为独立能力与 provider family
- `web_fetch` 可以通过 `curl` 或 headless browser backend 落地

也就是说：

- 不必为每个本地动作都发明单独 built-in tool
- `op_exec_shell` 是通用 escape hatch，但不是文件读写查改的默认路径
- 但需要结构化 provider 选择或外部信息能力的场景，应保留独立 capability family

## control

### Owns

- explicit control parsing
- approval workflow
- pause and resume semantics
- deterministic control actions
- system control event normalization

### Does Not Own

- general chat reasoning
- tool execution
- durable state storage
- plugin implementation

### Exposes

- `ControlPlane`
- `ControlAction`
- `ControlEvent`

## core

### Owns

- main event loop
- minimal control state
- coordination across planes
- turn orchestration
- follow-up event emission

### Does Not Own

- business data truth
- tool registry details
- durable memory
- work order details
- proactive run details

### Exposes

- `PalCore`
- event dispatcher
- turn runner

## tasking

### Owns

- plan review
- task context pack
- work order lifecycle
- minions orchestration
- checkpoint continuity
- minions lifecycle observation
- minions termination and replacement

### Does Not Own

- core governance
- channel transport
- LLM provider lifecycle
- global execution policy

### Exposes

- `TaskingPlugin`
- work draft and plan contracts
- minions checkpoint contracts
- minions observation and termination capabilities

## proactive

### Owns

- scheduled triggers
- recurring proactive task state
- proactive run lifecycle
- due event materialization
- output channel binding
- proactive action contract

### Does Not Own

- core loop
- memory ranking
- plugin routing
- minions execution policy

### Exposes

- `ProactiveDefinition`
- proactive trigger contracts
- proactive run contracts

### Proactive Shape Rule

proactive task 的内部定义至少应表达：

- 你要做什么
- 你如何做
- 是否要带某个 skill/manual 来做

并且：

- `out_channel_id` 可选；设置后必须能被 channel runtime 解析
- `out_reply_target` 保存 endpoint-specific reply metadata
- output channel 无法解析时，本次 proactive run 不发 channel 输出，但 run history 仍然保留

## 关键接口清单

这些接口名称在后续实现中视为保留名：

- `EventEnvelope`
- `ResponseHandle`
- `EndpointConfig`
- `PalCore`
- `ControlPlane`
- `CapabilityDescriptor`
- `CapabilityCall`
- `CapabilityResult`
- `ToolBinding`
- `PluginManifest`
- `MemoryService`
- `L3Provider`

## Invariants

- `Channel` 只做 I/O、normalize、reply route。
- `Pal` 自己持有用户前台 channel。
- `LLM` 不直接产生副作用。
- `LLM` 内部保持 canonical shape，对外再适配 provider shape。
- `Memory` 拥有 `L1/L2/L3` 语义，但不越过 `Execution` 做 effect。
- `Execution` 拥有副作用治理权。
- `Control` 拥有显式控制语义。
- `Core` 只做协调，不拥有领域真相源。

## Non-Goals

- 不定义字段级数据库 schema
- 不定义具体 Python 包内文件名
- 不规定第一版 plugin marketplace 机制
- 不把 `Task`、`Proactive` 提升为 Core owned subsystem
