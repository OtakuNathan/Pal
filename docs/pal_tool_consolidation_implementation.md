# 工具面收口实施记录

2026-10-15，用户授权逐个实施本轮六份计划。保留既有 LLM/endpoint/向导及其他无关工作树修改。以下按实施顺序记录当时状态；最终清理与验证见文末。未复制安装文件或激活运行实例。

## 已实施范围

| 模块 | 审查基线 → 当前源码 | 实现 |
| --- | --- | --- |
| LSP | 15 → 10 | 全部间接调用；统一 hub/workspace/单 server inspection，位置查询用 kind，调用关系用 direction；保留 rescan，预备调用链由内部处理。 |
| Plugins | 14 → 8 | manage_plugin(name, operation)；inspect_plugin(name?)；扫描模式合并；保留摘要目录和四个包工具。 |
| Execution | 24 → 21（含当前 remote shortcut） | 撤下 delete_path、list_tools 和 native shell 的超级会话入口；保留八个简单会话动作、run_shell_desktop、WOL 与关机的独立入口。 |
| Browser | 28 → 23 | read_browser_page(url?) 统一打开/读取且保持唯一常驻入口；扩展 inspection 合并；会话维护用枚举，reset 仍要求 confirm=true；撤下 fetch。 |
| Artifact | 8 → 4 | 文件归一化与描述；仅图片在输入渲染时附内容，list/search 合并，read/grep/select 撤下，待统一测试。 |
| Bunshin | 13 → 11 | pause/resume/cancel/archive 统一控制；rebind 降为间接，start 保留直调，角色工具不改，待统一测试。 |
| MCP | 7 → 5 | 连接控制枚举；artifact 转换下沉到 MCP 调用适配层，撤销独立图片工具，待统一测试。 |
| Skill | 8 → 7 | 保留独立 hub/单体查询；停用并入 update_skill，统一版本及文件/数据库写入路径，待统一测试。 |
| Proactive | 11 → 8 | 输出 channel/target 合为 set_proactive_destination；启用/停用/删除合为 manage_proactive_task；调度独立，控制合并待统一测试。 |
| Channel | 11 → 7 | manage_channel_endpoint(name, operation) 五合一；查询、发送和凭据工具保留。 |
| Memory | 减少 6 个后端维护入口 | pal memory CLI 通过 resident owner 管理后端；离线 embedding 迁移；模型只保留日常工具和健康摘要。 |
| Emotion | 3 个情绪动作 → 1 个 | 独立协调插件 show_emotion(emotion)；渲染端词表并集、广播到所有启用端点、不支持则跳过、失败独立报告；不暴露 duration。 |

表中沿用各计划审查时的模块基线，不宣称当前在线实例已采用新数量。Browser 的截图尺寸/纯 resize 及 snapshot/filter 合并仍属待定，不在本轮实施范围。

前六块完成时，`audit_tool_surface.py` 导出 Pal 自有源码联合目录：130 个 resident_union 别名、36 个 role_only 别名；本次 Channel 五合一后审计确认主工具为 126 个；resident 中 20 个 native（17 direct + 3 native opt-in）。它排除运行时安装的 remote/eye/face/desktop 和外部 MCP 工具，不等于此前在线实例的 173/174 个工具口径。Emotion 基础契约纳入静态审计，实际词表由当前渲染端动态生成。

## 关键行为

- Hub inspection 默认摘要；指定对象后才返回那个对象的充分诊断。LSP 的无参数 inspection 不启动 sidecar，workspace-only 不主动探测 server；显式 name/file 才主动诊断。
- Browser inspection 不启动 sidecar/浏览器；配置和 worker 观察不等于扩展行为已验证。页面完整快照、代理读取、登录状态及 ref 恢复边界保留。
- Memory hub 的后端计数来自被动 owner 摘要，不发起 embedding 网络健康探测；详细探测仍在 CLI。Memory CLI 在线故障不降级成直接改数据库；后端控制在忙时拒绝，selection 明确只在当前运行实例生效。原 provider_id/mem_ref 核对保留。
- Embedding 迁移在宿主停止、持有租约时进行。先构建未发布 generation，验证原内容/引用、完整向量和样本检索，再发布和写配置。同维度不同模型仍重建；中断可续跑。发布后配置尚未完成时阻止宿主启动；恢复核对完整配置摘要。原 generation、workflow pins 和私有配置备份保留。详见 [CLI 说明](pal_memory_cli.md)。
- 情绪贡献随插件 generation 注册/注销。依赖闭包撤下时不会被贡献清理回调重新发布；reload 后旧渲染端不再收到调用。多个桌面端点各接受一次，时长和状态恢复继续归各 runtime。
- Native shell 原有 compact 后 retained output_ref 恢复修改保留；本轮没有用重跑命令替代输出恢复。

