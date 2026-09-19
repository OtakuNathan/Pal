# LLM Projection Refactor v2 — 交付报告

**分支：`refactor/llm-projection-session-v2`（worktree `~/Documents/coding/Pal-projection-v2`）。**
**基线：`7e0b1f74`。安全分支：`safety/llm-projection-before-v2-7e0b1f7`（本地）。**
**日期：2026-09-18。执行者：Pal。main / 线上 Pal / 生产配置全程未动。**

> **交付状态修正（2026-09-18 外部 review 后）：** 本分支的交付状态是
> **“新组件旁路原型 + native 捕获接线”，不是“新实现全部完成，只差 P7 部署”。**
> 请求热路径（`endpoint.py::_iterate()`）仍走 per-message ReplayEnvelope
> 编码；EndpointProjectionSession / joint checkpoint / Bunshin owner 未进入
> 实际请求与恢复链。原 PLAN 的 P4（resident 集成）、P5（Bunshin/Manager
> proxy 集成）、P6（旧热路径清理）在分支中完成的是**组件层与隔离测试**，
> 不是集成交付；本报告原阶段命名与 PLAN 同名不同义，易波误读，特此修正。
> 集成部分仍待做，完成前不得作为可切换实现验收。详见“外部 review 修正记录”。

## 分支提交

| commit | 内容 |
|---|---|
| 706b1df | P0 基线：环境、三项预检确认、分文件套件基线、续接契约矩阵 |
| d55799d | P1 形式化：SANY/TLC 首次真实运行；修复方案包模型三处缺陷 |
| 90b48ad | P1 类型：projection_contracts（构造期不变式） |
| 978f95d | P2：结构化续接契约 + hook 前 native 捕获 |
| c0cbeca | P3：会话级 projection owner（receipt 驱动 chunk 冻结） |
| fa6d123 | P4：checkpoint 原子编解码 + invoker 捕获接线 |
| 3ef4c9f | P5：多 role 隔离与 worker 重启续接（receipt 台账跨重启） |
| (P6) | 收口：file_read 死代码清理、前缀摊还缓存、微基准、本报告 |

## 已实现（代码在分支上）

1. **类型层** `llm/projection_contracts.py`：identity/binding/cursor/receipt/round/
   PreparedRequest 全链构造期校验，§4.2 禁止组合不可表示。
2. **续接契约** `llm/continuation_policy.py`：三 shape 结构化验证（signature/
   encrypted_content 缺失 → Degraded；未知块/条目 → 显式 issue；坏 payload →
   Unsupported）。不按厂商名猜必需字段。
3. **native 捕获** `llm/native_capture.py`：codec 与 response hook 之间的直通
   捕获器；DeepSeek hook 剥信封的丢失点已被测试钉死并绕过。
4. **会话 owner** `llm/projection_session.py`：单写者、receipt 驱动 frontier、
   chunk 冻结（含 anthropic 尾部 user 修剪——源码验证的合并行为使 user 结尾
   不是前缀稳定边界）、幂等/冲突/迟到 receipt、切换销毁旧 lineage、
   required-native 缺失显式 ContinuationUnavailable。
5. **checkpoint** `llm/projection_checkpoint.py`：scope/binding/generation/
   frontier/native/receipt 台账同快照；拒绝缝合错代；legacy 快照走新生代。
6. **invoker 接线** `endpoint.py`：decode 经 NativeCapture（语义流不变），
   `LLMAttemptResult.native_payload_json` 附带捕获产物（新增可选字段）。

## 已运行验证（真实执行的命令与结果）

- **TLC**：single 8340/3818、isolation 92017/26244 全过（distinct 状态数与包内
  Python 模型完全一致）；mutant 以 `DraftAligned is violated` 失败（符合设计）。
  日志 `tlc/`，jar sha256 `936a2620…`，Java 21.0.10。模型修复三处（嵌套量词 ×2、
  absent 字段判别）有源码级注释。
- **定向测试**（每次改动的爆炸半径）：P1 类型 17、P2 捕获 9、P3 会话 10、
  P4 checkpoint/接线 7、P5 隔离 5，全绿；llm 全族回归 190 passed + 60 subtests
  + 6 skips（real-integration 按设计跳过，无付费调用）。
