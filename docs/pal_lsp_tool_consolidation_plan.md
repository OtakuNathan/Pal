# LSP 工具面收口计划

日期：2026-10-15
状态：已按确认范围实施到源码；定向验证见[实施记录](pal_tool_consolidation_implementation.md)。尚未安装或激活。

## 已确定的方向

LSP 按插件能力继续提供在线工具，`rescan_lsp_servers` 保留，不迁入 CLI。

按 [Browser 工具面收口计划](pal_browser_tool_consolidation_plan.md) 的原则，优先合并重复诊断和参数完全相同的查询，不用复杂条件参数构造通用超级入口。

目标为公开工具 **15 → 10**；`prepare_lsp_workspace` 从常驻直调降为间接工具后，LSP 不再占用常驻直调位置。以下为目标，尚未实际生效。

## 当前基线

依据 `src/pal/lsp/plugin.py` 的能力声明，当前有 15 个工具，只有 `prepare_lsp_workspace` 为常驻直调。此为源码口径，不是运行实例的工具快照。

| 能力组 | 现有工具 | 数量 |
| --- | --- | ---: |
| 健康诊断 | `inspect_lsp_provider`、`inspect_lsp_status`、`diagnose_lsp_server` | 3 |
| 项目环境准备 | `prepare_lsp_workspace` | 1 |
| 错误与文档 | `read_lsp_diagnostics`、`read_lsp_hover` | 2 |
| 符号位置 | `find_lsp_definitions`、`find_lsp_implementations`、`find_lsp_references` | 3 |
| 调用关系 | `prepare_lsp_call_hierarchy`、`find_lsp_incoming_calls`、`find_lsp_outgoing_calls` | 3 |
| 符号目录 | `list_lsp_document_symbols`、`search_lsp_workspace_symbols` | 2 |
| 配置刷新 | `rescan_lsp_servers` | 1 |
| 合计 | | **15** |

## 计划调整

### 健康诊断：3 → 1

保留一个 LSP inspection 入口，按对象范围查询：

- 不指定内部对象时，查询模块及 manager 健康度、必要计数和 server 摘要，不默认展开每个 workspace/server 的全部细节。
- 指定 workspace 时，查询该 workspace 的环境准备记录、相关 server 状态及故障原因，不读取无关 workspace 的详情。
- 指定具体 server 时，查询该 server 的完整相关诊断，包括二进制可用性、项目上下文、初始化结果和故障原因。保留现有 server 选择能力；最终选择参数在实现时确定，避免重复的 file/path 等等价字段。

当前三个工具并非完全相同：provider 检查驻留宿主状态，workspace status 查询已有记录，doctor 会检查并可能初始化选定 server。

合并须明确保留这条行为边界：默认模块与 workspace 汇总查询保持被动，不因检查而启动 manager 或全部 server；具体 server 的主动诊断只针对显式选择的对象。实施时核对执行分类、角色权限及错误语义，不能把初始化效果隐藏在普通全局状态读取中。

Manager 不可用时仍返回可取得的宿主状态和故障原因；缓存观察明确标记为历史，不作为当前运行或成功查询的证据。缺少 workspace preparation 记录不等于所有 LSP 查询不可用。

最终公开名称在实施时确定，优先复用 `inspect_lsp_status`。撤下另外两个独立诊断别名，并同步修复所有恢复指引。

### 调用层级准备：1 → 0

撤销模型可调用的 `prepare_lsp_call_hierarchy`。

现有 incoming/outgoing 查询已经在内部执行 `textDocument/prepareCallHierarchy`，并查询全部返回的候选 items；其结果也包含 items。保留这套内部准备流程，模型无需为查询调用关系先单独执行准备。

保留空结果及多个候选的真实含义，不任意选第一个候选，不将空结果解释为 server 健康故障。

### 调用关系查询：2 → 1

合并 `find_lsp_incoming_calls` 和 `find_lsp_outgoing_calls`，使用一个必选方向枚举：

```text
direction = callers | callees
```

共用现有 file、line、character，以及可选 workspace 和 server 选择参数。两个方向不切换必填或互斥字段，只执行选定方向，不默认把两个方向全部查出。

保留 LSP 位置约定：行号从 0 开始，character 为从 0 开始的 UTF-16 偏移；保留来源、文件版本和实际查询结果。