## 源码与打包

Pal、`../pal-shell-native` 和 `../desktop_avatar` 都有改动，需要一起审查和按所有权发布。Pal wheel 已成功构建，确认含 emotion runtime 和 builtin manifest。

未找到 st7789_face 原始仓库，已将安装快照导入版本化 [plugins/st7789_face](../plugins/st7789_face/README.md)，记录来源与原文件哈希，再修改渲染接入。新 host-Python 包成功构建于 `/tmp/st7789_face-consolidated/plugin-st7789_face-0.1.1.palpkg`。这不代表硬件或 sidecar 已实际验证；安装前须对比目标配置和独立修改。若找到原仓库，应回迁这些变更。

## 定向验证

本地未跑完整四批 CI，以下是模块定向验证，数字之间有重复覆盖，不能相加当作去重总数：

- LSP 及相关发现/结果投影：原一轮 437 项通过，三处旧测试接口引用修正后，相关 38 项复测通过。
- Plugins 生命周期/包管理：55 项通过；工具面、发现与加载相关 81 项通过。该加载测试进程退出时有临时 Bunshin 数据库清理后的后台错误日志，pytest 本身通过。
- Execution 文件、失败回执、配置可见性及展示：123 项通过。删除后端回归通过测试专用 adapter 保留，不重新公开生产工具。
- Native shell production/session actions：34 项通过，两项因重复提示失败；修正后这两项复测通过。追加远端、分身权限、失败契约及观察/输出恢复：81 项、19 个 subtests 通过；同时更新 remote 手册和技能引用。
- Browser 导航、扩展、恢复、正文快照、网络脚本、截图及错误保真：120 项通过。
- 记忆 CLI、embedding、generation、对象检查及发现词表：326 项通过；追加迁移保护与词表检查复测 10 项通过。
- 情绪广播、face、本轮新增 inspection/CLI、工具发现与 import boundary 等组合：134 项通过。
- 评测机制、记忆任务、channel 插件与技能：62 项、25 个 subtests 通过；新增合并动作判分两项通过。评测不再将 reload 误选 attach 仅凭相同工具名计为正确。
- Bootstrap 的 backend 重载、维护工具撤销及记忆读写往返：3 项通过。另有两组 architecture 定向检查分别 11 项和 7 项通过。
- Desktop 多端点、别名、本地 runtime 时长及 factory：3 项通过；未进行浏览器/物理设备端到端验证。
- 追加记忆 hub 被动摘要、CLI 与任务评测：50 项通过。

本轮新增六模块回归文件合跑 41 项通过。最新源码目录/词表审计两项通过；三个仓库 diff 格式检查通过。完整 CI 交给 GitHub。

## 模型冒烟

新增 `scripts/benchmark_consolidated_tools.py`，预先固定 16 个中文场景；只执行发现，对业务调用校验工具、direct/indirect 路由、schema 和关键参数，不实际执行。模型为此前指定的 OpenRouter `openai/gpt-6-luna`，单次采样，最多 6 轮，fixture 有自己的挂载目录，初始只开放发现入口。

结果 15/16（93.75%）通过，平均 3.5625 个响应轮次；1 个 round_limit，无 endpoint 失败。插件 reload/attach/enable、Browser close/cache/reset、LSP definition/implementation 和 callers/callees 的枚举都正确；旧记忆后端切换工具不存在的场景也正确拒绝替代。

唯一失败为重读当前页：模型首搜自行加 namespace=action 和 module_name=browser，过滤掉真实候选；后续仍猜内部 module filter。它在目录第二页找到了 read_browser_page 并读了契约，但已耗完 6 轮。该失败保留在原结果中。冻结原 query、仅移除猜测的分类过滤器可重新找到 read_browser_page，诊断不另作模型成功计分。

原始记录在忽略目录 `test-logs/tool-consolidation-luna-20261015/`（manifest/runs/report/miss-diagnostic）。这是新契约小样本冒烟，不是全量工具召回率、生产流量测量或旧版本的可比前后实验；实际生产中常驻工具初始即有契约，本评测初始只给发现入口。

