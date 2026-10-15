# Browser 工具面收口计划

日期：2026-10-15
状态：已按确认范围实施到源码；定向验证见[实施记录](pal_tool_consolidation_implementation.md)。尚未安装或激活。

## 设计原则

目标是减少重复入口和模型需要协调的参数关系，保持日常浏览操作的明确语义。工具数量减少不等于模型使用效果必然提升。

- 一个枚举表达几种清晰操作、参数基本共用时，可以合并。
- 含义稳定、可以自然组合的参数可以保留，例如截图尺寸和是否全页。
- 不同操作切换整套必填、互斥字段时，暂缓合并。
- 能由 runtime 推导或处理的恢复行为不增加模型参数。
- Inspection 默认查询模块及当前会话摘要；指定内部对象时返回该对象的充分诊断信息，不默认展开全部对象详情。
- 保留有实际意义的状态设置、证据类型、执行分类及失败语义。

## 当前能力基线

依据 `src/pal/web_fetch/capabilities.py` 的公开能力声明，Browser 当前共 28 个工具，其中仅 `navigate_browser` 为常驻直调，其余 27 个为间接工具。这是源码声明口径，不代表运行实例已加载的工具快照。

| 能力组 | 现有工具 | 数量 |
| --- | --- | ---: |
| 打开与读取 | `navigate_browser`、`read_browser_page` | 2 |
| 历史导航 | `navigate_browser_history` | 1 |
| 页面结构与布局 | `capture_browser_snapshot`、`find_browser_text`、`inspect_browser_layout` | 3 |
| 截图 | `capture_browser_screenshot` | 1 |
| 鼠标与表单 | `click_browser_target`、`hover_browser_target`、`drag_browser_target`、`scroll_browser`、`fill_browser_field`、`type_browser_text`、`press_browser_key`、`select_browser_option`、`set_browser_checked_state` | 9 |
| 标签页、视口、弹窗 | `manage_browser_tabs`、`resize_browser_viewport`、`handle_browser_dialog` | 3 |
| 脚本与网络 | `evaluate_browser_script`、`fetch_browser_url`、`manage_browser_network_capture` | 3 |
| 扩展 | `inspect_browser_extensions`、`manage_browser_extension` | 2 |
| 健康度 | `inspect_browser_status` | 1 |
| 会话维护 | `close_browser`、`clear_browser_cache`、`reset_browser` | 3 |
| 合计 | | **28** |

## 已确认的收口范围：28 → 23

### 打开网页与读取正文：2 → 1

统一为一个页面读取入口：提供 URL 时打开并读取，不提供 URL 时读取当前页。最终公开名称在实现时确定，保留一个常驻入口。

两种现有能力底层已共用导航和正文提取路径。保留正文、元数据、链接以及完整文本和链接快照；读取预览之外内容时继续使用已有文件能力。

导航已完成但正文提取失败时，应读取当前页恢复正文，不重复导航。显式 URL 跳过损坏的旧页面恢复，不通过清除登录数据规避问题。

兼顾分身现有的 `read_delegate` / broker 路径和角色授权，不能因主工具面合并而扩大分身权限或破坏代理读取。

### 模块与扩展诊断：2 → 1

将 `inspect_browser_status` 与 `inspect_browser_extensions` 收口到一个 inspection 入口：

- 默认返回 Browser runtime 健康度、当前会话状态及已配置扩展摘要。
- 指定 `extension_id` 时返回该扩展的完整相关配置、可观测运行状态和诊断限制。
- 模块级健康检查在没有会话或临时分身不支持扩展时仍可使用，不因附加扩展信息整体失败。
- 不为检查状态启动浏览器；区分已配置、浏览器是否运行、实际观察到的 worker 和未验证的行为。

已配置扩展不证明扩展行为正常；缺少 worker 也不证明扩展失败。保留目标页面与 manifest 验证的提示。扩展 mount / reload / unmount 继续由 `manage_browser_extension` 负责，不合入诊断工具。

### 低频会话维护：3 → 1

统一到 `manage_browser_session`，使用一个必选 `operation` 枚举：

| operation | 行为 |
| --- | --- |
| `close` | 关闭当前会话浏览器，保留 profile 和登录态；已关闭时幂等成功 |
| `clear_cache` | 清 HTTP 缓存，保留 cookies 和站点存储；不隐含刷新、登出或删除 service-worker CacheStorage |
| `reset` | 关闭并删除当前会话 profile，包括登录态；继续要求现有显式 `confirm=true` |

