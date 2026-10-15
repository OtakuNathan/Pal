# 缓存期限预估提醒

提醒用于在长对话闲置时提示用户按需 compact，不表示 provider 已确认缓存命中。
先前的提醒直接依赖严格边界证据；该证据当前不可用，导致提醒始终处于
`boundary_evidence_unavailable`。现在提醒使用独立的本地预估，严格热缓存入口保持原有约束。

计时从成功响应完整返回、本地结算时开始，使用 monotonic clock；UTC 返回时间仅供诊断。
请求生成期间、回合提交和消息投递期间的时间不会重新计入一个完整窗口。
失败、取消、重复结算和晚到的旧请求不会延长当前窗口。只读当前 resident scope 与
endpoint/model/provider/协议/base URL 对应的成功响应；分身或其他 endpoint 的响应不续期。
端点禁用缓存时不提醒。

TTL 选择顺序是：请求实际使用的已知缓存 TTL、精确模型规则、provider 策略、通用兜底。
模型规则尚无条目，以后发现同一 provider 内模型期限不同时单独补充。
内部策略集中在 `pal.llm.cache_reminder`，不更改请求的缓存参数：

| 来源 | 当前预估窗口 |
| --- | --- |
| OpenAI | 30 分钟 |
| OpenRouter | 30 分钟（本地预估，实际取决于下游 provider） |
| Anthropic | 5 分钟；显式请求的 TTL 优先 |
| DeepSeek | 1 小时（本地提前提醒策略，provider 未承诺固定 TTL） |
| 其他 provider，包括未配置 TTL 的 GLM | 30 分钟 |

默认提前 5 分钟提醒，且上下文至少 32,768 tokens。未知 provider 因此在成功返回约
25 分钟后提醒。若整个 TTL 不长于提前量，则立即进入提醒窗口。
优先使用 provider 返回的输入 token 总量；缺失时使用本地编码估算。
提醒状态仅在内存保存，受 coordinator 容量限制；进程重启后等待下一次成功响应。

新用户活动清除旧提醒；成功回合结束后按最新响应重新安排。提醒到期会撤回，按钮仍受
当前 epoch、过期时间、运行中回合和一次性消费校验。提醒文案明确标注“预计”。点击该
预估提醒执行普通手动 compact；普通 compact 依照已有规则尝试合法的前缀复用，必要时走冷路径。
预估信息不会授权严格热缓存 compact，也不保证缓存命中或节省费用。

只读状态与参数配置仍使用 `pal core cache-reminder show/configure`，不增加模型工具。
这次修改包含 resident core/LLM 代码，源码更新后需要独立重启运行中的 Pal 才会加载；
插件 reload 无法激活这些变更。此次实现未重启在线实例，也未修改运行配置。

策略依据：[OpenAI 缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)、
[Anthropic 缓存文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、
[DeepSeek 缓存文档](https://api-docs.deepseek.com/guides/kv_cache/)、
[OpenRouter 缓存文档](https://openrouter.ai/docs/guides/best-practices/prompt-caching)。
这些文档描述 provider 行为；上表的调度估计不是 provider 的保留承诺。
