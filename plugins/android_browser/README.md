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