不设置隐含的默认维护动作。保留各动作的执行约束和错误语义，实现时核对现有执行分类的表示方式。runtime 的恢复与资源管理路径继续独立负责自动兜底，不能把 profile 删除引入普通故障恢复。

### 撤掉独立浏览器 fetch：1 → 0

`fetch_browser_url` 退出模型工具面：

- 普通 HTTP/API 请求由主 Pal 通过 `run_shell` 调用 curl。
- 必须复用浏览器登录态时，由 `evaluate_browser_script` 在页面中执行 fetch。
- 页面内请求仍受浏览器的来源、cookies 和 CORS 规则约束，不能宣称普通 curl 自动等价。
- 角色没有 shell 或脚本权限时继续遵守原权限边界，报告能力缺口，不借替代路径扩大授权。

该调整减少一个入口，但登录态请求需要模型生成 JavaScript，不能直接宣称任务准确率提高。撤销前更新相关指导和恢复提示；写请求结果不确定时先核实实际状态，不盲目重试。记录专门工具与通用脚本在参数校验、错误报告上的差异，并纳入验证。

## 待定设计，不计入 23 个的收益

### 截图支持可选视口尺寸

支持“调整视口后截图”的组合工作流：提供尺寸时先调整实际视口再截图，不提供尺寸时沿用当前视口。尺寸变化会影响响应式布局及后续交互，不是只缩放输出图片；需要时重新取得页面结构和有效 ref。

仍需明确仅调整视口、不截图的调用路径。不能为了撤掉 `resize_browser_viewport`，强制每次调整都生成图片；也不能未经设计就把尺寸字段扩散到所有会话维护操作。因此本轮暂保留独立 resize，其撤销不计入确定收益。

### 页面快照与文本过滤

`capture_browser_snapshot` 返回页面结构及可交互元素 ref，不是像素截图。若 `find_browser_text` 可以作为同一份结构快照的过滤能力收口，且保持范围限制和有效 ref，可进一步合并。

如果实现仅变成 snapshot/find 模式分支，并要求模型协调两套不同参数契约，则暂不合并。该项还需核对现有 snapshot/find 底层语义，不预设过滤行为等价。

## 本轮保留的边界

- `fill_browser_field`、`select_browser_option`、`set_browser_checked_state` 不合成切换三套参数契约的通用表单工具。设置勾选状态不能退化为点击切换。
- `type_browser_text` 与 `press_browser_key` 暂不合并；前者向当前焦点输入文本，后者发送按键或组合键。
- click、hover、drag、scroll 保留独立入口，避免混合目标、坐标和动作专用参数。
- 结构快照、布局检查、截图分别提供控件结构、样式几何和像素证据；不默认同时返回全部内容。
- 历史导航、标签页、弹窗及扩展管理已有按对象组织的枚举，保持现有边界。
- 脚本执行与网络捕获用途不同，不继续强行合并。

## 实施与验证

1. 对齐最终公开名称、输入契约、执行分类和角色投影，再调整公开能力及搜索指导；同步清理旧工具提示和手册引用。
2. 保留正文快照、截图 artifact、会话与登录状态的所有权及现有恢复路径。
3. 定向验证打开与重读、导航成功但正文失败、分身代理读取、无会话健康检查、扩展诊断，以及三种会话维护的实际状态变化。
4. 验证独立 fetch 退出 native schema、搜索目录及可读取契约，覆盖普通请求和浏览器登录态请求的替代流程及权限限制。
5. 用模型对比原方案与收口方案。除首次及补搜召回，还要检查操作选择、参数合法性、最终页面状态、调用轮次和错误恢复；不能只按旧工具名命中或新入口命中判成功。
6. 本地运行相关定向测试，完整 CI 交给 GitHub。未完成实现前不宣称 23 个已生效或准确率已改善。

本文件保留审查时的基线与范围；已确认部分的实现状态见实施记录。待定项目仍不在本轮范围内。

## 相关模块计划

- [情绪广播与工具面合并计划](pal_emotion_broadcast_plan.md)
- [记忆后端维护迁入 CLI 计划](pal_memory_backend_cli_plan.md)
