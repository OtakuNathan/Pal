# 第二次工具效率采样：Petra 重启排障

来源：`/tmp/pal/pal.log`，mtime 2026-09-23 17:44 BST。本文件是最后一次请求的上下文及回复快照，不是逐请求计时 trace；以下只能统计可见历史，不能推算精确 LLM 时延、费用或 compact 前完整轮次。

## 关键结论

样本仍使用旧版 checklist 指导、包含完整 echo 的工具结果，以及无 filter_suggestions 的空搜索结果。本轮源码修改尚未在这些调用中体现，不能当作优化后的对照样本。

重启任务从消息索引 31 的用户“重启一下”开始，到索引 74 的回复结束。其间有 20 个带 tool_call 的 assistant 消息，每个恰好一个工具调用，另有最终文字答复。不是完整 provider attempt 数，重试无法从该快照恢复。

| 类别 | 工具调用轮数 | 观察 |
| --- | ---: | --- |
| 行为/手册路径 | 7 | advise_behavior、3 次 search_tools、read_tool、skill_search、skill_inject |
| checklist 管理 | 5 | upsert、3 次 check、clear；全部单独占轮 |
| 健康工具发现 | 2 | family=endpoint 导致空结果，第二次放宽找到目标 |
| 远端执行与复查 | 4 | 查服务、重启、等待复查、原生采样 |
| channel 查询 | 2 | 列出 endpoint，再查已知 petra 的 bridge health |

## 具体浪费来源

1. **发现筛选分类与日常直觉不一致。** skill_search 是 operation namespace，模型却先用 inspect/introspection；两次错误筛选后才改用 action 找到。search_tools 的现有描述把 inspect 解释为查状态、action 解释为做事，并不能准确预测 owner 注册分类。健康检查也再次误用 family=endpoint。已经实现的零结果恢复建议能缓解，但更直接的指导是未知分类时省略 namespace/family，不能根据“只读”猜 namespace。
2. **已知名称仍走完整搜索链。** 开始发现时，查询已明确包含 pal.self.maintenance；找到 skill_inject 且知道 name 参数后，仍 read_tool(skill_search) → skill_search → skill_inject。查阅运维约束本身有理由，问题是到达手册前的多余步骤。skill_guide 只给陌生场景的 search→inject 路径，缺少同样明确的已知名称直达路径。
3. **advise_behavior 没有给出相关建议。** 输入已写明 target、launchd、权限和验证方案，返回却是 Minion 记忆吸收、文件读取、edit_file、file_state。说明本次路由匹配质量不足；不能据此断言全部 behavior 都无用，也不能把它当作有价值的前置检查。样本中的历史“必须问是否吸收记忆”规则与当前任务无关，应收窄来源规则适用范围。
4. **清单维护单独消耗轮次。** 前一段调查甚至在证据收集后才 upsert 三个 pending 项，再连续 check 三次、clear 一次。对于收尾补记，没有增加调查信息。新自动 clear 可消除末尾一轮，其他更新仍应与实际工作同轮，或在不需要清单的简单任务上省略。
5. **结果量来自命令本身。** 调查段三份 shell 结果约 15.5K、8.7K、11.5K 字符，包含重复原生栈和广泛 SQLite 全仓搜索；并未获得具体 Python 调用者/数据库归属。分页不是范围规划的替代品。应围绕“下一条证据能否区分假设”选择采样和代码搜索范围，而非再加静默截断。

## 有效行为

- run_shell 使用已配置 target=3，没有绕去手写 SSH。
- 本地同文件多段读取用了 read_file(ranges=...)，且三个文件同轮调用。
- 真正只重启一次；PID 变化得到验证，未将 bridge healthy 宣称为主运行时恢复。
- 重启后原生采样验证同一忙点复现，有诊断价值。
- 后续用户说“又卡死”时，把进程检查与采样合在一次 run_shell 中，只用了一个工具 round。这说明工具本身不要求之前那条冗长路径。

## 下一步取舍

优先激活已完成的工具修正，再做可比较采样。进一步可收紧 namespace/family 说明、已知技能名直达指导和 behavior 的不相关候选；无需增加另一套通用编排框架。重启这类已有目标与授权的任务，可将流程规划为少量具有实际依赖的步骤，但不保证固定轮数，也不跳过必要的首次运维约束检查。

本次只分析本地记录；没有连接 Petra、重启进程、检查或修改其数据库。SQLite 原生栈仍不足以证明数据库损坏。
