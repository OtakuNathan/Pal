# 工具 affordance 审计（2026-09-23）

审计基线：`26bfb5e`。下文保留原始发现；本轮实现结果见文末。工作区还包含独立的 compact 文案和 OR hybrid 请求兼容性修复。

## 范围与证据

- 静态遍历 `src/pal`、`plugins`、`providers`，提取 24 个文件中的 199 个 `capability_action` 声明，查看工具 guidance、InputModel 及主要返回路径。
- 对其中 105 个集中生成的输入模型读取真实 Pydantic schema：304 个顶层字段，89 个没有字段 description。这是筛查信号，不是 89 个缺陷；普通 `name`、上层 guidance 已解释的字段不应机械补文案。
- 其余包括独立输入模型、空参数工具、动态 endpoint 定位参数；不能把没有显式 InputModel 等同于模型拿不到参数。
- 深查 discovery、LLM usage、channel/Telegram、checklist、artifact、browser、proactive；其余模块完成声明级扫描，不宣称全部错误分支已执行验证。
- 结合 `/tmp/pal/pal.log` 的 TG 排障历史。该文件是末次请求快照，不是每轮完整性能 trace。
- 离线调用真实 schema/函数，复现 discovery 错误筛选、browser 负索引，并测量 usage 快照字段数。浏览器底层执行用捕获 stub 替代，没有真实浏览器操作、付费调用或运行时变更。
- 动态 MCP server、独立安装的 `pal-shell-native` 以及其他仓库不在本次源码覆盖内。日志里的 native shell 不能归因于本体 `run_shell` 的声明。

## 确认的问题与建议

### A1：search_tools 空结果不能解释筛选冲突（高）

位置：`src/pal/execution/generated_tool_models.py:482`、`src/pal/execution/runtime.py:1057`。

`family` 是自由字符串，说明举 management/lifecycle/endpoint/search，但没有区分它和 module。TG 日志中模型传 `family=channel`，而相关工具 family 为 introspection。所有筛选先执行，facets 再从筛完的结果计算；因此即使 `facets=true`，零命中时也只能得到三个空数组。

离线复现：同一个 channel health 记录，family=channel 得到 0 个结果和空 facets；移除 family 后得到 1 个结果，family=introspection。

建议：说明 family 是注册表分类，未知时省略；零命中时返回有条件的筛选修正建议及可用分类，不自动放宽后假装精确命中。family/module 是动态集合，不能把当前值写死成永久 enum。`top_k`/`limit` 同时提供时目前前者优先，也应明确。

### A2：llm_usage 默认返回全部聚合细节，缺少视图选择（高）

位置：`src/pal/llm/capabilities.py:266`、`:383`；`src/pal/llm/usage.py:175`。

工具无输入参数，输出 active_model + usage 总计 + 所有 endpoint 桶。空 ledger 已有 42 个顶层字段，每个 endpoint 桶为 39 个字段；此外还有 active_model。包括最新请求细节、计数口径、覆盖率、reported_fields、异常、近义比例等。

这是聚合快照，不是无限请求明细表。已有结果分页不能解决默认信息层级不合适。

建议：默认展示进程级概览及数据完整性；按需选择 endpoint/detail。保留 cost_complete、未知费用/usage 的区别，不能为了简短把未知表示为零。当前 ledger 没有独立 turn 查询契约，不应承诺可直接查询本轮费用，除非另接正确数据源。

### A3：Telegram health 混合当前轮询状态与历史旁路错误（高）

位置：`providers/telegram/endpoint.py:873`、`:1381`、`:1396`、`:1999`、`:2010`。

`healthy` 依据 polling_running 和 last_poll_error；同一结果同时返回 last_status_error，后者可能来自 reaction/typing/menu，缺少发生时间和恢复信息。因此 healthy=true 与 Timed out 同时出现并不逻辑矛盾，但模型必须读源码才能知道判断边界。该现象已经在 TG 日志中触发额外调查。

建议：返回健康判断范围、错误操作类型、错误时间及后续成功情况。当前无积压只能说明当前队列为空，不能排除过去的消息延迟。底层观测元数据留在 harness；面向模型解释判断所需的信息即可。

### A4：checklist 的 UI echo 全量进入 LLM 输出，收尾契约过度串行（高）

位置：`src/pal/checklist/capabilities.py:135`、`:205`，`src/pal/checklist/prompt.py:40`。

