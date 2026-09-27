# Pal 工具与提示词完整路径效率审计

审计基线：`1893d38`。下列发现记录该基线；后续整改按文末已确认决策实施。不部署、不调用付费模型。

## 覆盖与方法

- 静态枚举 `src/pal` 中 194 处 `capability_action` 声明、25 处字典式工具声明位置（其中包含动态展开），不是在线已挂载工具数量。
- 检查 118 个生成的顶层输入模型，核对 schema 默认值与校验行为。
- 横向检查 Core/Worker 常驻提示、skill 手册、direct/indirect 路由、alias 搜索、参数校验、结果预算、快照、错误恢复、插件/渠道生命周期、package/shell 完成通知。
- 覆盖 execution、artifact、browser/search、memory、skill、checklist、proactive、channel、plugins/packages、LLM、LSP、MCP、Bunshin 的相关路径；检查仓库内 provider 的对外健康说明，以及相邻 `pal-shell-native` 的路由和完成通知实现。没有修改相邻仓库。
- 隔离复现：快照覆盖范围、nullable 默认值、skill patch、错误结果构造、MCP 错误展示、inventory 体积。
- 跑相关现有回归：53 passed，2 个依赖弃用警告。没有运行全量测试，没有测试真实浏览器网站、远端 MCP 服务或在线模型决策。因此下面的“绕路”描述是可见契约导致的路径风险，不是假称已经测出的线上发生率。

优先级：P1 = 正确调用仍可能丢证据、假成功或进入错误恢复；P2 = 明确额外调用/检索/上下文成本；P3 = 可合并实施的轻量优化。

## E01 · P1 · 任意附件快照被误当成完整工具输出

位置：`src/pal/execution/runtime.py:1294`；`src/pal/web_fetch/capabilities.py:415`。

预算层发现 `snapshot_refs` 非空时直接复用 `refs[0]`，并输出 “Complete output snapshot”。但引用可能只是正文、stdout 等组成部分，并非整个 `llm_text`。

隔离复现：browser_navigate 返回正文 `body` 和 80 条长链接，限制输出为 1,000 字符。结果为成功，仅一个快照，文件内容恰好是 `body`。链接在预览中被截去，但“完整输出”提示仍指向这个 4 字符文件。继续 read_file 不可能找回链接。

整改：区分附件引用和完整结果引用；预算截断必须保存完整可见结果，或给出能覆盖所有被省略字段的明确 manifest。不能用“已有文件”推导“文件包含完整结果”。保留当前引用生命周期与不可变语义。

验收：正文很短、metadata/links 很长；多个附件；带快照的失败结果；嵌套 call_tool；均能通过返回的文件恢复被省略内容，不需重执行。

## E02 · P1 · 默认值与输入 schema 不一致

位置：`src/pal/execution/generated_tool_models.py`，例如 `:19`、`:40`、`:130`、`:635`。

118 个生成输入模型中，164 个顶层字段的默认值不满足其字段 schema，典型形式是 `(str, Field(None))` / `(int, Field(None))`：允许省略，schema 却给出类型不接受的 `default: null`。

复现：发送附件 `{path: '/tmp/file.txt'}` 校验通过；加入 `caption: null` 被拒绝。相同矛盾遍布 artifact、memory、core 等工具。模型跟随默认值填 null 就会白耗一次失败调用；read_tool 展示的仍是同一矛盾。

整改：统一字段语义。确实允许 null 的字段声明为 nullable；只允许省略的字段不要发布无效的 null 默认值。核对 omit/null/空字符串是否具有不同业务效果，不能无差别把 null 当作“清空”。

验收：遍历全部 schema 及 `$defs` 检查 defaults；验证省略/null/空值/有效值四种输入与实际执行语义一致。

## E03 · P1 · skill_update 静默忽略 patch，却报告成功

位置：`src/pal/execution/generated_tool_models.py:1156`；`src/pal/skill/service.py:91`。

`patch` 只有任意字典 schema，没有字段契约。实现对字符串和列表普遍使用 `new_value or current_value`，未拒绝未知键，并无条件增加版本。

