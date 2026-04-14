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

### 模块级能力（SINGLETON_TARGET）

| canonical_path | 作用 |
|----------------|------|
| `introspection_module_web_search_show` | 模块概览 |
| `introspection_module_web_search_list_providers` | 列出所有 provider |
| `introspection_module_web_search_active_provider` | 当前活跃 provider |
| `operation_web_search_query` | 执行搜索 |
| `operation_web_search_management_set_active_provider` | 切换活跃 provider |

### Provider 实例级能力

每个注册的 provider 会自动生成实例级能力：

- `introspection_provider_web_search_health::<provider_id>`
- `introspection_provider_web_search_show::<provider_id>`
- `introspection_provider_web_search_auth_state::<provider_id>`
- `operation_web_search_management_enable::<provider_id>`
- `operation_web_search_management_disable::<provider_id>`
- `operation_web_search_management_set_config::<provider_id>`
- `operation_web_search_management_set_auth_material::<provider_id>`

## 插件集成

`web_search` 通过 `plugins_builtin/web_search/` 作为第一方插件加载。

`plugin.toml` 声明插件元数据，`runtime.py` 的 `build_plugin()` 负责创建 service 并注册到 core context。

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
