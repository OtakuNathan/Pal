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
| `1e8b42d` | 主项3 | **v2 物理删除 + two_segment 唯一模式 + 所有权收权**（详见 §3a/§4/§6） |
| `95373ef` | I11 | cache diagnostics 字节自足枚举：span 降级为路径地图，未认领项按 region 继承；astra chain 10→6（余 6 挂账见 §4.1，本轮 M17 关闭） |
| `a1058c5` | N8 实现 | review 95373ef R1-R6/S1/S2 修复主体（详见 §9）：配置 digest 准入 / 替换边界剥 native / L 视图覆盖（seed_reference_ids）/ 头部 preamble + 真实 merge 坐标 / 全 wire 诊断枚举 / attempt owner-match 收尾 / eligible anchor 读侧 / shell 只编码 preamble |
| `a923583` | N8 测试 | `tests/test_v3_review_95373ef_fixes.py` 19 条（reviewer 草案 9 条修 fixture 不弱化断言 + M16/M18 + M04 正反 + M05 异常变体 + M06 迟到清理）；M17 迁移 `test_prompt_cache_v2_runtime.py` 真 v3 compaction 入口 + 字节真相退役断言；warm_handoff_split eligible reader |
| 本 commit | N8 证据 | DELIVERY §9 + 红基线/全量回归日志（引用同树测试 a1058c5+a923583） |
| 本 commit | N8 后续 | M12 seam 变体收口：test_completion_system_seam_paths_shift_with_the_head（20/20 绿），M12 PARTIAL→PASS；余下挂账排序见 §4.6 |
| `faf2f41` | H族收口 | Q04/B04-B08 实现收口（见 §4.4）：Q04 BUSY_RETRY 回执、B05 base_over_budget 终态+报因、B04/B06/B07/B08 诚实 executor/engine 节点；H04/H06 OBSOLETE、H02 挂账 |
| 本 commit | H族账本 | acceptance_status 46/72 PASS（fill_ledger 逐 node 实跑 @ faf2f41）；§4.4/§4.6/N21 同步 |
| `3a7bbe9` | H02+H03 | 优雅停止 owner 状态随 resident checkpoint 持久化（见 §4.4）：HistoryRoot.owner_state/restore_owner_state + memory runtime schema 2→3；H02 app 级 SIGTERM 链端到端、损坏 fail-closed；H03 旧 checkpoint 可读无残留 |
| 本 commit | H02账本 | acceptance_status 48/72 PASS（fill_ledger 逐 node 实跑 @ 3a7bbe9）；§4.4 同步；分支首次 push（refactor/two-segment-session-v3，不合 main） |
| `faee0dd` | C 收口 | review pal_v3_review_c9cb2d2 三项修复（C1 完整冷源 / C2 TEXT-ONLY 交接验收 / C3 恢复·重绑后一次 projection bootstrap）+ `tests/test_v3_c9cb2d2_review_fixes.py` 11 节点（详见 §10） |
| 本 commit | C 证据 | DELIVERY §10 + 全量回归日志 `logs_full_regression_c9cb2d2_review_fixes.txt`（22 failed 与已知族长跑集逐项相同） |
| `af400d9` | B1 收口 | review pal_v3_review_4b14ce4 B1：bootstrap coverage 覆盖整个已物化 L 视图（seed_coverage_ids 由同一次 encode 建立；恢复/重绑不再把已 promote 普通历史重编码进 tail）+ `tests/test_v3_4b14ce4_review_fixes.py` B02/B04/B05/B06 四节点 + C02 加固（详见 §11） |
| 本 commit | B1 证据 | DELIVERY §11 + 全量回归日志 `logs_full_regression_4b14ce4_review_fixes.txt`（22 failed 与前次长跑集逐项相同；3098 passed + 533 subtests） |

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
1. **astra chain prefix_preserved 10 失败（翻转预存）——诊断层已修，余 6 挂账归因收敛**：认识论定案（Nathan 2026-09-21，PLAN §7/I11/I12/F15 锚定）：marker 非证据、回执（usage cached tokens）是命中唯一确认、本地唯一主张是出站字节级相同；frozen 前缀 span 不随行是 §6.2「不重写稳定前缀」的设计结果非缺陷，错在 describe_request 以 span 存在性为枚举门槛（无 span 消息字节蒸发→项流不对称→假 prefix_changed）。修复（诊断层 commit）：字节自足枚举——会话容器项全量描述，span 降级为路径地图（未认领项继承无 span 消息 region，混合默认 history）；红先行契约测试 tests/test_cache_diagnostics.py（cold→projected 不对称仍报 prefix_preserved + 真字节漂移仍报 prefix_changed 护栏）。结果 10→6：explicit/hybrid chain 4 例全绿，cache 族零连带。**余 6 同一根族：harness 投影 observe/promote 闭环未闭合**（est 全程平坦 6458、cache_tail=False 从不 prepared；[compaction] 轮 4 后旧 history 项原样保留=从未 promote 进 L，summary 只追加报 history_appended 而测试期望 prefix_changed；[dynamic] 另含 encode 不声明动态 context 项——276B 无主项按字节真相进前缀比对）。修法属 encode/planning 层，超出「修诊断不修投影」切片，挂账 Nathan。**环境警示**：本机 user-site editable 安装（pal_v2→~/Documents/coding/Pal main@4daab5f）会劫持 import，worktree 测试必须 PYTHONPATH=src（曾致一次假绿误判，已纠正）。**[N8/M17 关闭（a923583）]** 余 6 已归因关闭：根因是测试侧 legacy `memory.compact` 捷径绕过真实 v3 promote/observe 闭环（est 平坦、cache_tail 永不 prepared、旧 history 项原样保留皆为该捷径产物）；迁移到真 v3 入口（左跑+切割+安装+rebase，transport 应答 schema 合法 summary）后全文件绿，并新增字节真相退役断言（压缩掉的左段 reasoning 不重放、存活段逐字节原样、summary 请求独立记账）。见 §9/M17
2. **[已收（本日后续 commit）] 引擎 whole-source 内部残留**：已物理删除——`CompactionSnapshot.capture`（whole-source 生产者）、engine 的 MemoryCompactRequest 回退安装链（acompact_transactionally/compact_transactionally/acompact/compact getattr 阶梯）改为诚实 RuntimeError 守卫（无 two_segment_run_id 的手搓快照无受支持安装路径）；`MemoryService.compact/_compact_full_source/compact_transactionally/acompact_transactionally/acompact/mark_compaction_cleanup_pending` 全删；`MemoryCompactRequest` 留具名墓碑（陈旧 import 响亮报错），port 接口四方法删；`pal.memory` 导出清理。**保留**：engine 的 `source_stamp` 分支（414/487）非 full-source 专属——capture_left 亦设置左戳，属「带戳快照」通用规则（S03/S04 超限分离）；`l1_source_stamp` 只读 façade。测试侧：删 4 整文件（test_compact_continuity / test_p6_fault_matrix / test_p6_perf_ab / test_compaction_diagnostics）+ test_p6_matrix_gaps 3 条 + test_runtime_compaction 28 条（22 引擎族 + 5 策略 + 1 事务）+ 空类 CompactionPolicyTests + 死 helper `_snapshot`/`_capture_warm` + skeleton 1 条 + bunshin 2 条 + warm_handoff 5 条 + gated 真引擎 1 条；迁移 2 条策略容错测试到真 v3 路径（begin_left_compaction + capture_left + compact_left 安装，比原 whole-source 马甲更真）；n11 的 MemoryCompactResult 改指 pal.memory。诚实边界：部分引擎通用契约（checkpoint 两万上限、重试不膨胀 attempts、三失败隔离等）原本只经 whole-source 马甲覆盖，删除后无 v3 等价单测——挂账后续按需补 v3 版
3. **[已收（本日后续 commit）] v2 flake 族干净环境重跑归因**：空闲机（ollama 空、无并发负载）worktree 全量重跑 1454s：**22 failed（19 FAILED + 3 SUBFAILED）与既定族完全同集**——第五次连续同集（3575e48/29a879f/af51d74fix/N8 主线/本次空闲跑），单跑 19/19+3sub 绿。归因定案：串行全量长跑的时序/顺序敏感（bootstrap-telegram / bunshin_sandbox / bunshin_v2_public / browser_cli / control_plane 族），非 ollama 内存挤压、非任何 v3 改动、非真回归；若未来要根治需逐测试隔离归因（事件循环/端口/环境变量泄漏类），非 v3 收口阻塞项。环境方法论记录：换 venv/克隆属过度工程——历次基线同环境同 PYTHONPATH 纪律，归因变量是机器负载而非依赖；宿主 shell 全局 PYTHONPATH 携带 pal_v2 src（劫持源），worktree 前缀纪律已压住
4. **[已收（faf2f41 + 3a7bbe9）] H 族运行时项分类收口**：Q04/B04-B08 已实现并有诚实 node（Q04=test_compaction_gate BUSY_RETRY 回执；B04-B08=tests/test_v3_b_family_runtime.py，真 executor/engine/owner，网络侧 fake）；B05 代码收口=engine 新增 `base_over_budget` 终态（空 source 探针仍超窗→零次生成、不删 base 凑窗）+ 手动 compact 明确报因。H04/H06 判 OBSOLETE（v2 从未上线：无旧 schema L1、无旧 staging 文件可迁，无诚实输入）。**H02/H03 已收（3a7bbe9，Nathan 开单）**：优雅停止的 resident checkpoint 现携带两段 owner 状态（incarnation/cut/left_generation/abandoned_run；memory schema 2→3，跨版本恢复 fail-closed），恢复进程看到完整旧/新 root、replay envelope 保留可重建 native 关联、活跃 compact run 作废不重跑、损坏 owner 状态 install 前拒绝；H03=关停写失败旧 checkpoint 仍可读无残留。爆炸半径：170+24sub（faf2f41）/232+24sub（3a7bbe9，18 文件含 bunshin/behavior/runtime_state）全绿
5. bootstrap 全量回归需 ≥1800s 超时（v2 既有纪律；本轮实测 1790s，后续建议 ≥2400s）
6. **N8 后续安排（2026-09-22，Nathan 授权安排）**：① M10 三路径 final-wire 并排单测（✅ 已收，tests/test_v3_m10_final_wire.py）；② whole-source 引擎残留物理删除（✅ 见 2）；③ acceptance_status 补账（✅ fill_ledger 逐 node 实跑）；④ 22 长跑族干净环境重跑（✅ 见 3）；⑤ H 族运行时项（✅ 见 4，Q04/B04-B08 实现+H04/H06 OBSOLETE；H02/H03 后续 3a7bbe9 收口）。M12 seam 变体已收（b80fd1b）

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
| N21 发送预算门 | PASS | G4 B01/B03/Q03 + B04/B05（faf2f41：B04 当前视图硬溢出拦截、B05 base_over_budget 终态）；B06-B08 见 test_v3_b_family_runtime |
| N22 无预热/miss 不重发 | PASS | hot_cache 套件全绿（cache_epoch/replay_guard 语义在 v3 路径恢复）；warm 拆分 15196dd |
| N23 interrupt vs commit 竞争 | PASS | N1 cancel + compact_cancel_control |
| N24 rebase 失败禁旧投影 | PASS | stale-left 守卫测试 + af51d74 F6（lineage span 不 durable 冷回退） |
| N25 worker/owner 单写者 | PASS | owner 侧 prepare + 类型化下沉 + **receipt 授权 commit**（af51d74 F2：无 receipt/applied=False/attempt 不匹配均拒绝冻结） |
| N26 正常关闭恢复 | PASS(component) | projection_checkpoint 套件（含 span 恢复） |
| N27 宿主×shape×warm 真实 E2E | PASS(gated) | 3621a74 test_v3_real_provider_e2e（glm/deepseek/openrouter-luna 真跑，PAL_V3_E2E 门控）；luna tail 方言 anchor 读证据为 bounded follow-up |
| N28 最终矩阵+全量 | 本表+全量日志 | v2 删除后全量 3086+491+8sk/49f（1790s）：17 真回归修复后受影响 19 文件 299+29sub 全绿；10 astra 翻转预存：诊断层修复后余 6（observe/promote 闭环族，§4.1）；余 22 长跑族抽样单跑全绿 |