隔离复现：

- `activation_terms: []` 与 `avoid_when: ''` 无法清空旧值。
- `manual: 'new text'`（真实字段是 manual_text）被静默忽略。
- 上述操作都返回更新对象，版本从 1 变为 2。

结果是 Pal 要么错误宣布成功，要么再次读取、猜字段、重试。read_tool 也无法告诉它 patch 的准确内部字段。

整改：使用明确的 patch 模型，拒绝未知键；按字段是否出现决定修改，按业务约束处理空值；没有实际变更应返回 no-op，不制造版本更新。显式区分不可清空字段与可清空字段。

验收：合法修改、拼错字段、清空列表/文本、空 patch、相同内容 patch，以及应用成功后不确定重试。

## E04 · P1 · 错误原因在返回途中再次报错或被隐藏

位置：`src/pal/memory/capabilities.py:139,147,157,177`；`src/pal/plugins/l3/sqlite_vec.py:450`；`src/pal/mcp/plugin.py:270`。

全仓 AST 扫描发现 5 处 `IntrospectionResult` 构造缺少必填 `llm_text`。涉及 dreaming 服务缺失/配置错误、archive 不可用、memory maintenance 拒绝 detach 等路径。

已复现：dreaming 服务缺失时，本应返回 unavailable，实际抛 `TypeError: ... missing ... llm_text`。运行时能捕获异常，但模型得到的是包装错误，无法依据原始业务原因纠正操作。

另一个复现：mcp_image_prepare 的具体错误是 `image file not found: /tmp/missing.png`，模型文本仅为 `image prepare failed: ValueError`；具体信息保留在 structured 中，失败渲染不会自动把这些 details 展开给模型。

整改：统一错误结果构造与安全投影，保留足够具体的业务原因及最短恢复动作；不要把内部任意异常对象或敏感数据全量倒给模型。

验收：服务未挂载、维护中、无档案、参数错误、缺文件；模型可见文本应包含可采取行动的具体原因，且不能发生二次构造错误。

## E05 · P1 · 记忆恢复提示要求精确查询，但公开入口只有相似检索

位置：`src/pal/memory/capabilities.py:496,557`；`src/pal/plugins/l3/sqlite_vec.py:544`；`src/pal/memory/contracts.py` 的 MemoryQuery；`src/pal/memory/schema.py:48`；`src/pal/memory/repository.py:608,656`。

update/delete 的不确定结果恢复要求“recall that mem_ref”。但 recall_memory 仅提供 queries/topic/kind 等检索参数，没有精确 mem_ref 参数。实现走 FTS、LIKE 和向量融合，FTS 的 document_id 为 UNINDEXED，LIKE 也不匹配 ID。

因此，把 `fact:...` 填进 queries 不是可靠的按 ID 读当前记录。memory_history 可按 ref 读历史，但其契约明确不是当前事实，不能代替当前状态对账。

整改：提供当前记录的精确查询路径（可扩展 recall_memory），返回 present/deleted/superseded/not_found 与可用 successor；恢复提示携带确切参数。搜索未命中不能充当删除已完成的证据。

验收：更新成功但响应丢失、删除成功但响应丢失、已被 dreaming 替代的 ref、内容完全不包含 ref 的记录；无需语义搜索猜状态。

## E06 · P1 · PDF 页索引已存在，按页视觉读取仍未闭环

位置：`src/pal/artifact/service.py:671,933`；`src/pal/artifact/capabilities.py:84,218`；`src/pal/execution/generated_tool_models.py:75`。

自动附图始终取第一张 image representation；artifact_select 只接收 artifact_id 并刷新 TTL，不能选页或 representation。read_artifact/read_file 都是文本读取。

用户要求“看第 2 页图”时，模型可知道 image_file_path，却没有已有 PDF representation 的直接附图入口。可行绕路是再 artifact_import 那张 PNG，形成新 artifact；但 import 的 do_not_use_when 又说已有 artifact_id 不要用。超过预渲染范围的页也没有同一契约下的按需视觉路径。

