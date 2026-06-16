# Pal Web Fetch Contract

> 目标：定义 `web_fetch` 子系统的职责、对象模型，以及与 capability forest 的集成方式。

## 目标

`web_fetch` 负责让 `Pal` 获得网页内容抓取能力。

它不等于搜索引擎，也不等于 HTTP client 库。

它负责：

- 抓取网页并保留页面元信息，再提取文本内容
- 支持 Playwright 浏览器渲染（处理 JS 页面）和纯 HTTP 抓取
- 多 provider fallback
- provider 健康检查与状态报告
- fetch provider 的生命周期管理

## Owns

- web fetch provider registry
- 页面抓取执行
- provider fallback 链
- active provider 选择
- browser service 进程管理
- provider health / auth 状态

## Does Not Own

- Playwright 浏览器二进制（由 `playwright install` 管理）
- LLM 调用决策
- 抓取内容的后续处理
- channel 传输

## 核心对象

### WebFetchProvider

fetch provider 是一个可替换后端。

当前已实现：

- `PlaywrightFetchProvider` — 使用 Playwright 渲染页面后提取文本。支持 JS 渲染，需要 Chromium 二进制。通过 `BrowserServiceManager` 管理外部 browser service 进程。
- `PlainHTTPFetchProvider` — 纯 HTTP 抓取 + HTML 解析。无需浏览器，作为 fallback。

每个 provider 实现 `WebFetchProviderPort` 协议：

```python
class WebFetchProviderPort(Protocol):
    provider_kind: str
    def read(self, record, request: WebFetchRequest) -> WebFetchDocument | dict[str, object]: ...
```

`dict` 返回仅用于兼容旧 provider；新 provider 应返回 `WebFetchDocument`。

### WebFetchDocument

`web_fetch` 内部保留 typed document，而不是只保留正文字符串：

- `requested_url` / `final_url`
- `status_code`
- `content_type` / `content_length`
- `title` / `text` / `text_truncated`
- `raw_content` / `raw_content_truncated`（内部保留，capability 默认不把全文输出给 LLM）
- `links`
- `metadata`（例如 `description`、`canonical_url`、`language`）
- `response_headers`（只保留安全排错字段）

LLM-facing 的 `web_read` 仍然返回紧凑文本，但 structured payload 会带上这些字段，并用 `raw_content_available` / `raw_content_truncated` 标明是否保留了原始内容，方便引用、排错和后续处理。

### BrowserServiceManager

Playwright 的进程管理器。负责：

- 拉起 `pal browser-service` 子进程
- 通过 HTTP + token 认证与子进程通信
- 并发控制（BoundedSemaphore）
- 空闲超时自动关闭
- 进程健康检查

子进程通过 `pal main browser-service` 启动，独立于 Pal 主进程运行。

### WebFetchService

核心服务层。负责：

- 维护 provider 注册表
- 按 priority 和 enabled 状态选择 provider
- 执行抓取，失败时自动 fallback
- 追踪最近错误（`last_errors`）

### WebFetchProviderModel

数据库模型，存储 provider 配置：

- `provider_id` — 唯一标识
- `provider_kind` — 对应哪种实现
- `enabled` — 是否启用
- `priority` — 优先级
- `settings_blob` — provider 特定配置（如 `idle_timeout_seconds`、`max_concurrency`）
- `auth_material_blob` — 认证信息

## 与 Capability Forest 的集成

`web_fetch` 通过 `IntrospectionProvider` 注册到 capability forest。

### 模块级能力（SINGLETON_TARGET）

| canonical_path | 作用 |
|----------------|------|
| `${1}_show` | 模块概览 |
| `${1}_providers` | 列出所有 provider |
| `${1}_provider` | 当前活跃 provider |
| `web_read` | 抓取网页内容 |
| `op_web_fetch_mgmt_set_active_provider` | 切换活跃 provider |

### Provider 实例级能力

每个注册的 provider 会自动生成实例级能力：

- `${1}_provider_health::<provider_id>`
- `${1}_provider_show::<provider_id>`
- `${1}_provider_state::<provider_id>`
- `op_web_fetch_mgmt_enable::<provider_id>`
- `op_web_fetch_mgmt_disable::<provider_id>`
- `op_web_fetch_mgmt_set_config::<provider_id>`
- `op_web_fetch_mgmt_set_auth_material::<provider_id>`

## 代理支持

Playwright provider 自动读取环境变量 `https_proxy` / `http_proxy`，通过 `--proxy-server` 传递给 Chromium。

## User-Agent

默认 `User-Agent` 使用桌面 Chrome 风格字符串，而不是 `Pal` 自定义标识。`WebFetchRequest.user_agent` 可以覆盖默认值；Playwright 和纯 HTTP provider 都必须使用同一请求级 UA。

## 插件集成

`web_fetch` 通过 `plugins_builtin/web_fetch/` 作为第一方插件加载。

## Supervisor 默认配置

`supervisor` 的 `seed_defaults()` 会预置两个 provider：

1. `playwright_fetch_default` — Playwright 渲染抓取，priority 0
2. `plain_http_fetch_default` — 纯 HTTP 抓取，priority 10

活跃 provider 默认为 `playwright_fetch_default`。

## Invariants

- `web_fetch` 提供 fallback-capable provider family。
- 抓取失败时自动尝试下一个 provider。
- Browser service 是独立子进程，崩溃不影响 Pal 主进程。
- 抓取结果以 `WebFetchDocument` 保留页面元信息；返回给 LLM 的正文经过文本提取和截断。

## Non-Goals

- 不在本文件定义抓取内容的后处理和摘要策略
- 不在本文件定义抓取缓存策略
- 不在本文件定义 URL 安全校验策略
