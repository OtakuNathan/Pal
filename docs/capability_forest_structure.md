# Capability Forest 结构说明

这份文档专门描述 `PalV2` 里新的 **Unified Capability Forest**。

目标不是重复代码注释，而是回答这几个更重要的问题：

- 为什么要引入 forest
- `Execution` 和 `PalCore` 到底各自管什么
- Blueprint / Hydration / Dispatch 分别发生在什么时候
- 为什么 search 和 execute 要分开
- 为什么实例级能力一定要自动注入 `target_id`

## 先看一句话

`Capability Forest` 是 `PalV2` 的统一能力目录真相源。

它做三件事：

1. 组织能力
2. 编译能力
3. 路由能力

但它**不直接负责治理**。治理还是 `PalCore` 的事。

所以职责是：

- `PalCore` 决定谁挂上来、谁摘下去
- `Execution` 持有 forest，并把它编译成可搜索目录和可执行哈希表

## 宪法补充：控制命令不是给 LLM 看的能力

Capability Forest 里虽然有 `introspection` 和 `operation` 两类能力面，但
control-plane slash command 不属于这两类对 LLM 可见的调用面。

这条规则要写死：

- slash command 是 runtime-private governance ingress
- 它可以改变系统治理状态
  例如暂停工具、切 finalization-only、调整挂载状态
- 但它的原始命令文本不进入 LLM 可见 surface
- 也不写入 L1 作为会话记忆

换句话说：

- LLM 最多只能感知控制结果
  例如“当前 tool use disabled”
- 不能感知原始 `/pause-tools`、`/detach ...` 之类命令文本

## 为什么不是直接手写 CapabilityDescriptor

旧思路是：

- 每个模块自己 `describe()`
- 返回一堆平坦 `CapabilityDescriptor`

这个方式的问题是：

- 模块自己手搓 descriptor，容易越来越散
- 实例级对象很难表达
  例如 `channel endpoint`、`llm endpoint`、`l3 provider`
- 很难同时兼顾：
  - LLM 看得懂的名称
  - 稳定的执行主键
  - 模块动态挂载/摘除

所以现在改成：

- 静态真相源是 **Blueprint**
- 运行时真相源是 **Hydrated Subtree**
- `CapabilityDescriptor` 只是编译产物

## 物理结构：一个 forest，不是两棵实现完全不同的树

逻辑上有两条 namespace：

- `introspection`
- `operation`

但物理上只有一个 `CapabilityForestRegistry`。

这么做的原因很简单：

- 搜索一个目标对象的全部能力时，不用合并两张完全不同的树
- 将来新增第三种 namespace，不用推翻底层结构

所以更准确的理解是：

- 一个 unified forest
- 多个 namespace root

## 核心对象

### `CapabilityNodeBlueprint`

文件：

- [capability_forest.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/shared/capability_forest.py)

它是**静态模板**。

它描述：

- 这个节点属于哪个 namespace
- 它是 `module` / `endpoint` / `provider`
- 它属于哪个 source
- 它如何从运行时实例里拿到 `target_id` / `target_label`

它不是运行时节点。

最关键的一点：

- Python 装饰器发生在导入期
- 运行时实例发生在挂载期

所以装饰器只能生产 Blueprint，不能直接生产实例节点。

### `CapabilityActionBlueprint`

同文件。

它描述一个 action 的模板，例如：

- `observe`
- `configure`
- `attach`
- `detach`

它提供：

- namespace
- scope
- action name
- family
- 参数/结果 schema 模板
- alias 和 metadata

它同样不是最终可执行对象。

### `HydratedCapabilityNode`

同文件。

这是运行时真正挂进 forest 的节点。

它已经带有：

- `target_id`
- `target_label`
- `module_id`
- `node_id`

例如：

- 模块级 `channel`
- 实例级 `telegram_main`
- provider 级 `mock_l3`

### `MountedSubtreeHandle`

同文件。

这是一次 subtree mount 的“回收句柄”。

它记录：

- 本次 mount 创建了哪些 node
- 注册了哪些 bound action key
- 注册了哪些 search record id

这很重要，因为 detach 时绝不能靠扫整张哈希表来删。

