# P0 BASELINE — refactor/full-context-compaction-v2（合并树）

生成：2026-09-19（Pal 实施者）。依据 `pal_compaction_handoff_v2` HANDOFF P0 要求。
本文件只记录已核实事实；「现成/需改/未接」判定以 HANDOFF §12 语义为准。

## 1. 环境

| 项 | 值 |
|---|---|
| worktree | `/home/nathan/Documents/coding/Pal-full-compaction-v2` |
| 分支 | `refactor/full-context-compaction-v2` |
| 基线 | `0e33220e75c9bd011ac06005517161bded66c4e8`（origin/refactor/llm-projection-session-v2，已核对一致） |
| main 合并 | `4daab5f086d5b08753cb254fa7ed2f222874516e`（origin/main，已核对一致） |
| merge commit | `614529e347de03719ee00d6ef3b55fa743555def` |
| merge tree | `4a47e242f8b96c8db2c796ffcf9d08d5f3b83012` |
| merge 冲突 | 无（ort 干净合并；main 侧 5 文件：telegram endpoint/provider/pyproject、turn_executor 2 行删除、bootstrap 测试 +186 行） |
| Python | 3.13.5（系统解释器） |
| pytest | 9.0.3 |
| `pal.__file__` | `/home/nathan/Documents/coding/Pal-full-compaction-v2/src/pal/__init__.py` |
| 测试命令 | `cd <worktree> && PYTHONPATH=$PWD/src python3 -m pytest tests/test_X.py -q`（worktree 内一切测试必须 PYTHONPATH=$PWD/src） |
| 测试文件数 | 153 |

## 2. P0 五问结论（HANDOFF「先执行P0」第4条）

### 2.1 actual durable inbox / ACK —— 缺位，需新增

- 入口队列 = `core/runtime.py::pending_channel_turns`（内存 deque；`_schedule_admitted_channel_turn_async` L823-844 busy 时 append，L842）。
- 出队 = `_start_next_queued_turn_async` L793-806（持 `channel_turn_transition_lock`，查 `resident_quiescing` + active）。
- 插话注入 = `core/interjection.py::inject_pending_interjection_async` L14-138：初次 snapshot 持锁（L28-31），最终 append+ack 重取锁并复查队列头身份（L82-108），append 幂等按 message_id，异常后用 `contains_l1_message`/active turn 兜底判「已提交」（L174-205）。
- **没有任何 durable staging**：进程崩溃即丢 pending；没有独立 event receipt 表——判重依赖 L1 中原文存在，compact 把原文压掉后跨重启判重失效（Q08/R03 直接命中的缺口）。
- 结论：需在现有持久化设施上加最小 staging/receipt（不允许新消息总线）。可用挂点：`RuntimeSnapshotCoordinator`（见 2.2）或 memory 模块既有持久层；P1 定方案。

### 2.2 runtime snapshot 根 —— 骨架现成，install 级原子性需接线验证

- `core/runtime_state.py`：`RuntimeStatePort`（snapshot/prepare/install/reset 四方法，L13-27）；`RuntimeSnapshotIdentity` 含 `incarnation`/`sequence`/`producer_fencing_token`（L32-64，防 ABA 的身份已有）；`RuntimeSnapshotCoordinator.snapshot/restore/reset`（L66-118），restore 逐模块 schema+身份校验 fail-closed（L85-116，`validate_runtime_snapshot` L152）。
- memory 模块 port：`memory/runtime_state.py::MemoryRuntimeStatePort`（L28-139）——快照/恢复 l1_turns（含 per-turn revision/state）+ l2 entries + top_of_mind + heat。
- 调用方：`runtime_app.py` L161/L221（应用级 save/restore）、`core/runtime.py` L1665（soft reset）、`bunshin/runner.py` L2883（worker 级）。
- 结论：durable root 骨架现成；「seed+新空段+epoch+receipt 一次原子提交」所需的单事务多字段写入在 coordinator 层可行，但当前 compact install 路径（见 2.3/§3）完全没走它——P2 需把 install 接到该根并验证原子性。

### 2.3 epoch owner —— memory 级 context epoch 不存在，需新增单字段

- projection 侧已有完整 fencing：`llm/projection_checkpoint.py` 的 `history_epoch`（frontier 与 L1 cursor 一致性校验 L224-237）+ `owner_fence`（现值与历史 receipt 源 fence 分离，L90-93/429-490，B4/C2 已加固）。
- 但 projection 的 epoch 语义是「投影 cursor 代」，不是 memory owner 的语义上下文代。`memory/service.py::compact`（L512-561）与 `compact_transactionally`（L563-584）无 epoch 概念；`L1TurnStore.replace_all`（`memory/turn_ir.py` L459-473）无容器级 CAS（只有 per-turn `replace` 强制 revision 递增 L441-447、`restore_turn` expected 校验 L449-457）。
- 结论：按 PLAN §2 在 memory checkpoint 根加**一个**整型 context epoch（成功安装+1、reset 换身份），复用 RuntimeSnapshotIdentity.incarnation 防 epoch=0 ABA；不得在 executor/summary/provider 各造一套。