- **微基准**（本机 Pi，OPENAI_COMPLETION，800 消息 400 轮）：构建 3.82s；
  稳态增量 prepare 17.4ms（尾部 1 条）；全量 encode 14,655ms；**840 倍**；
  切换后一次性全量重建 14,775ms。残留 O(n) 为 payload 序列化（json.dumps +
  thaw_json），在 PLAN §5.4 声明的包络内（"不承诺端到端 O(tail)"）。
- **全量套件（P6 gate）**：分文件隔离跑，结果见下表。

### 全量回归（branch @ P6，分文件隔离）

| 项 | 基线（P0） | 分支（P6） |
|---|---|---|
| 文件 | 141 | 146（+5 个新测试文件） |
| rc=0 | 139 | 144 |
| 通过测试 | 2557 | **2605**（+48） |
| 非零 | 2（已知环境项） | 同 2 项，完全同款 |

非零项与基线一致：`test_package_installation` 1 failed（主 checkout 复现过的
setuptools/_vendor 环境性失败）与 `test_tool_schema_properties` 模块级 skip
（缺 hypothesis_jsonschema）。**零回归，全部新增测试通过。**

## 静态推导（有测试支撑的类型/构造保证，未做端到端运行验证）

- resident 与 Bunshin role 会话共用同一实现，隔离由 per-instance 状态 +
  scope 身份保证（S20/S13/S21/S22 会话层测试覆盖；未接 Manager proxy 全链路）。
- 断电/崩溃半写的拒绝路径（frontier 超前 L1、截断 native、错 schema）由
  构造与 checkpoint 测试覆盖；真实进程级崩溃注入未做。

## 外部 review 修正记录（2026-09-18）

`~/Documents/coding/pal_branch_review_2026-09-18`（REVIEW.md + 13 个产品 API 级反例
草案）核实八项问题全部属实，已在分支修复：

| # | 问题 | 修复 |
|---|---|---|
| R2 | PreparedRequest 只保留 items，丢 system/tools/policy | prepare 保留 codec 全部外壳字段（新增 `request_shell` 参数；Anthropic system 回归测试钉住）；零 tail 时复用 shell 不调 codec |
| R3 | chunk 封存的是 prepare 输入而非已接受输出 | observe_commit 新增物化：native payload 按 shape 抽取为 wire items，或 `accepted_messages` IR 经 codec 编码；repair 的 calls/results 同样物化 |
| R4 | requires_native/attach/native_committed 三者不闭环 | commit 验证 native 材料存在性与 call inventory 一致性；requires_native 缺失拒绝；布尔自称不再生效 |
| R5 | restore 后 frontier 非零但 prefix 空，旧历史丢失 | snapshot 持久化 chunk 链，restore 重建 prefix 并验证链接到 frontier；“非空 frontier 无 chunks”拒绝 |
| R6 | 一致性 gate 只查“不超前”；fence 被改写为 0 | epoch 必须相等（拒跨 compact 拼接）、同位必验 digest；fence 原样保存/恢复；committed/chunk 链验证 |
| R7 | 构造保证未挡非法路径 | attach_native 验开放轮 + binding（shape/endpoint/model）；chunk 公开快照深冻结（改即 TypeError）；PreparedRequest.build 拒悬空 tool calls；owner fence 单调 |
| R8 | inventory 空比较被短路跳过 | 三 shape validator 无条件双向比较 inventory |

反例测试已合入 `tests/test_projection_review_adversarial.py`（13 项 + 3 subtests，
断言强度未削弱，仅 output 形态适配 codec 实际结构）。

**性能数字（840x）降级为合成场景观察**：在完整请求等价、恢复与 native
正确性达成前，不作为验收结论；旧 finalize_cache_spans 的累积前缀开销
是可独立测量的旧成本，benchmark 需在集成后重测。

## 第二轮外部 review 修正（2026-09-19，针对 ae04397）

`pal_branch_review_2026-09-18/pal_projection_review_ae04397` 五项指控全部属实，已修：

