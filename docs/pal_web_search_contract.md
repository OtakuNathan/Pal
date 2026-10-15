# Pal Web Search Contract

> 目标：定义 `web_search` 子系统的职责、对象模型，以及与 capability forest 的集成方式。

## 目标

`web_search` 负责让 `Pal` 获得网络搜索能力。

它不等于通用 HTTP client，也不等于浏览器。

它负责：

- 结构化网页搜索
- 多 provider fallback（Brave Search、DuckDuckGo）
- provider 健康检查与状态报告
- 搜索 provider 的生命周期管理

## Owns

- web search provider registry
- search query 执行
- provider fallback 链
- active provider 选择
- provider health / auth 状态

## Does Not Own

- HTTP transport 实现（由各 provider 自行负责）
- LLM 调用决策
- 搜索结果的后续处理
- channel 传输

## 核心对象

### WebSearchProvider

搜索 provider 是一个可替换后端。

当前已实现：

- `BraveSearchProvider` — Brave Search API，需要 API key
- `DuckDuckGoSearchProvider` — DuckDuckGo Instant Answer API，无需 key，作为 best-effort fallback

每个 provider 实现 `WebSearchProviderPort` 协议：

```python
class WebSearchProviderPort(Protocol):
    provider_kind: str
    def search(self, query: str, *, max_results: int, ...) -> WebSearchResult: ...
```

### WebSearchService

核心服务层。负责：

- 维护 provider 注册表
- 按 priority 和 enabled 状态选择 provider
- 执行搜索，失败时自动 fallback 到下一个 provider
- 追踪最近错误（`last_errors`）

### WebSearchProviderModel

数据库模型，存储 provider 配置：

- `provider_id` — 唯一标识
- `provider_kind` — 对应哪种实现
- `enabled` — 是否启用
- `priority` — 优先级（数字越小越优先）
- `settings_blob` — provider 特定配置
- `auth_material_blob` — 认证信息（如 API key）

## 与 Capability Forest 的集成

`web_search` 通过 `IntrospectionProvider` 注册到 capability forest。

### 模型工具面

| 工具 | 入口 | 作用 |
|------|------|------|
| `search_web(query, limit?, region?, safe_search?)` | 常驻直调 | 搜索；由 service 选择 provider 并执行 fallback |
| `inspect_web_search()` | 间接只读 | 无参数，一次返回 Hub 与全部 provider 的诊断 |

诊断始终采用同一个返回结构，不根据参数切换语义，也不分页或截取 provider：

- Hub：mounted/degraded、provider 数量、启用数量、配置首选和有效首选。
- `providers`：每个 provider 的公开配置、认证是否齐全、最近请求健康度。
- `diagnostics`：观测时间、来源、部分不可用的 section；单 provider 的独立
  auth/health 读取失败不会丢失其他已知信息。

这是配置和最近请求的被动观测，不发起联网探测。有效首选表示当前候选顺序
中的首位，不保证该 provider 的网络或认证可用；搜索结果报告实际执行的后端。
认证材料值不返回。模型可见的错误报告去除 traceback，并限制每条 600 字符；
完整最近请求报告继续保留在 resident service，不被摘要覆盖。

旧 `inspect_web_search_state`、`list_web_search_providers` 和
`inspect_web_search_provider` 不再发布。原 provider 维护 handler 可供内部
兼容调用；配置、认证、启停和首选切换通过 `pal web-search` CLI 完成，模型
工具不提供这些写操作。

## 插件集成

`web_search` 通过 `plugins_builtin/web_search/` 作为第一方插件加载。

`plugin.toml` 声明插件元数据，`runtime.py` 的 `build_plugin(context: PluginBuildContext)` 负责创建 service 并注册到 core context。

## Supervisor 默认配置

`supervisor` 的 `seed_defaults()` 会预置两个 provider：

1. `brave_search_default` — Brave Search，priority 0，需要配置 API key
2. `duckduckgo_search_default` — DuckDuckGo，priority 10，无需 key

活跃 provider 默认为 `brave_search_default`。

## Invariants

- `web_search` 提供 fallback-capable provider family。
- 搜索失败时自动尝试下一个 provider。
- Provider 的 `auth_material_blob` 中的敏感信息不应明文暴露给 LLM。
- 搜索结果经过标准化后才返回给 LLM。

## Non-Goals

- 不在本文件定义搜索结果的后处理和摘要策略
- 不在本文件定义搜索缓存策略
- 不在本文件定义 rate limiting 实现
