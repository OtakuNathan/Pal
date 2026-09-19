# P1 设计记录 — 门控与背压（resident 宿主）

依据 `pal_compaction_handoff_v2` PLAN §4/§5/§6/§13-P1。本文件是实施决策记录，供 review；验收以 TEST_MATRIX A/Q/X（P1 阶段项）+ acceptance_status.json 为准。

## 范围

- resident 宿主全接线；Bunshin 宿主 P4 接线（TurnExecutor 新参数默认 None=旧行为，不破坏其现行隔离）。
- P1 不改 capture/install 语义（P2）；warm 请求构造不动（P3）。gate 对现有 compact 调用是外包裹。

## 1. Ticket / Gate（`core/compaction_coordinator.py`，新文件）

- `CompactionTicket`（frozen dataclass）：`scope`、`op_id`(uuid)、`trigger`("manual"|"manual_hot"|"auto")、`phase`(CLAIMED→GENERATING→READY→COMMITTED→RELEASED 终态)、`cancelled: bool`、`claimed_at_monotonic`、`deadline_seconds`。P2 追加 `source_epoch`/`source_stamp_digest` 字段（append-only 演进，R04 fail-closed）。
- `CompactionGate`：持 `CoreRuntimeState.compaction_tickets: dict[scope, CompactionTicket]`。
  - `claim(scope, trigger)`：**调用方必须已持 `channel_turn_transition_lock`**（文档+断言 `lock.locked()`）；已有 ticket → None（I05 单准入）。
  - `cancel(scope, reason)`：标记 cancelled（撤销提交资格），不摘除 holder——dying coroutine 仍持门到自己的 release（X01/F07：迟到 finally 身份校验后无权释放后继）。
  - `release(ticket)`：op_id 身份匹配才摘除；不匹配 → no-op 返回 False。
  - `is_active(scope)` / `is_cancelled(scope, op_id)`：供 barrier 检查。
- 不新增第二套互不相干 bool；不动 `resident_quiescing`/`memory_maintenance`（Q13/F08：compact release 只摘自己的 ticket，不清别人的 quiesce）。

## 2. 三个查门位置（PLAN §5.3）

1. 新消息执行入口 `_schedule_admitted_channel_turn_async`：busy 条件加 gate → 排队 + **durable staging 写入**（§4）。
2. `_start_next_queued_turn_async`：同锁判 gate，不出队。
3. `inject_pending_interjection_async`：**初次 snapshot（已持锁段）与最终 append+ack 临界区两处**都查 gate；任一活跃 → 不 append 不 pop，消息留队列（Q03）；claim 等已在临界区的 append 先完成（同锁天然串行，Q04 测试钉住）。

## 3. 顺序决策：preflight 权威化（替代 _after_tool_batch 本地预估）

- `_after_tool_batch_async` **不再**注入插话（现行为：批次后立即注入，会把新用户输入喂进即将到来的 compact source）。
- 注入点移到 `_handle_llm_preflight`：advice READY 且队列非空且无 gate → 注入 → 重建 prompt → 复跑一次 preflight（有界 2 次/效果；第二次不再注入）。advice COMPACT_REQUIRED → 不注入，走 MemoryCompactEffect。
- compact 成功后（`_handle_memory_compact` release 之后）调 runtime 注入回调 drain 队列 → 下一 loop-top preflight 看到新消息（Q05：FIFO 各一次、没有先执行一轮旧任务）。
- 交接期间到达的消息全部 durable 排队（§4），release 后按 continuation 资格处理。

## 4. Durable staging（`core/ingress_staging.py`，新文件）

- `IngressStagingStore`：runtime root 下单 JSON 文件（ResidentCheckpointStore 模式：tmp+fsync+os.replace+目录 fsync），存 pending 记录 + accepted receipts。
- 记录：scope、event_id、correlation_id、endpoint(endpoint_id/channel_kind/binding_key/send_policy)、reply_target、payload（dict 原样；IR payload 走 `memory/runtime_state.py` 既有 IR round-trip）、queued_at。**envelope 完整重建**（ChannelEnvelope 全 JSON 原语字段）。
- 顺序（Q08/R03 骨架）：append L1 → **写 receipt（durable）** → pop 内存队列/staging；crash 间隔由 receipt 或 L1 message_id 幂等二者之一判重，去重键=源 event ID，非 batch hash。
- 界限（Q10）：64 条/4MiB envelope 元数据（可配置起点）；满 → `IngressStagingFull` → 调用方**不发已排队确认**、不 pop 最旧，通道侧重投或用户重试。
- 写失败（Q11）：异常上抛 → 无确认、无 ack。
- 诚实边界：staging 文件的持久粒度=每次 enqueue/receipt 原子替换；**硬崩溃丢自上次写入以来的窗口**属通道重投域（与 HANDOFF「通道可重投且未 ack」一致）；R 类真实崩溃测试在 P4 用真实临时文件钉。