## 9. N8 — review pal_v3_review_95373ef（基线 95373ef，R1–R6/S1/S2）收口实录（2026-09-22）

固定输入：`95373efff0fe23d4fc4f6292901e97bbe5250dfe`（reviewer 包 MANIFEST 固定）；修复树 `a1058c5`（实现）/`a923583`（测试）/本 commit（证据）。全程 `PYTHONPATH="$PWD/src:$PWD"` 且 `pal.__file__` 指向本 worktree（user-site 劫持警示 §4.1）。未 push、未合 main、未部署、无真实 provider 调用。

### R 项逐条

- **R1（同 ID ≠ 同 profile）已修**：`_endpoint_config_digest` 从活端点重算（capabilities/输出上限/spec revision 全入 digest），drift → 旧投影/旧 plan 冷回退或重编译；runtime 注入选择（cache-policy 代数计数）移出身份视图，无害 refresh 不再毁 lineage。反例 M01；正常对照 M02 后半（配置未变重试复用、不增探测）
- **R2（恢复/替换后 native 资格）已修**：真 LENGTH→continuation 恢复 receipt.native=None、冻结件带合并全文（M03）；finalization 替换在 F4 边界剥 native（M04 负例）；未改原件 byte-true 冻结（M04 正例）。诚实边界：observe 层未加「伪造 receipt 文本比对」守卫——按 HANDOFF「不三处补独立推测条件」，生产替换路径已被两道防线（F4 剥离 + call_ids 对账）全部闭合；手工伪造 receipt 的 API 滥用不构成产品反例
- **R3（一份 L 模型视图）已修**：rebase 安装 `seed_reference_ids` 为 L 覆盖，下轮 prepare 不再重注相同 standalone 种子（M07 sentinel 恰一次）；连续两次 compact 共享同一 raw→standalone 映射（M08）
- **R4（接缝坐标忠于实际 wire）已修**：preamble 只取头部 system/developer 连续段（中段 developer 按时间序留在会话流）；tail span 重映射用真实 merge 结果（M11 未合并→真 assistant 坐标 / 合并→block offset 对照）；continuity_target 与 cache/wire 同一 remapper（M12）。Completion system seam 变体由后续切片补入（test_completion_system_seam_paths_shift_with_the_head，全局坐标 + 标记不落错消息，一次过）
- **R5（诊断枚举完整 wire）已修**（95373ef 诊断层基础上补齐负例）：M13 嵌套 span 覆盖变化不误报 / M14 顶层 system 漂移不隐没 / M15 dynamic 子块不吞历史归属；P01/P02 正对照保留于 test_cache_diagnostics
- **R6（attempt 不失控）已修**：prepare→observe 间一切退出（cancel/接受异常/owner 异常）由同 owner finally 收自己的 draft（M05 两变体）；迟到清理 owner-match：不碰后继轮、不回滚已提交（M06）
- **S1（合法本地前缀可直接尝试）已定**：`eligible_anchor_request` 只查 scope/endpoint/TTL，`read_confirmed` 是诊断位不是许可位；confirmed 读者不变（hot-only 费用边界原样）（M09 + warm_handoff_split）
- **S2（性能收尾）已做**：shell envelope 编码只见 preamble，M16 证明 40 消息 shell 下单轮 encode 输入 ≤2 条消息。不承诺固定提速倍数