### 2.4 final provider payload capture —— resident idle 段现成，active cut 段缺位

- `turn_executor.py::_resident_compaction_replay_request`（L2041-2113）：读 `llm_runtime.prompt_cache_confirmed_anchor_request`，continuity_id 校验后，从 `build_pack(include_l1_recent_context=False)` 的 **settled** turns 投影 anchor 之后缀消息——active turn 完全不在 suffix（W02/F10 缺口属实）。
- 触发条件 `logical_scope_id == "pal:resident" and continuation is None`（L1915）——即只有 resident idle 手动路径有 warm replay。
- hot-gated：`cache_warm_deadline.claim_compaction(cache_epoch)`（runtime.py L1698）+ engine `replay_guard`（L2014-2026，TTL/epoch 复查）——hot-only 不偷转 cold 的骨架已在。
- 前缀/覆盖双证明（PrefixProof/CoverageProof）不存在；实际最终 payload 级对比（tools/thinking/hooks 不变性）无捕获点。P3 需加 after-hook final payload capture。
- `_scope_safe_snapshot`（compaction.py L188-200）已做：剥离 pal_authored developer context、replay 内过期指令直接弃 warm。

### 2.5 Bunshin scope gate —— 单任务 worker 自洽，跨 scope coordinator 需 scope 化

- `bunshin/runner.py`：worker 用独立 TurnExecutor + `BunshinCompactionPolicy`（L1517）、`compaction_clock_provider=llm_round_count`（L1518）；compaction purpose 请求跳过 llm_round 计数/心跳（`is_compaction` L259-279，满足「compact 请求不计成普通工具轮」）。
- scope 身份：`control_scope_key=f"bunshin:{run_id}"`（L872/876）；compact_memory_async 的 scope 解析已分 `pal:resident`/`bunshin:<id>`（executor L1880-1893）。
- 缺：coordinator/ticket 尚不存在（本轮新增），设计时按 scope 键隔离（I17/Q15）；Manager proxy 无明文 private checkpoint 通道的现状保持。

## 3. HANDOFF §12 接线地图逐行判定

| 行 | 实际位置（本树行号） | 判定 |
|---|---|---|
| `agent_turn_program` | turns.py L231-309：preflight advice COMPACT_REQUIRED（L245）与 finish_reason COMPACT_REQUIRED（L281）双触发；`compact_generation_count>=3` 上限（L246/282）；MemoryCompactEffect L93 | 现成；需改：safe-round 触发点接 `_after_tool_batch_async` 边界、epoch 变化不清计数的问题按 I11 处理 |
| `_handle_llm_preflight` | turn_executor.py L160-202：`_is_hard_budget_overflow` → `context_budget_exhausted` 失败信号（L180-201，文案即「active 不能压」旧语义） | 需改：拆「不可压缩底座超窗」vs「active 可压缩超窗」，预留交接余量（B01/B02） |
| `_handle_memory_compact`/`compact_memory_async` | executor L204-268 / L1837-2039（claim/snapshot/generate/install/release 目前全内联在 engine.run，无 ticket/gate） | 需改：抽共用入口+ticket（本包核心）；L1 要求 `_ensure_l1_turn_async` settled 才继续（L206-218，round-safe 的一部分） |
| `_resident_compaction_replay_request` | executor L2041-2113 | 需改：覆盖 active cut 的 anchor+suffix、显式 prefix/coverage proof（W01-W03） |
| `core/compaction.py` | capture L84-128（active 过滤 L102-113）；engine.run L214+（max_attempts=3/timeout180s/max_output 64k）；`_shrink` L635-664（pop(0) 头部丢 unit——「静默缩源」现行行为）；`_commit` L667-720（acompact_transactionally+after_commit=retire diff L1984-2012） | 需改：capture 含 active、source stamp、warm 配置继承；**禁止** _shrink 用于全压安装（S03：source_too_large） |
| `memory/service.py` / contracts | compact L512-561（`replace_all([summary_turn, *active_turns])` L543——active 原文保留）；compact_transactionally L563-584（内存回滚，非 durable CAS） | 需改：source-aware install、新空段+revision+epoch、receipt、durable 接线（§2.2/2.3） |
| `memory/turn_ir.py` / continuity | replace L441-447 / restore_turn L449-457 / replace_all L459-473；`_close_incomplete` L257+（interrupt 共用闭合 normalize） | 现成：闭合/回滚/revision 语义可复用；需改：容器级换代 CAS、fresh 段 metadata 白名单复制（F24） |
| `core/runtime.py` / ingress | 队列 L823-844、出队 L793-806、manual compact L1687-1773（双查 active L1690/1714 但 **claim 与检查不同临界区**——A08/F01 竞态属实）、reset quiesce L1645-1685（`resident_quiescing` bool，finally 无条件 False——Q13/F08 命中现有反模式） | 需改：manual 原子 claim、start gate、release 后 drain、stop/reset 优先（P1 主体） |
| `core/interjection.py` | snapshot L28-31 + 最终临界区 L82-108（结构上正是「两处查门」要的形状，只差 ticket 检查） | 需改：两处加同 ticket 门（Q03/Q04）；不重造 |
| `bunshin/runner.py` | is_compaction L259-279、policy L1517、coordinator L2883 | 需改：同一 coordinator、role scope gate、checkpoint 恢复接 epoch（P4） |
| runtime_state / checkpoint ports | 见 §2.2 | 需接线：install 原子根（P2）、pending inbox 恢复（P4） |
| cache tracker / projection owner | `prompt_cache_confirmed_anchor_request`/`cache_warm_deadline`（claim_compaction/clear_for_compaction/ignore） | 现成（idle hot 流程）；需改：handoff 独立 consumer、安装后失效旧 lineage（W08）、anchor 覆盖 active（P3） |