## 激活边界

未修改安装目录或现有 runtime 配置，也未向运行中的 Pal 发送激活消息。Resident core/execution/CLI 修改需要宿主代码更新和独立重启安排；插件代码用 manage_plugin(operation=reload) 替换已有 generation。Rescan 只更新发现，attach 保留既有实例，均不能替代代码激活。Pal 忙时延期。

## 追加 Channel 五合一

用户随后明确授权直接实施五合一。本次只修改端点控制公开入口，未开展凭据迁 CLI，也未合并发送/查询。五个旧别名撤销；原 Python handler 留作内部 owner 委派。同步更新恢复 affordance、生命周期锁识别、失败窗口的 canonical 选择、内置技能及评测；评测按 operation 区分 attach 与 restart，模拟反馈也按 operation 匹配，防止合并后错误计分。

源码变化未安装或激活；验证结果见本节后续记录。

本轮已验证：端点 attach/发送/错误回执及生命周期锁 58 项通过；发现词表、目录和调用评测 319 项通过；新增合并与 recovery socket 保护、相关 architecture 定向检查 15 项通过。审计确认 Channel 7 个间接工具，Pal 源码主工具联合目录 126 个，角色工具 36 个另计，native 20 个不变。本次未重新跑付费模型评测或全量 CI。

补充验证完成：Channel 合并/插件集成/对象诊断组合 31 项通过；模拟恢复反馈按 operation 匹配、动作判分和根模块入口检查 5 项通过。工作树格式检查通过。本轮未提交、push、复制安装文件或激活运行实例。

## 追加 Proactive 目的地合并

本轮仅获授权合并 `set_proactive_output_channel` 和
`set_proactive_output_target`，统一为间接本地写入工具
`set_proactive_destination(name, out_channel_name?, out_reply_target?)`。
启用、停用、删除和调度修改保持独立，没有实施此前讨论的控制枚举。

省略 channel 保留当前端点；显式 null 清空 channel 和 target；只提供任务名
拒绝执行。改端点而不提供 target 时重新解析当前/默认目的地，不能继承旧端点
的 target。显式空 target 对象只清除 target；null target 无效。所有保留或指定
端点的更新统一经 Channel resolver 验证，显式清空 target 也先核对端点存在，
不会重新填入默认 target。channel/target 一次持久化后发布；验证或持久化
失败不改变内存定义及已有存储。

两个旧公开别名及其 schema 已撤销；同步迁移现有调用测试和发现说明。
当前源码审计：Proactive 10 个入口，Pal 主工具联合目录 125 个、角色工具
36 个另计，native 20 个不变。它不是运行实例目录，也不是模型召回率。
本轮不重新跑付费模型评测，不进行安装或运行态激活。

验证：目的地行为、channel/proactive 错误回执、源码目录审计及发现词表组合
361 项通过；原 architecture 的 proactive 挂载与管理两项通过。
测试覆盖目标单改及持久化、显式清空、跨端点不继承、未知端点与错误 provider
目标拒绝、空参数不误清空、存储失败不发布半更新、旧工具不可读不可调用。
工作树格式检查通过。未跑完整四批 CI；未提交或激活运行实例。

## 追加 Proactive 控制三合一（待统一验证）

用户随后授权将 enable/disable/delete 三个同参数动作合为
`manage_proactive_task(name, operation=enable|disable|delete)`，并明确本轮先改，
暂不跑测试，后面与其他模块一起验证。原三个公开 alias 和输入 schema
撤下，内部 handler 保留；新入口使用原有间接本地写入语义并委派现有 owner。
暂停仅影响后续调度，不取消已经在运行的任务；删除移除任务定义。

现有调用及回归引用、发现词表和模型评测目标同步迁移；调用型评测校验
operation，避免暂停误选删除却按相同 alias 判为正确。新增/迁移用例涵盖
必填枚举、旧工具撤销、真实启停删除及持久化、参数错误不触达 owner、
持久化错误保真和动作判分。上述用例本轮均未执行；前一节 363 项通过的
结果属于目的地合并，不能作为本次控制合并的验证结果。

按声明变化推算 Proactive 10 → 8，Pal 主工具联合目录 125 → 123；角色
36 个、native 20 个不变。目录审计也留待下一批验证。未提交或激活。

## 追加 Skill 停用合并（待统一验证）

