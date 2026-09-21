# DELIVERY — pal-two-segment-v3 (refactor/two-segment-session-v3)

状态：**two_segment 唯一模式（v2 已物理删除）+ 收口挂账中**。基线 `7e6773f`；v2 交付事实由 git 历史携带。未 push 新增 commit、未合 main、未部署。

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
| `5642542` | N3 主体 | **ordinary 轮真正走投影**：executor owner 侧 prepare（durable-id 过滤，瞬态内容冷回退永不冻结）→ 类型化不可变 (EncodedRequest+Binding) 下沉 → runtime W3 端点匹配（不符即冷）→ invoker 发送 → 真实 decode/accept → observe_commit 冻结（ERROR 轮 reject 不冻结）；`test_v3_n3_vertical_trace.py` 两轮 trace：冻结块 span 覆盖 q+a、第二轮 payload 每条内容恰好一次；98 文件 1445+347sub 零失败 |
| `695efbc` | N4 切片 | W1/N20：请求边界 promote 闭合旧组（长活 turn 不再永久 no_benefit）；cut 规则修正：闭合前缀不得以悬空 user 结尾（L01 旧断言与其名义矛盾，已对齐 PLAN §3.3）；J7 过期守卫：root.left_generation（仅 install 跳）vs session.history_left_revision，rebase 未消费即冷回退不重播已退休历史 |
| `1900125` | N7（pal_v3_af51d74_review） | review F1-F6 六项全修：F1 `prepare_generation_plan` 一次性派生不可变 prepared plan（resolved endpoint + 编译后 effective request + 强 binding：真实 spec digest / continuation contract version / 覆盖能力画像的 config fingerprint；spec-1/policy-1/provider:base_url 占位符全部消灭），投影编码 effective_request 且 generate/astream 在 plan 端点原样复用编译结果；F2 类型化 `ProjectionSendReceipt`（attempt id + resolved endpoint + applied + native）随 LLMGenerationResult / `last_projection_receipt`（stream）返回，observe_commit 仅由匹配的 applied receipt 授权，端点 fallback 报 applied=False 不冻结，sync stale-spec 递归不再丢投影参数（与 stream 对齐）；F3 invoker 按次把 NativeCapture 候选经 native_sink 交回，applied receipt 携带到 owner，真 continuation policy：清单匹配且 PRESERVED → byte-true native_committed 冻结，不匹配（边界替换/DSML 提升）→ 丢弃 native IR-only 冻结，降级 → 拒绝冻结冷回退；F4 显式 AcceptedContribution 边界 `_finalize_accepted_contribution`（finalization 替换先于投影 commit，receipt 穿越替换不被丢弃贡献冻结）；F5 pending tail 条目携 span 归属（`_PendingWireItem`）、chunk 新增对齐 `item_spans`（checkpoint 双向往返），on_left_replaced 按 kept_set 退休 pending 与幸存 chunk 内的退休 span 字节；F6 soft_reset 显式 roll HistoryRoot incarnation（不再从长度推断自愈），reset 流程 retire LLMRuntime 托管投影 session，owner prepare 新增结构性守卫：lineage 冻结 span 不再 durable 即冷回退；`test_v3_af51d74_review_fixes.py` 12红→12绿，19 邻接套件 192+67sub 零回归 |
| `3eb036b` | N7 fixup | native_sink 仅传给 send 方法签名可接受的 invoker（收窄签名的子类调用时 TypeError 计为端点失败——全量暴露 3 个真回归 test_llm_runtime_ir stream，单跑 3/3 复现修复后全绿）；全量回归（1900125+本 fix）：3134 passed + 491 subtests + 7 skipped，25 failed = 3 个已修复真回归 + 19 节点 + 3 subtest 行与上轮全量完全同集，单跑 20/20 零复现（logs_flake_rerun_af51d74fix.txt）同族长跑争用归因 |
| `15196dd` | 主项1 | warm handoff 拆分：executor 在 begin_left_compaction fence 后取最终 LEFT ids 调 `_resident_compaction_replay_request(left_message_ids=...)`（anchor 前缀相等校验、anchor 必须完全在 L 内、不合规诚实冷回退）；test_v3_warm_handoff_split 5红→6绿，138 邻接零回归 |
| `3621a74` | 主项2 | prompt_cache 读证据确认（`_AnchorReadEvidence`：cached_input_tokens ≥ prefix_tokens 且更晚 sequence 才 confirm，resident-only，TTL；dffb210 边界内重建）；projection_session prepare 产出 spans（tail spans 重映射含 anthropic merge，否则显式缓存规划被绕过）；tests/test_v3_real_provider_e2e.py（gated PAL_V3_E2E，glm/deepseek/openrouter-luna 真跑通过） |
| 本 commit | 主项3 | **v2 物理删除 + two_segment 唯一模式 + 所有权收权**（详见 §3a/§4/§6） |

