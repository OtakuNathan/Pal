# DELIVERY — pal-two-segment-v3 (refactor/two-segment-session-v3)

状态：**v3 双段实现交付（dual-mode）+ N1 收口中**。基线 `7e6773f`（= origin/refactor/full-context-compaction-v2 tip，main 是其祖先；v2 的交付事实由该基线携带并仍可在 git 历史查阅，本文档按 MIGRATION.md 取代其作为本分支交付标准）。未 push 新增 commit、未合 main、未部署。

## 1. Formal gate（G0）

- tla2tools.jar SHA `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`（官方 v1.7.4 release 与本地副本字节一致）
- 真实 SANY / TLC safety / liveness / 大参数正例 + 6 mutant 反例：**10/10 PASS**（`~/Documents/coding/pal_two_segment_v3_20260920/logs/tlc_results.json`）
- `implementation_gate.py` exit 0；TLC 2923 distinct states 与包内 Python 参考模型一致

## 2. 提交链（基线之上）

| Commit | Gate | 内容 |
|---|---|---|
| `04da350` | G1 | `memory/history_root.py` 两段 authority：cut/promote/replace-left/终态仲裁/producer token；L/C/X 全族 owner 级 33 测试 |
| `45e1623` | G2 | projection 两段化：`semantic_span`、`prepare_handoff`、`on_left_replaced`、`rebind`；三 shape codec oracle（P01-P08 + W 侧） |
| `cb39341` | G3(a) | MemoryService facade：`history_root`（惰性自愈）/`left_transcripts`/`begin_left_compaction`/`compact_left` |
| `939db45` | G3(b) | I18：builder 公开转发 compaction 四参数，删 runtime/bunshin build 后私有写 |
| `7aa4030` | G3(c)-1 | executor `compaction_mode='two_segment'` 左段流：promote→begin→capture_left→engine→compact_left（cold-left） |
| `eb25fdc` | G3(c)-2 | interrupt/reset 双模式路由（two_segment 走 root 仲裁）；root 自愈升级 |
| `352d831` | G3(c)-3 | 准入分支：v2 ticket/ledger/outbox/lease 机器只留在 full_source；staging 自动接线按模式关断 |
| `0695976` | G3(d) | H01 十二组合（2 宿主×3 shape×2 模式）真实 executor 链路 + retire 钩子共享修复（逮住 UnboundLocalError 真 bug） |
| `0bb2b31` | G4 | owner 级 B01/B03/Q03 钉测 |
| `5e042c6` | 后置 | **projection session 接入真实链路**：LLMRuntime 托管 per-scope session（活端点 binding、换端点单次 rebind）；two_segment install 后驱动 on_left_replaced（F13 不回滚） |
| `e58d1cd` | N1 | review 包 F1/F2/F5/F6 修复：root 终态收口（失败/取消/超时/deadline 门）、左 stamp 改不可变内容基 + settlement 不改写 cut 覆盖消息、终态记录瘦身 + reset 清档案、IN_PROGRESS 不入闭合组；`tests/test_v3_n1_root_lifecycle.py` 9红→10绿，红证据 `logs_n1_prefix_red.txt` |
| `5a7258a` | N1收紧 | 对照 origin 包 PLAN §5.2 自审：stamp 覆盖全部消息身份字段（parts/semantic_kind/prompt_region/replay/metadata）并绑定 owner-issued cut 版本；补 metadata 篡改负例，N1 套件 11/11；73 文件 1225+285 subtests 零失败 |
| `7d6eb0d` | N1.1 | review 包 pal_v3_n1_review_29a879f 四项：R1 promote 封口（冻结前完成 neutral reasoning 退休，settle/interrupt 不再冲突）、R2 单一终态出口（提交后打包故障按 owner 终态归类，success+memory_result+rebase；reset 后迟到收尾幂等 close_run）、R3 发布前最终门（READY/身份/deadline 重验，>= 统一）、R4 stamp 补 message.state；12 新测试 7红→12绿，74 文件 1237+285 零失败 |
| `a8530e7` | N2 | review F3/F4/W2：handoff 改 base+L+instruction 单次全量编码（三 shape oracle 对齐，不再丢 in-container preamble）；rebase 重放幸存 chunk 原始 wire（native 保真）+ pending tail 全保留 + head-system 按 span 归属 + left_revision 单调校验；executor 传真实冻结 R（frozen_message_ids）+ epoch 来自 root；PreparedRequest 携带完整 wire 契约（spans/extra_body/breakpoints）；checkpoint 补 semantic_span；红→5+5sub 绿，75 文件 1242+290 零失败 |
| `702f1bc` | N2/N11 | cut 拥有的 seed 走 project_standalone 独立 L reference（永不绑进 R 用户消息），normal/handoff 共享同一 L 投影，已有 summary 投影不双注；full_source anchor 行为不变；1红→3绿，13 文件 186+17sub 零失败 |
| `7d75ddb` | N3 切片 1 | invoker encode 点接受 owner 备好的不可变 projection（类型化参数，非 metadata 夹带）；无 projection 时 codec 路径字节不变；2 新测试 + 受影响 97 文件 1443+347sub 零失败；剩余 N3：runtime owner 侧准备（W3）、accept→observe_commit 闭环（W5）、完整纵向 trace |