用户明确拒绝合并 hub inspection 与单技能读取，仅同意将停用并入
`update_skill`。查询和注入的参数、行为保持原样；prepare/commit 保持分离。
模型用 `patch={"status":"disabled","enabled":false}` 停用，恢复仍需
`patch={"status":"active","enabled":true}`，没有额外改变状态字段语义。

撤销 disable_skill 公开入口及其输入/输出 schema，给 update_skill 补充停用、
恢复的用途、检索词及参数说明。内部 SkillService.disable_skill 兼容调用
也委派到 update_skill，使文件镜像、数据库、版本和重复 patch 的 no-op
处理走同一条路径；底层 repository 方法和旧 capability 的防御性 deny 项保留。
继承 update 的不确定写入核对要求，不宣称文件和数据库具有跨存储事务。

同步更新说明测试、发现词表、只读 worker 的写入不可见断言；新增公开工具
撤销、真实停用/恢复的文件与版本、重复更新以及文件失败回执用例。按用户
此前要求本轮未执行测试或导入式目录审计，只做语法与格式检查。

按声明推算 Skill 8 → 7，主工具联合目录 123 → 122，角色 36、native 20
不变。上一轮已通过的测试不作为本次变更的验证结果。未提交或激活。

## 追加 MCP 控制与附件适配（待统一验证）

用户授权 attach/detach 合为 `manage_mcp_server(name, operation)`，原 handler、
投影更新和故障隔离逻辑保留；旧两个别名撤下。独立 prepare_mcp_image 及
其实现/schema 撤销，能力下沉到调用适配层。

本地 server 配置 `artifact_inputs.<external_tool>.<parameter> = representation`
明确 path/url/data_url/base64，不从模糊的字符串参数名猜测。配置的顶层
string 参数额外接受只含 artifact_id 的对象；宿主按当前 turn 的附件 scope
调用 Artifact owner，转换后直接交给 server，按原始 schema 再验证。
未配置参数和 prompt 模板不改变，未知参数/非 string 映射拒绝配置。

URL 转换使用本地 data URL，不暴露下载来源里的凭据，也不暗示存在 HTTP
文件托管。path 需要目标 server 能访问本机文件；仅接受 HTTP(S) 的 server
仍需显式托管集成。编码限制每源 8 MiB，并限制总参数体积；失败在外部
调用前报告 not_started。运行时配置尚未编辑，映射需按实际 server 参数配置。

同步现有公开入口调用、错误回执与凭据保护用例；补充 facade 接收附件引用、
原始 schema 校验、scope/文件失败不调 server、格式转换、编码限制、管理
枚举和旧入口撤销用例。遵照此前安排，本轮不执行测试、付费模型评测或
导入式目录审计；语法和格式检查见最终记录。

按声明推算 MCP 7 → 5，主工具联合目录 122 → 120，角色 36、native 20
不变。未提交、安装或激活运行实例。

本轮语法解析与 git diff --check 通过；未执行测试或模型调用。调用型评测已
补充 MCP attach/detach 的 operation 判分，相关用例同样待下一批执行。

## 追加 Bunshin 控制合并与 rebind 降级（待统一验证）

用户同意合并控制并明确要求 rebind 不常驻。公开入口
`control_bunshin_workflow(task?, operation=pause|resume|cancel|archive, reason?)`
替代原 control/resume/archive 三入口；command 参数改为 operation，不保留
旧参数或两个旧别名。原 handler 与 service 状态机保持不变：archive 选择
包含终态工作流，暂停/取消仍是异步请求；resume 仍能将孤儿工作项转入
triage；重复归档保留 effect=none。restart、triage、人工决策、问答独立。

rebind_bunshin_task_delivery 改为 INDIRECT_CONTROL，原 actor、目的地、
Manager-owned binding 核对保持不变；start 仍 DIRECT_CONTROL。内部
resume/archive 方法保留，但不再发布独立工具和输入模型。

迁移现有归档行为及错误保真测试、精确工具面和发现词表；补充枚举与旧
入口撤销、操作分派与终态选择、triage 回执、重复归档、rebind 调用模式和
目的地绑定用例。调用评测按 operation 判分。本轮未执行测试、付费模型
评测或导入式审计；只做语法与格式检查。

按声明推算 Bunshin 13 → 11，主工具联合目录 120 → 118；native 20 → 19
（direct 17 → 16，三个 native opt-in 不变），角色 36 个不变。未提交、
安装或激活运行实例；待与前面的 Proactive/Skill/MCP 一起验证。