## 5. 触发与准入（PLAN §4 决策表落地）

- MANUAL（`_handle_compact_memory_async` 重构）：claim 与全部前置检查（无 active、无未完成 turn_tasks/teardown、`pending_channel_turns` 空=A10 先到事件优先、非 memory_maintenance）**同一临界区**；claim 后才发状态通知（X08：通知失败不取消 gate、不授权二次 claim）；无预约。
- MANUAL_HOT（cache_epoch）：同上 + 既有 `claim_compaction`/`replay_guard`（hot-only，不偷转 cold）。
- AUTO（`_handle_memory_compact`）：effect 入口做 round-safe 判定后 claim；round-safe = `_ensure_l1_turn_async` settled + active L1 无 IN_PROGRESS 消息 + 工具协议闭合（`l1_tool_protocol_validation_error(allow_pending=False)` 语义，实施时核对该函数真实签名）+ L1 无存活 `_ActiveRound`（流未闭合）+ `continuation.waiting_effect_id is None`。A04（unjournaled mutation ledger）：实施时核对 `execution/runtime.py` 既有 ledger API；有则接、无则记录缺口并归 P4 故障注入。
- 既有 `compact_generation_count>=3`/turn 上限保持（turns.py 不放宽）。

## 6. 控制（PLAN §6）

- `/interrupt`/stop（`_handle_interrupt_turn_async`）：无 active turn 时若 gate 活跃 → cancel ticket 并回复（X05）；有 active turn 时照旧 interrupt（task.cancel 自然打断 compact await；compact 包装的 finally 身份校验 release，CancelledError 不吞成半状态）。
- 已确认 reset：quiesce 临界区内先 `gate.cancel_all("reset")` 再走原 reset（X04：旧 ticket 失效、reset 新代不被迟到摘要覆盖）。
- shutdown：既有 shutdown checkpoint 路径会 interrupt active turn → 同上自然取消；staging 文件随 runtime root 存续。

## 7. 测试

- 新文件 `tests/test_compaction_gate.py`（A/Q/X 的 P1 项）：复用 interjection 测试的真 fixture 风格（真 CoreRuntimeState/TurnExecutor/MemoryService + fake LLM + **barrier 式 stub engine**（覆写 `run()` 挂 asyncio.Event）），零真实 sleep。
- 手动/自动、竞态、背压、取消各用 asyncio.Event 屏障控制时序；每条映射 TEST_MATRIX ID。

## 8. 实施期间确定的偏差（2026-09-19，均与规格不冲突）

1. **失败也 drain**：release 的 finally 里统一 `_start_next_queued_turn_async()`（成功与失败同路径，Q06 要求失败释放后排队输入不卡死）；成功后另有 executor 内 after_compaction 注入（活跃 turn 场景，下一 preflight 前入 L1）。
2. **typed ingress 先行**：队列入口前 dict payload 已被编译为 LLMMessageIR，staging 记录 payload_kind="message_ir"，用 llm/serde round-trip 保持类型忠实。
3. **直接执行路径也查门**：`process_channel_turn_async`（同步 API）在 quiescing/dreaming 检查中加入 gate，I03 无旁路。
4. **A04 现状**：无可公开查询的 effect ledger（"outcome unknown" 是工具结果内部语义）；round-safe = 协议配对 + 流闭合 + 无在造 effect + effect 顺序性。A04 完整语义（unjournaled mutation 故障注入）归 P4，已在 BASELINE.md 记缺口。
5. **X01 活跃 turn 中断**：interrupt_active_turn 的 task.cancel 自然打断 compact await，finally 身份校验释放；ticket 同时标 cancelled。engine 中途撤销 HTTP 计费不承诺（P2 给 engine.run 加 cancel 事件）。

## 9. 明确不在 P1

- capture 含 active、新空段/epoch/receipt 安装（P2）
- warm anchor 覆盖 active、prefix/coverage proof（P3）
- Bunshin gate 接线、真实崩溃恢复矩阵、Manager proxy（P4）
- 全量回归 + 108 项汇总（P5）
