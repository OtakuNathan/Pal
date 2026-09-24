# Result Guidance Contract — 工具指导分层与结果侧 Affordance

版本:v2 实施 · 2026-09-24 · 任务包 `../pal-result-guidance/origin/`(v2.0)

## 三路分工

指导信息按"何时需要"分三处存放,由三个不同的 owner 负责:

| 通路 | 存放位置 | Owner | 进入模型视图的时机 |
|---|---|---|---|
| 静态能力理解 | descriptor(`purpose/use_when/do_not_use_when` + `NextToolHint`) | 工具作者 | 编译进稳定描述;`read_tool` 同源展示 |
| 失败恢复 | 结果侧 `recovery_hint` + `affordances` | 业务 owner 优先;声明 fallback 兜底 | 仅真实失败结果,择优单源渲染 |
| 本次行动建议 | 结果侧 `affordances`(绑定真实参数) | 业务 owner 按本次事实判断 | 仅结果特定事实支撑时 |

**默认行为**:成功结果的 `affordances=[]`、`recovery_hint=""`。成功、换 workspace、
出现路径/ID、首次见到工具,都不构成建议理由。这不是"首次提示、后续去重"——第一次
就没有理由提示。选择不依赖任何展示历史;不存在跨轮 seen set、提示计数或 TTL。

## 类型与字段

- `ToolGuidance.failure_next_steps: str = ""` — 可选宿主侧 fallback 声明。不再进入
  默认模型描述(`compile_tool_description` 不投影该字段)。旧非空声明继续有效。
- `recovery_hint: str = ""` — 简短、不可表示为完整工具调用的本次修复指导。贯通
  `CompleteResult/FailedResult/RejectedResult`、`ToolHandlerResult`、
  `ToolExecutionError/ToolRejectedError`、`CapabilityResult`。
- `affordances` — 已有 `ToolAffordance(tool, arguments, reason)`;`CapabilityResult`
  新增该字段(此前会跨 normalization 丢失)。
- 建议不构成批准、读取授权、快照 owner 或新指令层级。

## 失败择优(§6.1 → `pal/execution/result_guidance.py`)

1. 执行前宿主拒绝(unknown tool/mode/schema/target):保留精确修正 affordance,
   不追加业务 fallback。
2. handler 明确指导(recovery_hint/affordances)优先。
3. 无 handler 指导时,声明的 `failure_next_steps` 填入 `recovery_hint`,单源渲染,
   不再拼接进正文、不再给无条件 read_tool、不再附加 recall_memory 固定段落。
4. 无适用 fallback:只保留事实/effect/retry。

## 动作身份与去重(§8 → `result_guidance.py`)

`key = (logical_alias, canonical_json(arguments))`;direct 与 `call_tool(name=...)`
包装识别为同一目标;`read_tool(foo)` 与执行 foo 是不同动作;对象键排序、数组顺序与值
类型差异保留;单结果内去重保留首个最具体来源。对捕获 generation 校验 alias、
input schema、目标绑定和角色范围,按 direct/indirect 投影合法调用方式,随后取最多
3 个有效动作。坏候选单独丢弃并记录诊断,不覆盖业务结果。归一化幂等。

## 唯一出口与预算(§9 → `runtime._finalize_invocation_result`)

所有 `_invoke_tool_record_sync/_async` 出口(提前拒绝、异常、builtin、call_tool
递归)统一经过 finalizer:候选校验 → 正文与恢复信息预算。
按 review 后用户确认的新规则,可选 `affordances` 在截断预算外完整呈现,
不挤占正文空间,也不因正文预算被删掉。预算仍包括正文、status/effect/error/retry、
recovery 和快照提示;包含可选建议的最终字符串允许超过配置字符数。
native 的 normalize 后置追加发生在 finalizer 之前,遵循相同规则。
CapabilityResult 和异常载体的坏建议会单独丢弃;失败 guidance helper 意外抛错时,
保留原始 error/effect/retry/details,仅降级指导字段。
`CancelledError` 继续传播,不伪造成普通失败。

## 长行提示仅认真实读取(§10 → `_budget_invocation_result`)

`manifest.operation == 'read'` 且非受管快照才给源文件 shell 查看/修改说明;
edit/write 的 diff proof_length 不再被当作源行长度;受管快照的长行只提示查看有界
片段,不建议修改。

## 迁移清单(§4 五类分类)

