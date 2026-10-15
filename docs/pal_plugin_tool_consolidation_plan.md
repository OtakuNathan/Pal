# Plugins 工具面收口计划

日期：2026-10-15
状态：已按确认范围实施到源码；定向验证见[实施记录](pal_tool_consolidation_implementation.md)。尚未安装或激活。

## 已确定的方向

插件管理继续提供在线能力。按 [Browser 工具面收口计划](pal_browser_tool_consolidation_plan.md) 的原则，合并参数共用的生命周期操作与扫描操作，统一 hub 和单插件诊断；包管理保留独立入口。

公开工具目标 **14 → 8**。`list_plugins` 从常驻直调降为间接目录工具后，plugins 不再占用常驻直调位置。以上为实施目标，尚未生效。

## 当前基线

依据 `src/pal/plugins/capabilities.py` 的能力声明，当前有 14 个工具，只有 `list_plugins` 为常驻直调。这是源码口径，不是在线实例的工具快照。

| 能力组 | 现有工具 | 数量 |
| --- | --- | ---: |
| 查询与诊断 | `inspect_plugin_host`、`list_plugins`、`inspect_plugin` | 3 |
| 生命周期 | `attach_plugin`、`detach_plugin`、`reload_plugin`、`enable_plugin`、`disable_plugin` | 5 |
| 扫描发现 | `rescan_plugins`、`rescan_and_attach_first_party_plugins` | 2 |
| 包管理 | `install_package`、`prepare_package`、`uninstall_plugin`、`inspect_package_status` | 4 |
| 合计 | | **14** |

## 生命周期：5 → 1

统一为 `manage_plugin`，共用必填插件名字和一个必选操作枚举：

```text
manage_plugin(name=..., operation=attach | detach | reload | enable | disable)
```

不增加按操作切换的专用参数。模型仍需明确选择动作；合并减少发现入口，不意味着五种生命周期语义可以互相替代。

| operation | 保留的行为 |
| --- | --- |
| `attach` | 加载已启用插件；保留已经加载的实例，不隐含代码刷新 |
| `detach` | 暂时卸载实例，保留 enabled 状态 |
| `reload` | 替换已启用插件的实现及所拥有的资源，按现有规则处理受影响的依赖插件；已卸载的启用插件可被加载 |
| `enable` | 启用并加载，按原有成功条件持久化启用状态 |
| `disable` | 卸载并持久化禁用状态，保留卸载失败时的原有处理 |

Runtime 显式分派到现有 host 生命周期方法，不因合并自动把 attach 升级为 enable 或 reload。保留依赖恢复、逻辑卸载已提交、资源清理未完成、失败回执和不确定结果的原有语义。

撤销五个旧模型入口，不保留可被搜索到的重复管理别名。Host 内部方法以及其他 owner 使用的生命周期接口继续保留。

模型调用采用 `reload` 这个公开动作；host 内部现有 `reattach` 实现名称可保持不变，不为统一工具面额外重命名内部协议。

## Hub 与单插件诊断：2 → 1

将 `inspect_plugin_host` 与 `inspect_plugin` 收口到 `inspect_plugin(name?)`：

- 不指定 name：返回 hub 本身的健康摘要、插件与 generation 计数、扫描错误和待清理计数，不默认展开所有插件的完整状态。
- 指定 name：返回该插件的完整相关诊断，包括 enabled/attached、生命周期状态、generation、依赖与阻塞原因、清理进度及错误、安装状态和最近错误。
- 依赖以引用形式展示，不递归展开其他插件的全部状态。
- 未找到对象时报告 unknown plugin，并引导通过插件目录发现名字。

沿用现有单插件检查对配置值的处理，不因收口把完整敏感配置默认输出给模型。安装状态暂时不可取得时保留其他可用诊断，并明确缺失部分。

## 插件目录继续保留，降为间接工具

`list_plugins` 继续返回可用插件名字、所属模块、一方/第三方来源及简短启用、挂载和生命周期状态，不返回每个插件的完整详情。

它负责发现具体插件对象；`search_tools(domain="plugin")` 负责发现插件管理工具，二者不能直接替代。