| # | 问题 | 修复 |
|---|---|---|
| F1 | system 首轮存活、增量轮丢失；developer 位置投影在独立 tail 编码下错判；零 tail 忽略新 shell；restore 后零 tail 不可用 | shell/tail 彻底分离：request_shell.messages = preamble，每次 prepare 原样编码外壳（system 跨轮稳定、per-request budget 生效、零 tail/restore 后均可用）；tail 独立编码带显式边界标志 ShapeContext.has_conversation_prefix（Completion codec 位置判定改用）；空数组 fallback（"Continue."）用 codec 自身 spans 排除 |
| F2 | anthropic tool result 被 trim 后消失但 frontier 已跨过；下轮 pending-call 校验直接阻断续接 | 语义接受与 wire 冻结分离：trim 尾部进入 session 持有的 _pending_wire_tail，每次 prepare 自动注入直到后续冻结；跨 checkpoint 持久化；pending/tail 边界按 codec 合并规则拼接（user-user），调用方不再手动重喂已提交结果 |
| F3 | REQUIRED-native repair 同一调用双重物化；OPTIONAL 分支 native_committed=True 却不 attach | 同一 assistant contribution 单一表示：有 native 物化时不重建 IR calls（仅 results 走 IR）；REQUIRED/OPTIONAL 均从 ClosedRound attach material；observe_commit 拒绝 native+assistant-IR 双表示 |
| F4 | set 比较丢次数/顺序：孤儿 result、同轮重复 call、result 先于 call 全部放行；直接构造 PreparedRequest 绕过校验 | 线性顺序敏感 pairing 状态机（call 开组、result 消费、组内唯一、无残留）；__post_init__ 同规，构造器/反序列化不可绕过 |
| F5 | restore 浅拷贝与调用者 snapshot 共享嵌套对象，事后篡改污染 prefix | restore 边界一次性 JSON 深拷贝（ownership transfer），不逐 prepare 复制 |

新增测试：review 10 反例（tests/test_projection_followup_review.py）+ 离线纵向路径
（tests/test_projection_vertical_path.py：shell→native 回答→REQUIRED-native repair→
checkpoint→新 owner 恢复→完整请求，与全历史参考做内容/顺序/计数断言）。
合并回归：llm 全族 760 passed（2 失败为 bunshin 同进程混跑预存干扰，单跑皆过，
与本分支无关）。

### 已知限制（诚实声明）

anthropic 的相邻同 role 合并语义与冻结边界存在一处未收口的等价性差异：当已冻结
的 assistant item 之后紧跟本轮接受的 native assistant 轮（文本回答后直接接工具
调用），全量编码会合并为一条 message，增量路径保持两条相邻 message。**内容块、
顺序、计数完全等价**，仅 wire item 划分不同（Anthropic API 接受连续同 role
message）。曾尝试物化边界合并补丁，因会改写已冻结区而回滚——按 review
"先立契约再打补丁"原则，需要重新设计稳定冻结边界（含完整 tool 组）后解决，
不做局部补丁。该差异已用测试钉住（纵向测试注释）。

## 第三轮外部 review 修正（2026-09-19，针对 4afee09）

`pal_projection_review_4afee09` 四项指控（G1/G2/G3 P1 级 + G4 P2 级）逐条源码核实
**全部属实**，已修：