### M01–M18 实况（node 均在 tests/test_v3_review_95373ef_fixes.py，另注除外）

| 项 | 状态 | 证据 |
|---|---|---|
| M01 | PASS | RuntimeAndHistoryContracts::test_same_endpoint_profile_drift_invalidates_old_projection（实际发送对照见 M02） |
| M02 | PASS | ::test_sync_stale_refresh_recompiles_with_the_new_profile（真 stale→refresh 收 cap→单发重编译→applied=False） |
| M03 | PASS | ::test_old_attempt_native_cannot_replace_a_recovered_accepted_message（真恢复路径） |
| M04 | PASS | ::test_replaced_contribution_through_real_boundary_drops_native（负，经真 F4 边界）+ ::test_unmodified_provider_original_freezes_native_byte_true（正） |
| M05 | PASS | ::test_cancelled_ordinary_generation_closes_projection_attempt + ::test_failed_l1_acceptance_closes_its_own_draft_and_next_round_prepares |
| M06 | PASS | ::test_late_cleanup_never_touches_successor_rounds_or_committed_history |
| M07 | PASS | ::test_new_summary_occurs_once_after_real_rebase_and_continuity_view |
| M08 | PASS | ::test_consecutive_compacts_share_one_l_identity_mapping |
| M09 | PASS | ::test_unconfirmed_local_anchor_is_eligible_until_ttl_expiry + tests/test_v3_warm_handoff_split.py |
| M10 | PASS | tests/test_v3_m10_final_wire.py 三路径并排（成功：marker 恰一次 + 左段退役不重播；失败/取消：历史原封不动；R 原件含原生 tool call/result 逐字节保真；三路径工具零重执行）；成功路径物化走诚实冷编码（prepare 返回 None，与 N24 stale-left 守卫一致），continuity 视图的 summary-恰-一次由 M18 互补 |
| M11 | PASS | ::test_no_merge_uses_no_merge_coordinate_transform + ::test_merged_user_boundary_uses_merged_block_offset（full prepare） |
| M12 | PASS | ::test_continuity_target_is_remapped_with_cache_and_wire_paths + ::test_completion_system_seam_paths_shift_with_the_head（Completion system seam 全链：全局坐标 + 标记不落错消息；后续切片补入） |
| M13 | PASS | DiagnosticContracts::test_identical_wire_with_nested_span_coverage_change_is_preserved |
| M14 | PASS | ::test_top_level_system_drift_is_not_hidden_by_missing_span |
| M15 | PASS | ::test_dynamic_subspan_does_not_hide_unspanned_history_in_same_item |
| M16 | PASS | ProjectionSpanContracts::test_shell_encode_never_scans_the_whole_history |
| M17 | PASS | tests/test_prompt_cache_v2_runtime.py 全绿（真 v3 入口迁移 + 字节真相退役断言 + summary 独立记账）；§4.1 余 6 挂账关闭 |
| M18 | PASS(composed) | ::test_composed_trace_normal_recovery_install_next（normal→LENGTH 恢复→install→next，summary 恰一次）；单循环内再插同 ID spec refresh 未做（M02 独立覆盖），如实记录 |