本轮语法解析与 git diff --check 通过；测试及模型调用均未执行。

## 追加 Artifact normalizer（待统一验证）

用户确认 Artifact 定位为文件接入/归一化/描述，不再承担模型正文阅读。
统一描述包含原文件名、扩展名、MIME、大小、状态、只读原始及处理后路径；
PDF 额外返回页码索引导航。未知格式如 xlsx 保留原文件并说明未解析，
不把没有 parser 当作接收失败。

文本无编码变化时复用 managed original；需要编码转换时只增加一份 UTF-8
文本，不再生成 chunks。PDF 新接入只生成逐页文本/图片和 page_index，
取消重复全文、chunks 及全文行号偏移。保留原页号、部分处理与失败状态。
旧附件文件不主动迁移或删除，内部兼容读取仍可用。

沿用 channel ingress 和本地 import 的 ArtifactRefPartIR：L1 保存引用，
构造模型输入时才生成文件描述及图片内容。只有 kind=image 且视觉能力/
预算允许时附像素；文本、音频转录及 PDF 内容不内联，PDF 按需导入指定
页图。历史 summary 正文预览不进入新引用或自动描述。inspect 使用同一
描述 builder，不返回数据库/representation/lifecycle 内部清单。

模型面剩 import/search/inspect_info/inspect_state 四个间接入口；list 合并
到 search（空 query 列举、limit/offset、total/has_more/next_offset），read/
grep/select 公开 schema 和入口撤销。保留 runtime select 及现有 pending/L1/
compact 租约，未改变作用域、过期或物理清理所有权。

迁移发现、评测场景、既有附件行为测试；新增单份文本/编码转换、PDF
无重复文件与部分页面、xlsx passthrough、只注入图片、引用不被渲染回写、
文件描述不含正文/内部字段、完整分页及跨 scope 拒绝用例。所有新增与
迁移测试本轮均未执行，留与前面模块统一跑。旧模型评测记录不改写，
修改后的场景不与旧结果冒充严格前后对比。

按声明推算 Artifact 8 → 4，主工具联合目录 118 → 114，native 19、角色
36 不变。本轮不运行模型评测、导入式审计或全量测试，不提交、不激活。

本轮 git diff --check、变更 Python 文件的语法解析及 Artifact 输入模型静态
引用核对通过；没有导入运行时、执行测试或调用模型。

## 追加 Web Search 无参数全量诊断（待统一验证）

用户明确要求体量小的 Web Search 只保留一个无参数诊断能力，不按参数
切换 hub/单 provider 语义。inspect_web_search() 一次返回 hub 选择/数量/
生命周期和所有 provider 的公开配置、auth、最近请求 health，不分页或截取。
旧 hub/list/provider 三个公开入口撤下；search_web 常驻及内部 fallback 不变。
维护操作继续使用 pal web-search CLI；Checklist 本轮只讨论，未作修改。

独立 auth/health 读取失败保留其他信息，并汇总到顶层 partial；每条模型可见
错误去除 traceback 并限 600 字符，不覆盖 resident 完整报告。健康明确为
配置和最近请求观测，不主动联网探测。同步发现词表、旧入口恢复提示、
配置评测适配和文档；新增无参数拒绝选择器、全量/空目录、被动查询、错误
摘要与部分失败用例。按此前安排，新增和迁移测试均留到其他模块一起执行。

按声明推算 Web Search 4 → 2，主工具联合目录 114 → 112，search-only
95 → 93，native 19、角色 36 不变，合计 148。未导入运行态做目录审计，
未运行测试或模型评测，未提交、安装或激活。

本轮变更 Python 文件的 AST 语法解析、review JSON 解析、Web Search 两个
公开声明的静态核对及 git diff --check 通过；没有导入运行态或执行测试。

## 追加 LLM 无参数查询与 active 标记（待统一验证）

用户同意三个查询入口合为 inspect_llm()，并要求在 endpoint 列表像 inline
keyboard 一样仅标 (active)。新工具无参数，一次返回全部已加载/启用
endpoint 的公开能力和限制、当前 thinking、进程累计与各 endpoint 用量
摘要。顶层只保留配置/有效 ID，不另展开 active 模型能力；原 ID 不加标记。