已确认整改：parse 时生成逐页正文、整页渲染图和 1…N 页索引，保留便于 rg 的扁平文本。需要视觉内容时使用现有 artifact_import 导入索引中的 image_file_path；修改 import 指引，明确 PDF 父 artifact_id 不代表页图已注入。不新增选页工具。

验收：图像型多页 PDF 的第 2 页、超过预渲染范围的页、图文混合 PDF、非 vision 模型、图像预算耗尽；不能把“路径已返回”当成“像素已看见”。

## E07 · P2 · 浏览器仍有其他在采集端丢信息的位置

位置：`src/pal/web_fetch/browser_service.py:625,1046,1071,1140`。

已修的正文/snapshot/find/evaluate 不代表所有 browser 输出完整：

| 数据 | 当前行为 | 可恢复性 |
| --- | --- | --- |
| links | DOM 脚本只取 maxLinks+1，能力返回再裁为 max_links（上限 500） | text_file 只含正文；超过预算的 href 需重新提取 |
| inspect_layout | 仅取前 max_elements 个，最多 20 | 有总数/truncated，无 offset/cursor；需换 selector 或自行 evaluate |
| click 的 open_tabs | 字符串裁为前 12,000 字符 | 提示可通过 browser_tabs list 获取；已有恢复路线，但新增一次采集 |
| network request body | 采集时裁成 2,048 字符 | 未标明单条 body 截断/原长度，network 分页只能翻条目，恢复不了 body 尾部 |

整改：按数据类型建立统一预算表。文本/结构化输出使用完整快照或可续读游标；确需有损的网络采样明确声明采样范围、截断和可恢复性，避免为补证据重发请求。不是把所有上限取消。

验收：501+ 链接、21+ 同类元素、长 tabs 输出、长请求体；分页或文件能恢复的必须可恢复，不能恢复的必须如实说明。

## E08 · P2 · 部分动作的条件参数与真实边界没有进入 schema

位置：`src/pal/web_fetch/tool_models.py:48,155` 及 BrowserExtensionManageInput；`src/pal/web_fetch/browser_service.py:655,882`。

- browser_extension_manage 只枚举 operation；path/extension_id 都可省略且没有条件描述。mount 实际要 path，reload/unmount 实际要 extension_id。
- browser_find 的 text/regex 互斥目前靠 prose 和执行时检查，schema 两个都不要求。
- resize、scroll、layout 的部分数值边界只在实现中 clamp；例如 resize.width=100 可通过模型校验，实际执行成 320。模型需靠返回值反推限制。

整改：把确知的必需关系、互斥关系、范围和单位写入输入模型/字段说明；尽可能让首次调用可成功。对业务条件使用明确的输入拒绝，别让 read_tool 返回一个仍然解释不了问题的 schema。保留合理默认值。

验收：逐 operation 的最小有效输入、缺失条件参数、互斥输入、范围边界；不能静默执行与请求不同的动作参数而不说明。

## E09 · P2 · 少量导航和能力说明仍不对应真实能力

位置：`src/pal/identity/capabilities.py:52`；`src/pal/memory/capabilities.py:134`；`src/pal/core/capabilities.py:68`；`src/pal/execution/tool_registry.py:824`。

- identity_show 推荐 behavior_show，仓库内没有该公开 alias。
- memory_dreaming 推荐 `update`，实际公开名为 update_memory。
- core_configure 举例 normal/maintenance，但实现只是给 state.mode 赋字符串。仓库内的维护/排空控制是另外的状态机制；这里不保证停接任务、排空、维护或恢复。
- 现有引用投影主要识别 canonical path，next_tool_hints 有解析；普通 prose 中错误的公开 alias 不会被统一发现。

整改：修正这些具体引用；明确 core_configure 的真实效果，不能用状态标签冒充生命周期能力。为内置 guidance/skill 增加公开 alias 引用检查，并区分条件可用插件与不存在的工具，不能粗暴禁止所有可选插件引用。

验收：未挂载可选插件、Worker 裁剪后、alias 重命名后，提示仍指向可执行路径或诚实说明缺失。

