# Pal Browser Contract

> `web_fetch` is a first-party RAII plugin bundled with Pal and managed by the Plugin Hub; what it exposes externally is session-level browser capability, not a fetch provider family.

## Ownership

The plugin owns:

- A token-authenticated local browser sidecar and all of its child processes
- On-demand installation of the pinned-version `@playwright/cli@0.1.19` and its matching Chromium
- A separate persistent profile per main conversation, plus a temporary session per Bunshin workflow
- The browser command whitelist, result trimming, screenshot artifact writing, and profile reclamation
- The declarative builtin skill `pal.web.browser`

The plugin does not own web search, arbitrary HTTP clients, LLM decisions, or Bunshin role authorization. `web_search` remains a separate provider family.

## Lifecycle

`web_fetch` follows `raii.v1`: the plugin instance creates resources first and publishes capabilities afterward; on detach, capabilities are withdrawn first, then the sidecar, CLI daemon, and browser are shut down. The module's own attach/detach capability is not registered externally; the lifecycle entry point is `manage_plugin(name="web_fetch", operation=...)`. Attach reuses an already-loaded instance; reload unloads and reloads in a single call.

The sidecar exits after being idle and is rebuilt on demand for the next call. Main conversation profiles are not deleted on sidecar exit, plugin refresh, or Pal restart; `manage_browser_session(operation=close)` only releases live processes, and only operation=reset with confirm=true deletes the current conversation profile.

## Session model

- Main Pal: derives an irreversible session key from the logical execution lifetime; the profile is persistent.
- Bunshin: derives a temporary session from the workflow run id; not persistent.
- The same session executes serially; different sessions are bounded by the global concurrency limit.
- Profiles are retained for at most 30 days, with a total budget of 2 GiB; only inactive profiles are deleted, by LRU.
- Web page URLs must be absolute `http` / `https` URLs; mounted extensions allow `chrome-extension://` pages of the corresponding ID, and `file://` cannot be browsed.

## Public capabilities

`read_browser_page` is exposed directly by default and merges opening a page with reading its body: when a url is provided it opens the page; when omitted it reads the current page. Other browser tools are discovered via `search_tools(domain="browser", query=...)` and invoked with `call_tool`; `read_tool` is only added when contract information is missing. Discovery or status queries do not require prior navigation.

Indirectly exposed read, interaction, and management paths:

- `read_browser_page`, `capture_browser_snapshot`, `find_browser_text`

- `click_browser_target`, `fill_browser_field`, `type_browser_text`, `press_browser_key`
- `hover_browser_target`, `select_browser_option`, `set_browser_checked_state`, `scroll_browser`
- `drag_browser_target`, `evaluate_browser_script`, `manage_browser_network_capture`
- `resize_browser_viewport`, `navigate_browser_history`, `manage_browser_tabs`, `handle_browser_dialog`
- `inspect_browser_layout`, `capture_browser_screenshot`, `inspect_browser_status`
- `manage_browser_session`, `manage_browser_extension`

`manage_browser_session(operation=clear_cache)` only clears the current conversation browser's HTTP cache, preserving cookies, localStorage, IndexedDB, and service-worker CacheStorage. It does not provide password management and is not responsible for logging out. If the browser has not been started, it opens a blank page with the original profile without restoring old URLs; success is only reported after the browser confirms the cleanup. Whether to re-navigate or refresh afterward is decided by the current task.

### Local extension development

The browser uses the full Chromium that matches the currently pinned Playwright CLI (currently 153 / revision 1243), running headless with `channel=chromium`; the installer uses `playwright install chromium --no-shell`. System Chromium is not used, and the old Python Playwright cache is not relied upon.

`manage_browser_extension(operation=mount, path=...)` mounts a local Manifest V3 extension directory containing `manifest.json`; `reload` and `unmount` use the `extension_id` returned by `inspect_browser_status`. Configuration is isolated per current persistent conversation profile, up to 16; temporary Bunshin browsers and store installs are not supported. Extension source stays in the development directory; after changes, reload explicitly.

