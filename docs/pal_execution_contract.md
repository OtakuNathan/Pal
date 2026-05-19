# Pal Execution Contract

## Current Implementation Overlay

The current runtime uses `Execution` as the Pal-native capability registry, discovery index, and dispatch surface.

Current boundaries:

- `Execution` publishes and invokes Pal-native `CapabilityDescriptor` / bound action projections.
- Direct LLM tool exposure is selected by `src/pal/core/tool_surface.toml`; capability availability remains a live runtime fact.
- MCP does not enter `Execution` as protocol detail. The MCP manager plugin compiles external MCP tools/prompts into Pal-native capabilities and declared skills, then publishes that projection.
- Artifact capabilities accept `artifact_id`; local artifact paths may appear only as safe metadata for capabilities that explicitly accept paths.
- External tools, including MCP-projected tools, must still pass through Pal execution, approval, and risk policy.

> 目标：定义 `capability / tool / plugin / execution` 的宪法。

## 目标

`Execution` 负责让 `Pal` 从“知道什么能力存在”过渡到“安全地执行什么动作”。

它不是简单的 tool registry，而是：

- 能力语义面
- 副作用路由面
- 插件治理面
- introspection 汇聚面

## 五个核心对象

## Capability

`Capability` 是给 LLM 看的语义能力面，同时也是 `Execution` 内部的路由入口。

它回答：

- 能做什么
- 能观察什么
- 这个能力来自哪里

`Capability` 不是最终执行原语。

默认情况下，一条 capability 注册项只路由到一个最终 handler。

复杂编排应上移到：

- `Pal Core`
- `tasking`
- `minions`

## Tool

`Tool` 是最小执行原语。

它回答：

- 怎么实际做
- 产生什么副作用
- 结果如何标准化返回

所有真实副作用都必须通过 `Tool` 落地。

在新架构里：

- `Tool` 可以来自本地函数
- `Tool` 可以来自内建 plugin
- `Tool` 也可以来自 MCP server

对于本地环境能力，推荐保持最小 built-in tool 集：

- `op_tool_search` / `op_tool_read` / `op_tool_call` 作为 capability discovery 和 invocation 元能力
- `op_file_read` / `op_file_edit` / `op_file_write` / `op_file_state` 作为 UTF-8 文本文件读写改的结构化路径
- `op_exec_shell` 作为命令、测试、构建、脚本和无专用 capability 时的 escape hatch
- `web_search` 作为独立 capability / provider family
- `web_fetch` 可通过 `curl` 或 headless browser tool backend 落地

也就是说，`MCP` 只是 tool 的一种来源，而不是独立平级哲学层。

`op_exec_shell` 不再是文件读写查改的默认路径。LLM-facing 描述会要求模型优先使用 dedicated Pal capabilities，并避免在已有专用能力时用 shell 跑 `find`、`grep`、`cat`、`sed`、`awk`、`echo` 或 heredoc 文件编辑。

provider native tool calling 只是外部厂商协议层的调用壳。

它可以帮助 `LLM` 以原生方式表达 tool intent，但不能绕过本地 `Execution`：

- tool schema 仍然由本地 runtime 导出
- tool call 仍然回到本地 routing table
- tool result 仍然回到本地标准化边界

## Plugin

`Plugin` 是 provider。

它负责：

- 提供 `Tool`
- 注册 `Capability`
- 提供能力说明
- 提供参数说明
- 提供限制条件和依赖信息

`Plugin` 不只是“工具包”，而是某个能力族的实现提供者。

## Skill

`Skill` 永远是 manual / playbook / guidance layer。

它负责告诉模型：

- 某类目标通常怎么做
- 推荐步骤是什么
- 常用哪些 capability / tools
- 哪些场景要停下来等用户确认

`Skill` 不是 executable runtime object。
它不直接产生副作用，也不参与 runtime recursion。

`Skill` 的格式在新架构中继续尽量兼容 Anthropic 风格的 manual/package 形态。

## Execution Runtime

`Execution` 维护：

- `capability registry`
- `capability routing table`
- `tool registry`
- fallback-capable provider registries
- `introspection index`
- validator pipeline
- `SkillManualRegistry`
- discovery surface

## 运行模型