## E10 · P2 · 发现信息仍有一条全量膨胀路径和一种系统性重复

位置：`src/pal/execution/runtime.py:398`；`src/pal/execution/tool_presentation.py:49`；`src/pal/mcp/compiler.py:351`。

- exec_tools 返回整个 inventory 的 description + search_text + input_schema。search_text 是索引材料，和 description 有重复；工具没有筛选/精简视图。隔离仅加载 execution 的 11 个工具，渲染已达 **17,424 字符**。这是字符数，不是 token 数；未假设线上挂载数量。
- MCP 的 `_mcp_guidance` 把外部完整描述同时放进 purpose 与 use_when，渲染与搜索契约会重复携带同一段内容。外部工具说明越长，成本越明显。

整改：inventory 默认给可检索的简短目录，完整契约继续由 search/read 获取；过滤内部索引字段。MCP 保留一份完整说明，use_when 补充适用条件而非复制。只去重、分层，不砍掉调用必需信息。

验收：多工具库存、长 MCP 文档，比较字符/token 体积；首次正确选工具与参数成功率不得退化。

## E11 · P3 · 按能力选择模型仍需 list 后逐个 show

位置：`src/pal/llm/capabilities.py:118` 的 LLMModelListItem 与 list_endpoints。

llm_list 仅提供名称/provider/wire_shape/priority 等信息，不包含 vision/tools/context_window。用户要“切到支持视觉的模型”时，未知配置的 Pal 得先 list，再逐个 llm_show，才能选择。调用可并行但额外轮次仍存在。

整改：列表增加最小选择摘要或能力筛选，不返回整个 capabilities_blob。保留 llm_show 用于详细契约。

验收：多个 endpoint 中只有一个支持 vision，列表结果就足以完成选择；不泄露 auth 信息。

## 已检查但不建议本轮重做

- alias 按下划线等分词、英语 domain/action/object、精确 alias 优先、默认 1–3 个结果、purpose 同义补充：保留。use_when/do_not_use_when 没有被错误纳入正向评分。
- direct/indirect 与 Worker 实际暴露面的区分：保留。shell-native Worker 有实际 indirect session/status 能力，不能一律删除 read/call。
- file read/edit 的 digest/CAS、范围读取和独立调用批处理：保留；保护协议不能为了省轮次绕过。
- package 和 native shell 的完成通知：继续由 harness 唤醒，不改回模型轮询；本轮相关回归通过。
- plugin attach/reattach、channel reload/restart 的职责、proactive 当前 channel 与唯一默认目的地：现有分工保留。
- checklist 完成自动关闭、技能正文通过 context_messages 注入、常驻提示的证据复用：没有把这些已修路径再次列成新缺陷。
- 浏览器错误代码的一个初步怀疑被排除：真实 sidecar 会把 worker ValueError 转为 invalid_arguments，不能用直接 mock 绕过 transport 得出的 missing_execution_scope 结果断言生产路径错误。
- MCP 不信任远端 readOnlyHint 的保守执行语义、Bunshin 审批与角色边界、离线 host 重启边界属于权限/正确性要求，不作为可随意删除的效率成本。
- 音频转录算力按用户既定决定暂不扩展。

## 一次性整改组织

建议按三个相互配合的改动组实施，在同一轮完成总体复核，而不是每修一个工具就重新询问剩余问题：

1. **契约与恢复**：E02/E03/E04/E05/E08。统一输入默认值、条件字段、patch、精确对账与错误 envelope。
2. **输出与读取**：E01/E06/E07。统一完整输出/组成部分引用，打通 PDF 视觉页选择，明确所有 browser 截断的恢复策略。
3. **发现与提示**：E09/E10/E11。清理错误引用和虚假效果说明，目录分层、重复信息去重、最小选择摘要。

最终验收按用户任务路径覆盖，而非仅对字符串断言：

- 发现 → 首次有效调用；校验失败 → 一次明确纠正。
- 动作成功但回包丢失 → 精确对账，不重做副作用。
- 大结果 → 可恢复文件/游标，不重复采集。
- 多页 PDF → 直接选择所需页的像素；无 vision 时如实降级。
- 查询/修改 skill → 未知字段拒绝、清空有效、no-op 不制造版本。
- Worker 与主 Pal 分别验证路由、文件可达性和快照生命周期。