### reviewer 草案与 TLA 边界

- `test_review_95373ef.py`（reviewer 未运行草案）在修复树实跑 6/9 过；3 条失败逐条定性 fixture 漂移（猜参直调内部 helper / 无 live lineage 即 rebase / 手工伪造 receipt），按「修 fixture 不弱化断言」修复后全部进入 a923583（断言面等于或强于草案）
- 本轮 spec/ 零改动。改动转移（attempt 开/关/迟到清理、native 资格、eligible reader）无既有 TLA 模型覆盖（spec/llm 为端点调用/缓存锚点/条目提交/L1 轮次，不含投影 attempt FSM）——按 HANDOFF 以产品测试为证；`eligible_anchor_request` 为纯增量读侧，PromptCache* 模型语义（frontier 推进需读证据、marker 不授权 dffb210）未动，旧 TLC 结果对其继续有效，未复制为新路径证据

### 本轮测试实况

- 红基线：reviewer 草案 9 条在 95373ef 树 9/9 红（logs_v3_review_95373ef_draft_red_baseline.txt）；修复后成稿 19/19 绿
- reviewer 草案原样实跑（修复树）：6/9 过，3 失败即上 fixture 漂移定性证据
- 宽域：projection/prompt_cache/cache/compaction/runtime/continuity/history/v3 族 515 passed + 82 subtests + 1 skip（62s）
- 全量回归（a1058c5+a923583 树，1356s）：**3134 passed + 491 subtests + 8 skipped，22 failed（19 FAILED + 3 SUBFAILED）**——与上轮长跑族（3eb036b 归因集）完全同集（上轮 3 个 llm_runtime_ir 真回归未复现）；单跑 **19/19 + 3 subtests 全绿（7.6s）**，第四次同族验证（3575e48/29a879f/af51d74fix/本轮）。运行中段受 ollama bge-m3 驻留（1.2GB）内存挤压降速，模型卸载后恢复，总时长仍在历史区间。不宣称全量全绿，干净环境重跑仍为最终归因步（§4.3 挂账继续）

## 10. c9cb2d2 定点复审收口（2026-09-22，review pal_v3_review_c9cb2d2）

固定输入：`refactor/two-segment-session-v3@c9cb2d250271192a06198abd587eb462f9000b5f`（reviewer 包 MANIFEST 固定），产品尾部 `3a7bbe9`。修复树 `faee0dd`（实现 + 测试），证据随本 commit。全程 `PYTHONPATH="$PWD/src:$PWD"` 且 `pal.__file__` 指向本 worktree。未 push、未合 main、未部署、无真实 provider 调用。

reviewer 结论三项（Request changes，C1 P1 / C2 P2 / C3 P2）逐条闭环：

- **C1 完整冷源（已修，core/compaction.py）**：`build_compaction_units` 不再对每条消息做 head/tail 字符投影（删除 `unit_text_limit` / `_bounded_source_text`）；合法内容整条进入冷源，是否装下由引擎真实 preflight 发送预算判定——超窗走既有 `source_too_large` / `base_over_budget` 终态并完整保留旧 L，不再靠源裁剪偷偷通过。证据：A01（10k 头 + 中间唯一约束 + 10k 尾、窗口足够 → 约束原样送达、无投影 marker）、A02 短源正对照、A03 超窗对照（READY 窗口 fixture 按实测请求字符数判定：完整源 = COMPACT_REQUIRED → 空源探针 READY → `source_too_large`；生成零调用、旧 L/R 逐 id 不变）。
- **C2 最终交接 TEXT ONLY（已修，core/compaction.py）**：唯一 generation-result 验收点在任何解析前拒绝 TOOL_CALLS finish 与 tool-call parts（即便文本是合法 checkpoint JSON）；重试沿原有 attempts/绝对时限；未完成调用不搬入 repair history（不设 repair_output）。证据：B01（TOOL_CALLS + 合法 JSON + ToolCallIR → 拒绝、零安装、turns 不变）、B02（同 parts 但 finish=STOP 亦拒绝；纯文本正对照安装）、B03（首次非法→二次合法：attempts=2、单次安装、retry 请求无 `compaction_failed_output`/`compaction_repair_request` 语义）。
- **C3 恢复/绑定后一次 projection bootstrap（已修，turn_executor + projection_session）**：`EndpointProjectionSession.has_materialized_content()` 区分「fresh/rebound（未物化）」与「已物化 stale」；mismatch 且未物化时以当前 canonical L 模型视图 + CURRENT R 经 `on_left_replaced` 冷重建一次——与 post-commit rebase 共享 `_install_left_replacement` 同一入口（不新增第三条恢复路径）；内容与 coverage 一起建立后才记录已消费 generation；已物化 lineage 保持严格拒用（不动 counter）。证据：C01a（focused 保存/恢复两轮重入，summary/历史恰一次）、C01b（app 级真实宿主 save/publish→restore + app 自有 executor 真实 PromptCompiler 路径两轮：`RESTORED SEED SUMMARY` 与各用户消息在 wire 上各恰一次、首轮即 freeze、无 stale/bootstrap-failed 诊断）、C02（切 endpoint binding：旧 native 不跨 provider、新投影从当前 L/R 建立、后继轮复用、发送数 == 轮数零预热）、C03（gen0 正例不退化；恢复后 bootstrap 恰一次、第二轮不重入；已物化 stale 继续拒用、显式 rebase 后才 engage）。

证据与命令（工作区 `~/Documents/coding/Pal-two-segment-v3`）：