## 3. 设计要点

- **单一物理历史**：L/R 是 L1TurnStore 上的逻辑 cut 视图，无镜像历史（A03）
- **Compact 只替左**：capture_left 只读左段（I10），install 单段无 await 发布、R 逐字保留（I05/F04，活动 round 内容折入物理记录而非拒绝）
- **身份分离**：left_revision 不 fence R producer，仅 session incarnation（reset）fence（I09/X05/X06）
- **准入**：two_segment 模式下 owner 即仲裁者（单活 run / 终态互斥 / L08 最小种子防环）；full_source 模式保留 v2 gate 全套——**双模式并存，v2 行为零破坏**
- **projection rebase**：chunk 绑 semantic span，替换左段按 span 保/删，R 原 wire/native 字节保留；anthropic 用户接缝退 pending tail 复用 F2 机制

## 4. 诚实边界（acceptance 账本同步 NOT_RUN）

0. **review 包（pal_v3_review_2014db4，Request changes）收口中**：N1 已修（F1/F2/F5/F6）；**F3（prepare_handoff 丢 in-container base preamble）与 F4（rebase 保留 R 未在真实 helper 成立：空 keeper、IR 重建丢 native、硬编码 epoch）属 N2**；W1-W5 接线缺口按 NEXT_STEPS N2-N6 推进（28 项收口矩阵全 NOT_RUN）
1. **warm anchor 未接入双段流**：handoff 走 cold-left 全量编码；H01 warm 列验证的是「资格不可用时诚实回退 cold」。warm 拆分（cached L 前缀 + 未缓存后缀直发）是后续项
2. **compaction_coordinator 未物理拆除**：full_source 模式仍在用；文件级删除等 two_segment 转默认后进行。two_segment 路径零依赖它
3. **ingress_staging / artifact_lease**：自动接线已按模式关断（two_segment 下永不构造）；模块文件保留至模式翻转，届时随 v2 专用测试一并移出
4. **H 族运行时项**：H02（root 快照持久化）、H04（旧 schema 显式迁移）、H06（staging gate UX）、Q04（two_segment 的 BUSY 路由）、B04-B08（发送门矩阵）未实现——账本 NOT_RUN
5. bootstrap 全量回归需 ≥1800s 超时（v2 既有纪律）；证据见回归日志与账本

## 5. 验收证据

- v3 新测试族（终 HEAD 复跑）：history_root 33 / projection_two_segment 14+12sub / memory facade 7 / executor flow 3 / builder 2 / H01 1+12sub / budget facts 3——全绿
- 继承族逐 gate 抽查：runtime_compaction / full_compaction_source / hot_cache / memory / history / projection 全系 / bunshin harnesses / hosts_recovery——绿
- 全量回归（HEAD `3575e48`，26:15）：**3050 passed + 486 subtests + 7 skipped，22 failed**。归因：19 个可提取失败节点（bootstrap/telegram keyboard、browser LRU、bunshin sandbox/v2public/verification、control_plane 键盘渲染）**单独复跑全部 PASS（0 复现）**，涉事文件批跑亦无失败复现——判为长时间全量运行下的资源争用/时序 flake（与 v3 改动面零交集：不触及任何失败断言所在路径）；另 3 个失败未产出可提取节点行。不宣称全量全绿；需干净环境重跑全套作最终归因
- acceptance_status.json：41/72 PASS（每项带真实 node/command/exit/product_sha，由 fill_ledger_v3.py 逐 node 实跑复核后写入）；其余 31 项 NOT_RUN 且关键项带原因备注（无 fill 脚本冒充）

## 6. 全链路逼出的真 bug（本任务）

1. `compact_memory_async` two_segment 分支引用定义在其后的 `after_compact`（UnboundLocalError）——此前无测试真正驱动该入口，H01 矩阵首跑即暴露；retire 钩子移到模式分支前共享（`0695976`）
2. `right_turns()` intra==len 时把边界 turn 重复放回 R（G1 自审发现）
3. anthropic rebase 后 prefix 尾部 user 接缝不合并 → 退 pending tail 复用 F2（G2）
4. **N1 终态出口（已由 N1.1 关闭）**：一切提交后故障（打包异常、超时、外部取消）按 owner 终态归类——已 COMMITTED 则报 success+已接受 memory_result+驱动 rebase，不伪造空成功、不宣称「历史未变」；reset 已退休的 run 迟到收尾幂等不抛 StaleRun。曾描述的「wait_for 计时器恰在 commit 与 return 之间插入」窗口未单独证明可达（单 event loop 同步段内 timeout callback 不插入），且已被同一归类路径覆盖，不再作为独立边界

## 7. 回退

分支独立，未动 main / v2 分支；worktree 删除即回退。runtime 默认 `llm_compaction_mode='full_source'`，v3 行为需显式配置开启。