同一结果重复包含 plan/markdown/echo.payload.plan/echo.markdown。TG 实例中一次打勾约 1.1K 字符，最后三次 check 加 clear 用了四个模型轮次。

建议：UI echo 仍保留给 channel/harness，LLM 默认只看状态变化和必要下一步；文案明确整份计划可以批量更新，任务结束无需为了清空先逐项打勾。不必创建一个更大的 checklist 框架，也不要让 runtime 自行猜测任务已完成。

### A5：artifact 返回建议中的公开别名错误（高）

位置：`src/pal/artifact/service.py:1195`；`src/pal/execution/generated_tool_models.py:32` 等。

PDF next_actions 建议调用 `artifact_read`，公开 alias 实为 `read_artifact`；多个 artifact_id 描述要求从 `artifact_search` 获取 ID，公开 alias 实为 `search_artifacts`。内部方法同名并不等于可调用工具别名。

建议：统一使用真实公开 alias，增加针对注册表引用的契约检查。无需增加发现流程来掩盖错误建议。

### A6：search_artifacts.time_hint 被接受但完全忽略（高）

位置：`src/pal/execution/generated_tool_models.py:59`、`src/pal/artifact/service.py:534`。

schema 暴露字符串 time_hint，默认 recent；实现直接 `_ = time_hint`，实际只使用固定的 hot-state 有效性与 recency 加权。模型传入 last_week 等值也不会产生相应过滤。

建议：删除/明确拒绝无效参数，或定义真实支持的时间语义再实现；不能用 enum 包装一个仍不起作用的参数。

### A7：browser_tabs 的负索引会悄悄指向标签页 0（高）

位置：`src/pal/web_fetch/tool_models.py:111`、`src/pal/web_fetch/browser_service.py:648`。

index 没有范围/编号说明；schema 接受 -1，dispatch 使用 max(0,index)。离线通过真实 schema 和 dispatch 复现：`{operation:close,index:-1}` 生成 `tab-close -- 0`。这不只是额外轮次风险，还会选择错误操作目标。

建议：明确索引来自 tabs list，添加 ge=0 并让非法索引报错；说明 select/close 缺省 index 的含义，按底层 CLI 的真实契约决定是否要求显式值。不要替用户猜目标。

### A8：read_artifact 的 page/chunk 编号与相互关系没有说明（中）

位置：`src/pal/execution/generated_tool_models.py:42`、`src/pal/artifact/service.py:881`、`src/pal/artifact/processors.py:436`。

page/chunk 是无说明、无下界的可选整数；分块实际从 1 开始。二者同时传时 `_select_representation` 优先 page，schema 没有说明。max_chars 也没有解释与工具结果分页的区别。

建议：解释编号来源、起点及适用 representation，拒绝或明确处理互斥组合；不要把工具结果分页的 page 与 artifact 页码混为一谈。已有 `read_tool_result` 的 1-based/head/tail 说明是可参照的正例。

### A9：proactive 输出路由参数的发现入口不可靠（中）

位置：`src/pal/execution/generated_tool_models.py:1184`、`:1227`；`providers/telegram/endpoint.py:913`。

out_reply_target 的说明要求查询 endpoint auth_state 获取 session_id/request_id。但 Telegram auth_state 只有 paired/authorized/token_present；其 reply_target 使用 chat_id/message_id/thread_id 等字段。通用工具说明把一种通道的字段写成了普遍契约。

建议：通过 channel owner 返回可用目的地或提供明确的 provider 路由 schema；模型不应猜内部路由字段。schedule 目前有例子，但仍是任意 dict，未来可用基于 cadence 的输入结构表达条件必填字段。

### A10：browser 目标/键值参数缺少足够具体的语法说明（中）

位置：`src/pal/web_fetch/tool_models.py:45`–`:81`、`:111`，`src/pal/web_fetch/capabilities.py:258` 起。

target 只有 str，guidance 说 current snapshot ref or unique locator，却未提供允许的 locator 语法/示例。key、modifiers、select.value 的合法形式也未在字段中说明。模型通常需要另读手册或靠经验猜 CLI 语法。

建议：静态集合用已验证的 enum；开放表达式给示例、来源与失效边界；不能随意把 CSS、Playwright locator 或其他 CLI 的语法混用。浏览器新页面后的 ref 变化应由返回 affordance 告知，而非反复发通用操作手册。

## 不应误改的地方