| # | 问题 | 修复 |
|---|---|---|
| G1 | preamble 同时进入独立编码与 conversation chunk：prepared_items 混入 preamble 而 _frontier_item_count 是会话坐标，首轮 commit 即把 S 冻进前缀，后续请求 S\|S\|Q1\|A1… 且前缀身份破坏 | prepared_items 只存会话跨度项（prefix+pending+tail）；preamble 每次 prepare 在装配时新鲜注入容器头部，永不进入冻结流；anthropic 拼接边界改用纯会话坐标 |
| G2 | Anthropic 提升分支只看本地 messages 空：tail 单独编码时首部 developer（甚至 SYSTEM）被提升到 tail 的 top-level system，被 prepare 丢弃 | 提升条件改为边界感知（本地空且 has_conversation_prefix 为假）；中段 system/developer 一律按时序降级为 user 块；prepare 把 tail 提升的 system parts 按序合并到 shell system 之后，不再丢弃 |
| G3 | native/语义兼容只对 call-ID：name/args 不同也放行；native+纯文本 IR assistant 双份入历史 | ClosedRound 构造期验证 native 载荷与语义记录逐调用有序 id+name+args 语义相等（bool≠数字、int/float 数值等价）；observe_commit 在 native 在场时拒绝一切 assistant 角色 IR（文本与调用同拒） |
| G4 | _item_tool_events 拆两个列表，validator 先消全部 calls 再消 results，item 内顺序被抹平，且不看所在 role | 单遍历有序事件流（call/result 交错保序）+ 同遍 role 门禁（tool_use 仅 assistant、tool_result 仅 user、completion tool_calls 仅 assistant）；build/直接构造/反序列化同一实现 |

**超出 review 描述的同族问题（修复时源码验证发现）：**

1. `has_conversation_prefix` 把 preamble 算进「会话前缀」——对 Completion 的
   developer 提升判定是错信号（F1 只修了一半）：首轮 preamble 后的 tail
   developer 会被错误降级。修正为纯会话语义（prefix+pending，不含 preamble）。
2. Completion 相邻 system 文本合并不发生在 preamble|conversation 拼缝：增量得到
   两条 system，全量是一条合并文本。装配时补对称拼缝合并（O(1)，仅
   completion，镜像 codec 的 _merge_instruction_text）。
3. G2 修复的深化：tail 首部提升的请求头内容渲染在 top-level system，容器坐标
   冻不进 chunk——当轮保住了但后续轮永久丢失。新增 session 持有的
   `_committed_head_system`（anthropic 专属路径）跨轮重入每次请求、随
   checkpoint 持久化。

**本轮确立的更强契约（场景矩阵钉住）：** 对三个 wire shape 参数化的多轮场景
（shell preamble + 首部/中段 developer 插入 + 多轮 commit + checkpoint/restore），
**装配后的增量请求与全量 codec 编码逐字节相等**（容器与 top-level system 均
等）——不再只是内容/计数等价。旧「相邻 assistant 合并」限制仍在（见下），
矩阵场景刻意避开该边界。

**性能（Nathan 明确要求，A/B 同机连跑）：** 稳态 prepare @800 前缀项、尾部 1 条：
openai_completion 14.62→14.53ms、openai_response 20.28→20.13ms、
anthropic_messages 18.70→18.67ms（基线 4afee09 vs 修复后）。首版 G4 双趟扫描
一度回退至 26/34/32ms，已合并为单趟线性扫描恢复至持平。G3 验证仅在
ClosedRound 构造时解析一次 native 载荷（每轮一次，非每消息）。

**契约澄清（fixture 适配，非断言削弱）：** shell 与 view 单一供给——同一条
逻辑消息不再同时经 shell 与 view 双通道供给（双供给会在合并语义下合法产生两份
 top-level system）。两个旧 fixture 按此修正
（test_anthropic_system_survives… 改 view 不再携带 shell 已有的 system；
test_closed_round_native_association… 的抽象载荷改为携带与语义记录一致的
tool_use——G3 后 ID 声明必须被载荷内容背书）。

新增测试：review 8 反例 + 3 shape 场景矩阵（含 restore）+ 边界补充（中段 SYSTEM、
头部 developer 合并 system、数值/布尔参数等价性、role 门禁反例）合入
`tests/test_projection_third_review.py`（15 项 + 14 subtests）。

### 已知限制（2026-09-19 更新）

- **相邻 assistant 合并差异（既有，未变）**：见上文第二轮声明，本轮未触碰，
  仍需冻结边界重设计；场景矩阵避开该边界。
- **旧快照兼容**：G2 持久化之前的快照没有 committed_head_system 段，restore
  置空（原型阶段无生产快照，无实际影响）；schema 版本未 bump（结构增量，
  缺失键有确定语义）。