## 4. 现行行为基线（改动前语义快照）

- capture 排除 active turn（compaction.py L102-113）；install 保留 active 原文（service.py L543）——即「全量换代」两端都未实现。
- manual compact 仅 idle（runtime.py L1690/1714 busy 拒绝）；hot 提醒 hot-only（L1696-1713 + replay_guard）。
- auto compact 在 turn 内经 preflight/finish_reason 双触发，3 次上限/turn。
- compact 请求 purpose 标记使 Bunshin 不计轮次；resident 无此区分（时钟 kind=USER_TURN）。
- 退役 tool results：install 成功后按 call_id 差集 `retire_tool_results`（executor L1984-2012，经 after_commit 事务边界）。
- 成功后 `clear_execution_cursors(continuation)`（L2038）。

## 5. 已知失败 / 环境项（合并树组合基线跑前声明）

沿用 0e33220 全量回归已定性的两枚环境项：
1. `tests/test_package_installation*.py`：setuptools 环境性 fail（基线已知）。
2. `tests/test_tool_schema_properties.py`：缺 `hypothesis_jsonschema`，EXIT 5（基线已知）。
3. `tests/test_bootstrap_and_repositories.py` 固有 >10 分钟（每 test 全量 provision runtime），per-file timeout 需 ≥1800s。

## 6. 组合基线回归结果

命令：per-file 隔离 `PYTHONPATH=$PWD/src python3 -m pytest tests/test_<name>.py -q`，每文件 timeout 600s（bootstrap 1800s），日志 `/tmp/fullcomp_v2_baseline.log`。

状态：**DONE（2026-09-19，套件在 61c87db 修复前跑完，修复后受影响面复跑全绿）**。

- 157 文件全部执行完毕：**2743 passed + 461 subtests passed，3 failed**。
- 非 PASS 文件 4 个，全部可归因：
  1. `test_architecture_skeleton`（1 failed）→ 真 bug①：`_round_safe_for_compaction` 的 `has_open_round` 检查在真实 auto 路径上永真（L1 流式簿记在终端响应后仍开）→ auto claim 永拒。**61c87db 修复**（删该准入检查，保留 store 方法），复跑绿。
  2. `test_runtime_stability`（1 failed）→ 真 bug②：P1 注入在 turn 首个 preflight 即吸干同 scope burst 队列（与 `inject_pending_interjection_async` docstring 的 "after a tool batch" 语义不符），合成 prompt 触发 compact_required，stub 端点无法产出 compact JSON → 单条 recovery 回复。**61c87db 修复**（注入 admission 加 `llm_round_index >= 1`；compact 后 `_after_compaction` drain 不变），复跑绿。
  3. `test_package_installation`（1 failed）→ §5 环境项①，基线已知。
  4. `test_tool_schema_properties`（EXIT 5）→ §5 环境项②，基线已知（缺 hypothesis_jsonschema，收集失败不计 failed）。
- 修复验证（61c87db 后受影响面复跑）：stability + interjection_injection + compaction_gate + architecture_skeleton = 167 passed；full_compaction_source + warm_handoff + control_plane + cache_wire + memory_l1_ir_service = 125 passed。
- `test_bootstrap_and_repositories` 141 passed / ~13min，timeout 1800s 下正常完成。
- 两枚真 bug 均为直连内部方法的单元/barrier 测试无法发现、只有 process_channel_turn 全链路形态逼出的接线缺陷（同族：P4 已修的 waiting_effect_id 自我拒绝）。
- 剩余已知缺口（对应 acceptance NOT_RUN 项）：receipt/checkpoint 崩溃窗口 outbox 未实现；E03/E04 全故障矩阵未跑；attachment lease、deadline sweep、usage 记账未实现；E05 性能 A/B deferred。
