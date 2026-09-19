# P2 设计记录 — 完整源 capture 与原子安装

依据 PLAN §7/§10/§13-P2 与 TEST_MATRIX S01-S10、I01-I14（P2 部分）、E01 cold 变体。

## 1. 数据契约（memory/contracts.py）

- `MemoryCompactRequest` 追加（缺省=旧局部语义，直接调用旧 compact 的路径不变）：
  - `op_id`：安装幂等键（receipt 主键）
  - `source_stamp`：capture 时对 turns 序列的摘要（非空即全压模式）
  - `active_turn_id`：需要新空后继段的逻辑 turn（idle manual 为空）
  - `expected_epoch`：capture 时的 memory context epoch
- `CompactionReceipt`（frozen）：op_id、status=committed、epoch_before/after、summary_source_id、successor_turn_id/revision、removed_turn_ids、removed_result_refs、cleanup_status(ok|pending)、created_at。
- `StaleCompactionSource(ValueError)`：CAS/epoch 失败的显式类型（I03/I12/F18）。

## 2. Source stamp（memory/turn_ir.py，L1 owner 发出）

`source_stamp_for_turns(turns)` = sha256 over 每 turn 的 `turn_id:state:revision:message_count:message_id 列表`。
capture 与 install 校验**用同一函数**；caller 不能自填 block_count 冒充（PLAN §11）。

## 3. MemoryService 全压安装（memory/service.py）

- 新状态：`context_epoch: int = 0`（成功安装 +1；`soft_reset` 也 +1 换身份，PLAN §2）；`compaction_receipts: dict[op_id, CompactionReceipt]`（容量 256，超出丢最旧——receipt 是幂等事实不是无限台账）。
- `compact(request)` 顶部：`request.source_stamp` 非空 → 全压分支：
  1. **幂等**（I05/I06）：receipts 已有同 op_id → stamp 相同则返回已记录结果（不重跑、不二次 bump epoch）；不同 stamp → raise（不同候选冒用同 op_id 拒绝）。
  2. **CAS**（I03/I06/F18）：`source_stamp_for_turns(current) != request.source_stamp` → raise StaleCompactionSource（绝不把 capture 快照恢复覆盖新数据）。
  3. **epoch**（I12）：`context_epoch != expected_epoch` → raise StaleCompactionSource。
  4. 构造：summary_turn（沿用现行 continuity 通道）+ `active_turn_id` 存在时新空后继段 `L1TurnIR(same turn_id, ACTIVE, revision=old+1, messages=(), metadata={"compact_successor": True, "compact_op_id": op_id})`——metadata 只白名单这两键（F24）。`begin_l1_turn` 对已存在 ACTIVE turn 原样返回 → I10 天然成立（opening 不回灌）。
  5. 单一提交段（I04/I09）：`replace_all([summary_turn, *successor])` + `remove_projected_entries` + `context_epoch += 1` + receipt 写入，全部为不可抛的局部赋值；可抛步骤（校验/构造）全部在前。
- `compact_transactionally(request, after_commit)` 全压分支：compact 成功后 `after_commit()` 抛错 → **不回滚**，`receipt.cleanup_status="pending"` 并吞掉异常（F21/I07：cleanup 失败不伪装未提交）；旧局部路径回滚语义不变。
- `mark_compaction_cleanup_pending(op_id)`：I07 重试口。
- soft_reset：epoch+1；receipts 保留为历史事实（旧 op_id 重放会被 stamp/epoch CAS 拒绝，不复活旧 seed，X04/R07）。

## 4. Capture 与 engine（core/compaction.py）

- `CompactionSnapshot` 追加 `source_stamp`、`source_epoch`、`active_turn_ids`。
- `capture(..., include_active=True, source_epoch=0)`：全模式直接以 `turns` 序列取 transcript（`_transcript_from_turn` 投影，S01「同一 revision 集合」），stamp 用 §2 函数对 turns 计算；`include_active=False` 保留旧行为给既有直接调用。
- engine：`snapshot.source_stamp` 非空（全压模式）时，preflight COMPACT_REQUIRED / finish_reason COMPACT_REQUIRED 不再走 `_shrink`——返回 `source_too_large`，原文/epoch 不动（S03/S04；shrink 在全压路径不可达，方法保留待 P5 清理）。replay 分支超窗同样落到该终态。
- `_commit` 构造 MemoryCompactRequest 时转发 snapshot 的 stamp/epoch/op_id/active_turn_id（active 取 snapshot.active_turn_ids 中唯一项；多个 → S09 在 service 层拒绝）。

## 5. Executor 接线（core/turn_executor.py）

- `compact_memory_async`：capture 传 `include_active=True`、`source_epoch=memory_service.context_epoch`；active_turn_ids = store 中 ACTIVE turns（auto 路径即 continuation 的 turn；idle manual 为空）；`op_id` 生成 uuid 挂 snapshot metadata 并随 request 下发。
- 既有 after_compact（retire 差集）经 transactionally 全压分支获得 committed+pending 语义（I07）。

## 6. 持久化（memory/runtime_state.py）

snapshot payload 追加 `context_epoch`、`compaction_receipts`：
- 存在但非法（非 int/负数/bool、receipt 结构坏）→ restore raise（R04 fail-closed，不借旧格式回退）。
- 真正缺失（旧 checkpoint）→ `context_epoch=0` 受控迁移 + 记录（R05）。
- P2 测试用 MemoryRuntimeStatePort round-trip 作为 checkpoint storage 证据；真实进程重启恢复矩阵归 P4。

## 7. 明确不在 P2

- warm 请求构造/prefix+coverage proof/anchor 覆盖 active（P3）
- Bunshin 宿主接线、真实崩溃恢复、await/commit 故障注入全矩阵（P4）
- 预算拆分细化（B01-B05 余量预留）、全量回归与 108 项汇总（P5）

## 8. E01 cold 变体（纵向验收，tests/test_full_compaction_source.py 内）

按 E2E_RECIPES 步骤 1-10（11/12 重启与重投为 P4）：真实 engine + scripted LLM + 真实 MemoryService + staging；SEED0/Q_ORIGINAL/A_DECISION/call A/RESULT_A 哨兵；barrier 停在 generate；期间 M=ACTUAL_NEW_CORRECTION durable 排队、L1 冻结、epoch 不变；安装后 seed1+新空段（同 turn_id、revision+1、epoch+1、receipt）；M 恰一次进入新上下文；下一 preflight 请求中 seed1 与 M 各一次、Q/A/result 原文不再 raw 出现；工具 A 执行计数仍为 1；预算/guard 延续。