## 3. 设计要点

- **单一物理历史**：L/R 是 L1TurnStore 上的逻辑 cut 视图，无镜像历史（A03）
- **Compact 只替左**：capture_left 只读左段（I10），install 单段无 await 发布、R 逐字保留（I05/F04，活动 round 内容折入物理记录而非拒绝）
- **身份分离**：left_revision 不 fence R producer，仅 session incarnation（reset）fence（I09/X05/X06）
- **准入**：owner 即仲裁者（单活 run / 终态互斥 / L08 最小种子防环）；手动 /compact 用 gate 原语作 idle 门闸（准入不同、primitive 相同，MIGRATION）
- **projection rebase**：chunk 绑 semantic span，替换左段按 span 保/删，R 原 wire/native 字节保留；anthropic 用户接缝退 pending tail 复用 F2 机制

## 3a. v2 物理删除清单（本 commit，v2 从未上线，无兼容负担）

- **删除**：`ingress_staging.py` 整文件；`artifact_lease.py` 中 staged-records 分支（R08 L1 引用保活保留）；turn_executor v2 admission 块（ticket/no-progress ledger/candidate outbox/lease/Q12）；`compact_memory_async` whole-source 分支与 `CompactionSnapshot.capture` 调用；模式开关全套（`llm_compaction_mode` 配置、`_compaction_mode`/`_two_segment_compaction`/`compaction_gate`/`compaction_scope` 参数与属性）；bunshin worker carrier/gate；runtime_app ingress 自动接线；contracts 的 `compaction_no_progress`/`compaction_candidate_outbox` 字段；runtime 手动压缩的 staging 异常面；gate 文件 v2 语义测试（interrupt 取消 idle manual——CPT-01 后不取消；reset 直接撤票）与 x10/b09 ledger 测试；test_full_compaction_source/outbox/builder_mode_contract 三文件（fixture 迁 tests/test_compaction_fixtures.py）
- **保留（MIGRATION 依据）**：CompactionGate/Ticket 原语（「准入不同、primitive相同」）——手动 /compact 的 idle 门闸；interjection 两道门（auto 无票放行 R 增长、manual 有票堵队列）；R08 lease；手动 /compact 处理器全套（A06/A08/A10/X06/X07/X08）
- **所有权收权**：MemoryService 新增意图 façade `interrupt_compaction_for_turn`/`cancel_active_compaction`，runtime.py 不再三层深挖 history_root 内部；require_port 替代不存在的 get_port（见 §6.5）

## 4. 诚实边界（acceptance 账本同步 NOT_RUN）

0. **主项1/2 已交付**（15196dd warm 拆分、3621a74 读证据+spans+E2E）；主项3 v2 物理删除+模式收权已完成，全量回归 49 failed 归因：17 真回归已修（单跑复现+修复验证），10 个 prompt_cache_v2 astra/chain 失败为翻转预存阻塞（见 1；后续诊断层修复后余 6），余 22 与既有长跑争用族同集（抽样单跑全绿）
1. **astra chain prefix_preserved 10 失败（翻转预存）——诊断层已修，余 6 挂账归因收敛**：认识论定案（Nathan 2026-09-21，PLAN §7/I11/I12/F15 锚定）：marker 非证据、回执（usage cached tokens）是命中唯一确认、本地唯一主张是出站字节级相同；frozen 前缀 span 不随行是 §6.2「不重写稳定前缀」的设计结果非缺陷，错在 describe_request 以 span 存在性为枚举门槛（无 span 消息字节蒸发→项流不对称→假 prefix_changed）。修复（诊断层 commit）：字节自足枚举——会话容器项全量描述，span 降级为路径地图（未认领项继承无 span 消息 region，混合默认 history）；红先行契约测试 tests/test_cache_diagnostics.py（cold→projected 不对称仍报 prefix_preserved + 真字节漂移仍报 prefix_changed 护栏）。结果 10→6：explicit/hybrid chain 4 例全绿，cache 族零连带。**余 6 同一根族：harness 投影 observe/promote 闭环未闭合**（est 全程平坦 6458、cache_tail=False 从不 prepared；[compaction] 轮 4 后旧 history 项原样保留=从未 promote 进 L，summary 只追加报 history_appended 而测试期望 prefix_changed；[dynamic] 另含 encode 不声明动态 context 项——276B 无主项按字节真相进前缀比对）。修法属 encode/planning 层，超出「修诊断不修投影」切片，挂账 Nathan。**环境警示**：本机 user-site editable 安装（pal_v2→~/Documents/coding/Pal main@4daab5f）会劫持 import，worktree 测试必须 PYTHONPATH=src（曾致一次假绿误判，已纠正）
2. **引擎 whole-source 内部残留**：`service.compact` 的 source_stamp 分发与 `_compact_full_source`、compaction.py 的 `capture` whole-source 路径已无生产调用方（executor 只走 capture_left），但引擎内部与其直接测试（p6_perf test_a、runtime_compaction whole-source 族等 8 文件）未删——后续切片
3. **v2 flake 族干净环境重跑归因**仍未做（与 N6 轮同一挂账）；本轮全量的 22 长跑失败抽样单跑全绿同族
4. **H 族运行时项**：H02/H04/H06/Q04/B04-B08 未实现——账本 NOT_RUN
5. bootstrap 全量回归需 ≥1800s 超时（v2 既有纪律；本轮实测 1790s，后续建议 ≥2400s）

