# Getting Started

This guide covers installing, upgrading, starting, and connecting to Pal. It is
the user path; building a release from source is documented separately at the
end.

## Supported Systems

- Linux with Python 3.11 or newer and `bubblewrap`
- macOS with Homebrew Python
- Windows is not currently supported

The default Linux configuration fails closed when the Bunshin sandbox is
unavailable, and the installer runs the dependency doctor before it exits, so
`bubblewrap` (`bwrap`) is part of the normal Linux installation. Pal's core
conversation runtime works without Codex CLI or Ollama; those enable specific
model and memory options. The dependency doctor reports the difference between
required components and optional fallbacks.

## Install a Release

A Pal release bundle contains the `pal-v2` wheel, a runtime-root overlay,
independently installable channel-provider wheels under `providers/`, and
`install-pal.sh`. Bundled providers currently include Telegram and the WebSocket bridge.

Extract the bundle and run the installer:

```bash
tar -xzf pal_v2-install-bundle.tar.gz
./install-pal.sh
```

By default Pal installs into:

```text
runtime root:  ~/.pal
virtualenv:    ~/.local/share/pal/venv
launcher:      ~/.local/bin/pal
```

The locations can be changed without editing the script:

```bash
./install-pal.sh \
  --runtime-root /path/to/runtime \
  --install-root /path/to/install \
  --bin-dir /path/to/bin
```

On Linux, `PAL_PYTHON` can select a particular Python 3.11+ executable. On
macOS the installer uses Homebrew Python so SQLite extensions can be loaded.

## Select the Runtime

Use `--runtime-root` explicitly on runtime commands, especially when keeping
production and development instances together. Without it the CLI selects
`~/.pal` if that directory exists, otherwise `$PAL_HOME`, then `~/.pal`.
Setting `$PAL_HOME` does not override an existing `~/.pal`.

## Complete Setup

For a new runtime the installer launches `pal setup`. The wizard has five
sections:

1. identity and communication preferences;
2. one or more LLM endpoints, including an optional live text/tool preflight;
3. a local Socket or Telegram channel;
4. local or remote Ollama memory-embedding preferences;
5. review and confirmation.

The local recovery socket is provisioned even when another channel is chosen.
At the end, setup offers to register and start Pal as:

- a systemd user service on Linux;
- a LaunchAgent on macOS.

Service registration defaults to yes. If it is unavailable or declined, Pal
can run in the foreground:

```bash
pal run --runtime-root ~/.pal
```

## Connect

Open an interactive local session:

```bash
pal tty --runtime-root ~/.pal
```

Send one message from a script or shell:

```bash
pal client --runtime-root ~/.pal --message "hello"
```

Closing either client only disconnects that client. It does not stop the Pal
service or its background work.

On Linux, inspect the service with:

```bash
systemctl --user status pal
journalctl --user -u pal -f
```

## Verify Dependencies

The installer runs the doctor automatically. It can be rerun at any time:

```bash
pal doctor --runtime-root ~/.pal
```

Important results include:

- Python and required Python packages;
- Git;
- `bubblewrap` for sandboxed Linux Bunshin workers;
- optional Codex CLI support;
- optional local Ollama embedding fallback;
- service-manager availability;
- Node.js 18+ and npm for `web_fetch`. The release installer prepares and verifies
  built-in dependencies, including the pinned Playwright CLI and Chromium. A
  source installation can explicitly prepare them with
  `pal package prepare web_fetch --kind builtin --runtime-root ~/.pal-dev`.

## Upgrade

Run the installer from the new release bundle with the same runtime root:

```bash
./install-pal.sh --runtime-root ~/.pal
```

When `pal.sqlite3` already exists, the installer selects the non-interactive
upgrade path:

```bash
pal setup --upgrade --runtime-root ~/.pal
```

This applies the current LLM and Bunshin schema migrations without reopening the
wizard or replacing user configuration. It does not replace code already loaded
in a running host.

The packaged release bundle contains the Pal wheel, the runtime plugin overlay,
and independently installable channel-provider wheels under `providers/`.
`install-pal.sh` installs those provider wheels through `pal provider install`
and, on Linux, safely stops and restarts a Pal service that was already active.
Use `--no-providers` or `--no-service-restart` only when managing those
lifecycles separately.

On macOS, or when replacing a wheel outside the installer's service handling,
restart the actual service through its external supervisor. For the default
Linux unit that is `systemctl --user restart pal.service`.

For an individual plugin change, use `manage_plugin(name=..., operation="reload")`
in a running Pal. Repeated attach preserves an existing instance; rescan discovers
metadata, and endpoint restart only reconnects. Check lifecycle results and
load/cleanup errors before claiming activation. Resident/shared Python changes
require an external host restart.

For a narrow existing configuration change, prefer `pal llm`, `pal identity`,
`pal memory`, `pal web-search`, `pal core` or `pal bunshin` as appropriate; inspect
that command's `--help`. Full setup can change identity, endpoints, embeddings and
OS services, so inspect the existing runtime before rerunning it.

## Build a Release From Source

Maintainers can build the distributable bundle with:

```bash
scripts/build_package.sh
```

The result is:

```text
dist/pal_v2-install-bundle.tar.gz
```

For editable development rather than a packaged installation:

```bash
python -m pip install -e '.[test]'
pal doctor --runtime-root ~/.pal-dev
pal setup --runtime-root ~/.pal-dev
```

Run the development instance in the foreground:

```bash
pal run --runtime-root ~/.pal-dev
```

Inspect the service name and launch command before registering a development
service so it does not replace a production unit.
`scripts/pal_source.sh` prepends this checkout's `src/` to `PYTHONPATH`; use
`PAL_PYTHON` to select its interpreter. This selects source code, not the runtime:
continue to pass `--runtime-root ~/.pal-dev`.

For test batches, type checks and model validation, see
[Development and validation](development.md) and [Formal checks](../spec/README.md).