```mermaid
flowchart LR
    LLM["LLM Intent"] --> CAP["Capability Operation"]
    CAP --> ROUTE["Capability Routing Table"]
    ROUTE --> VALID["Validators + Pydantic Decode"]
    VALID --> TOOL["Tool Invocation"]
    TOOL --> RES["Tool Result"]
    RES --> ENCODE["Pydantic Encode + Standardization"]
```

## Capability Families

`Capability` 的主分桶方式不是 effect class，而是 family。

推荐 family 至少包括：

- `observe`
- `control`
- `memory`
- `tasking`
- `proactive`
- `introspection`
- `plugin`

其中：

- `observe.*` 主要提供读取、检查、诊断入口
- `control.*` 主要提供治理动作
- 具体业务能力分别归入自己的 domain family

也就是说，`Pal` 主要通过 family 理解“这类能力属于什么范围”，而不是依赖一套过重的 effect taxonomy。

## Capability Descriptor

`CapabilityDescriptor` 必须至少包含：

- `name`
- `family`
- `description`
- `source`

其中：

- `name` 是稳定 capability 名字
- `family` 是主分桶
- `description` 是给 `Pal` 看的人类可理解说明
- `source` 说明能力来源，例如 builtin plugin、本地 provider、MCP provider

## CapabilityCall

`CapabilityCall` 是 execution 的统一输入 envelope。

它应是一个 JSON object。

最小形状为：

```json
{
  "name": "memory.recall",
  "args": {
    "level": "deep",
    "queries": ["redis", "streaming"]
  }
}
```

可选保留一个极薄的扩展口：

```json
{
  "name": "memory.recall",
  "args": {},
  "meta": {}
}
```

其中：

- `name` 表示 capability 名字
- `args` 表示参数字典
- `meta` 是运行时扩展槽，不是业务参数主入口

## CapabilityResult

`CapabilityResult` 是 execution 的统一输出 envelope。

推荐最小形状为：

```json
{
  "status": "ok",
  "text": "已完成。",
  "structured": null
}
```

字段语义：

- `status`：执行状态
- `text`：给 LLM / 用户理解的主返回
- `structured`：可选扩展槽，用于携带结构化结果，例如 `memory_id`、`task_id`、`report_id`

## Skill Contract

`Skill` 可以独立存在于系统中，而不必伴随任何函数注册。

因此必须区分：

- `skill-only package`
- `plugin package`
- `hybrid package`

其中：

- `skill-only package` 只提供 manual、allowlist、tags、metadata
- `plugin package` 提供 capability 和 tool 的实现
- `hybrid package` 同时提供两者，但两层语义必须保持分离

这保证了从外部知识库或 skill hub 学来的方法手册，不会自动变成可执行权限。

### Plugin Refresh Rule

第一版 plugin 加载策略固定为：

- `Pal` 启动时扫描一次已知 plugin 目录
- 运行中不做文件系统 watcher 自动热加载
- 用户显式触发 refresh / rescan 时，才重新扫描并更新 runtime 挂载态

推荐主线是：

- 第三方 plugin bundle 放入指定目录
- 用户通过 slash command 触发：
  - `/plugins refresh`
- `Pal` 执行 rescan，再按 host 规则 attach / detach / reload

这条规则的目的不是偷懒，而是保证：

- runtime 行为可预期
- 插件加载失败边界清晰
- 不让文件系统变化偷偷修改当前运行中的能力面

### 两套可见集合

新架构中需要维护两套 skill 可见集合：

- `Pal skill set`
- `minions skill set`

这里的关键不是两种不同格式，而是：

- 同一份 `SkillManifest / SkillManual`
- 不同 visibility scope
- 不同 activation policy

其中：

- `Pal` 默认拥有更多 always-on 的 observe / control / introspection / memory manuals
- minions 只拿 task-scoped、执行相关的 skills

也就是说：

- `Pal` 需要“治理系统”和“理解自己”的 manuals
- minions 需要“完成任务”的 manuals

## Tool Registration Contract

`Tool` 在本质上仍然可以是普通 Python 函数。

但进入 execution runtime 后，必须统一转换成 handler 形态，便于：

- Pydantic 编解码
- 统一错误处理
- 统一审计
- 统一路由

推荐注册时至少提供：