- reviewer 草案 `test_review_c9cb2d2.py`（T01–T05，reviewer 未运行）：c9cb2d2 树实跑 3 failed / 2 passed（复现 C1/C2/C3）→ 修复树 **5/5 passed**。命令：`PYTHONPATH="$PWD/src:$PWD" python -m pytest <review 包>/test_review_c9cb2d2.py -q`，exit 0。
- 新仓库节点 `tests/test_v3_c9cb2d2_review_fixes.py`：**11/11 passed**（A01–A03 / B01–B03 / C01a / C01b / C02 / C03×2），同命令同前缀，exit 0。
- 受影响链复跑（修复树）：v3 链 14 文件 **107 passed + 12 subtests**；compaction 族 7 文件 **60 passed**；projection 族 13 文件 **133 passed + 74 subtests**；合计 **300 passed + 86 subtests**，全绿。
- 全量回归（`faee0dd` 树，1371.83s）：**3094 passed + 498 subtests + 7 skipped，22 failed（19 FAILED + 3 SUBFAILED）**——与空闲重跑基线（`logs_full_regression_idle_rerun.txt`）的 FAILED 集和 SUBFAILED 集逐项 diff 为空；无新增失败，全部为已知长跑时序/顺序敏感族（bootstrap-telegram / bunshin_sandbox / bunshin_v2_public / browser_cli / control_plane / bunshin_submission_errors / bunshin_v2_contract_protocol / bunshin_v2_verification）。passed 3094 vs 基线 3071 的增量对应间段新增节点（H02/H03、B 族与本节 11 节点）。不宣称全量全绿。
- 未运行 / 不主张：N27 真实 provider E2E（opt-in `PAL_V3_E2E` 门控，未设 → SKIP，不冒充已跑）；iqoo/slot.py 补丁（未获得确切 repo/branch/SHA，reviewer 与本轮均未 review）；TLA 未新增模型动作（本轮为源/验收/恢复复用修复，`on_left_replaced` 既有动作语义未变），未以旧日志替代新路径证据；SIGKILL/逐 compact 持久化/预热/durable inbox 均未引入（保持用户范围）。

## 11. 4b14ce4 复审收口（2026-09-22，review pal_v3_review_4b14ce4，B1）

固定输入：`refactor/two-segment-session-v3@4b14ce4542afd49c29871c207f03c15cbe2d7331`（reviewer 包 MANIFEST 固定），产品尾部 `faee0dd`。修复树 `af400d9`（实现 + 测试），证据随本提交。全程 `PYTHONPATH="$PWD/src:$PWD"` 且 `pal.__file__` 指向本 worktree。未 push、未合 main、未部署、无真实 provider 调用（B05 全 shape 走 prepare-only，零网络）。

reviewer 结论：C1/C2 关闭本轮发现；C3 判 PARTIAL，剩一个阻塞 B1（P1）。B1 已收口：

- **B1 症状（复现）**：`_install_left_replacement` 在 fresh/rebind bootstrap 时把**整个当前 L 模型视图**编码进 prefix，但该入口当年只为「刚 compact 完、L 只有摘要 seed」设计——coverage 只登记 standalone continuity 一个 id。恢复/重绑时 chunks 为空，于是 L 里已 promote 的普通 Q/A 不在 coverage 里，被 normal tail 再编码一次：summary 一份，普通历史两份。reviewer 草案 T01/T02 在 4b14ce4 树实跑 3 红（Q1/A1、POST_COMPACT_USER/ANSWER 各出现 2 次）。
- **修复（af400d9）**：物化与覆盖**同一次 encode 建立**——
  - `LeftReplacement.seed_reference_ids` → `seed_coverage_ids`：seed encode 实际接收的**每一条**模型视图消息的 id（post-compact rebase 仍等于 standalone 单条；恢复/重绑 bootstrap 是整个 L 视图）。仍是派生 coverage，不伪造 provider round chunk、不新增权威存储。
  - `EndpointProjectionSession._l_reference_ids` → `_l_coverage_ids`：`on_left_replaced` 整体置换、`bind`/`retire` 清空、`covered_message_ids()` 读取语义不变。
  - `TurnExecutor._left_view_seed` 从它交给 encode 的模型视图计算覆盖集（standalone 替换坐标保留，canonical id 不靠字符串去重进入覆盖）；三个无 seed 的 fallback 分支如实声明它们实际编码的 id。
  - 已 materialized 的 stale 投影继续严格拒用（C3 负例未动）；`_left_revision` 不靠改 counter 放行。
- **不变量核对**：B-I1 物化与声明同源（同一 encode）；B-I2 正常请求 L/R 各一次、顺序=当前合法模型视图（B04/B05/B06 计数断言）；B-I3 恢复/重绑只重建派生表示（零新增 provider 调用，B02/B06 发送数==轮数+compact）；B-I4 fresh 可初始化、materialized stale 仍拒、无 counter 放行（C03 两测 + B06 退休断言）。

证据与命令（工作区 `~/Documents/coding/Pal-two-segment-v3`，修复树 `af400d9`）：

- reviewer 草案 `test_review_4b14ce4.py`（reviewer 未运行）：4b14ce4 树 **6 failed / 8 passed**（T01 请求 2/3 各 2 次、T02 两请求 POST_COMPACT 各 2 次）→ 修复树 **8 passed + 12 subtests**（exit 0）。
- 红基线（本仓库节点，终版测试集）：把两个源文件临时 `git checkout HEAD~1 --` 回 4b14ce4 态后，`tests/test_v3_4b14ce4_review_fixes.py` 与加固后的 C02 共 **17 failed**（B02 4 subfail / B04 4 / B05 3 shape / B06 4 / C02 2），同时 15 passed + 18 subtests；`git checkout HEAD --` 恢复后 **15 passed + 35 subtests 全绿**，工作树核对无损。
- 新增 `tests/test_v3_4b14ce4_review_fixes.py`（矩阵 B02/B04/B05/B06，4 节点）：
  - B02：真实 app checkpoint 保存/恢复（compact 后又完成并 promote 普通问答）+ app 自有 executor 真实 PromptCompiler 两轮；summary / POST_COMPACT Q/A / 恢复后首问在两次 payload 各恰一次；projection 接入（left_revision=1、chunks=2）。
  - B04：seed + 两组已 promote 普通历史，第二组与当前 R 同属 logical turn T；恢复后一轮 payload 六个 sentinel 各一次；cut 合法（promote 后 L=[seed,q1,A1,q2,A2]、R=空，恢复后 L 逐 id 相同）。
  - B05：L 含完整聚合 tool call + result（`append_l1_tool_result`），R 含新工作；**三种受支持 shape** 逐一走真实 `_prepare_turn_projection`（真实 LLMRuntime plan/codec、真实 bootstrap），**只 prepare 不发送**：result sentinel 恰一次、call id 恰两次（声明+结果链接）、顺序 call<result、各 shape 用各自 codec（tool_calls/tool_call_id、function_call、tool_use/tool_result）；tail 恰为当前 R 一条。不声称工具会/不会重执行——本节点根本没有任何发送。
  - B06：bootstrap → 普通运行 → 第二次真实 compact（真引擎）：bootstrap 轮六 sentinel 各一次；二次 compact 后旧 L coverage 随旧 L 退休（covered 集合不再含 b06-q1/q2/q3），新摘要与 compact 后的新 R 各一次、被退休内容 0 次；发送数==3（两轮+compact），零预热。