用量一次读取并投影，保留费用/上报完整性，不展开 latest-request 字段、
缓存内部策略或原始记账。无记录和不可用均显式区分，不能解释为零成本；
进程总量可包含已从当前目录移除的 endpoint。修复 thinking_status 字典被
取 __dict__ 的问题；thinking/usage 独立失败，错误摘要有界。

旧 list/active/endpoint 三别名和专用模型参数 schema 撤下；内部 reader、
人类 /status、LLM transport 改造和配置保持原有逻辑。同步技能引用、
发现/评测用例、失败恢复 canonical 选择和文档，并顺手去除上一轮 Web
Search 失败恢复中已撤下的目录/单 provider canonical 引用。

新增单次 active 标记、无重复能力、全量与空目录、未知选择、thinking
字典、一次记账读取/摘要、部分失败、只读与无参约束、旧入口撤销及失败
恢复用例。按用户安排，本轮不执行测试、导入式审计或模型评测。

按声明推算 LLM 3 → 1，主工具目录 112 → 110，search-only 93 → 91，
native 19、角色 36 不变，合计 146。未提交、安装或激活运行实例。

本轮 137 个变更/新增 Python 文件 AST 语法解析、review JSON、LLM 唯一
无参数间接声明、全仓 generated schema 导入静态核对和 git diff --check
通过。未执行测试、导入运行态或调用评测模型。

## 追加 Failure 单入口与证据文件（待统一验证）

用户同意 Failure 两入口合为无参数 inspect_failure()：统计与最近 16 条
诊断摘要一次返回，不再重复最近四条。摘要保留诊断/动作/验证/重试语义，
文本和动作数有界并标明截断；原报告窗口完整复制到一个只读 JSON 内容
结果快照，返回路径供 rg/read_file 查看，使用现有诊断脱敏规则。

结果显式携带 snapshot_refs，复用当前对话 L1/compact 的结果文件所有权，
不新建孤立的证据目录。没有报告不落文件；存储上下文缺失、序列化或
落盘失败时保留摘要并标明 partial，不伪造路径或回退内联原始 evidence。
报告生成、最多 32 条 resident 保留、故障升级流程和重试判定不变。

旧 inspect_failure_state/list_failure_reports 两别名撤下，canonical show
保持用于默认故障恢复选择；内部 recent_reports reader 保留。同步发现
词表、撤销入口断言和文档，补充固定窗口、只读文件/引用、空记录不落盘、
文本截断、完整证据与脱敏、存储失败降级、调用归一化及无参约束用例。

按声明推算 Failure 2 → 1，主目录 110 → 109，search-only 91 → 90，
native 19、角色 36 不变，合计 145。遵照此前安排，不运行测试、模型
评测或导入式审计，未提交、安装或激活运行实例。

本轮 138 个变更/新增 Python 文件 AST 语法解析、review JSON、Failure
唯一无参数间接声明静态核对及 git diff --check 通过；未执行测试或导入
运行态，行为验证仍待统一批次。

## 全量发现元数据对齐（定义级审计，回归测试待统一执行）

本轮用户要求检查搜索词、search_objects、说明和 use_when 是否跟上工具
合并。定义级导出覆盖 145 个独立契约：主工具 109、角色 36；全部指导
字段/词表规则通过实际模型定义校验，145 份输入 schema 可解析。AST 按
声明确认 direct 16、native 19（含三个间接 opt-in）、search-only 90。
没有构造 Pal runtime、调用业务工具或读取/修改运行实例配置。

修改 18 个工具的发现元数据/说明：MCP/Proactive 控制枚举显式进入索引；
Artifact 去除 representation 旧对象词并说明文件归一化/未知格式保留；
Checklist 清理、取消和进行中状态；Memory 完整内容替换；Bunshin 状态/
人工审查读取和工作流专用 artifact；Browser 扩展诊断；Skill pending
candidate 恢复；MCP 观测诊断和隔离错误；目的地变更等辅助意图与能力
对应。修正空目录不能证明配置不存在的指引，Web Search 失败转向统一
诊断。两个公开参数描述分别撤掉旧 memory provider 目录工具和旧附件
展示名称。动作允许值、执行逻辑、工具数量和搜索排序代码均未改。

需区分模型说明和检索材料：当前 domain purpose 检索使用肯定的 purpose、
工具辅助词/选择的动作枚举、search_intents；search_objects 支持关键词
阶段。use_when/do_not_use_when、失败指引、后续工具提示及任意参数说明
不当作能力索引。把真实支持、但仅存在于 use_when 的意图移入肯定的
purpose/辅助意图，不索引整段否定和跳转文字。CSV 导出补齐这些指导字段
以便同表复查；词表回归 fixture 也补入真实使用的意图与声明 domain 信息。