- `name`
- `family`
- `description`
- `input_model`
- `output_model`
- `handler`
- `plugin_id`
- `requires_approval`（可选）
- `tags`（可选）

普通 Python 函数注册时，可以自动读取：

- 函数名
- docstring
- signature
- annotations

但仍然需要显式补足 runtime 需要的最小元信息。

## Provider Family Pattern

并不是所有 tool/provider 都适合同一种抽象。

### Fallback-capable provider families

适用于：

- `llm`
- `web_search`
- `web_fetch`

推荐统一模式：

- `ProviderRegistry`
- ordered candidates
- priority
- enabled / disabled
- fallback on failure

这类 provider 的共同点是：

- 单次调用可切换
- 失败可以重试下一候选
- 不会破坏长期 truth

### Migration-only provider families

适用于：

- `embedding`
- 其他索引型长期 backend

推荐统一模式：

- active provider version
- reindex / rebuild
- promote / rollback

这类 provider 不应在单次请求中 runtime fallback。

原因是：

- provider 变化会影响长期索引一致性
- 旧索引通常不能无损兼容新 provider
- 切换本质上是 migration，不是 retry

## 参数边界

参数契约采用两层模型。

### 对 LLM 的暴露

对模型暴露的是 `string-first semantic contract`：

- 参数名必须明确
- 参数语义必须明确
- string 内容必须说明 string 具体是什么
- 列表、枚举、对象也必须在语义层讲清楚

例如：

- `task_id`: task identity string
- `memory_id`: stable memory identity string
- `level`: one of `seed | warm | deep`
- `target_buckets`: string list of allowed memory buckets

### 对 Runtime 的落地

进入 `Execution` 后，参数必须转为 `Pydantic` typed models：

- 反序列化
- 校验
- 默认值补齐
- 结果序列化

这条边界是硬约束。

不允许把“全字符串解释逻辑”散落到各个 tool handler。

## Handler Contract

handler 推荐统一成 functor 形态，而不是散落的自由函数协议。

推荐签名：

```python
async def __call__(self, payload: InputModel, *, meta: InvocationMeta | None = None) -> OutputModel
```

其中：

- `payload` 是主输入
- `meta` 是可选运行时扩展槽
- 长期依赖应通过构造函数注入，而不是塞进大 `ctx`

也就是说：

- handler 可以是可调用对象
- `payload` 是核心
- `meta` 只承载 invocation-scoped metadata
- 不应把 `ctx` 做成 service locator

## Protected Invocation Rule

所有外部 handler 调用都必须通过统一保护壳执行。

最小要求：

- runtime 统一 try/catch
- 异常统一转为标准结果
- 统一写 diagnostics / failure reporting
- 不允许第三方 handler 直接把 `Pal` 主体打崩

也就是说：

- `Pal` 可以调用外部函数
- 但外部函数永远运行在 execution 的保护边界之内

## Capability Routing Table

`Execution` 必须维护 capability 路由表。

它至少表达：

`capability name -> source plugin/provider -> input model -> validators -> handler`

这张表的意义是：

- 让 LLM 面向 capability 思考
- 让 runtime 面向 handler / tool 执行
- 让治理和审计发生在中间层

## Plugin Responsibilities

每个 `Plugin` 至少负责：

- 注册 capability family
- 注册 tool implementations
- 提供 capability 描述
- 提供参数语义说明
- 提供 approval 元数据（如需要）
- 将自身 introspection surface 注册进 introspection index

如果 plugin 带 skill/manual，还应负责：

- 注册 skill metadata
- 声明 always-on 还是 on-demand
- 声明 minions 可见性和作用域

## Discovery-First Rule

`Execution` 默认采用 discovery-first 暴露方式。

这意味着：

- prompt 中不默认塞满所有 tools 和 skills
- builtin 与 external skills 都进入统一 manual registry
- 需要更多能力信息时，`Pal` 或 minions 通过 discovery surface 拉取

最小 discovery surface 至少包括：

- `tool_search`
- `tool_read`
- `skill_search`
- `skill_read`

## Introspection Index

`Execution` 必须维护 `introspection index`。

原因是：

- 每个子系统都必须可被观察
- 每个子系统都必须暴露受限修改和调试接口
- `Pal` 的自诊断、自维护、自扩展都需要统一发现面

因此：