## 5. 验收证据

- v3 新测试族（终 HEAD 复跑）：history_root 33 / projection_two_segment 14+12sub / memory facade 7 / executor flow 3 / builder 2 / H01 1+12sub / budget facts 3——全绿
- N1→N4 新增套件：n1_root_lifecycle 12 / n11_followup 12 / n2_projection_fixes 5+5sub / n2_continuity 3 / n3_invoker 3 / n3_vertical 4（含 promote→compact 全链与 stale-left）——全绿
- 继承族逐 gate 抽查：runtime_compaction / full_compaction_source / hot_cache / memory / history / projection 全系 / bunshin harnesses / hosts_recovery——绿
- 全量回归（HEAD `3eb036b`，25:47）：**3134 passed + 491 subtests + 7 skipped，25 failed**。其中 3 个为本次引入的真回归（native_sink vs 收窄 invoker 签名，test_llm_runtime_ir stream）——单跑 3/3 复现、修复后套件全绿；其余 19 节点 + 3 subtest 行与上轮全量（695efbc）完全同集，单跑 **20/20 零复现**（logs_flake_rerun_af51d74fix.txt），与 3575e48/29a879f 三次同族，归因长跑资源争用/时序。不宣称全量全绿，干净环境重跑仍为最终归因步
- acceptance_status.json：41/72 PASS（每项带真实 node/command/exit/product_sha，由 fill_ledger_v3.py 逐 node 实跑复核后写入）；其余 31 项 NOT_RUN 且关键项带原因备注（无 fill 脚本冒充）；28 项收口矩阵实况见 §8

## 6. 全链路逼出的真 bug（本任务）

1. `compact_memory_async` two_segment 分支引用定义在其后的 `after_compact`（UnboundLocalError）——此前无测试真正驱动该入口，H01 矩阵首跑即暴露；retire 钩子移到模式分支前共享（`0695976`）
2. `right_turns()` intra==len 时把边界 turn 重复放回 R（G1 自审发现）
3. anthropic rebase 后 prefix 尾部 user 接缝不合并 → 退 pending tail 复用 F2（G2）
4. **N1 终态出口（已由 N1.1 关闭）**：一切提交后故障（打包异常、超时、外部取消）按 owner 终态归类——已 COMMITTED 则报 success+已接受 memory_result+驱动 rebase，不伪造空成功、不宣称「历史未变」；reset 已退休的 run 迟到收尾幂等不抛 StaleRun。曾描述的「wait_for 计时器恰在 commit 与 return 之间插入」窗口未单独证明可达（单 event loop 同步段内 timeout callback 不插入），且已被同一归类路径覆盖，不再作为独立边界
5. **get_port 假绿链（翻转首炸）**：eb25fdc/1900125 写的 two_segment 分支调用不存在的 `MainContext.get_port`；full_source 默认使其不可达，翻转即引爆：/interrupt AttributeError 崩、reset 侧 X04/X05 静默失效、F6 lineage 退休静默失效（原测试直调 llm runtime 方法，PalCore 侧 wiring 从未真跑，假绿）。修复：façade 收权 + require_port（不吞）+ 真实 context 接线测试
6. **stub 漂移两例**：hosts 的 `_BarrierEngine` 缺 `timeout_seconds/max_attempts`（v3 deadline 读不到→任务静默死→测试挂死；且 NameError 留下未完成 barrier task 卡死事件循环）；hot_cache fixture 的 canned anchor 只有单消息（v3 W02/W03 前缀相等校验下永不接合）——均改为与真实引擎契约对齐
7. **翻转暴露的连带**：v3 左装订不张 `context_epoch`（i11）；compact_memory_async v3 分支无视传入 service 参数强制 require_port（hot_cache 直调族全断）；`_promote/_prepare` 摸 port 无防御（stub context 崩）——均已修。architecture_skeleton 两断言按 v3 设计改写：seal 后 IR 即 provider-neutral，reasoning 字节保真由 wire envelope/投影 native 承载（Nathan 最初核心关切的 v3 形态）

