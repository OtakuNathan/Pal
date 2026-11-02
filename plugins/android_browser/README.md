# Android browser

Native Pal plugin for the independent Pal Android APK, with an explicit legacy
Droidline transport for existing installations. No MCP server or extra Python
dependencies. Pal retains page understanding and decisions; the host executes
explicit primitives.

## Capabilities

Discover in the `android` domain, then call through Pal's indirect tool path:

| Alias | Behavior |
| --- | --- |
| `android_devices` | Paired devices, connection and accessibility readiness |
| `android_screenshot` | Owned native desktop PNG as a conversation artifact, independently of accessibility |
| `android_observe` | Current package/activity, screen dimensions, semantic tree |
| `android_query` | All current matches by id, text, description or class |
| `android_act` | Up to eight explicit steps, stop on first error, then fresh snapshot |

Example arguments for `android_act`:

```json
{"device":"_","steps":[{"cmd":"launch","package":"com.android.settings"}]}
```

Steps: `launch(package)`, `touch(by,value,nth?)`, `input(by,value,text,append?,nth?)`,
`tap(x,y)`, `swipe(x1="up"|"down"|"left"|"right",ms?)`, `back`, `home`, `recents`,
`wait_idle(ms?,timeout?)`. Query before selecting duplicate matches. Selectors
are evaluated live at each step; they are not stable element handles. Input
replaces text unless `append=true`. Coordinate taps are a fallback when a
semantic target is unavailable. Batch only steps whose targets are already
known; observe whenever the next action depends on newly loaded content.

Snapshots retain hierarchy, resource ids, text, descriptions, classes, bounds,
and meaningful state. Empty/default framework fields are omitted. Password
node text/description is masked. Every snapshot is self-contained; there is
no model-maintained diff chain. Accessibility only exposes what the app
publishes: recycled/offscreen rows and custom canvases may be absent. This
adapter does not promise full-page DOM or long screenshots. Scroll and observe
to inspect additional content. App-specific scripts and shell access can be
added separately after the primitive contract is validated.

## Setup

1. Install the official Droidline Android app and enable its accessibility
   service, preserving other enabled services. No root or default-keyboard
   change is needed for the basic path. ADB is optional for provisioning.
2. Run the official `droidline --home <private-state-directory> serve` on the
   PC. Pair the app using `droidline pair`. Keep the client HTTP listener on
   loopback (default `127.0.0.1:8780`). The agent uses a separate encrypted
   phone connection; pairing details belong to Droidline, not model context.
3. Optionally copy `config.example.toml` to
   `<runtime_root>/data/android_browser/config.toml`. Defaults work with the
   standard local server. Restrict permissions if storing a bearer token.
4. Build with `pal package build plugins/android_browser --output /tmp/pal-android-package`.
   Install through `install_package` in a running Pal, or the package CLI for
   an offline runtime. Inspect the installation's activation result before
   issuing another attach/reload.
5. Discover the Android domain, list devices, observe, then test a Settings
   launch. `_` is accepted only when exactly one phone is online.

This plugin owns no server process. Run Droidline under an external service
manager for persistent availability; stopping/detaching the plugin leaves that
server and phone pairing intact. It does not install APKs or grant phone
permissions during activation. HTTP uses one attempt, bypasses proxy environment
variables and rejects redirects. There is no automatic action replay.

The batch continues on the phone if its HTTP caller disconnects. Partial
results and uncertain outcomes require reconciliation before retrying. A
successful action followed by a failed snapshot remains an applied action;
call observe again. The default HTTP timeout is 90 seconds; eight steps with
at most ten seconds each plus final settling/snapshot fit the normal budget,
but external scheduling/network delays can still produce uncertain outcomes.

The upstream binaries and app are separate MIT-licensed components; they are
not bundled in the Pal package.

## Pal Android host

APK 0.3.0 起可通过 Tailscale 配对：手机填写 Pal 主机 MagicDNS 名称与
`/android_pair` 生成的一次性配对码。配对由 `android_companion` 插件负责，
主机用 `tailscale whois` 确认来源节点，自动登记独立 channel 与 RPC backend。
此适配器每次调用读取 `data/android_companion/devices.json` 的版本 1 契约，
新配对的手机立即可由现有 capability 使用，不新增工具定义、不需要重载。
地址保存为手机 MagicDNS 全名，不保存其当前 IP。