detach 必须拿着这个 handle 精准 teardown。

### `CompiledCapabilityIndex`

同文件。

它是**给搜索和 LLM-facing usage surface 用的平坦索引**。

里面存的是：

- `display_name`
- `aliases`
- `canonical_path`
- `target_label`
- `target_id`

注意：

- alias 允许冲突
- alias 只负责搜索
- alias 不负责执行

### `BoundActionIndex`

同文件。

这是**执行热路径**使用的 O(1) 哈希表。

key 固定为：

- `(canonical_path, target_id)`

这里就是最初你想要的那种 O(1) tool/capability dispatch。

Forest 的存在不是为了替代哈希表，而是为了**生成哈希表**。

## 命名：为什么要区分 canonical path 和 display name

我们现在明确把这两件事分开：

### canonical path

这是执行主键的一部分。

它必须：

- 稳定
- 可预测
- 不因为实例重命名而变化

规则：

- introspection：`introspection.<scope>.<module>.<action>`
- operation：`operation.<module>.<family>.<action>`

例如：

- `introspection.module.channel.list`
- `introspection.endpoint.channel.inspect`
- `operation.channel.management.attach`
- `operation.execution.exec.run`

这里有一条必须写死的命名宪法：

- capability 的 canonical path 一律带 namespace 前缀
- 主线只有：
  - `introspection.*`
  - `operation.*`
- `module` 是 canonical path 的稳定组成部分，不省略
- `family` 负责区分同一模块内的能力族
- `action` 负责最终动作名

也就是说，统一形状应为：

- `introspection.<scope>.<module>.<action>`
- `operation.<module>.<family>.<action>`

这样做的原因是：

- 不同模块即使有同名 action，也不会冲突
- `Execution` 可以稳定地按 canonical path 编译 O(1) dispatch key
- `LLM` 看到的名字也能天然表达：
  - 这是哪条 namespace
  - 属于哪个模块
  - 属于哪个 family
  - 最终做什么动作

这条规则对所有内建模块、第一方 plugin、以及未来第三方 plugin 都成立。

### display name

这是给人和 LLM 看的。

它可以带实例名，例如：

- `introspection.endpoint.telegram_main.observe`

所以设计上是：

- canonical path 负责稳定执行
- display / alias 负责可读与召回

## 为什么实例级 action 必须自动注入 `target_id`

实例级能力最典型的问题是：

- LLM 知道“我想观察 telegram_main”
- 但执行层需要的是：
  - `canonical_path="introspection.endpoint.channel.observe"`
  - `target_id="telegram_main"`

如果 schema 不强制，LLM 很容易漏传 `target_id`。

所以编译器在生成 LLM-facing schema 时，必须自动做这件事：

- 给实例级 action 自动加入 `target_id`
- 放进 `required`
- 如果当前实例是可枚举的，就把它编成 enum

这样作者不用手写，执行也不会歧义。

对应实现主要在：

- [capability_compiler.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/execution/capability_compiler.py)

## 为什么模块级 action 需要 `SINGLETON_TARGET`

如果模块级 action 没有 target，那么 dispatch 很容易写成：

- 有 target 的一套逻辑
- 无 target 的一套逻辑

这样会长出一堆 `if target_id is None` 的分支。

所以我们统一引入：

- `SINGLETON_TARGET = "__singleton__"`

模块级 action 的真实 dispatch key 也统一是：

- `(canonical_path, "__singleton__")`

这样：

- 模块级
- 实例级

两类能力都走同一条 O(1) 路由逻辑。

## Blueprint 到 Hydration 的完整流程

这条链是这次改动的核心。

### 第一步：模块导入期

模块类或方法上用：

- `@capability_node(...)`
- `@capability_action(...)`

这一步只会产生 Blueprint 元数据。

### 第二步：`register_with_core(instance)`

当模块实例真正被挂到 `PalCore` 时：

- `MainContext.register_module(handle)`
- `ExecutionRuntime.hydrate_module_handle(handle)`

这时候编译器会读取：

- provider 上的 blueprint
- provider 当前实例
- repository 或运行时暴露出来的实例集合

然后生成真实 subtree。

### 第三步：publish / mount