| 来源 | 分类 | 去向 |
|---|---|---|
| `compile_tool_description` 的 Failure next steps 段 | FAILURE_FALLBACK | 移出描述;runtime 失败时填 recovery_hint |
| `lsp_prepare_workspace` ready 分支 next_tools 菜单 + direction 尾巴 | DROP_REDUNDANT | 删除(不转 typed);ready 只报事实 |
| `lsp_prepare_workspace` partial/failed 菜单 | RESULT_DYNAMIC | 迁为 lsp_doctor/lsp_status 绑定当前 workspace/server 的 typed affordance |
| LSP prepare/doctor/status 静态 hints(use_when 已条件化) | STATIC_RELATION_KEEP | 保留 descriptor 投影与搜索文档 |
| `prepare_call_hierarchy` incoming/outgoing 静态 hints | STATIC_RELATION_KEEP | 保留;单 item 结果另给位置绑定 continuation |
| `_render_invocation_for_llm` 的 `_FAILURE_MEMORY_NEXT_STEP` 尾巴 | DROP_REDUNDANT | 删除;失败不再固定推荐 recall_memory |
| `_default_failure_affordances` 无条件 read_tool | DROP_REDUNDANT | 删除;无 fallback 时只留事实 |
| `_append_failure_guidance` 子串去重拼接 | FAILURE_FALLBACK | 改为 recovery_hint 单源 |
| `details.failure_next_steps` setdefault | FAILURE_FALLBACK | 删除(recovery_hint 为唯一通道;structured 不进模型正文) |
| `shared/tool_routing.py` 系统提示词 "description's Failure next steps" 引用 | PRECALL_KEEP(改写) | 改为引用结果侧 recovery/affordances |
| checklist/channel/browser 的状态条件内联提示 | RESULT_DYNAMIC(保留) | 真实状态条件触发,非静态重播 |
| plugins/package_*、memory recall、web_search 等 | 无结果侧追加 | 已合规;静态 hints 保留 |
| `turn_executor` timeout/rpc_failed 直接合成 | PRECALL_KEEP(加界) | 异常文本头尾截断,同一有界呈现策略 |
| bunshin `op_exec_shell` failure 文本中 "trapped 不换壳重试" | PRECALL_KEEP(迁移) | 迁入 `do_not_use_when`(调用前可见);failure 侧只留恢复流程 |

## 结果指导示例(交付物 4;稳定 descriptor 始终保留
"Preparation was partial or failed … lsp_status/lsp_doctor" 条件关系)

以下为实际出口产出的形态摘要:

1. **首次 prepare(A)→ready**:正文只有就绪事实(202 字符,含 status/servers);
   `affordances=[]`、`recovery_hint=""`、无 `next_tools`、无 direction 尾巴。
2. **跨 workspace 第二次 ready(B)**:与 1 完全同形;workspace_root/primary_server
   作为事实保留,不构成推荐理由;descriptor 哈希不变。
3. **两个 workspace 分别 partial(B 失败 clangd / A 失败 clangd)**:各给
   `affordances=[lsp_doctor(workspace_root=<该次>, name=<失败 server>)]`,
   reason 指向本次未就绪事实;互不压制(A partial 不因 B 提示过 doctor 而消失)。
4. **call hierarchy 单 item**:两个 affordance 绑定产生 item 的
   file/line/character(+可选 workspace_root/name),指向 incoming/outgoing;
   空 item 不构造;多 item 不擅自选第一个。
5. **ordinary failure(handler 抛错,无 handler 指导)**:错误事实 + effect + retry
   原样;`recovery_hint` = 声明 fallback(仅此一处出现);无 read_tool 强推、
   无 recall_memory 尾巴。
6. **unknown effect(effect=unknown)**:retry=reconcile_first 保留;建议不把原命令
   当作可无条件重试动作(`RetrySafetyPreserved`)。
7. **saved-output failure(capture OSError)**:操作 status/effect/receipt 不变,
   `output_error` 如实记录;有限预览 + "不要为取回输出重复副作用" 提示;
   native 侧 output_ref/session_id 恢复入口保留(不重跑命令)。
8. **long-line source read**:单行超预算且为原文件读取 → 有条件 shell 查看/修改
   说明(不含已读授权);授权仍只覆盖实际交付区间。
9. **snapshot read(受管快照)**:复用既有快照不复制;长行只提示查看有界片段,
   不建议修改;无原文件编辑授权。

## 测试矩阵映射(v2 110 场景 → 实际测试)

