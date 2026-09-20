# DELIVERY — pal-two-segment-v3 (refactor/two-segment-session-v3)

状态：**v3 双段实现交付（dual-mode）**。基线 `7e6773f`（= origin/refactor/full-context-compaction-v2 tip，main 是其祖先；v2 的交付事实由该基线携带并仍可在 git 历史查阅，本文档按 MIGRATION.md 取代其作为本分支交付标准）。未 push、未合 main、未部署。

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

## 3. 设计要点

- **单一物理历史**：L/R 是 L1TurnStore 上的逻辑 cut 视图，无镜像历史（A03）
- **Compact 只替左**：capture_left 只读左段（I10），install 单段无 await 发布、R 逐字保留（I05/F04，活动 round 内容折入物理记录而非拒绝）
- **身份分离**：left_revision 不 fence R producer，仅 session incarnation（reset）fence（I09/X05/X06）
- **准入**：two_segment 模式下 owner 即仲裁者（单活 run / 终态互斥 / L08 最小种子防环）；full_source 模式保留 v2 gate 全套——**双模式并存，v2 行为零破坏**
- **projection rebase**：chunk 绑 semantic span，替换左段按 span 保/删，R 原 wire/native 字节保留；anthropic 用户接缝退 pending tail 复用 F2 机制

## 4. 诚实边界（acceptance 账本同步 NOT_RUN）

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

## 7. 回退

分支独立，未动 main / v2 分支；worktree 删除即回退。runtime 默认 `llm_compaction_mode='full_source'`，v3 行为需显式配置开启。