- `call_tool` 已写清：已知准确 alias 和足够契约即可调用，不要求每次 read_tool。TG 那次多读说明也有模型策略因素。
- discovery 的 input_shape 是摘要，不是完整 schema。不能因此声称所有命中都可无条件调用；只在已有信息确实足够时省略 read_tool。
- 文件工具已有授权快照、批量 edits、分页；大范围读取首先检查调用参数，不能再硬截断证据。
- memory 的 kind/view 等已经有 enum 和较完整说明；分页能力不应倒退为按字符静默丢内容。
- Bunshin 的 task/subject、workflow command 等已有语义定位及部分明确 enum；本次声明扫描没有证明需要整体重构。
- `read_tool` 默认不展示完整 output_schema 是既有降噪选择。需要补的是关键结果语义，不能一律把完整 schema 再塞回每次工具结果。
- 参数可配置值来自 provider/MCP/插件时，不应强行定义一套不完整的全局 enum。

## 建议实施顺序

1. 修错误或会误选目标的契约：artifact 别名、无效 time_hint、browser 负索引、proactive 路由说明。
2. 修高频结果表达：llm_usage 默认摘要/按需详情、channel 错误时间与语义、checklist LLM/UI 分离。
3. 修发现与输入 affordance：空结果筛选建议、参数编号/冲突/语法、缺失动态能力入口。
4. 回归以“模型可见 schema → 实际校验/调用 → 模型可见结果”为单位，不写只验证说明文字非空的测试。增加错误 next-tool alias、静默忽略参数、非法目标被自动改写的反例。

本报告是审计清单，不代表全部建议已实施，也不将 TG 的全部轮次归因于工具。没有改变现有分页、执行权限或工具生命周期。


## 本轮实现结果

- A1：保留精确筛选语义；零命中时按查询匹配项返回 `filter_suggestions`，与实际 hits/facets 分开。补充 family/module 区别及 top_k 优先级。
- A2：`llm_usage(view="summary"|"detail", endpoint_id=...)`。默认只返回进程概览和 usage/cost 完整性；详情与 endpoint 桶按需读取。缺少 endpoint 记录明确返回 no_recorded_usage，不构造零费用结果。原 ledger 和用户 status 控制命令保留完整数据。
- A3：Telegram health 明确当前入站 polling 的判断范围；最近辅助错误附操作、UTC 失败时间和同类操作后续成功时间。历史错误保留，不把其他操作成功当成恢复；错误记录到本地 logger。
- A4：checklist 的模型更新结果只保留变化与进度；完整快照和 echo 留在 structured 供 UI 使用。upsert 可一次更新多个状态；最后一次 check 完成全部项目时，在 service 锁内复用 clear，返回完整完成快照并只发一次完成回显；batch upsert 保持原语义，取消、替换或批量完成后的关闭仍使用 clear。最后打勾前回顾需求与实际结果，不重复已完成的检查。
- A5：修正 read_artifact/search_artifacts 公开别名；新增 next_actions 与已注册 alias 的契约校验。
- A6：从公开 schema、工具转发及 service 签名移除无效 time_hint，旧参数由严格输入校验拒绝；文案明确只是 relevance/recency 排序，不是日期过滤。
- A7：schema 和执行层拒绝负 tab index；select 必须给编号，close 省略编号仍表示当前 tab，与实际 CLI 保持一致。
- A8：page/chunk 标明 1-based，拒绝同时传入，区分 artifact 页码与工具结果分页；说明 max_chars 是文本预览预算。
- A9：移除 auth_state 能提供目的地及通用 session_id/request_id 的错误承诺。说明使用已知 inbound reply_target 或既有 proactive 配置，Telegram 字段独立说明。本轮没有新增跨 provider 的目的地发现 API，也没有改造 schedule 输入结构。
- A10：依据本地安装的 @playwright/cli README 和 Playwright schema，补全 ref、CSS/locator、key、select value 的语法及失效边界，modifiers 使用已支持的 enum。没有新增浏览器操作或模型请求。

### 验证与生效

相关回归 169 passed（含 Telegram endpoint、browser、artifact、discovery、usage、checklist 和恢复场景）；未运行全部 CI 批次或真实网络/浏览器操作。SDK 缓存修复的测试另行记录。

修改在源码工作区，未提交或激活。execution/core 代码需主进程重新加载；Telegram 是独立 provider 源码，运行目录若为复制安装，需先更新该副本再走 channel_reload_provider，单纯重连 endpoint 不会加载新实现。没有修改运行时配置或重启正在工作的 Pal。