## 7. 回退

分支独立，未动 main / v2 分支（历史可查）；未 push。two_segment 是唯一模式：回退 = revert 本 commit（v2 行为由 git 历史携带，不从本分支重建）。

## 8. 28 项收口矩阵实况（2026-09-21，对照 pal_v3_review_2014db4/TEST_MATRIX）

| 项 | 状态 | 证据 |
|---|---|---|
| N01 失败后可再试 | PASS | test_v3_n1_root_lifecycle（schema failure） |
| N02 取消只收自己的 run | PASS | 同上（cancel barrier） |
| N03 超期不安装 | PASS | 同上 + N1.1 构造期过期/等号边界 |
| N04 同 turn R 增长不死 L | PASS | 同上（intra-turn） |
| N05 真改 L 拒绝 | PASS | 同上（正文/元数据两负例） |
| N06 IN_PROGRESS 不入 L | PASS | 同上 |
| N07 旧 L 可回收 | PASS | 同上（weakref） |
| N08 handoff 保留 base | PASS | test_v3_n2_projection_fixes（三 shape oracle） |
| N09 native 原件重放 | PASS | 同上（kept native） |
| N10 executor 真实 keeper | PASS | 同上（populated R + epoch） |
| N11 共享 L reference | PASS | test_v3_n2_continuity_lview |
| N12 实际 profile 编码 | PASS | runtime plan 侧真实 wiring：binding 携带 validated capabilities，投影/冷路径同源（af51d74 F1） |
| N13 端点不符冷回退 | PASS | test_v3_n3_invoker_projection（mismatch drop）+ af51d74 F2（receipt applied=False） |
| N14 fallback/spec refresh 切换 | PASS | af51d74 F2：双端点 fallback fixture（投影不冻结）+ sync stale-spec 刷新保投影参数 |
| N15 spans/extra_body 保留 | PASS | n2 handoff wire-contract 对拍 |
| N16 不预填未来 A | PASS | vertical trace（冻结在 accept 后）+ af51d74 F4（边界替换后仅冻结 fallback） |
| N17 工具结果 commit 故障不冻结 | NOT_RUN | 待接 tool-delivery 接受路径 |
| N18 stream partial/length 不冻结 | PARTIAL | ERROR 轮 reject 已测；stream 细分 NOT_RUN |
| N19 handoff 输出只进 validator | PASS(component) | engine 套件（I14 既有） |
| N20 长活 turn 能压 | PASS | test_v3_n3_vertical_trace（promote→compact 全链） |
| N21 发送预算门 | PASS(component) | G4 B01/B03/Q03；B04-B08 NOT_RUN |
| N22 无预热/miss 不重发 | PASS | hot_cache 套件全绿（cache_epoch/replay_guard 语义在 v3 路径恢复）；warm 拆分 15196dd |
| N23 interrupt vs commit 竞争 | PASS | N1 cancel + compact_cancel_control |
| N24 rebase 失败禁旧投影 | PASS | stale-left 守卫测试 + af51d74 F6（lineage span 不 durable 冷回退） |
| N25 worker/owner 单写者 | PASS | owner 侧 prepare + 类型化下沉 + **receipt 授权 commit**（af51d74 F2：无 receipt/applied=False/attempt 不匹配均拒绝冻结） |
| N26 正常关闭恢复 | PASS(component) | projection_checkpoint 套件（含 span 恢复） |
| N27 宿主×shape×warm 真实 E2E | PASS(gated) | 3621a74 test_v3_real_provider_e2e（glm/deepseek/openrouter-luna 真跑，PAL_V3_E2E 门控）；luna tail 方言 anchor 读证据为 bounded follow-up |
| N28 最终矩阵+全量 | 本表+全量日志 | v2 删除后全量 3086+491+8sk/49f（1790s）：17 真回归修复后受影响 19 文件 299+29sub 全绿；10 astra 翻转预存：诊断层修复后余 6（observe/promote 闭环族，§4.1）；余 22 长跑族抽样单跑全绿 |