保留现有全量 CI 分批策略。若后续跑模型 benchmark，应扩展为以上端到端路径，比较首次有效调用、完成轮次、重复副作用、输出可恢复性和 token 成本；单看 search top-1 无法证明整条路径更高效。

## 本轮验证命令

```sh
python -m pytest -q \
  tests/test_tool_search.py \
  tests/test_tool_discovery_contracts.py \
  tests/test_worker_tool_routing.py \
  tests/test_result_guidance_budget.py \
  tests/test_package_completion_delivery.py \
  tests/test_proactive_destinations.py
```

结果：53 passed，2 warnings。静态扫描和隔离复现没有修改运行中的 Pal 或业务配置。

## 已确认整改与验收路径

- E01：快照增加覆盖说明；预算层按内容摘要判断是否可复用，不能把正文或 stdout 片段当作完整结果。旧引用默认 unknown；保持 L1 所有权和文件读写权限。
- E02/E08：omit-only 参数不再发布无效 null 默认值；nullable 参数保留空值语义。记忆公开查询默认 limit=5、view=summary；浏览器条件参数和范围进入 schema 与实际校验。
- E03：skill patch 使用明确模型，拒绝未知键；辅助文本/列表支持清空，名称与正文必须非空；no-op 不写文件或增加版本。
- E04：修复错误结果缺失 llm_text；MCP 原始原因进入可见文本，并按参数、文件、服务连接给诊断路线。
- E05：recall_memory 的 mem_ref 精确查询直接查持久记录，索引 pending 不阻塞；保持落库与索引两个阶段。替代与删除状态按持久证据返回，不强制每次写入后验证。
- E06：逐页文本和整页图片，PDF 不默认注入第一页；按需 import，保留 vision 与预算控制。页数上限/页处理失败公开为 partial。
- E07：links/layout/tabs 等完整已采集文本落盘；请求体仍是明确标注的采样，不重发请求补证据。
- E09/E10/E11：修正错误 alias 与 core mode 声明；inventory 展示简短目录，完整契约保留；MCP 说明去重；模型列表提供能力选择摘要。

本地只跑相关回归；全量测试由 push 后 GitHub CI 执行。没有运行模型 benchmark，不能以离线测试通过宣称线上首次调用率已经提高。

整改验收：20 个相关测试文件共 **286 passed，17 subtests passed**，仅两个既有依赖弃用警告。覆盖参数契约、发现路由、skill、精确记忆、MCP、快照及预算、浏览器脚本、Worker、PDF 与图像注入、package 完成通知和 LLM 用量。全量 CI 留给 GitHub；没有部署到运行中实例。

最终 schema 复核同时补齐 browser_tabs select 的 index 条件以及现有字符串长度边界；此后 85 项相关定向回归全部通过。最后的 MCP 错误分类复核覆盖了文件名含 artifact 的缺失文件，避免误导至 artifact 查询。

## 后续扫描整改

- 快照读取复用原引用，不依赖已移除的源文件编辑 manifest；提示按需 `rg -n` / `wc -lc` 定位后读取范围，超长行截取原文件片段。
- proactive schedule 拒绝未知字段、类型错误、缺少 cadence 的定时参数及互斥字段，明确返回任务参数错误；保留合法 manual 默认值，移除过期日期示例。
- 记忆冲突直接展示已有 reason / successors；降级召回公开原因，不把未返回结果说成确定不存在。
- MCP 内部保留完整协议结果，展示层去掉额外复制的工具正文、错误正文和 prompt 消息；保留服务诊断提示。
- CI 修复快照递归落盘回归、过期契约断言及 macOS 临时目录符号链接比较。

本地最终相关回归 **114 passed**，警告来自既有依赖弃用提示。PDF 解析性能、MCP 图像路由及源文件扫描策略按讨论保持原状；全量验证交给 GitHub CI，未部署运行中实例。
