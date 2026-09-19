# DELIVERY — refactor/full-context-compaction-v2（pal_compaction_handoff_v2 全量 compact 改造）

日期：2026-09-19 ｜ 状态：**交付待审（未合 main、未部署、未推送）**

## 1. 交付物

- 分支：`refactor/full-context-compaction-v2`（worktree `~/Documents/coding/Pal-full-compaction-v2`）
- 基线：交接包基线 `0e33220` + 真实合并 main `4daab5f` = `614529e`（零冲突），本地未推送
- 提交链（6 个）：

| 提交 | 阶段 | 内容 |
|---|---|---|
| `c42aad9` | P0 | 基线合并 + BASELINE.md（接线地图、已知失败、P0 五问） |
| `fbb83f9` | P1 | CompactionTicket/Gate（身份校验 claim/cancel/release）、三查门、manual 准入原子化、round-safe 判定、插话注入、IngressStagingStore+receipts、interrupt/reset 到 ticket、release 后 drain（Q05/Q06） |
| `cfa76c8` | P2 | 完整源 capture（include_active）+ 原子安装（epoch/receipt/后继段）、memory runtime state schema v2（fail-closed+迁移）、S/I/E01-cold 验收 |
| `4cd4103` | P3 | warm handoff 覆盖 active cut、资格门、coverage proof（12 条 W 类测试） |
| `7dadb6b` | P4 | Bunshin scoped gate（专用 carrier）、真实文件恢复（R01-R03）、取消语义（X01）、修两个接线真 bug（属性名下划线、waiting_effect_id 自我拒绝） |
| `61c87db` | P5 | 修两个全链路真 bug：has_open_round 自我拒绝、首 preflight burst 吸收（详见 §4） |

- 设计文档：`BASELINE.md`、`P1_DESIGN.md`、`P2_DESIGN.md`（worktree 内）
- 验收账本：`acceptance_status.json`（worktree 根 + 交接包目录双份同步）

## 2. 验收账本（108 项）

**83 PASS / 25 NOT_RUN（全部带具体理由）**，无 FAIL、无假 PASS（acceptance 只填真实跑绿的 node）。

NOT_RUN 主要缺口分类（诚实声明，非遗漏）：
1. receipt/checkpoint 崩溃窗口：需 outbox 机制，未实现
2. E03/E04 全故障矩阵：未跑（按计划 deferred）
3. attachment lease、deadline sweep、usage 记账：未实现
4. E05 性能 A/B（no-compact 热路径、capture/assembly、install RSS）：deferred 到交付评审
5. 真实 transport 直连/ManagerProxy 泳道：本轮未跑

## 3. 全量组合回归（E06，BASELINE.md §6）

- 157 文件：**2743 passed + 461 subtests passed，3 failed**
- 3 失败全部可归因：2 真 bug（61c87db 修复，受影响面复跑 167+125 全绿）、1 基线环境项（setuptools）
- `test_tool_schema_properties` EXIT 5（缺 hypothesis_jsonschema，基线已知）
- bootstrap 141 passed / ~13min 正常完成

## 4. 全链路逼出的真 bug（4 个，两族）

**族一：准入谓词读「运行时自感标志」→ 真实路径永久自我拒绝**（单元/barrier 测试全绿，只有 process_channel_turn 全链路形态能逼出）：
1. `waiting_effect_id`：TurnExecutor dispatch 本 effect 前已设 → 永远看到自己（P4 修，7dadb6b）
2. `has_open_round`：L1 流式簿记在终端响应后仍开 → auto claim 永拒（P5 修，61c87db）
   修法：round-safe 判定只看协议配对 + 消息状态闭合（顺序 effect 模型的结构性保证）

**族二：接线属性/时机错位**：
3. PalCore/Bunshin post-build 设公共属性名，executor 读下划线私有名 → auto gate 和 preflight 注入整体关闭（P4 修，7dadb6b）
4. P1 注入接在 turn 首个 preflight：同 scope burst 队列被整批吸入 turn 1（与 inject docstring "after a tool batch" 语义不符）→ 合成 prompt 触发 compact_required → stub 无法产 compact JSON → 单条 recovery 回复（P5 修，61c87db：注入 admission 加 `llm_round_index >= 1`，compact 后 `_after_compaction` drain 不变）

## 5. 记录在案的偏差

1. P3：engine compaction-call 采样 envelope 不变（记入 4cd4103 commit message）
2. P0 组合基线 DEFERRED（用户决定）→ P5 全量补跑取代（BASELINE §6 DONE）
3. stub LLM 端点无法产出 compact 生成 JSON（`stub_llm_default` 三次 `schema:output is not valid JSON`）——真 stub 环境下 auto compact 必失败并走 recovery 路径；wiring 层语义已由 fake compactor 家族覆盖，stub JSON 支持留作测试基建后续项

## 6. 待 Nathan 拍板

1. 本分支是否推送（含 4245512 旧全量回归记录，projection 分支遗留）
2. 旧 projection 分支大项：冻结边界重设计、resident/Bunshin 真实链路集成、benchmark 重测、P7 canary、write_busy 租约粒度提级
3. §2 缺口的排期（outbox / 故障矩阵 / 性能 A/B / lease 等）