撤销 `list_plugins` 的常驻直调地位，保留间接调用和搜索发现。维护技能与恢复指引按需要引用目录，不在每次成功操作后重复要求列举插件。

## 扫描发现：2 → 1

保留一个 `rescan_plugins`，使用单个模式枚举：

```text
mode = scan | scan_and_attach_new_first_party
```

两个模式不切换额外参数。默认保持只扫描；自动挂载模式须由调用者明确选择。

- `scan`：发现目录及 manifest 元数据变化，保留现有 legacy channel provider 发现路径；不隐含重载已运行代码。
- `scan_and_attach_new_first_party`：执行同样扫描后，仅自动挂载本次新发现且已启用的一方插件。

不扩大到已有插件重载、禁用插件启用或第三方插件自动挂载。分别保留扫描错误与挂载错误，部分完成不能宣称全部激活成功；保留现有错误状态和效果回执。

撤销 `rescan_and_attach_first_party_plugins` 独立模型入口。底层复合实现可继续使用。

## 包管理四个入口保留

| 工具 | 保留原因 |
| --- | --- |
| `install_package` | 按本地包路径安装或升级，准备私有依赖，并通过所属 owner 激活 |
| `prepare_package` | 按已安装对象的名字和类型准备或修复依赖 |
| `uninstall_plugin` | 移除第三方插件，具有独立的数据清理选项及生命周期约束 |
| `inspect_package_status` | 查询异步作业的阶段、失败与激活结果，包括插件尚未注册或已经卸载的情况 |

不将不同的 path/name/kind/purge_data 参数混入一个通用包操作工具。单插件 inspection 的安装状态与特定异步 job 诊断保留各自对象边界。

保留现有完成通知、无通知时的有界等待和结果核对路径；成功结果不追加反复轮询或完整工具菜单。

## 目标工具面

| 目标入口 | 数量 |
| --- | ---: |
| `manage_plugin` | 1 |
| `inspect_plugin` | 1 |
| `list_plugins` | 1 |
| `rescan_plugins` | 1 |
| 四个现有包管理入口 | 4 |
| 合计 | **8** |

核算：生命周期减少 4 个，inspection 减少 1 个，扫描减少 1 个，合计减少 6 个。所有目标入口为间接工具。

## 实施前检查与验收

1. 确定输入契约及执行分类，盘点 host、公开 provider、技能、提示、affordance、包管理激活流程、文档及测试中的旧入口引用。
2. 仅撤销模型入口，保留内部生命周期 owner 和包管理依赖的 host 接口。更新维护指引，包括 `pal.self.maintenance` 中的插件 reload 调用方式；不把频道端点重连混入插件 generation 替换。
3. 定向验证五个动作的路由及原有 enabled/attached 持久化、依赖处理、reload 代码替换、逻辑卸载回执、清理失败与恢复行为。
4. 验证 hub 和单插件诊断范围、目录名字发现、安装诊断部分失败和未知插件处理。
5. 验证只扫描与自动挂载新一方插件的范围，覆盖禁用插件、已有插件、第三方插件、扫描错误和挂载错误。
6. 确认 native schema、工具搜索及完整契约读取只展示目标工具面；角色权限继续约束可访问的插件和生命周期动作，不能因合并而放宽授权。
7. 模型评估同时检查工具发现、枚举选择、参数合法性、真实生命周期结果和调用轮次；不以减少名字数量直接宣称效果改善。
8. 本地只跑相关定向测试，完整 CI 交给 GitHub。修改源文件、复制安装文件、运行实例激活与实际行为验证分别记录；Pal 忙碌时不追加激活请求。

本计划的已确认范围已实施到源码；安装与运行实例激活尚未进行。

## 相关模块计划

- [Browser 工具面收口计划](pal_browser_tool_consolidation_plan.md)
- [Execution 工具面收口计划](pal_execution_tool_consolidation_plan.md)
- [LSP 工具面收口计划](pal_lsp_tool_consolidation_plan.md)
- [记忆后端维护迁入 CLI 计划](pal_memory_backend_cli_plan.md)
- [情绪广播与工具面合并计划](pal_emotion_broadcast_plan.md)
