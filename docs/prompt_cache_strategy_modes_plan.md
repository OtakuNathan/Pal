# Pal 缓存档位与尾部断点简化计划

状态：已实施，离线回归与形式验证通过；运行实例尚未激活。

审阅基线：`99506b6`。实施时以实际工作树为准，先检查后续变更。

本计划替代会话中“保留 OpenAI 经济 controller”的上一版方案。保留已确定的 hybrid 范围，以及 Anthropic 暂不迁移交接算法的决定。保存文档不代表修改运行配置、激活、提交或发布。

## 1. 为什么简化

假设已有可读取边界 F，新增稳定区间 D；普通输入价格为 1，写入为 w，读取为 r。当前请求之后还有 K 次完整复用，且缓存一直可读、写入按未命中的新增区间线性计费。

立即写入成本为 `(w + Kr)D`；延迟 j 个请求再写为 `[j + w + (K-j)r]D`，其中 `0 <= j <= K`。延迟多付 `j(1-r)D`。因此，对最终一定会缓存的区间，等待本身不节省写入溢价。

立即写相对永不写的收益为 `[K(1-r) - (w-1)]D`。在 `w=1.25, r=0.1` 时，有一次后续成功复用就足以覆盖写入溢价。价格依据见 [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)。

未来未知时，`E[K] > (w-1)/(1-r)` 是立即写与永不写这两个选择的期望比较；它没有证明所有“等待新信息后再决定”的自适应策略都更差。只有 K 取 0 或 1 时，27.8% 才直接是继续一次的概率门槛。

据此移除 OpenAI 新策略中的 R、estimated_net、收益触发门槛、候选三次预算、冷却和估算 ACK。首版采用明确的 eager 策略：只要存在合法的新稳定尾部，就提供断点机会，不再估计 continuation probability。它是一个简单策略选择，不承诺真实 provider 下的全局最优。

## 2. 三个档位

endpoint 在现有 `capabilities_blob.prompt_cache.mode` 中选择 `implicit`、`explicit` 或 `hybrid`。

| 档位 | 行为 |
| --- | --- |
| implicit | 不插入本地断点，不运行尾部规划；沿用必要的稳定会话标识，由上游决定缓存行为 |
| hybrid | 固定 S/T 显式断点，保持上游自动缓存；本地不管理 P/C |
| explicit | 在支持的 OpenAI 路径使用 S/T/P/C；关闭上游自动断点，机械提供最近的尾部复用机会 |

S 是稳定 system/developer 前缀末尾；T 对应当前实现的 U：普通 turn 为当前用户输入末尾，compact 后沿用现有 compact 块边界规则。T 是结构位置，不代表已确认缓存成功。

