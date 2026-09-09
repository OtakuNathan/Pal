# Pal Browser Contract

> `web_fetch` 是由 Pal 随包提供、由 Plugin Hub 管理的第一方 RAII 插件；它对外发布的是会话级浏览器能力，而不是 fetch provider family。

## Ownership

插件拥有：

- 经 token 认证的本地 browser sidecar 及其全部子进程
- 固定版本 `@playwright/cli@0.1.19` 与对应 Chromium 的按需安装
- 每个主对话独立的持久 profile，以及每个 Bunshin workflow 的临时 session
- 浏览器命令白名单、结果裁剪、截图 artifact 写入和 profile 回收
- 声明式 builtin skill `pal.web.browser`

插件不拥有 web search、任意 HTTP client、LLM 决策或 Bunshin 的角色授权。`web_search` 继续是独立 provider family。

## Lifecycle

`web_fetch` 遵循 `raii.v1`：插件实例先创建资源，能力后发布；detach 时能力先撤下，再关闭 sidecar、CLI daemon 和浏览器。模块自己的 attach/detach 能力不对外注册，唯一生命周期入口是 Plugin Hub 的 `plugin_attach` / `plugin_detach`。

sidecar 空闲后会退出，下一次调用按需重建。主对话 profile 不随 sidecar、插件刷新或 Pal 重启删除；`browser_close` 只释放活进程，`browser_reset(confirm=true)` 才删除当前对话 profile。

## Session model

- 主 Pal：以 logical execution lifetime 派生不可逆 session key，profile 持久化。
- Bunshin：以 workflow run id 派生临时 session，不持久化。
- 同一 session 串行执行，不同 session 受全局并发上限约束。
- profile 最长保留 30 天，总预算 2 GiB；只按 LRU 删除不活跃 profile。
- 所有 URL 必须是绝对 `http` / `https` URL，不能浏览 `file://`。

## Public capabilities

默认仅直接暴露 `browser_navigate`，其描述说明完整 Chromium 和本地扩展开发能力，并通过结构化 next-tool hints 引出页面读取、定位、状态和扩展管理。后续工具通过 `read_tool` / `call_tool` 调用，也可通过 tool search 发现；发现入口不要求先执行导航，已有页面可直接读取。

间接暴露的读取、交互及管理路径：

- `browser_read`, `browser_snapshot`, `browser_find`

- `browser_click`, `browser_fill`, `browser_type`, `browser_press`
- `browser_hover`, `browser_select`, `browser_check`, `browser_scroll`
- `browser_resize`, `browser_history`, `browser_tabs`, `browser_dialog`
- `browser_inspect_layout`, `browser_screenshot`, `browser_status`
- `browser_close`, `browser_clear_cache`, `browser_reset`
- `browser_extensions`, `browser_extension_manage`

`browser_clear_cache` 只清理当前对话浏览器的 HTTP 缓存，保留 cookies、localStorage、IndexedDB 和 service-worker CacheStorage。它不提供密码管理，也不负责退出登录。浏览器尚未启动时会使用原 profile 打开空白页，不恢复旧 URL；只有浏览器确认清理后才报告成功。清理后是否重新导航或刷新由当前任务决定。

### 本地扩展开发

浏览器使用当前固定 Playwright CLI 所配套的完整 Chromium（当前为 153 / revision 1243），以 `channel=chromium` 无头运行；安装器使用 `playwright install chromium --no-shell`。不使用系统 Chromium，也不依赖旧 Python Playwright 缓存。

`browser_extension_manage(operation=mount, path=...)` 挂载含 `manifest.json` 的本地 Manifest V3 扩展目录；`reload` 和 `unmount` 使用 `browser_extensions` 返回的 `extension_id`。配置按当前持久化对话 profile 隔离，最多 16 个；不支持临时 Bunshin 浏览器或商店安装。扩展源码保留在开发目录中，修改后显式 reload。

配置变更先确认关闭当前浏览器，再保存配置；所有标签会关闭，但登录资料保留。下一次导航按新配置启动。`browser_extensions` 区分配置清单与观察到的 service worker；worker 缺失可能是休眠或扩展没有后台 worker，不能据此断言加载失败。用目标页面验证内容脚本，也可通过 navigate/read 访问已挂载 ID 的 `chrome-extension://` 页面（包括 manifest、popup、options）。未挂载 ID 不允许作为导航目标。

扩展目录或 manifest 损坏时仍可查询配置并 unmount。扩展的浏览器运行结果才是加载与功能证据，挂载返回仅确认配置成功。当前不提供统一的扩展后台错误收集工具。

canonical path 统一为 `op_browser_*`；状态查询仍是模块 introspection。旧的 `read_web`、`inspect_web_layout`、`screenshot_web` 及 `web_fetch_provider_*` 名称不保留兼容别名。

写操作均为 indirect、non-idempotent、reconcile-first；失败后必须重新检查页面，不能自动重复。截图是受治理的本地 artifact 写入。reset 是明确确认后的不可恢复本地写入。

页面正文、链接、snapshot 和 layout 结果均有调用级上限；单张截图超过 32 MiB 会被拒绝，避免 sidecar 把异常页面放大成主进程内存压力。

## Restricted surface

插件不发布任意 JavaScript、上传、cookie/local/session storage 读写、网络拦截、请求正文读取、录制、trace、video、PDF 或 dashboard。`browser_read` 和 layout inspection 内部使用固定脚本，但调用者不能注入脚本。

## Dependency and repair

运行环境需要 Node.js 20+ 和 npm。正式安装通过公共 `pal package prepare web_fetch` 流程准备依赖；CLI 与完整 Chromium 安装在 runtime root 的 `data/web_fetch/` 下，不写项目依赖、用户级 npm 或 Pal 的 Python 环境。匹配的 Node/npm 会复用，否则准备插件管理的 Node。首次缺失或 Chromium build 漂移时 sidecar 调用同一准备流程，并返回可重试的 `dependency_installing`。安装和运行时修复共享跨进程锁；`browser_status` 与 doctor 使用相同的版本/路径检查。验证只启动临时 profile 和测试 MV3 扩展，不恢复或清理用户会话。详见 [插件安装协议](pal_package_installation.md)。

不再有 Python Playwright provider 或 plain-HTTP fallback。只有主 Pal 在 navigate/read 失败且原始 HTTP 足够时会得到 curl 提示；插件本身不会偷偷改变执行语义。Bunshin 必须报告证据缺口。

代理配置继承 `http_proxy` / `https_proxy`（含大写形式）与 `no_proxy`，并交给 Chromium。具体直连、分流规则仍由用户的代理服务负责。

## Bunshin boundary

Bunshin participant 不直接获得交互浏览器。只有启用 external research 的 software-engineering architect 可通过 host broker 使用 `browser_read`；其他角色保持 search/read-only 边界，不能 click、fill 或启动自己的持久 profile。

## Upgrade

`pal setup --upgrade --runtime-root ...` 删除退休的 `web_fetch_providers` 表和 `active_web_fetch_provider_id`。非默认旧 provider 会先以权限 `0600` 归档到 `data/web_fetch/legacy_provider_backup.json`。

## Invariants

- Plugin Hub 是 `web_fetch` 生命周期的唯一 owner。
- 插件 detach 后没有残留公共能力或受管浏览器进程。
- 主对话之间不共享 profile；Bunshin 不继承主对话登录状态。
- 页面写操作永不自动重放。
- CLI 版本固定、安装原子切换，安装进程也受 RAII 回收。
- 浏览器失败不会退回旧 provider；curl 只是显式建议。