- B01 = 加固后的 `test_C02`（同轮 c9cb2d2 文件）：第二、三次 payload 中 "REBIND SEED SUMMARY"/"First round."/"REBOUND_1" 各恰一次。
- B03 = 既有对照未弱化：C01b（seed-only app 正例）、C03 两节点（gen0 正例不退化 + bootstrap 恰一次 + materialized stale 仍拒）继续全绿。
- 受影响链复跑：v3 链 15 文件 **111 passed + 47 subtests**；compaction 族 7 文件 **60 passed**；projection 族 13 文件 **133 passed + 74 subtests**；全绿。
- 全量回归（`af400d9` 树，1377.37s）：**3098 passed + 533 subtests + 7 skipped，22 failed（19 FAILED + 3 SUBFAILED）**——FAILED 集与 SUBFAILED 集与 `logs_full_regression_idle_rerun.txt`（已知长跑族）逐项 diff 为空，无新增失败；passed 增量 3098-3094=4 为本轮 4 个新节点，subtests 增量 533-498=35 为 4 节点 33 sub + C02 加固 2 sub。不宣称全量全绿。
- 未运行 / 不主张：N27 真实 provider E2E（opt-in `PAL_V3_E2E` 门控未设 → SKIP）；iqoo/slot.py 补丁（仍未获得确切 repo/branch/SHA，未 review）；TLA 未新增模型动作（物化/coverage 表示不变量，未引入新 ownership 状态转移；`on_left_replaced` 既有动作语义未变），不以旧日志替代新路径证据；SIGKILL/逐 compact 持久化/缓存预热/durable inbox 未引入。

## 12. 7d182fd 定点复审收口（2026-09-23，review pal_v3_review_7d182fd，S1 跨 cut user seam）

固定输入：`refactor/two-segment-session-v3@7d182fdb32a5aa080d4dc40ff639787b487f5938`（reviewer 包 MANIFEST 固定，产品尾部 `af400d9`）。全程 `PYTHONPATH="$PWD/src:$PWD"`，`pal.__file__` 指向本 worktree。未 push、未合 main、未部署、无真实 provider 调用（fake 网络帧仅产品解码器格式）。

reviewer 结论：原 B1 关闭；新开 S1·P1——Anthropic user seam 合并时旧 L 字节被重新归给 R，下一次左替换无法退休它们。

- **S1 症状（实跑复现）**：
  - 组件级：reviewer 草案 `test_review_7d182fd.py`（reviewer 仅 py_compile）在 7d182fd 树实跑 **2 failed / 2 passed + 3 subtests**——T01 旧 L 文本 `RETIRED_LEFT_INSTRUCTION` 借存活 R chunk 的混合 user item 复活；T02 旧 `tool_result` 成 orphan，`prepare` 直接被工具协议校验拒（`ProjectionContractError`，比 review 说的「退 cold」更硬）；T03 三 shape 对照 + T04 interrupted turn 合法性对照绿。
  - 产品级（本轮新纵向）：bootstrap（L 以 interrupted user-only turn 结尾）→ 接受 R 回答 → 只 compact 左侧（R 从不 promote，`_rebase_projection_after_left_install` 同 post-commit 接口）→ 下一轮 anthropic wire 中旧 L 文本复活 1 次（`Old interrupted ask` 在 round2 payload count=1）。**产品路径工具变体到不了**：`_ensure_assistant_closure`（turn_ir）保证 L1 turn 以 TOOL 结尾必加 assistant 桥，L 视图永不以 tool_result 收尾——工具 orphan 变体由组件级 T02 覆盖，产品级由文本变体覆盖，两者同一机制（空 span 在 seam 合并时被 R 的 span 吞掉）。
- **修复（块级归属 + seed L-owned 标记，同一批）**：
  1. **per-BLOCK ownership 贯穿**：`ProjectionChunk.item_block_spans` / `_PendingWireItem.block_spans` / `_ActiveRound.prepared_item_block_spans` / `_prefix_item_block_spans` 四条轴线与既有 per-item span 平行。`prepare` 组装 conversation 三元组；`_merge_anthropic_user_boundary_pairs` 的块级归属按 content 顺序拼接（merge 就是 `[*left.content, *right.content]`，块序即来源序）；`observe_commit` 的 accepted 物化块级归本 commit span，尾部 trim 带走块级；checkpoint 序列化/恢复同步（缺字段 → () = legacy 整项规则，宽松降级不拒快照）。
  2. **seed 归属修正**：`on_left_replaced` 的 seed items span 从无条件 `()` 改为 `seed_coverage_ids`（B1 同源坐标）。空 span 语义只剩 legacy（保守保留）；有 coverage 的 seed 字节在**下一次**左替换时退休——修复了 pending 直通路径（bootstrap 尾部 user 不再跨 replace 幸存）与 seam 路径的共同根因。空 coverage（legacy 调用方）保持旧保守行为。
  3. **块级退休重建**：新 `_retire_wire_item(item, span, blocks, kept)`——有对齐块级时逐块判定（空 span/⊆kept 留，其余删），部分退休则**重建 item** 只留存活块，新 item span = 存活块 span 去重并集；无块级轴线时维持 F5 整项规则。`on_left_replaced` 的 chunk 过滤与 pending 过滤统一走它。不重编码 frozen prefix、不伪造 thinking/signature、无新 coordinator/终态/ticket（PLAN 边界全守）。