清点结束后，当前指导/输入 schema 中已撤下工具的引用为零，空必填指导
为零，失效 next-tool hint 为零；旧的未公开兼容方法/历史报告不算当前契约。
另外只读查看了 native shell/remote 和受版本控制 face/desktop 渲染贡献的
源码边界，没有修改兄弟仓库或安装目录；这些不计入 Pal 145 个源声明。
外部 MCP 服务和其他运行实例插件的动态契约仍不在本次源码联合目录内。

20 条人工挑选的、明确 domain 的英语查询用于定位和修正词表缺口，同一
组 top-3 回放从 16/20 到 20/20，top-1 从 16/20 到 20/20。样本参与了
元数据调整，不能当成独立评测或真实模型召回率，且未验证模型选 domain
和参数的能力。记录及完整指导 CSV：
`test-logs/tool-guidance-alignment-2026-10-15/{report.json,catalog.csv,before.json,after.json}`。

本轮只运行定义级检查、纯检索函数词表回放、语法与 diff 格式检查；新增
和迁移 pytest 用例继续留到统一批次，未调用评测模型、未提交或激活。

## 提交前过期测试清理与隔离验证（2026-10-15）

清理 Browser 常驻入口的矛盾断言、Plugin/MCP 恢复动作的旧别名、LSP 导航方法的旧接口、已撤销后端维护工具的说明断言，以及合并后重复的用例/允许集合。保留旧别名不可调用、作用域/TTL/回收、错误保真和生命周期取消顺序等有效回归。词法拒绝与 purpose 候选回退分别验证，已知 Artifact 域的列举使用明确 domain。

同时修复保留的 Artifact 内部内容搜索中未定义变量，补齐 Memory embedding 迁移所需的配置辅助函数；新增动态访问边界逐项记录原因。提交内容导出到独立源码目录，验证不依赖尚未提交的 LLM WebSocket 改造。

验证记录分组不可相加：初始定向组合 593 项通过、13 项失败，随后迁移旧断言并修复真实问题；独立提交目录 572 项通过、7 项失败，其后针对这些问题与生命周期修改的复测 21 项全部通过。另一个生命周期/错误回执组合 122 项通过、1 个旧 LSP 方法调用失败，修正后的该用例已包含在 21 项复测中。形式化清单检查通过。完整四批跨平台回归留给 GitHub CI。

模型全量评估见 [GLM/DeepSeek 对比](pal_tool_discovery_evaluation_2026-10-15.md)。原始模型日志不进入提交。LLM WebSocket/传输实现、数据库 schema 和向导传输配置等既有工作保留在工作区。未更新安装目录、配置或运行中的 Pal。

本地契约 mypy 首次分析持续约 7 分钟尚未完成，已停止并交由现有 GitHub contracts job 执行，不记为通过。

## 首轮 GitHub CI 失败项跟进

首轮契约检查（含 mypy）与 Ubuntu/macOS 四个 Bunshin 批次全部通过；两平台 core-a/core-b 共暴露 10 个不同失败用例。迁移遗留的工具目录入口、LSP 调用模式、只读插件目录暴露、记忆后端内部生命周期断言、附件错误注入方法和 checklist 说明断言。工具说明审计改读完整源码目录，覆盖 emotion 动态契约；ID 来源检查接受附件描述、已有记忆结果及官方 CLI 等实际来源。Browser 评测 fixture 现在支持合并后的 read 动作。

MCP 限额用例不再通过模块路径修改常量：插件重载后，该模块可能与收集阶段导入函数的 globals 不同。改用实际超过 8 MiB 的稀疏文件，验证编码拒绝及 path 模式保留；生产限制未放宽。独立提交源码中按插件重载在前的顺序执行，67 项全部通过。

配套 shell 的 prototype host 保持自己的测试专用 multiplexer，并将共享说明中的下一工具提示映射到它实际注册的入口。SSH 测试改为等待监听就绪，替代固定 150 ms 延迟；相关 20 项通过。Desktop 测试直接验证渲染端公开情绪词表，避免以源码引号风格判断支持能力；相关 10 项通过。再次交由 GitHub 全量验证；未激活运行实例。
