# Provider Continuation Contract Matrix

**状态：P0 事实基线 + 待验证项（TBD）。未知一律标 TBD，不填 none。**
**日期：2026-09-18。基线：`7e0b1f74`。数据来源：`~/.pal/pal.sqlite3` 表 `llm_endpoints`（只读查询，未含密钥）、`~/.pal/config.toml` 活动端点、PLAN.md [W1]–[W3] 引用的官方续接文档。**

## 使用规则

1. `provider_id`/endpoint_id 只作为**已配置的 policy selector**；不从厂商名字推断完整兼容性（PLAN §7）。
2. 同一厂商 + shape 可因模型版本、网关、thinking/tool 配置而不同；本矩阵按 endpoint 逐行记录。
3. TBD 项必须在 P2 用真实 fixture（10.4 规范：脱敏、不新增付费调用）验证后才能改写为结论。
4. 本矩阵只覆盖 ContinuationContract 轴；PromptCacheProfile（implicit/hybrid/explicit）是独立轴，另行记录。

## 现役舰队（11 个 enabled 端点）

| endpoint_id | model_id | wire_shape | 必需 native 续接（已验证/待验证） | 缺失时行为（目标） |
|---|---|---|---|---|
| **deepseek-v4.1-flash ★ACTIVE** | deepseek-flash | anthropic_messages | Anthropic-compat thinking 块：thinking 开启时须回传完整未修改 thinking 块（含 signature/redacted、原顺序）[W2 类比]；DeepSeek thinking+tools 要求 reasoning_content 完整回传 [W3]。**TBD-P2**：本网关 anthropic-compat 是否实际下发 signature 字段（fixture 验证） | ContinuationUnavailable；不得静默剥 thinking 继续 |
| deepseek-reasoner | deepseek-v4.1-pro | anthropic_messages | 同上家族；reasoner 为 thinking-first 模型，[W3] 约束更强。**TBD-P2**：signature 下发行为 | 同上 |
| glm-5.3 | glm-5.3 | openai_completion | **TBD-P2**：Zhipu chat-completions 的 reasoning 字段续接要求未核对（Zhipu 官方文档尚未如 W1–W3 那样引用核对；response_hooks.py 已有 Zhipu normalizer 可参考解码侧） | 验证前按 RequiredNative 保守处理 |
| glm-5.3-bigmodel | glm-5.3 | openai_completion | 同 glm-5.3 | 同上 |
| glm-5.3-bigmodel-anthropic | glm-5.3 | anthropic_messages | 同家族 anthropic 形态。**TBD-P2**：thinking 块/signature 行为 | 同上 |
| glm-5.3-flash | glm-5.3-flash | openai_completion | 同 glm-5.3 | 同上 |
| glm-5.3-flash-bigmodel | glm-5.3-flash | openai_completion | 同 glm-5.3 | 同上 |
| openrouter-gpt-5.6-luna | openai/gpt-5.6-luna | openai_response | OpenAI reasoning：stateless 续接靠 output items 中 `encrypted_content`；不能只存可读 summary [W1]。**TBD-P2**：openrouter 透传 encrypted_content 的完整性 | 同上 |
| openrouter-gpt-5.6-terra | openai/gpt-5.6-terra | openai_response | 同 luna | 同上 |
| openrouter-gpt-5.6-sol | openai/gpt-5.6-sol | openai_response | 同 luna | 同上 |
| gpt-6-astra | openai/gpt-6-astra | openai_response | 同 [W1] 家族。**注**：astra 的 cache 路线（automatic/hybrid/explicit）切换决策已被 Nathan 搁置（2026-09-17，case_f3f42d3777b1），本矩阵只记录续接契约，不触发 cache 路线变更 | 同上 |

★ACTIVE：`~/.pal/config.toml` 的 `endpoint_id` 与 `review_endpoint_id` 均为 deepseek-v4.1-flash。**现役端点走 anthropic_messages shape，[W2]/[W3] 类契约直接约束生产路径**——这是本改造优先级的现实依据。

## 现状（v1 机制）与目标（v2）的映射

- 现状：每条 assistant 消息的 `ReplayEnvelope`（shape+endpoint+model+payload）即事实上的 native 存储，随 L1 快照整体持久化；同端点重放，协议修理时置空。它对上表三 shape 都"存了"，但**没有按协议验证字段完整性**（例如不会校验 encrypted_content/signature 是否存在），也不区分 required/optional。
- 目标（PLAN §3/§7）：`NativeContinuationStore` 按 ContinuationContract 做类型化验证存储；unknown 字段不得伪装成已知；切换端点销毁 active lineage 数据。
- 迁移（PLAN §8.2）：旧 ReplayEnvelope 数据按实际 active binding 与协议一致性筛选迁移，缺必需字段的明确报告，不造假空签名。

## 验证义务（进入 P2 前）

1. 每个 TBD 行产出脱敏 fixture 或明确标注"无法离线验证，需 P7 canary 单独授权"。
2. fixture 分两类：synthetic-opaque（只证明"不被改动"）与 provider-real（声称 provider 端可接受）；后者才是 contract 证据。
3. 本矩阵的任何行从 TBD 变为结论时，须引用 fixture 文件与测试名。