最终公开名称在实施时确定，不保留可被搜索到的旧方向专用别名。

### 定义与实现查询：2 → 1

合并 `find_lsp_definitions` 和 `find_lsp_implementations`，使用一个必选查询类型枚举：

```text
kind = definition | implementation
```

共用原来相同的文件位置、workspace 和 server 选择参数。按选择返回定义位置或具体实现位置，不默认查询并返回两份结果。

`find_lsp_references` 独立保留：它查询使用位置，且有自己的 `include_declaration` 参数，不再把该参数加入新查询工具制造条件组合。

最终公开名称在实施时确定，不保留两个旧操作的独立工具别名。

### 项目环境准备降为间接工具

`prepare_lsp_workspace` 保留现有环境配置及按需预热能力，包括编译数据库、include 路径、标准和 fallback flags，但从常驻直调降为间接工具。

普通导航和诊断在项目环境可用时直接调用所需工具，由 runtime 启动或复用 server。工具搜索、静态说明及失败恢复明确这一点，不把 prepare 变成每次工作前的固定步骤。

环境准备成功时继续只报告事实；部分失败或失败时给出绑定当前 workspace、必要时绑定当前 server 的 inspection 恢复入口，不在每次结果重复整套工具菜单。

### 配置刷新继续保留在线入口

`rescan_lsp_servers` 继续作为间接插件工具，在新增或修改 LSP server 配置后刷新配置与健康状态。

它与插件 reload 的职责继续区分：rescan 读取配置，reload 更换插件及 manager 生命周期；不能相互冒充。同步更新 LSP template development 技能中的诊断和验证入口，不迁入 CLI。

## 明确保留的独立能力

- `read_lsp_diagnostics`：源文件编译或类型错误与警告，不并入 server 健康诊断。
- `read_lsp_hover`：已知位置的类型、签名及文档。
- `find_lsp_references`：符号引用位置。
- `list_lsp_document_symbols`：单个文件的符号结构。
- `search_lsp_workspace_symbols`：按名字查询项目符号，与文件符号目录使用不同输入，不合并。

成功空结果继续表示没有返回对应证据，不自动触发准备、重新扫描或健康诊断。

## 目标工具面核算

| 调整 | 减少数量 |
| --- | ---: |
| 三种健康诊断收口 | 2 |
| 撤下独立调用层级准备 | 1 |
| 合并两个调用方向 | 1 |
| 合并定义与实现查询 | 1 |
| 合计 | **5** |

目标 10 个入口：统一 inspection、workspace prepare、文件 diagnostics、hover、定义/实现查询、references、调用关系查询、文件符号、项目符号、rescan。全部为间接工具；最终名称和完整 schema 在实施时确定。

## 实施前检查与验收

1. 确定新公开名称、输入契约和执行分类，盘点 provider、生成模型、manager、技能、恢复 affordance、文档与工具发现测试中的引用。
2. 撤销旧公开入口，不误删 manager 内部仍需的协议操作、共享实现或分身 client-only 生命周期约束。
3. 定向验证被动 hub/workspace 检查、manager 不可用、历史缓存、指定 server 主动检查、无关 workspace 隔离、失败恢复和角色权限。
4. 定向验证两个查询枚举的实际路由、错误枚举拒绝、调用层级内部准备、全部候选处理、成功空结果、来源和文件版本证据。
5. 核对 native schema、搜索目录及契约读取只展示新工具面，prepare 可发现但不常驻；rescan 仍可在线调用。
6. 模型验证同时检查召回、枚举选择、参数合法性、最终结果及调用轮次，不以减少工具数量直接宣称改进。
7. 本地运行相关定向测试，完整 CI 交给 GitHub。实现、文件安装与运行实例激活分别记录；按插件实际 reload 边界安排激活。

本计划的已确认范围已实施到源码；安装与运行实例激活尚未进行。

## 相关模块计划

- [Browser 工具面收口计划](pal_browser_tool_consolidation_plan.md)
- [Execution 工具面收口计划](pal_execution_tool_consolidation_plan.md)
- [记忆后端维护迁入 CLI 计划](pal_memory_backend_cli_plan.md)
- [情绪广播与工具面合并计划](pal_emotion_broadcast_plan.md)