`android_devices` 返回设备 UUID 与名称，不返回地址或凭据。多手机时必须显式
指定设备 UUID；`_` 不会因为一台手机离线而改为操作另一台。已有 config.toml
作为独立的旧 backend 保留；新配对不会覆盖它。仅用配对设备的新部署无需
创建该配置文件。配对未改变无障碍或无线 ADB 的手机本机权限流程。

The APK uses its own versioned RPC, selected explicitly in configuration:

```toml
protocol = "pal.android.v1"
url = "http://<phone-address>:8780"
token = "<APK-generated secret>"
```

Native RPC has one device descriptor (`GET /v1/device`) and a closed operation
union (`POST /v1/control`). Every command carries a request UUID and server
session UUID. The adapter validates correlation and never guesses a protocol,
retries a write, or falls back to another host after failure. Native `recents`
is unsupported and is rejected before submitting any part of the batch.
Omitted `nth` requires a unique target; explicit `nth=0` selects the first match.

`android_apps` lists launchable labels/packages. The APK returns per-step
applied/rejected/indeterminate receipts and a settled snapshot under one lock.
A failed observation does not erase applied actions. Proven rejection before
actions is reported as not applied; lost transport and indeterminate effects
require observation before retry. Snapshots carry the workspace generation and
an explicit truncation flag. The legacy host remains the default only when
`protocol` is omitted; set `protocol = "legacy.droidline"` to document that choice.

## Pixel observation (0.4.0)

`android_screenshot` requires Pal Android 0.4.8+. Its `device` routing matches
other Android capabilities. Optional `max_edge` defaults to 1920 (256–1920).
The default preserves the native viewport; resized images return both image
and viewport dimensions so Pal can scale coordinates. RPC validates request
correlation, PNG dimensions and SHA-256 before storing a managed local artifact.
The result contains metadata/path and registration status, never base64 text.
Successful artifact registration contributes a conversation reference; Core
attaches pixels only for vision-capable models. If registration is unavailable,
use `import_artifact` on the saved path; reconcile uncertain registration with
`search_artifacts` first.

For canvas-only pages, screenshot → coordinate tap/swipe → screenshot provides
an observation loop without accessibility. Semantic selectors and text input
still require exposed nodes; this version supplies no blind keyboard. Launch
confirmation uses system task information. Successful actions with unavailable
accessibility retain applied receipts and a structured `snapshot_error`.
Screenshots are limited to the owned background display, reject human takeover,
and cannot bypass secure windows. They do not enable legacy Droidline capture.

## Editor-session input (0.5.0, Pal Android 0.4.9+)

`android_focus(device?)` inspects a real editor connection without requiring an
editable accessibility node. A ready result carries `editor`, `package` and
`field_id`; the field id is descriptive, not a stable identity. Otherwise the
result contains an explicit unavailable fault. A tap or visible keyboard is
not a focus receipt.

After a ready observation, use the existing `android_act` primitive:

```json
{"device":"<device UUID>","steps":[{"cmd":"input","by":"focus","package":"com.example","editor":"<editor from android_focus>","text":"你好"}]}
```

This inserts into the current cursor/selection via Android's accessibility IME
connection. It does not replace the entire field, append at an assumed end, or
press Enter. Existing node-targeted `input` replacement/append behavior remains
available. Missing leases and unsupported extra modes are rejected by schema.

The backend binds the lease to the editor session and workspace, invalidates it
on input restart/finish or service disconnect, and checks app/window/task scope
again before dispatch. Shared same-package tasks on other displays/users,
truncated task inventories, unidentified application windows on another screen,
missing ownership evidence and password/key-only editors refuse this path.
This is deliberately conservative; an IME connection alone has no public display
identifier. Scope checks may therefore refuse some custom/OEM windows.

A submitted commit is never automatically repeated. Matching bounded editor
readback produces an applied receipt. Missing/unreliable readback or a focus
change after dispatch produces an indeterminate receipt and stops the batch;
use `android_screenshot` to reconcile before continuing. No clipboard fallback,
keyboard replacement or implicit sending is performed. Android 11/12 and legacy
Droidline hosts retain node input but do not expose this editor-session path.

The source/package is prepared for later device testing; installing the APK and
coordinating the plugin package update are separate activation steps.
