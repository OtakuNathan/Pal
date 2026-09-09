# Plugin package installation

Package installation prepares files and dependencies. PluginHost still owns Pal
plugin generations; ChannelEndpointProviderManager still owns channel providers.
Their existing attach/detach protocols and plugin-owned sidecar managers remain
the runtime lifecycle boundary.

## Commands and tools

```sh
pal package build ./my-plugin --output dist
pal package install dist/plugin-my_plugin-1.0.0.palpkg --runtime-root ~/.pal
pal package prepare my_plugin --runtime-root ~/.pal
pal package prepare web_fetch --kind builtin --runtime-root ~/.pal
pal package prepare --all-builtin --runtime-root ~/.pal
pal package status --runtime-root ~/.pal
```

The CLI installs while Pal is stopped; starting Pal or using the existing owner
rescan/attach path activates prepared files. A runtime lease prevents CLI file
replacement under a running Pal. In a running Pal, use indirect `package_install`
or `package_prepare`; they return a job id immediately. `package_status` reports
job progress, package records and activation results. `plugins_list` describes
these follow-up tools. Downloads do not hold the runtime lifecycle write fence.

Legacy `pal provider install *.whl --force` remains supported through the shared
installation pipeline. Existing hand-deployed plugins without installation
metadata keep their previous behavior. Builtins remain in the Pal wheel; the
release installer calls their preparation entries before the setup wizard can start the service.

## Package format

A `.palpkg` is a ZIP with `package.json`, a `host/` payload, an optional backend
wheel and an optional installation hook file. The builder writes file SHA-256
values and checks runtime manifest and wheel versions. The installer rejects
unsafe archive paths, symlinks, duplicate entries, oversized archives and changed
contents before executing hooks.

Author a `package.toml` alongside the backend's `pyproject.toml`:

```toml
id = "my_plugin"
kind = "plugin" # or provider
version = "1.0.0"
python = "venv"
host_files = ["plugin.toml", "runtime.py", "capabilities.py"]
hooks = "install_hooks.py" # optional
```

The ordinary `plugin.toml` declares the existing `raii.v1` runtime entrypoint.
For providers, include `provider.toml` and the provider's existing entrypoint
instead. `host_files` are copied with project-relative paths. They must contain
the complete host-side entrypoint and only import Pal's existing API/dependencies.
The backend wheel is installed inside the private environment, not imported by
the host. Backend wheel version and package/runtime versions must agree.

Use `python = "host"` for an adapter that has no private Python backend or new
Python dependencies. This mode has no backend wheel and never invokes pip in
Pal's environment. Heavy runtimes such as Node/Chromium belong in plugin-owned
tool directories and may be prepared by hooks in either mode.

## Hooks and environments

`check(context)`, `prepare(context)` and `verify(context)` are optional functions
in the hook file. Each returns a JSON-compatible dictionary with boolean `ok`;
return `{"ok": false, "detail": "..."}` or raise to stop installation.

The check runs before the backend is installed and must use only the standard
library and bundled files. Prepare and verify run with the plugin's interpreter.
Every hook runs in its own subprocess and temporary working directory. The
context provides `runtime_root`, `package_dir`, `target_dir`, and
`python_executable`. `package_dir` is immutable input; keep persistent state in
the runtime's plugin-owned data directory. Verify with temporary resources and
do not touch production profiles or hardware merely to validate packaging.

Private venvs disable system site packages and user site packages. Their paths
include the package digest and Python version and are final from creation:
moving a venv would invalidate absolute script shebangs. Upgrades prepare a new
environment rather than editing one used by a running sidecar. Reinstall and
prepare also use a fresh environment, since hooks may change dependencies. Pip
can reuse its download cache. Failed attempts
can be retried with `package_prepare`; old environments are retained.

The host receives the selected environment through `PluginBuildContext.environment`
or `ChannelProviderBuildContext.environment`. Wire it into the plugin's existing
sidecar manager:

```python
environment = context.environment
if environment is None:
    raise RuntimeError("Install this backend using pal package install")
process = subprocess.Popen(
    [str(environment.python_executable), "-m", "my_backend"],
    env=environment.child_env(),
)
# Register stop/wait cleanup in the existing plugin lifecycle immediately.
```

Do not resolve the interpreter symlink to the base Python, reuse a hardcoded
`/usr/bin/python3`, or add the venv's site-packages to Pal's `sys.path`.
`child_env()` removes host Python import overrides while preserving normal proxy
and OS settings. A missing declared environment is an error, not a fallback.

Builtin manifest entries use the same hooks without a private Python backend:

```toml
[installation]
protocol = "pal.install.v1"
entrypoint = "pal.plugins_builtin.example.installation"
```

## Failure and activation

Install records live under `runtime_root/packages/records`; jobs, immutable
artifacts, private environments and previous host payloads have separate
directories there. Stage and status are separate: a package can fail verification
while its old runtime generation is still active. A successful CLI install
reports `pending_rescan`, not a live attached generation.

All dependency preparation uses a cross-process install lock. Only the final
runtime switch takes the existing lifecycle write fence. It first detaches the
old generation, then switches files and activates through the existing owner.
Failure restores previous files and attempts to reactivate the old generation;
restoration failures are reported, never hidden as successful rollback.

This is exception rollback, not a crash-safe filesystem transaction. Power loss
or SIGKILL between directory renames can leave the target absent; previous
payloads remain under `packages/previous`. Recovery then requires reinstalling
the artifact or restoring the previous payload while Pal is stopped. There is
no automatic crash-recovery journal yet.

Package installation never upgrades Pal's Python dependencies. It also never
deletes user profiles, credentials, application data or older environments as
part of dependency preparation. System package changes are not rolled back.

`web_fetch` reuses suitable Node/npm, otherwise downloads the pinned Node build
and checks the official checksum. It installs its pinned Playwright CLI and the
matching full Chromium, then verifies browser launch and a local MV3 extension
in a scratch profile. Missing Chromium system libraries use Playwright's apt
dependency installer; unavailable administrative privileges fail noninteractively.
No live browser worker is constructed during installation verification.