On configuration changes, first confirm the current browser is closed, then save the configuration; all tabs will close, but login data is retained. The next navigation starts with the new configuration. `inspect_browser_status` distinguishes the configuration manifest from the observed service worker; a missing worker may mean it is dormant or the extension has no background worker, and cannot by itself be asserted as a load failure. Verify content scripts with the target page, or access `chrome-extension://` pages of mounted IDs via `read_browser_page(url=...)` (including manifest, popup, options). Unmounted IDs are not allowed as navigation targets.

If the extension directory or manifest is corrupted, configuration can still be queried and unmounted still works. The extension's browser runtime results are the evidence of loading and functionality; a mount return only confirms successful configuration. There is currently no unified tool for collecting extension background errors.

Canonical paths are unified as `op_browser_*`; status queries remain module introspection. The old `read_web`, `inspect_web_layout`, `screenshot_web`, and `web_fetch_provider_*` names have no compatibility aliases.

All write operations are indirect, non-idempotent, and reconcile-first; after failure the page must be re-checked and cannot be automatically repeated. Screenshots are governed local artifact writes. Reset is an unrecoverable local write after explicit confirmation.

Page body, links, snapshot, and layout results all have per-call limits; a single screenshot over 32 MiB is rejected, to prevent the sidecar from amplifying an abnormal page into main-process memory pressure.

## Restricted surface

`evaluate_browser_script` executes a caller-provided JavaScript function on the current page, able to read page-internal state or use fetch with the current origin; prefer read-only expressions, writes require already-authorized intent, and are subject to page permissions and CORS. `manage_browser_network_capture` observes fetch/XHR with injected hooks; after navigation it must be started again, with installed=true confirmed. It does not provide network interception or modification.

The plugin has no dedicated upload, cookie/storage editing, recording, trace, video, PDF, or dashboard tools. When raw HTTP suffices, use the main Pal's `run_shell` with curl; only consider in-page fetch when browser login state is needed.

## Dependency and repair

The runtime environment requires Node.js 20+ and npm. Official installation prepares dependencies through the public `pal package prepare web_fetch` flow; the CLI and full Chromium are installed under the runtime root's `data/web_fetch/`, without writing to project dependencies, user-level npm, or Pal's Python environment. A matching Node/npm is reused; otherwise a plugin-managed Node is prepared. On first-missing or Chromium build drift, the sidecar invokes the same preparation flow and returns a retryable `dependency_installing`. Installation and runtime repair share a cross-process lock; `inspect_browser_status` and doctor use the same version/path checks. Validation only starts a temporary profile and a test MV3 extension, and does not restore or clean user sessions. See [Plugin installation protocol](pal_package_installation.md).

There is no longer a Python Playwright provider or a plain-HTTP fallback. Only the main Pal receives a curl hint when navigate/read fails and raw HTTP suffices; the plugin itself never secretly changes execution semantics. Bunshin must report evidence gaps.

Proxy configuration inherits `http_proxy` / `https_proxy` (including uppercase forms) and `no_proxy`, and passes them to Chromium. The specific direct-connection and routing rules remain the responsibility of the user's proxy service.

## Bunshin boundary

Bunshin participants do not directly get an interactive browser. Only the software-engineering architect with external research enabled can use `read_browser_page` via the host broker; other roles keep the search/read-only boundary and cannot click, fill, or start their own persistent profile.

## Upgrade

`pal setup --upgrade --runtime-root ...` deletes the retired `web_fetch_providers` table and `active_web_fetch_provider_id`. Non-default legacy providers are first archived to `data/web_fetch/legacy_provider_backup.json` with permission `0600`.

## Invariants

- Plugin Hub is the sole owner of the `web_fetch` lifecycle.
- After the plugin detaches, there are no residual public capabilities or managed browser processes.
- Profiles are not shared between main conversations; Bunshin does not inherit main conversation login state.
- Page write operations are never automatically replayed.
- CLI version is pinned, installation switches atomically, and the installation process is also subject to RAII reclamation.
- Browser failures never fall back to the old provider; curl is only an explicit suggestion.