- **单一供给契约**：增量装配假设 shell 与 view 不重复供给同一条逻辑消息；
  双供给不再被静默去重，而是按合并语义产生两份（见上）。

### 本轮回归（分文件隔离，PYTHONPATH=$PWD/src）

- 受影响面 15 文件全绿（projection×7 + llm×5 + prompt_cache×3 + setup_wizard
  + continuation_capture，218 passed）。
- llm 全族 27 文件：**347 passed + 78 subtests + 6 skipped**
  （real-integration 按设计跳过，无付费调用），零失败。
- 受影响面之外未跑全量 146 文件套件（本轮改动仅触及 llm/projection 面）。

## 第四轮外部 review 修正（2026-09-19，针对 422c7ba）

`pal_projection_review_422c7ba` 三项发现（H1/H2 P1 级 + H3 P2 级）逐条源码核实
**全部属实**，已修。review 同时确认了第三轮 G1-G4 修复本身成立；本轮三项
均为修复生命周期与既有残留（H1/H2 为检查修复生命周期时确认的既有残留，
H3 位于本轮新增的 native 参数兼容性检查中）：

| # | 问题 | 修复 |
|---|---|---|
| H1 | `observe_commit` 拒绝发生在 pending 安装之后：`_pending_wire_tail` 已被改写而 frontier/台账未推进，close_round 不还原——按旧 frontier 重喂 tail 会双倍内容（review 隔离 probe 实证） | 原子化重写：全部验证/裁剪/chunk 构造在本地候选变量完成，单一安装块一次性提交（拒绝路径零 session 可见变更，同 receipt 重试确定性，prepared_items 不再被预改）；零可冻结提交（anthropic 全 user 项）改为接受语义提交——frontier 随 receipt 推进、全跨度进 open tail（F2 同规则）、以空 chunk 封口使链仍止于 frontier（restore 零改动） |
| H2 | `ClosedRound` 只有 attempt/calls/results/continuation，无 native 修复时保留的 assistant 正文没有输入通道——helper 只能从 calls 重建工具清单，正文永不回投影（frontier 取 tail 补不回来） | `ClosedRound.assistant_texts` 新通道（非空字符串元组）；native material 在场时拒绝 semantic texts（每份 assistant contribution 单一表示）；`accept_repaired_round` 无 native 时物化为单条 assistant 消息（texts 先、calls 后）；text-only 修复（全调用剪除）单独可表示 |
| H3 | `_native_arguments_match` 把 None 补成 `{}` 参与比对但重放不补；三 shape 提取全用 `.get()` 无法区分缺字段/null；参数字段 wire 类型未按 shape 校验（OpenAI 字符串 vs Anthropic 对象混为任意值） | contracts 层：`_MISSING` 哨兵区分缺字段/显式 null/空对象，各自独立拒绝文案；比对前按 shape 校验原始 wire 类型（openai×2 必须 JSON 字符串、anthropic 必须对象，不能验证时补值重放时保持原值）；continuation_policy 层：三个 validator 增参数字段结构校验（缺字段/null/错型 → Degraded → attach_native 显式 ContinuationUnavailable，覆盖不经 ClosedRound 的直连 attach 路径），contract 版本 bump 至 *-2 |

**策略选择（H1，review 给了两条一致策略任选其一）：** 选「接受语义提交 +
持有 open tail」而非「拒绝但状态不变」——receipt 是 durable L1 提交的证据，
拒绝会把 projection lineage 永久留在 L1 之后，只能靠昂贵的 rebind 全量重建
恢复；接受路径复用 F2 已有的 pending 机制，restore 零改动。唯一保留的拒绝
（nothing to seal：frontier 之外零项）现在完全原子（快照级断言钉住）。

**SCENARIOS.md 三变体全部钉住（三 shape 参数化）：** 正文+部分调用保留
（B 剪除，A call/result 对应）、纯正文修复（全调用剪除）、零内容修复
（触达 H1 零冻结路径）；均含 checkpoint/restore 后重复断言与
增量==全量编码逐字节相等。