`PalCore.publish_module_capabilities(module_id)` 会让 `ExecutionRuntime`：

- 把 subtree 挂进 forest
- 把 descriptor 注册进 compiled search index
- 把 bound action 注册进 O(1) dispatch index

所以 hydration 和 mount 是分开的：

- hydration：生成 subtree
- mount：让 subtree 真正变成可见且可调用

## 为什么 search 和 execute 必须严格解耦

这是整个系统里非常重要的一条线。

### search

search 是模糊的。

它允许：

- alias 命中
- 关键词召回
- 同名多候选

### execute

execute 必须是严格的。

它只能接受：

- `canonical_path`
- `target_id`

如果缺少 `target_id`，而该能力是实例级：

- 执行层应该返回结构化错误
- 绝不能猜测

所以：

- alias 只进入 search
- alias 永远不直接进 execute

## 模块节点 vs plugin 节点

这里也要明确：

- 普通模块节点不是“新业务能力注入点”
- plugin/provider 才是

模块节点主要负责：

- introspection
- configure
- attach
- detach

plugin 节点负责：

- 注入新的 operation 能力
- 注入新的 provider / tool / backend surface

这让系统边界更干净：

- 模块是对象
- plugin 是能力扩展点

## Capability Forest 宪法

这条规则必须长期成立：

- **父节点只管理自己的直接下一层**
- **任何节点都不允许越级管理更深层子树**

换句话说：

- 模块节点只管理它的直接子节点
- endpoint 节点只管理它的直接子节点
- provider/plugin 节点只管理它自己的直接子节点

不允许出现这种越级行为：

- `channel` 模块节点直接配置某个更深层的 telegram 私有对象
- 模块级 `configure` 直接穿透到底层 adapter 私有配置
- 上层节点绕过中间节点直接操作孙节点或更深层节点

这条规则存在的原因是：

- 防止上层节点膨胀成“大总管”
- 防止能力边界漂移
- 让子树可以独立演化
- 让 attach/detach/refresh 的治理范围天然清晰

因此：

- 通用治理能力放在父节点
- 更具体的配置和操作必须下沉到对应子树自己实现

例如：

- `channel` 只负责 `channel endpoint` 这一层的通用治理
- 更具体的 `telegram` 配置必须在 `telegram endpoint` 或它自己的子树里完成

## 关键文件阅读顺序

如果你要自己顺着代码看，我建议按这个顺序：

1. [capability_forest.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/shared/capability_forest.py)
   先理解 Blueprint、HydratedNode、MountedSubtreeHandle、索引结构

2. [capability_compiler.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/execution/capability_compiler.py)
   看 Blueprint 如何在 runtime 被水合和编译

3. [execution/runtime.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/execution/runtime.py)
   看 forest 怎么变成搜索索引和 dispatch 哈希表

4. [core/main_context.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/core/main_context.py)
   看 hydration 在什么时候触发

5. [core/runtime.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/core/runtime.py)
   看 `PalCore` 怎么 publish / withdraw / detach / reattach

6. 具体 provider 样例：
   - [channel/introspection.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/channel/introspection.py)
   - [llm/introspection.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/llm/introspection.py)
   - [plugins/l3/stubs.py](/Users/nathan/Desktop/coding/Pal/PalV2/src/pal/plugins/l3/stubs.py)

## 当前第一版已经做到什么

已经落地的点：

- unified forest
- blueprint -> hydration
- compiled search index
- O(1) dispatch
- `SINGLETON_TARGET`
- 实例级 schema 自动注入 `target_id`
- precise teardown
- 模块 lifecycle operation 进 forest
- `channel endpoint` / `llm endpoint` / `l3 provider` 的实例级节点

还没做满的点：

- 全量业务 tool family 树化
- 更丰富的 LLM-facing usage 文本生成
- 更复杂的 subtree refresh 策略
- worker 侧复用同一套 forest

## 一句话总结

`Capability Forest` 可以理解成：

- **Blueprint 是设计图**
- **Hydration 是按当前运行时把设计图变成真实节点**
- **Execution 把真实节点编译成“可搜索目录 + 可执行哈希表”**
- **PalCore 只负责决定这些节点什么时候挂上、什么时候摘下**
