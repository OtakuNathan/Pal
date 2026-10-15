# Execution 工具面收口计划

日期：2026-10-15
状态：已按确认范围实施到源码；定向验证见[实施记录](pal_tool_consolidation_implementation.md)。尚未安装或激活。

## 设计原则

延续 [Browser 工具面收口计划](pal_browser_tool_consolidation_plan.md)：优先减少重复入口；简单枚举可以合并，不将多套操作专用参数重新塞进一个超级工具。

Shell 保留独立、明确的日常操作。工具描述说明用途和前置条件；出现会话时提供简短、相关的指引，不在每次输出重复整套会话管理手册。

## 当前基线

按 execution 能力及当前 native shell / remote shortcut 声明，共 24 个入口、8 个常驻直调。这是源码与当前配置口径，不是在线实例快照；当前 remote 配置生成一个 `run_shell_desktop` 快捷入口。

| 分组 | 工具 | 数量 |
| --- | --- | ---: |
| 工具发现、调用及注册状态 | `search_tools`、`read_tool`、`call_tool`、`list_tools`、`inspect_execution_state` | 5 |
| 文件 | `read_file`、`edit_file`、`write_file`、`delete_path` | 4 |
| Shell 执行 | `run_shell`、`run_shell_desktop` | 2 |
| Shell 会话 | `manage_shell_session`、`read_shell_session`、`write_shell_session`、`resize_shell_session`、`terminate_shell_session`、`release_shell_session`、`watch_shell_session`、`extend_shell_session`、`unwatch_shell_session` | 9 |
| Shell 状态 | `inspect_shell_status` | 1 |
| Remote | `list_remote`、`start_remote_target`、`shutdown_remote_target` | 3 |
| 合计 | | **24** |

## 已确认撤销的入口

### delete_path

撤销模型可调用的专用路径删除工具，文件删除由已有 shell 能力承担；不再为此保留一个等价的新工具或可搜索旧别名。

实现时盘点工具声明、恢复提示、角色投影、文档及回归测试中的引用。内部路径处理代码是否有其他消费者，单独检查后决定清理范围。

现有删除工具有 expected hash、路径类型及不可变输出快照检查，不能宣称 shell 删除具有相同的结构化契约；runtime 的快照引用和生命周期仍由原 owner 管理。

### manage_shell_session

撤销模型可调用的八操作超级入口。保留 read、write、resize、terminate、release、watch、extend、unwatch 的简单工具入口，暂不把其中几项重新组装成新的管理工具。

这只撤销模型工具面，不能直接删除独立工具共用的内部 `session_async` 分发、状态核对和输出恢复实现。

更新 `run_shell`、会话结果、状态检查、内置技能及其他工具的 next-tool 指引，避免继续推荐已撤销入口。

提示约定：

- `run_shell` 与相关工具的静态描述解释返回 `session_id` 后如何按需要读取、输入或终止。
- 首次返回会话，或发生需要操作的新状态时，必要时给出简短且相关的指引。
- 不在每次结果重复所有工具名称、参数表及生命周期说明。
- 输出导出失败继续给出准确的 `read_shell_session` 恢复入口；保留 `output_ref`，不能要求重跑生产命令。

## list_tools：由现有 domain 目录承接

当前 `list_tools` 一次返回所有注册工具的名字、用途、所属模块及调用模式，供全局清单浏览。

`search_tools(domain="shell")` 不带 query 时，已经提供该域的目录分页，每页最多 16 条名字和简短用途，支持 `directory_offset` / `next_offset`。这类目录导航不同于相关性检索的完整候选契约；后者继续默认最多 3 条，上限 5 条。

建议撤销单独 `list_tools` 入口，统一先发现 domain，再浏览指定 domain 的目录；不把全量工具契约塞进 search 结果。实施时确认无 query、无 domain 的现有返回能提供可见 domain 清单；盘点原全局清单的调用者和测试，所需行政统计或完整导出可继续使用内部 registry / CLI。

目录只是导航：选中候选后按现有规则读取完整契约。保留工具可见性和角色授权边界，不因替换入口而展示无权调用的工具。

## release 的含义与处理

`release_shell_session` 用于主动丢弃已完成会话的保留输出，或通过 `output_ref` 放弃尚未成功导出的结果。运行中的会话不能 release；取消进程由 `terminate_shell_session` 负责。

此前“再加入 release”指把它加入一个生命周期管理枚举。按本轮撤销超级入口、保留简单工具的方向，暂不采用该合并，release 独立保留。

## 明确保留的边界

- `read_file`、`edit_file`、`write_file` 分别负责读取、精确局部替换、完整内容写入，保留各自读版本和前置条件。
- `read_shell_session`、`write_shell_session`、`resize_shell_session` 不合并，避免输出、PTY 文本和终端尺寸切换参数契约。
- `watch_shell_session` 安排后台决策通知；`extend_shell_session` 延长已有有限期限；`unwatch_shell_session` 关闭主动通知，均保留原语义。
- `start_remote_target` 与 `shutdown_remote_target` 分开。当前使用场景分别为启动/WOL 与受管理的关机，启动实现仍以 target 宣告的配置动作为准；不将二者拼成新的操作枚举。
- `list_remote` 保留目标汇总、指定目标详情及按需刷新，不新增细碎状态工具。
- `inspect_execution_state` 负责注册工具及 generation 诊断；`inspect_shell_status` 负责会话及保留输出盘点，保留诊断对象区别。
- `search_tools`、`read_tool`、`call_tool` 保留发现、契约读取、执行的职责划分。

## 尚未单独确认的建议

`run_shell_desktop` 是固定 target 的常驻快捷投影，可考虑撤销并统一为 `run_shell(target=...)`。撤销前核对角色投影、固定目标约束和目标发现流程；当前尚不将其视为本轮已确认撤销项。

## 数量与验证

- 已确认撤销 delete_path 和 manage_shell_session：24 → 22，常驻数量不变。
- 若采用 list_tools 的目录承接方案：22 → 21，常驻数量不变。
- 若后续再撤销当前一个 remote shortcut：21 → 20，常驻 8 → 7。

实施时分别核对 Pal 仓库及 `pal-shell-native` 仓库的所有权和贡献指南，保持现有未提交修改。需要定向验证工具可见性、旧别名撤销、独立会话操作、会话恢复、compact 后标识恢复、domain 目录分页及角色权限。

模型评估同时检查发现、正确工具与参数、实际执行结果、重复提示量和调用轮次；不以减少名字数量直接判断改进。完整 CI 交给 GitHub；代码修改、安装文件复制与运行实例激活分别记录。
