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

A Pal release bundle contains three files:

- the `pal-v2` Python wheel;
- the runtime-root overlay for detachable providers and harnesses;
- `install-pal.sh`.

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
pal doctor
```

Important results include:

- Python and required Python packages;
- Git;
- `bubblewrap` for sandboxed Linux Bunshin workers;
- optional Codex CLI support;
- optional local Ollama embedding fallback;
- service-manager availability;
- optional Playwright Chromium, with plain HTTP fetch as a fallback.

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
wizard or replacing user configuration. Restart the service after installing a
new wheel if it is already running:

```bash
systemctl --user restart pal
```

Individual detachable plugins can instead be refreshed through their lifecycle
operation when only that subsystem changed.

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
python -m pip install -e .
pal doctor
pal setup --runtime-root ~/.pal-dev
```

Run the development instance in the foreground or accept service registration
under a distinct runtime root.