- **不变量核对**：S-I1 seed 物化有可追溯归属（coverage 即 span，空 span 不再是永久无主）；S-I2 merge 不改源归属（块级拼接，两侧各自保留）；S-I3 替换后退休 L 原文消失、R 文本/工具结果各保留一次（T01/T02/纵向 round2/3 计数断言）；S-I4 wire 跨 cut 不拆 semantic 工具组（T02 修复后 tool_use 随纯 L item 整项退休、tool_result 块级退休，存活请求无 orphan——工具协议校验通过）；S-I5 覆盖集仍对应当前物化（`_l_coverage_ids` 机制未动，B1 测试族全绿无重复 append）。
- **不主张**：没有把组件测试等同宿主 E2E；没有运行真实 provider；全量回归的既有 22 项失败不属于本轮必修（只要求失败集合不新增）；TLA 无新增持有/转移状态机故未新增模型；checkpoint 独立 codec 的完整 rebased prefix roundtrip 仍未宣称（新字段是 best-effort 增量，缺省退化 legacy 整项规则）。

证据与命令（工作区 `~/Documents/coding/Pal-two-segment-v3`）：

- reviewer 草案：7d182fd 树 **2 failed / 2 passed + 3 subtests**（T01/T02 红）→ 修复树 **4 passed + 3 subtests** 全绿（exit 0）。
- 新增 `tests/test_v3_7d182fd_review_fixes.py`（矩阵 S02 纵向 + S03 openai 对照，B05 延伸）：修复前 anthropic 纵向红在 `Old interrupted ask` 复活（SUBFAILED round=2）→ 修复后 **2 passed + 22 subtests** 全绿。节点内容：B05 的 LEFT 完整工具组（interrupted 收尾，无桥）+ interrupted user-only turn 折入同一尾部 user item（S01 文本 + S02 工具同场景）；接受 R 回答后只 compact 左侧（R 从不 promote），断言 round2/round3：旧 call/result/旧摘要/旧 interrupted 文本 0 次，新摘要/R Q/A/下一问各 1 次，无 stale/prepare_failed/rebase_failed/bootstrap_failed 诊断（不靠 cold 回退），round3 继续 warm（chunks>0、left_revision=2）；openai_completion 对照同退休/保留（无 seam 亦无回归）。
- 受影响链复跑：reviewer 草案 + 纵向 + checkpoint 族 + B 族 + C 族（5 文件）**28 passed + 62 subtests**；projection/native/v3 聚焦族（21 文件）**206 passed + 79 subtests**；全绿。
- 全量回归（修复树，1359.85s）：**3100 passed + 557 subtests passed + 7 skipped，22 failed（19 FAILED + 3 SUBFAILED）**——FAILED/SUBFAILED 集与基线 `logs_full_regression_4b14ce4_review_fixes.txt` 逐项 diff 为空，无新增失败；passed 增量 3100-3098=2、subtests 增量 557-533=24 均为本轮新测试节点（S02 纵向 + S03 对照，2 tests + 22 subtests），不宣称全量全绿。日志 `logs_full_regression_7d182fd_s1_seam_fix.txt`。



## 13. 58eaadf 复审修复：指导可见性与冻结断点映射（2026-09-23）

基线 `58eaadf`，本节工作尚未提交、合并或激活。无真实 provider 请求。

- **过期指导退出投影**：原检查只验证 frozen id 仍在 L1，导致已被正常视图过滤的 turn-scoped 指导仍被重放。现在对照 effective request 的可见 context id；已覆盖指导失效时，使用既有 rebind 边界重建一次。原始 L1 不改写，同一可见视图重复 prepare 不重复换代。恢复/重绑的左段 bootstrap 同样过滤过期和 excluded context，同时保留 compact source 的既有 continuity 替换。
- **冻结位置索引随字节保留**：codec 产生的 cache targets、wire paths、continuity target 按 item 相对坐标保存；冻结、pending、接缝合并、逐块退休和 checkpoint 均携带索引。最终组装时重定位，再按实际 payload 计算前缀描述。不重新编码冻结历史，不猜测断点位置；native accepted output 沿 codec 既有 target 规则映射。无 ACK/controller/收益策略改动。
- 回归 `tests/test_projection_visibility_cache.py`：原两条反例在基线实跑失败；新增三 shape 地址保留、恢复后左替换、Anthropic pending 合并/部分退休、explicit 三轮 T/P/C、bootstrap 过期指导、hoisted system 地址恢复等覆盖。
- 验证（均 `PYTHONPATH="$PWD/src:$PWD"`）：投影/v3 最终套件 **257 passed + 145 subtests，1 skipped**，日志 `/tmp/pal-v3-projection-final-tests.txt`；压缩/历史 owner/checkpoint 相关 **95 passed + 24 subtests**，日志 `/tmp/pal-v3-visibility-final-tests.txt`。较早的投影/v3/cache/prompt_cache 联合回归 **358 passed + 145 subtests，1 skipped**，日志 `/tmp/pal-v3-visibility-cache-tests.txt`。这些集合有重叠，不相加作为总数。
- 本轮未跑全仓、macOS、真实 provider 或新增 TLC；不将既有全量 22 项失败宣称为已解决。修复复用既有 rebind/commit/rebase 生命周期，位置索引为派生数据。

## 14. Lossless L1 and post-admission round freezing (2026-09-23)

This section supersedes earlier claims that round/turn closure retires reasoning.
Changes remain on `refactor/two-segment-session-v3`; no runtime activation,
commit, push, or paid provider request was performed.

- L1 retains accepted reasoning, native replay, opaque continuation fields,
  exact argument strings and full tool results. Codec capture reaches the
  accepted L1 message even when a provider hook clears its temporary replay.
  Textual DeepSeek tool adaptation keeps both the legal executable inventory
  and original source evidence; abort/recovery repairs only revoked calls.