新增测试：`tests/test_projection_fourth_review.py`（**16 项 + 20 subtests**）。
review 自带的 7 项验收草案（含 H1 双分支兼容写法、H3 验收、head-system
生命周期 positive controls）在修复后代码上原样跑通：**7 passed + 9 subtests**。

### 本轮回归与性能

- llm 全族 30 文件（含新增 fourth_review）分文件隔离：**363 passed +
  100 subtests + 6 skipped 零失败**（real-integration 按设计跳过，无付费调用；
  日志 /tmp/proj4_regress.log，ALL_DONE）。
- 性能 A/B（Nathan 要求看住回退，同机同脚本 /tmp/pal_bench_prepare.py，
  prepare@800 前缀项、尾部 1 条、50 轮）：openai_completion 14.5→**14.34ms**、
  openai_response 20.1→**19.52ms**、anthropic_messages 18.7→**18.15ms**
  （基线 422c7ba vs 修复后）——持平略优，零回退。本轮改动均在 commit/构造
  路径（每轮一次）与 ClosedRound 构造期，prepare 热路径未触碰。

## 第五轮外部 review（2026-09-19，针对 82a2311）：修复级通过，无新阻塞项

`pal_projection_review_82a2311` 结论：**H1-H3 全部关闭；在本增量的检查范围内
未确认新的阻塞性缺陷**（隔离 probe 9 项全过：零冻结接受/幂等/拒绝可重复/
候选失败不污染 active+pending+head/pending 后续封存/head 只转移一次等）。
两条非阻塞性文档整理已本轮处理：

1. projection_session.py 模块头 chunks 描述统一（commit 时冻结 = 末次请求
   frontier 之外项 + 本轮新接受输出；零冻结 chunk 记录语义跨度）；
2. DELIVERY 未完成项措辞统一（resident/Bunshin 离线接线属于实施与集成，
   P7 是另行批准的上线 canary，两者分离）。

review 同时明确了下一阶段 gate（非本轮范围）：稳定冻结 cut/完整工具组、
真实 runtime 接受与 durable checkpoint、resident/Bunshin 共享实现；live canary
不能替代离线集成。

## 第六轮外部 review（2026-09-19，pal_full_branch_review_f12730e，整分支 vs main）：B1-B4 已修

范围从「最后一个修复提交」扩大到全部净变更、旧调用链兼容与新公共 API 的
退出/恢复语义。四项发现都在**尚未接线的新组件**上，不是已确认的线上回归；
G1-G4 / H1-H3 不重新打开。

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| B1 | P1（接入阻塞） | session 重建 ShapeContext 只给 shape/endpoint_id/model_id，capabilities 回落 {}：端点声明 unsupported_request_parameters=["temperature"] 时旧完整编码省略、新入口重新生成——请求契约不一致（probe 实证） | bind() 新增 capabilities 关键字参数（调用方解析的已验证 profile，深冻结存储，随 lineage 生命周期）；新增 _shape_context() 单一构造点，shell/tail/accepted-message 三处编码共用同一 profile；checkpoint 持久化 capabilities 并在 restore 回传（缺失键 → 空，pre-B1 快照忠实重建） |
| B2 | P2 | attach_native 提交前即写入 native_by_attempt；close/reject 只清 _active 不清未接受 native；snapshot 不过滤——取消的 payload 留在权威 store 与 checkpoint | close_round/reject_commit 丢弃本轮未接受 native（draft 随 round 生死）；snapshot native_records 按 committed receipt 过滤（开轮中的 draft 永不入 checkpoint）；已提交 native 原样保留，迟到 attach 仍拒 |
| B3 | P2 | legacy/unbound restore 对 populated target 只 retired=False+return False，旧 identity/chunks/native 仍可用——与「fresh lineage」承诺不符 | restore 开头 pristine 检查（identity/active/chunks/native/ledger/pending/head/frontier 任一非空 → 拒绝且零修改）；legacy/unbound 与 bound 路径同一规则，不再静默合并；fresh target 兼容恢复不变 |
| B4 | P2 | begin_round 推进 current fence 与 commit 无关，但 snapshot 只存历史 receipt 的 source fence，restore 用 max(source) 重建——取消过的高 fence 重启后回退，重启前拒绝的 stale worker 重启后被接受 | current owner fence 独立持久化（snapshot owner_fence 字段）；restore 从该字段重建并 max(persisted, max-committed-source) 保单调；pre-B4 快照缺失键回退 max(source)（忠实重建，测试钉住） |