OpenAI implicit 模式允许携带额外显式断点，自动断点占用四个写入名额之一。hybrid 的 S/T 在此容量内。[协议说明](https://developers.openai.com/api/docs/guides/prompt-caching)

Anthropic 首版统一选择接口，保留现有 explicit 实现；暂不迁入新的滚动策略，也不开放新 hybrid。其历史回查和 usage 语义需要单独处理。[Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## 3. explicit 的最小本地状态

P 表示此前实际提交过的尾部位置，C 表示本次最新合法稳定尾部。两者都不是服务端成功确认。

1. 沿用 codec 的精确内容路径与前缀身份，选择 S、T，以及 T 之后最新的合法稳定 C。工具历史必须在完整协议批次边界选择，不移动到其他角色或伪造用户消息。
2. 本地只保留同一内容代次内最近两个不同的已提交尾部描述。P 取其中位于 C 之前、前缀仍完全相同的最近位置。
3. 发送去重后的 S/T/P/C，最多四个。C 与 T 重合时不额外发送；S/T 的合法断点不受收益或命中确认门槛抑制。
4. 仅在实际 provider attempt 提交时登记本次 C；反复 build 不轮换，重试相同 C 不挤掉前一个不同位置。提交身份及代次检查保证幂等。
5. 响应只更新原请求的 usage 和诊断。成功、失败、缺失 usage、迟到及重复响应都不确认或推进所谓 effective frontier。
6. turn 结束清空尾部记录；新 turn 重新定位 T。compact、模型或 endpoint 变化、前缀改写会使相应位置失效。不能仅凭 message_id 保留位置。

仍然需要的本地状态是位置、前缀身份、代次和提交身份。删除的是推测远端缓存成功的交接控制器，而非删除请求生命周期和计费幂等。

### 明确接受的失败行为

机械轮换不能保证保住最远的可用缓存。例如：

- 请求 1 成功写入 C1。
- 请求 2 携带 P=C1、C=C2，读到 C1，但 C2 没有写成功。
- 请求 3 携带 P=C2、C=C3，已经撤掉 C1；可能回退到 T，即使服务端仍保存 C1。

OpenAI explicit-only 只查当前请求提供的显式边界，因此这个反例成立。[查找规则](https://developers.openai.com/api/docs/guides/prompt-caching)

新策略没有“误确认”的状态，但仍可能撤掉一个有价值的旧位置。回退后重新写 C 还可能覆盖此前已经写过的区间，此时理想证明的“一次写入后持续增量复用”假设不成立。

这里接受最近两个尾部机会加 S/T 回退的 best-effort 行为，不重新加入 ACK 来补偿不可靠的服务端。遇到这样的 provider，可由用户选择 hybrid 或 implicit；不按命中率自动换档。社区的工具结果断点故障报告作为已知问题记录，不能承诺 hybrid 已修复它：[报告](https://community.openai.com/t/gpt-5-6-responses-api-breakpoint-on-function-call-output-is-accepted-but-never-writes-cache/1386415)。

## 4. 模块与配置改造

- 能力层根据 endpoint/provider、模型和 api_shape 列出可用档位、合法路径、容量和 TTL；能力声明表示协议支持，不保证运行时每次命中。未知组合仅允许 implicit，不用模型名称的宽泛猜测开放显式写入。
- 策略层只选择 structural anchors 和 eager tail；adapter 负责 OpenAI Responses、Chat Completions 等实际字段、精确注入、全部 marker 审计及 usage 归一。OpenRouter 转换字段也纳入审计。[OpenRouter 文档](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
- 保留 endpoint 的统一 prepare_attempt 入口、已编码 spans 与 turn 策略快照。预检和发送使用同一份 resolved policy，包含 endpoint/base URL 身份，避免两处能力判定不同。
- 新 `mode` 优先于旧配置；`enabled: false` 保持最高关闭优先级。旧 OpenAI explicit profile 映射新 eager explicit，旧 implicit 映射 implicit，旧 hybrid 映射 S/T hybrid。文档明确这两处行为迁移，不声称旧 explicit/hybrid 行为原样保留。Anthropic 旧配置保持现行语义。
- 没有 endpoint 或 model hook 选择时默认 implicit。明确选择不支持的档位，在请求前报配置错误。保留旧配置名的读取兼容，但不永久保留第二套 OpenAI R/ACK 实现。
- 模式改变在下一 turn 生效。本地状态按会话、endpoint、模型、shape 和配置代次隔离；上游已有 key 保持兼容，新 mode 的 key 不随 round 或配置代次变化。
- 各模式从未注入缓存控制的编码结果构建，清除属于 adapter 的遗留控制字段；内容身份与 marker 审计保持分开。日志记录实际档位、断点与真实 usage，缺失数据保持未知，不进入 LLM 上下文。

## 5. 离线验收与实施顺序

测试覆盖普通 turn、单 round 连续对话、多轮工具循环、compact、重复构建、重试、乱序 usage、模式切换和前缀失效。检查最终 payload 的精确路径、marker 集合、角色与内容不被策略改写，以及没有额外模型请求。

独立模拟 provider 验证两类结果：理想增量缓存下能读取上一次尾部并写入新增区间；注入 C2 写入失败时能复现上述回退，同时客户端不会谎报确认。模拟服务端状态不得进入客户端选择条件。

TLA+ 缩减为 Build、Submit、Observe、BeginTurn、Invalidate：Build 不变更尾部记录；Submit 按身份幂等轮换；Observe 不改变 marker；代次变化清理失效位置。验证断点容量、精确前缀有效性、跨代次隔离及无额外请求，不再验证“ACK 归因”或“F 永远有效”。旧模型作为历史记录标注退休。

后续分三步实施，每步随附相关回归：

1. 提取能力与档位解析，接通配置兼容和只读诊断。
2. 接入 S/T hybrid 与 eager explicit，删除 OpenAI 经济累计和估算交接代码，保留通用计费幂等；同步更新离线模型。
3. 完成 resident/Bunshin 集成验证与迁移文档。

已按后续指令实施本计划，并增加 `pal llm add --replace --cache-mode` 作为配置入口。运行配置、激活和发布尚未执行；付费对照另行安排。使用说明见 [缓存档位](prompt_cache_modes.md)。
