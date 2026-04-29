# Pal Channel Contract

> 目标：定义 `channel` 子系统的职责、交互契约，以及 IM 场景下的 ingress acknowledgement UX。

## 目标

`channel` 子系统负责：

- 接收外部输入
- 归一化输入
- 为 `Pal Core` 创建标准 envelope
- 根据 channel 规则发送最终回复
- 维护用户可理解的接收中与处理中反馈

它不负责：

- agent reasoning
- memory 逻辑
- capability 执行
- control 决策

## Owns

- inbound receive
- inbox / poll loop
- normalize
- endpoint lookup
- response routing
- segmented send
- typing / status feedback
- ingress acknowledgement UX
- optional ingress / egress delivery log

## Does Not Own

- prompt 组装
- llm provider 调用
- memory lifecycle
- capability routing
- tool execution
- conversation-owned durable route state

## Attachment Ingress

Channel endpoints may normalize incoming platform attachments into `payload.attachments`.

Rules:

- The endpoint may download/cache platform bytes when required by the platform.
- The endpoint must not decide prompt exposure or LLM serialization.
- The endpoint must not expose platform callback/file details beyond normalized metadata.
- `PalCore` hands normalized attachments to `pal.artifact`.
- After registration, inner layers use `artifact_id` and artifact tools, not channel-specific file handles.

For the full managed artifact lifecycle, see [artifact_manager.md](artifact_manager.md).

## 简化结论

`channel` 在新架构中的统一形状应尽量简单。

它只需要做到：

1. 从外部世界收消息
2. normalize 成统一 `ChannelEnvelope`
3. 投递到 mailbox
4. 在 envelope 自带的 respond/status 接口上完成回复

换句话说：

- `Pal Core` 不应理解 Telegram、stdio 等平台细节
- `channel` 也不应反过来承担业务推理
- 如需写 ingress / egress log，应由具体 channel 实例自己完成
- 不再需要把 channel route durability 做成 memory 或 core 的一等真相源

## 核心对象

### `EndpointConfig`

描述一个可接收、可回复的 channel endpoint。

至少包含：

- `endpoint_id`
- `channel_kind`
- `binding_metadata`
- `enabled`
- `send_policy`
- `reply_policy`

其中 `send_policy` 至少应表达：

- `max_message_chars`
- `preferred_parse_mode`
- `segment_by_default`
- `preserve_code_blocks`
- `supports_typing`
- `supports_receipt_marker`
- `supports_message_edit`

也就是说，channel 数据库中必须有“单条输出上限”这一类发送约束配置，而不应把它硬编码在 `Pal Core` 中。

### `ResponseHandle`

描述如何把回复送回原来源。

至少包含：

- `respond(text: str)`
- `set_status(kind: str)`
- `capabilities`

其中：

- `respond(text: str)` 接收最终回复文本本身
- `set_status(kind: str)` 用于 `typing`、`received` 之类的轻量状态反馈
- chunk、parse mode、MarkdownV2、平台长度限制都在 handle 内部完成

`ResponseHandle` 是对旧版 `DeliveryRoute.reply_handle + adapter.send(...)` 的简化收口。

### `ChannelEnvelope`

`channel` 创建并投递给主循环的统一入口对象。

它至少表达：

- `channel_kind`
- `endpoint_id`
- `message_id`
- normalized payload
- `ResponseHandle`
- correlation id
- created_at

推荐心智模型是：

- `ChannelEnvelope` 是主循环唯一需要理解的 channel 输入
- 主循环不再需要理解 platform route 细节
- 回复路径完全通过 `ResponseHandle` 回到 channel 实例

## Unified Channel Interface

推荐每个 channel adapter 对外都收成同一组接口：

- `start()`
- `stop()`
- `iter_inbound() -> AsyncIterator[ChannelEnvelope]`
- `normalize(raw) -> ChannelEnvelope`
- `respond(handle, text: str)`
- `set_status(handle, kind: str)`

如果某个实现更喜欢把最后两步收进 handle 自身，也可以等价理解成：

- `envelope.response_handle.respond(text)`
- `envelope.response_handle.set_status(kind)`

关键不是面向对象风格，而是统一语义：

- 输入统一成 envelope
- 输出统一成 respond/status
- 具体平台差异留在 channel adapter 内部

## 主路径

```mermaid
sequenceDiagram
    participant U as User
    participant CH as Channel
    participant Q as FIFO Queue
    participant CORE as Pal Core

    U->>CH: inbound message
    CH->>CH: normalize
    CH->>CH: ingress acknowledged
    CH->>Q: ChannelEnvelope
    Q->>CORE: event
    CORE-->>CH: final response
    CH->>U: final reply
```

## Ingress Acknowledgement

`channel` 在接收到用户消息并成功入队后，应尽快给出一个轻量、明确、低打扰的“已收到”反馈。

这个反馈的目标不是回复业务内容，而是告诉用户：

- 消息已被 `Pal` 收到
- 已经进入处理队列
- 不是丢了，也不是机器人没有看见

## Ingress Feedback 三阶段

IM channel 的默认反馈流程固定为：

1. `ingress accepted`
2. `processing visible`
3. `final response delivered`

也就是说：

- 一入队先做“已接收”反馈
- 然后切到 typing / processing 状态
- 最后再发送正式回复

## Telegram Realization

对于 Telegram 这类 IM channel，默认 realization 定为：

1. 消息成功进入 `Pal` 队列后，立即给用户消息打一个 `eyes` 标记
2. 随后切换到 typing indicator
3. 等 `Pal Core` 产出最终回复后，再按 Telegram policy 分流并发送成品内容

