# Turn Runtime Structure

这份文档专门描述 `PalV2` 里新加的 turn runtime 骨架，方便顺着代码找入口。

## 1. 从哪里开始看

最推荐的阅读顺序：

1. `src/pal/core/runtime.py`
   这里是 `PalCore`、`TurnManager`、`MainLoop` 的主编排层。
2. `src/pal/core/turns.py`
   这里定义了 turn computation 本体，以及它会 `yield` 的 effect。
3. `src/pal/channel/runtime.py`
   这里是 channel side 的 mailbox/outbox 交界面。
4. `src/pal/core/tool_stagnation.py`
   这里是工具循环卡死检测。
5. `src/pal/llm/contracts.py` 与 `src/pal/memory/contracts.py`
   这里是 `preflight / compact / commit` 这些 turn-time contract。

## 2. 现在的主数据流

当前一条用户消息的主路径是：

1. `channel` 完成 normalize
2. normalize 后的 `ChannelEnvelope` 被写入 `channel mailbox`
3. `MainLoop` 轮询 mailbox-backed source
4. `TurnEventHandler` 只把真正的 conversational ingress 交给
   `PalCore.process_channel_turn(...)`
5. slash command 这类 control-plane ingress 会在 runtime 内部被单独处理：
   - 不进入 LLM
   - 不进入 prompt assembly
   - 不写入 L1
6. `TurnManager` 创建并驱动 generator 风格的 `TurnProgram`
7. `TurnProgram` 依次 `yield`：
   - `llm.preflight`
   - `memory.compact`（必要时）
   - `llm.request`
   - `tool.call`（必要时，按顺序）
   - `mailbox.reply`
8. `mailbox.reply` 成功写入 `channel outbox` 后，turn 算 egress 完成
9. `PalCore` 触发 post-turn `memory.commit_l1`
10. 后续真正发送成功或失败，由 `channel` 产出 `reply.delivered/reply.failed`

要点：

- turn 不等真正送达，只等 outbox 接收成功
- delivery 事件只用于 channel-side diagnostics
- tool loop 的“继续还是强制收口”由 stagnation guard 决定
- slash command 不是 conversational input；LLM 只能看到其治理结果，不能
  看到命令本身

## 3. 关键文件与职责

### `src/pal/core/runtime.py`

这里是 runtime 总调度器。

- `MainLoop`
  - 轮询各个 source
  - 拉取 mailbox 事件
  - 分发给 handler
- `TurnManager`
  - 创建 `TurnContinuation`
  - suspend / resume generator
  - 持有 `ToolStagnationGuardProcess`
- `PalCore`
  - 驱动 turn effect
  - 做 prompt assembly
  - 调 `llm.preflight`
  - 调 `memory.compact`
  - 调 `Execution.execute_tool`
  - 把 final reply 写入 `channel outbox`
  - 做 post-turn `memory.commit_l1`

### `src/pal/core/turns.py`

这里是 turn computation 的 contract 和默认 channel turn program。

- `TurnProgram`
  - 一个 generator，表达完整 turn 调用链
- `TurnContinuation`
  - 运行时恢复句柄，不是业务状态大对象
- `EffectRequest / EffectResult`
  - turn 和 runtime 之间的 effect 协议
- `channel_turn_program(...)`
  - 当前默认的用户消息 turn 实现

### `src/pal/channel/runtime.py`

这里是 channel runtime 的 ingress / egress 边界。

- `mailbox`
  - 持有已经 normalize 完成的内部事件
- `outbox`
  - 持有待实际发送的 reply
- `queue_reply(...)`
  - 只负责把 final reply 放进 outbox
- `flush_outbox(...)`
  - 真正尝试发送，并产出 `reply.delivered/reply.failed`

### `src/pal/core/tool_stagnation.py`

这里是工具循环卡死检测。

- `canonical_tool_signature_hash(...)`
  - `tool_name + canonicalized args (+ provider)` 的稳定 hash
- `canonical_result_fingerprint(...)`
  - 归一化后的结果 fingerprint
- `ToolStagnationGuardProcess`
  - 判断：
    - `repeat_stagnation`
    - `oscillation_stagnation`

### `src/pal/llm/contracts.py` / `src/pal/llm/runtime.py`

- `LLMPreflightRequest`
- `LLMPreflightAdvice`
- `LLMRuntime.preflight(...)`

这一层负责预算感知，而不是直接 orchestrate compact。

### `src/pal/memory/contracts.py` / `src/pal/memory/service.py`

- `MemoryCompactRequest / Result`
- `MemoryCommitRequest / Result`

这一层只执行 compact / commit，不决定什么时候做。

## 4. Finalization-Only 是什么

当 `ToolStagnationGuardProcess` 返回 `terminate_tool_loop` 时，`PalCore` 会把当前 turn 切到 `finalization_only`。

这时会发生几件事：

1. 下一次 `llm.request` 的 `tools` 被物理清空
2. prompt 里会注入 finalization directive
3. `Execution.execute_tool(..., allow_tools=False)` 会拒绝执行任何新工具
4. 只允许最后一次 text-only finalization attempt
5. 如果模型仍不收口，就走 runtime fallback final reply

这套设计的目的，是保证 turn 最终一定能安全收口。

## 5. 现在有哪些还是骨架

以下部分目前还是第一版骨架：

- `TurnProgram` 只实现了 channel turn 主链
- `LLMRuntime.generate(...)` 仍是 stub
- `LLMRuntime.preflight(...)` 还是简化预算估算
- `memory.compact(...)` 目前只是最小摘要压缩
- `memory.commit_l1(...)` 目前是最小 append/重试骨架
- approval 还只是 execution-wrapped tool 的约束方向，没有完整审批流

## 6. 读代码时最容易混淆的点

- `active_turns` 里现在放的是 `TurnContinuation`，不是完整业务 turn state
- `reply.delivered/reply.failed` 不影响 turn 是否完成
- `PromptFragmentRegistry` 仍然是 prompt 的唯一输入源
- `channel inbox` 现在本质上是 mailbox 视图，不再是原始 adapter 输入队列
- `stagnation guard` 是独立 process，不是写死在 turn loop 里的 if/else