- After preflight/conditional compact, the owner captures a closed R prefix.
  SDK response admission (headers, before nonstream body parsing) or Bunshin's
  provider-start event delivers an idempotent submission receipt on the owner
  loop. Only the captured prefix moves to L. Send failure, reset, changed
  input and duplicate receipts cannot promote new work. Later output stays R;
  generation failure after admission does not undo the already submitted input.
- Automatic compact replaces existing L only. Manual compact may archive a
  settled closed tail, but cannot absorb active unsent work. Checkpoint schema
  4 preserves replay and the cut; schemas 1/2/3 remain readable. Legacy snapshots
  without an owner cut initialize L from closed imported turns.
- Model-selection UI/tool affordances recommend successful compact before
  switching. Incompatible opaque continuation fails explicitly instead of
  silently deleting reasoning. Cache modes and eager chase-tail are unchanged.
- A legal L/R cut may split a projection round between sent input and newly
  accepted output. Per-message item/block ownership now permits that split,
  retires only the covered wire content and its cache addresses, and keeps
  surviving native output. Ambiguous legacy ownership still fails explicitly.
- Projection remains disposable: restart/rebind/bootstrap rebuild from L1.
  The 100-tool-round regression verifies unchanged serialized message values
  through settlement, restore and a cold projection rebuild; all three codec
  shapes also retain their settlement/recovery replay tests.
- The LLM TLC script checks six models, including new `RoundSubmission`.
  All six passed locally. The new bounded model explored 1,277 generated /
  420 distinct states; three deliberately broken configurations found the
  expected counterexamples (early freeze, R loss during compact, reasoning
  deletion). These are local lifecycle proofs, not claims about provider cache
  hits or all possible SDK/provider behavior.

Validation used `PYTHONPATH="$PWD/src:$PWD"` on Linux / Python 3.13. Source
compile and whitespace checks passed. No macOS or real-provider test was run.

Final validation evidence (counts are separate runs, not an additive total):

- `core-a`: **1166 passed, 6 skipped, 121 subtests passed**;
  `/tmp/pal-lossless-core-a-verified.txt`.
- `bunshin-a`: **464 passed, 77 subtests passed**;
  `/tmp/pal-lossless-bunshin-a-verified.txt`.
- `bunshin-b`: **478 passed, 124 subtests passed**;
  `/tmp/pal-lossless-bunshin-b-verified.txt`. The earlier sandbox timeout also
  passed standalone on both the original baseline and modified tree.
- Core B was split after the slow bootstrap/repository tests: the initial run
  passed **205 tests** before manual interruption, covering the first two files
  and part of the remaining selection (`/tmp/pal-lossless-core-b.txt`).
  The remaining 78 files were rerun separately against the final implementation:
  **859 passed, 1 skipped, 240 subtests passed**
  (`/tmp/pal-lossless-core-b-verified.txt`).
- Final projection/compact regression: **151 passed, 74 subtests passed**
  (`/tmp/pal-lossless-projection-last.txt`); gate/cancel/artifact regression:
  **26 passed** (`/tmp/pal-lossless-gates-final.txt`).
- Native output surviving compact + restart: **1 passed**;
  precise vs ambiguous partial-chunk boundary checks: **2 passed**.
  The 100-round test is also included in the final Core B remainder.
- TLC evidence: `/tmp/pal-lossless-llm-tlc.txt`; deliberate mutation logs:
  `/tmp/pal-round-early.log`, `/tmp/pal-round-erase_right.log`,
  `/tmp/pal-round-strip_reasoning.log`.

## 15. Main-to-v3 final review (2026-09-23)

Reviewed the accumulated branch against local `main` (`4daab5f`), including
projection ownership, lossless L1 replay, request admission, left-only compact,
checkpoint migration, cancellation and the resident/Bunshin transport paths.
The final follow-up fixes are:

- Chat stream null placeholders no longer overwrite accumulated text,
  reasoning, call IDs, function names or argument strings.
- Chat `reasoning_details` retains every received chunk in order instead of
  overwriting earlier chunks. This follows the streaming sequence described in
  [OpenRouter's reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
  The `reasoning` alias also participates in the incompatible-model guard.
- Memory checkpoint schema 1 (the current main format) can enter the existing
  legacy migration path. A coordinator-level regression checks that settled
  imported history becomes L while active input remains R.
- Scheduled channel admission again releases its ingress reservation in a
  `finally` block. Preparation failure and cancellation cannot permanently
  prevent memory maintenance.

New null-delta, reasoning-chunk and schema-1 regressions reproduced their
failures before the fixes. The ingress regression likewise reproduced the
leaked reservation on both failure and cancellation.

Offline verification on Linux / Python 3.13, with worktree `PYTHONPATH`:

- Projection/v3/history/continuation suite: **340 passed, 1 skipped,
  162 subtests passed** (`/tmp/pal-v3-final-review.txt`). This run precedes
  the follow-up fixes above; the affected paths were retested below.
- Final lossless/codec/recovery/checkpoint/recall suite: **94 passed,
  61 subtests passed** (`/tmp/pal-v3-review-fixes.txt`), including the
  100-tool-round replay through settlement, restore and cold rebuild.
- Compact/cancel/artifact/recovery/Bunshin transport suite: **66 passed,
  2 subtests passed** (`/tmp/pal-v3-review-compaction.txt`).
- Endpoint/runtime/hooks/FD ownership: **82 passed, 11 subtests passed**
  (`/tmp/pal-v3-review-transport.txt`).
- Final ingress/compact-gate/cancellation suite: **31 passed,
  2 subtests passed** (`/tmp/pal-v3-review-ingress.txt`).
- All six LLM TLC models passed (`/tmp/pal-v3-review-tlc.txt`).
- `git diff --check` passed. Earlier full CI-batch evidence is in section 14;
  the above overlapping suites are not an additive test total.

No remaining blocker was found within the reviewed paths. This establishes
local preservation and lifecycle behavior, not upstream cache-hit guarantees.
No paid provider calls, macOS checks, merge to main, push or runtime activation
were performed during this review.