review 另确认：旧热路径（_iterate）未接 session；usage/proxy 路线（已读链路）
未见新增 native 泄漏；共享 Anthropic codec 位置规则变更需旧链路回归（已含在
本轮全族）；file_read 净变更为删死代码；TLA+ Restart/Cancel 边界由产品测试
闭合（B4 已闭合）。

**合并提醒（review 建议采纳）**：本分支与 main 已分叉（main 独有 Telegram
pin/正文/provider 0.3.3 三提交，两侧文件无交集，未做真实 merge 测试）；
合并前先在 worktree 合并当前 main 跑组合回归，不用分支旧 tree 替换 main。

新增测试：`tests/test_projection_branch_review.py`（**13 项 + 10 subtests**，
B1 含普通 endpoint 保留 temperature 对照组、capabilities 深冻结、非 Mapping
拒绝、restore 后存活；B3 含 bound 路径同规则；B4 含 pre-B4 快照回退语义）。
review 自带验收：B2/B3/B4 六项原样通过；B1 按其文件头说明需 fixture 注入新
通道（断言不变，注入后在本仓库测试中全绿）。

### 本轮回归与性能

- llm 全族 31 文件（含新增 branch_review）分文件隔离：**376 passed +
  110 subtests + 6 skipped 零失败**（real-integration 按设计跳过，无付费调用；
  日志 /tmp/proj6_regress.log，ALL_DONE）。
- 性能 A/B（同机同脚本 /tmp/pal_bench_prepare.py，prepare@800 前缀项、
  尾部 1 条、50 轮）：openai_completion 14.65ms、openai_response 20.01ms、
  anthropic_messages 18.69ms vs 基线 14.5/20.1/18.7——持平（±1% 噪声内），
  零回退。B1 改动为每 prepare 一次 context 构造（引用传递），无热路径开销。

## 未覆盖 / 明确未做

1. **热路径尚未接线（离线集成与上线 canary 分离）**：resident LLMRuntime 仍走
   per-message ReplayEnvelope 编码（原路径未动，全部新组件旁路就绪）。
   resident/Bunshin 的真实接线属于实施与集成阶段（P4/P5 的集成部分）；
   P7 是另行批准的上线 canary（手动 restart + canary），两者不是同一件事。
   这是有意的安全边界，不是遗漏。
2. **矩阵 TBD 未销账**：GLM 系（openai_completion）与 DeepSeek 网关 signature
   下发行为仍是 synthetic-opaque 级验证；provider-real fixture 需你授权的脱敏
   日志或 P7 canary。
3. **compact / RewriteReceipt**：context-view 的 excluded 通道升级为显式 receipt
   已在 baseline.md 记录为设计要求，本轮未实现（PLAN §5.3 审计结论：正常路径
   无早期重写，通道有界）。
4. **性能项**：未做 10/100/1000/5000 全梯度矩阵与 RSS 剖析（单一规模实测代替）；
   未引入零拷贝/手工 JSON（PLAN §11 明说最小可行版本不做）。

## 迁移与回退

- 旧快照无 projection 段 → 新生代 lineage，ReplayEnvelope 消息保持只读兼容。
- 回退 = 切回 main（`safety/llm-projection-before-v2-7e0b1f7` 为本地备份指针）；
  新 schema 只在分支上写，main 的生产数据未被任何分支代码触碰。
- 不存在旧代码读新 schema 的路径（分支未部署）。

## 停工条件核查（PLAN §13）

七条逐一核对：无必需 native 丢失后赌 API 接受（Degraded → 显式失败）；
无以关 thinking/换模型掩盖协议失败；main/线上 DB 未动、可回退；无共享
mutable projection；无未跑先报通过（TLC/测试均有日志）；无放宽断言；
frontier 推进全部走 receipt 验证。