- introspection 不是临时调试接口
- introspection 是 execution 的正式注册面之一

此外，给 `Pal` 暴露的 capability surface 中，必须始终包含一部分 observe / control / introspection 能力。

这保证 `Pal` 具备：

- 自检
- 自省
- 自配置
- 自扩展入口

同时，`Pal` 还需要一组最小内置能力面，至少包括：

- `read / write / edit`
- `exec`
- `web_search`
- `web_fetch`
- `memory.recall`
- `memory.commit`
- `control.*`
- `introspection.*`

其中：

- 本地文件与 shell 能力属于最小执行工具箱
- `memory.*` 属于主体连续性的最小能力面
- `control.*` 属于主体治理能力
- `introspection.*` 属于主体自观察与自排障能力

## Built-in Families

以下 family 必须内建：

- `control`
- `introspection`
- `plugin`

其中：

- `approve` 属于 built-in `control` capability
- 子系统自观察与自排障能力属于 built-in `introspection` capability family

## `introspection.self_diagnose`

`introspection` 家族中应内置一个聚合诊断入口：

- `introspection.self_diagnose`

它的职责不是替代各子系统 introspection，而是：

1. 先检查 `Pal` 自身的最小运行状态
2. 再聚合各子系统的 introspection 状态

### `Pal` 自身最小检查面

由于 `Pal Core` 很轻，self 检查也应保持极简，重点包括：

- event loop 活性
- queue 健康度
- 最近错误状态
- registry 是否已加载
- 当前是否处于 paused / degraded / maintenance 模式

### 子系统聚合检查

`introspection.self_diagnose` 应调用并聚合这些家族的状态能力：

- `introspection.channel.*`
- `introspection.memory.*`
- `introspection.execution.*`
- `introspection.control.*`
- `introspection.proactive.*`
- `introspection.tasking.*`
- `introspection.plugin.*`
- `introspection.llm.*`

也就是说：

- 真正的 subsystem health 由各自 introspection capability 提供
- `self_diagnose` 负责统一调用、汇总、评级和后续建议

## Tasking Choice Rule

“是否进入 task 系统”主要不是 execution metadata 决定的，而是 `Pal` 选择 capability 的结果。

也就是说：

- 直接能力由 `Pal` 自己调用
- 长链专业任务通过 `tasking.*` capability 进入 task subsystem
- execution 负责解析、路由、校验、保护调用
- execution 不需要再发明一层过重的 effect taxonomy 替 `Pal` 预先分类整个世界

## Invariants

- `Capability` 是语义面和路由入口，不是最终执行原语。
- `Tool` 是唯一执行原语。
- `Plugin` 提供 `Tool` 并注册 `Capability`。
- `Skill` 永远是 manual，不是 executable runtime object。
- `Execution` 必须维护 capability 路由表。
- 所有真实 effect 必须最终落到 `Tool`。
- 参数进入 runtime 后必须经过 `Pydantic` 编解码边界。
- introspection surfaces 必须注册进 `Execution`。
- 给 `Pal` 暴露的可见能力面必须包含 observe / control / introspection 入口。
- `Pal` 与 minions 可以共享一套 execution 机制，但不共享同一套可见 skill 集合。
- 所有外部 handler 调用必须被 try/catch 包裹，不能让 `Pal` 主体崩溃。
- `MCP` 只是 tool 来源之一，不是独立平级执行哲学层。
- provider native tool calling 不能绕过本地 `Execution`。

## Behavior Layer Integration

Execution does not decide when a capability should come to mind. That is owned by the `behavior` subsystem.

The split is:

- `Execution` owns capability inventory and invocation.
- `Behavior` owns scenario-to-action advice.
- `Skill` remains manual-only.
- `Affordance` points to capability refs, skill refs, and memory query hints.

The LLM-facing difference is:

- use `op_tool_search` to discover available capability inventory.
- use `op_behavior_advise` to ask which route fits a scenario.
- use `op_skill_inject` to fetch a manual after advice returns a `skill_ref`.

See [pal_behavior_contract.md](pal_behavior_contract.md).

## Non-Goals

- 不在本文件定义具体 Python class 实现
- 不在本文件定义审批 UI
- 不在本文件定义某个 plugin 的私有参数
- 不允许把 `Capability` 做成第二套隐式 `Tool`