| 矩阵组 | 测试位置 | 说明 |
|---|---|---|
| A01-A10 | test_result_guidance.py::TestDescriptorLayering + test_tool_guidance.py(4,修订) | 描述/搜索契约;A06 空合法性;A07 静态投影;A09 同代稳定 |
| B01-B16 | test_result_guidance.py::TestFailurePickBest/TestDirectResultCarriers + test_immutable_tool_facade.py(修订) | 择优/事实保真;B15 五载体参数化;B12 由 finalize 的异常降级分支保证 |
| C01-C14 | test_result_guidance.py::TestActionIdentity/TestResolveFailureGuidance | key 归一化/去重/幂等;C08 由 finalize generation 过滤 + bunshin 角代号验证覆盖;C09-C10 由 generation 捕获语义覆盖 |
| D01-D20 | test_result_guidance_budget.py(全部) | 两个 P2;D13 授权保持由既有 test_result_snapshots.py 覆盖;D18 快照长行 = managed 分支 |
| E01-E10 | test_result_guidance_h.py::TestProviderLevel/TestEndToEndSequence | LSP 决策表 + 其它 producer;E06/E08/E09 由"无结果侧追加"事实 + B11/E01 泛化覆盖 |
| F01-F12 | F01 可空默认(wire 往返测试含旧 payload 分支);F02 `test_wire_roundtrip_preserves_guidance_fields`(TOOL_INVOCATION_RESULT_ADAPTER JSON 往返);F03 冻结历史不重渲染;F04/F05 既有 snapshot/L1 测试;F06/F07/F10 native 套件(151 tests OK,含未提交 lease 断言);F08 events/observation 改走 deliver_invocation_result;F09 bunshin 角代号;F11 取消语义未触碰;F12 native 未提交 diff 完整保留 | |
| G01-G07 | G01/G02 = TestActionIdentity/既有 snapshot 哨兵测试;G03 = 本文档离线对比;G04 搜索命中一致;G05 = 四批 CI + native 套件;G06 = 本文档;G07 迁移清单(上文) | |
| H01-H22 | test_result_guidance_h.py(H01-H05/H07/H09-H11/H13/H14/H20/H21 显式;H06 无历史状态 = H20+无序列化;H08 = H21;H12 = `TestScopedRoleFiltering`(角色外动作被滤 + 静态关系降级);H15-H18 = producer 无结果侧追加事实 + B11/E01 泛化;H22 = 本工作区从干净基线实施,v1 未曾开工,不适用) | |
| B12/D12 补充 | B12 = `TestGuidanceFailureDegrades`(强制 helper 异常,事实保真降级);D12 = `test_unicode_emoji_bom_budget_maps_spans_not_decorations`(emoji/CJK/BOM 预算与授权区间映射) | |

## 初版 CI 与回归记录(§13,历史执行记录)

以下是初版实施记录,不代表 review 修订后的 CI 结果。review 修订只运行针对性测试,
未重跑 CI;其中预算断言已更新为“正文与恢复信息有界,可选动作独立呈现”。

| 批次/套件 | 结果 |
|---|---|
| core-a(修复 2 处钉旧断言 + 补 B12/H12/D12 残留测试后的终验) | **1205 passed**, 5 skipped(首跑 1200+2 failed:`test_minimal_operating_rules_prompt_omits_route_specific_tools` 短语恢复、`test_read_tool_full_contract_is_retained_only_in_structured_channel` 按 A01/A03 反转) |
| core-b(含 D12 残留测试的终验) | **1119 passed**, 2 skipped |
| bunshin-a | 464 passed(含 bunshin A02 迁移验证) |
| bunshin-b | 477 passed |
| native tests/pal_host(unittest) | 151 tests OK(含未提交 lease/output_ref 断言;test_remote 的隔离 SSH E2E 依赖 sshd,套件自带 skip) |
| 聚焦回归(6 个 guidance 相关文件) | 85 passed |
| AGENTS.md 指定 test_tool_activity.py | 6 passed |

未运行/受限:test_remote 的隔离 SSH E2E 需要 sshd(套件自带 skip 标注);native 需本地 venv + dist wheel 提供 C++ 扩展。

| 指标 | before | after |
|---|---|---|
| provider 描述总字符 | 9,186 | 7,460(-19%) |
| read_tool(lsp_prepare_workspace)字符 | 637 | 280(-56%) |
| 普通成功结果最终字符 | 15 | 15(不变,零建议) |
| 50KB 异常最终模型可见字符 | 50,727(**无界**) | 688(有界) |
| 失败固定 recall_memory 尾巴 | 存在 | 移除 |
| prepare(A)→ready 最终字符 | 680(含菜单词) | 202(零菜单词) |
| prepare(B)→ready | 680(含菜单词) | 202(零菜单词) |
| prepare(B)→partial | 0 个 typed 建议(菜单散文) | 1 个 lsp_doctor 绑定 B/clangd |
| prepare(A)→partial | 0(同上) | 1 个绑定 A/clangd |
| descriptor 哈希(序列内) | 稳定 | 稳定 |
| 固定搜索查询命中 | — | 与 before 逐项一致(静态关系保留) |

测量脚本逻辑:同 generation 假 LSP RPC、`ToolCallBudget(max_output_chars=2000)`、
单次 handler 调用计数。这些数据只证明投影/预算/重复控制变化,不据此声称真实模型
调用轮数或端到端延迟下降。

## 不变量(性质测试约束)

见 `tests/test_result_guidance*.py`:操作事实保真、无执行副作用、建议经捕获
generation 校验、单结果动作 key 唯一、确定性选择、归一化幂等、成功无静态重播、
ready prepare 零推荐、无基于历史的抑制、descriptor 同代稳定、正文与恢复信息有界。