这样做的原因是：

- bot 自己发出的消息天然有双勾，无法表达“是否已收到用户消息”
- 眼睛标记更像 receipt marker，而不是额外噪音消息
- typing 适合表达“Pal 正在处理”
- 最终回复仍保持干净，不混入临时确认文本
- 长回复和复杂格式应由 Telegram 侧分流策略负责安全拆分

## Why Not Raw Delta Streaming

IM channel 默认不应直接承接 provider raw stream。

原因包括：

- 分段限制
- edit/send 频率限制
- Markdown / code block 截断
- 半成品文本抖动

因此：

- `llm` 内部可以 streaming
- `channel` 默认只做 status/typing 呈现
- 最终成品再由 `channel` 按 send policy 输出

## Channel-Specific UX Rule

`channel` 子系统允许为不同 IM 平台定义各自的 acknowledgement UX。

但必须满足共同约束：

- 不额外污染对话流
- 不假装已经完成理解或执行
- 不与正式业务回复混淆
- 不替代 typing/status

Telegram 的 `eyes` 标记是其中一种 channel-specific 实现，而不是全局强制 UI。

## Segment Send Rule

最终回复一旦形成，应由 `channel` 根据 endpoint policy 决定：

- 一次性发送
- 分段发送
- 是否需要拆 code block
- 是否需要按平台限制截断重组

这一步发生在最终回复阶段，而不是 provider streaming 阶段。

更一般地说：

- 如果某个 channel 的输出侧存在单条消息上限
- 或者存在严格的格式完整性要求

那么该 channel 就必须拥有自己的 response segmentation 逻辑。

也就是说，是否分流的判定不应写死在 `Pal Core`，而应由 channel policy 决定。

推荐的配置来源就是 endpoint 对应的 `send_policy.max_message_chars`。

### Telegram Segmentation Rule

Telegram 默认应使用 channel-owned response segmentation。

也就是说：

- `Pal Core` 产出的是统一 final response
- `channel` 负责把它按 Telegram 限制拆成安全消息段
- 分流后的每段仍然属于同一次正式回复，而不是新的业务事件

Telegram 分流策略至少应考虑：

- 单条消息长度限制
- Markdown / code block 完整性
- 列表与引用块不要在中间被破坏
- 尽量在语义边界处分段，而不是机械按字符截断
- 多段消息的发送顺序必须稳定

推荐行为：

- 优先按段落、标题、代码块边界拆分
- 超长代码块单独成段
- 如有必要，在后续分段中保留最小上下文提示

这样可以避免：

- Telegram 截断成半截消息
- 代码块闭合错误
- 多段回复顺序混乱
- 用户误以为是多次独立回复

## Current Telegram Implementation Alignment

当前仓库里的 Telegram 适配器实现位于：

- [telegram.py](/Users/nathan/Desktop/coding/Pal/src/pal/channel/adapters/telegram.py)

当前依赖位于：

- [pyproject.toml](/Users/nathan/Desktop/coding/Pal/pyproject.toml)

当前实现已明确依赖：

- `python-telegram-bot>=20.7,<22`
- `telegramify-markdown>=0.4,<1`

当前 Markdown 渲染路径是：

1. 先把 LLM 输出交给 `telegramify_markdown.markdownify`
2. 再以 `MarkdownV2` 作为 `parse_mode` 发送
3. 如 MarkdownV2 失败，则回退为 plain text

这说明：

- Telegram 输出格式本身就是 channel-owned concern
- 分段逻辑也应继续由 Telegram adapter 自己负责

## Logging Boundary

如果需要记录 channel ingress / egress log，应由 channel 实例自己负责。

例如：

- 原始 inbound message receipt
- response send attempts
- segmentation outcome
- status/receipt marker failures

这些 log 的归属是 channel implementation concern，而不是 `Pal Core` concern。

这条边界的意义是：

- 不让 core 理解 Telegram/stdio 的发送细节
- 不让 memory / tasking 背负 channel delivery 现场
- 保留每个 channel 对自己平台特性的自治处理能力

## Telegram SDK Helper 结论

当前设计不应假设 `python-telegram-bot` 会替我们自动完成长消息分块。

原因是：

- 当前仓库实现里没有使用任何现成 chunk helper
- 官方文档明确暴露了消息长度限制常量，但没有形成一条可直接依赖的“自动安全分块发送”主路径

因此，架构结论保持为：

- `python-telegram-bot` 负责 Telegram API 交互
- MarkdownV2 转换由 `telegramify-markdown` 负责
- 安全分流、代码块保护、语义边界切分，仍由 `channel` 自己实现

## Invariants

- `channel` 只做 I/O、normalize、route 和 UX feedback。
- `ChannelEnvelope + ResponseHandle` 是 channel 与主循环之间的统一接口。
- ingress accepted 后必须尽快给出 receipt-like feedback。
- IM channel 默认采用 `receipt marker -> typing -> final reply` 的三阶段 UX。
- Telegram 默认使用 `eyes` 作为 ingress receipt marker。
- 任何存在单条输出上限的 channel 都必须实现自己的 response segmentation。
- Telegram 最终回复默认由 channel 自己按平台规则分流。
- channel ingress / egress log 如有需要，由 channel 实例自己记录。
- raw provider streaming 不直接进入 channel 用户输出。
- 最终回复才是 channel send 的正式输入。

## Non-Goals

- 不在本文件定义 Telegram API 细节
- 不在本文件定义具体 reaction/typing SDK 调用
- 不在本文件定义 UI 样式
- 不让 `channel` 承担业务解释或 agent 推理
