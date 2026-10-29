# Development and Validation

This guide covers a source checkout. For release installation, runtime selection
and configuration, use [Getting Started](getting_started.md).

## Editable Setup

Python 3.11 or newer is required. Install source and test dependencies:

```bash
python -m pip install -e '.[test]'
pal doctor --runtime-root ~/.pal-dev
pal setup --runtime-root ~/.pal-dev
pal run --runtime-root ~/.pal-dev
```

Inspect an existing runtime before setup: the wizard can change identity,
endpoints, embeddings and OS services. Keep a development runtime separate from
production. Inspect the service name before registering a development service.

`scripts/pal_source.sh` prepends the checkout's `src/` to `PYTHONPATH` and accepts
the same arguments as `pal`. Set `PAL_PYTHON` to select its interpreter. This
selects source code, not a runtime; continue to pass `--runtime-root` explicitly.

## Repository Map

- `src/pal/`: resident subsystems and plugin-owned services.
- `src/pal/shared/`: cross-domain contracts, including the Tool Protocol IR.
- `src/pal/foundation/`: low-level infrastructure.
- `src/pal/bootstrap/`: in-process composition.
- `src/pal/plugins_builtin/`: packaged first-party manifests.
- `plugins/`: additional plugin source; `providers/`: channel-provider source.
- `tests/`: pytest suites; `scripts/`: build, audit and validation utilities.
- `docs/`: guides and contracts; `spec/`: formal models and their inventory.

Read the owning subsystem's README before changing a boundary. The sibling
`../desktop_avatar` is a separate repository with its own instructions.

## Tests and Quality Checks

Run relevant tests first:

```bash
python -m pytest -q tests/test_tool_activity.py
```

The four stable batches cover the full pytest suite, including nested tests:

```bash
python scripts/run_test_batch.py core-a
python scripts/run_test_batch.py core-b
python scripts/run_test_batch.py bunshin-a
python scripts/run_test_batch.py bunshin-b
```

Check selected domain types and architecture boundaries:

```bash
python -m mypy --config-file mypy-contracts.ini
python -m pytest -q tests/test_import_boundaries.py tests/test_port_contracts.py tests/test_dynamic_access.py tests/test_bunshin_ownership.py
python scripts/run_tlc.py --inventory-only
```

Tests use pytest, unittest-style fixtures and Hypothesis. Ordinary CI runs Python
3.12 on Ubuntu and macOS, domain type checks, architecture boundaries and formal
inventory. Inventory success does not mean TLC ran. Full TLC and model/runtime
conformance have a separate [nightly workflow](../.github/workflows/tlc-nightly.yml);
see [Formal Checks](../spec/README.md). Report skipped checks and platform limits.

Write raw validation output under ignored `test-logs/` or outside the checkout.
Keep commands, revision, results and limitations in the relevant report.

## Build a Release

```bash
scripts/build_package.sh
```

The output is `dist/pal_v2-install-bundle.tar.gz`, containing the Pal wheel,
runtime overlay, channel-provider wheels and installer. See
[Getting Started](getting_started.md#build-a-release-from-source) for installation.

## Runtime Activation

Configuration, copied files, loaded code and verified behavior are separate
states. Read the `pal.self.maintenance` manual in
[builtin_skills.py](../src/pal/skill/builtin_skills.py) before deployment.

- `rescan_plugins` discovers metadata.
- `manage_plugin(name=..., operation="attach")` loads an enabled detached plugin
  and preserves an attached instance.
- `operation="reload"` replaces a plugin generation; inspect load/cleanup errors
  and representative behavior. Failed reload does not guarantee rollback.
- Endpoint restart reconnects a transport without reloading provider code.
- Resident/shared implementation changes require an external host restart.
  Never stop, kill, restart or schedule a delayed restart of Pal's own host from
  its active turn. Defer additional activation while Pal is busy.

Online package installation may already activate its package. Inspect the result
before adding another attach/reload. Preserve runtime configuration and unrelated
working-tree changes.

## Reference Audits

For cached reachability and candidate dead-code chains:

```bash
python scripts/audit_dead_code.py
python scripts/audit_dead_code.py --focus src/pal/bunshin/ --changed src/pal/bunshin/verification.py
```

The standard-library audit stores its SQLite cache and JSON report under ignored
`test-logs/dead-code-audit/`. It separates runtime and test references and estimates
affected test files without running them. `--entry module:symbol` adds dynamic
entry points. Candidates need manual review because external consumers and
unmodeled dispatch can change reachability.
