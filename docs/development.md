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

Tests use pytest, unittest-style fixtures and Hypothesis. Install the complete
`.[test]` extra so clean environments include the JSON Schema and Peewee type
stubs. Runtime fixtures use declared ports and real lifecycle primitives;
reloadable classes used with patched module state are imported at the point of use.

The [ordinary CI workflow](../.github/workflows/ci.yml) first checks domain types,
architecture boundaries, the formal inventory and generated relations. It then
runs all four pytest batches on Python 3.12, on both Ubuntu and macOS, and executes
the Core and Memory TLC families. New pushes cancel older runs for the same ref.
Pytest has at most four independent runners; ordinary TLC groups run one at a
time with one worker and a 1 GiB heap. On a small development machine, run local
checks sequentially and avoid overlapping pytest, mypy and TLC processes.

Each pytest matrix job uploads `pytest-<os>-<batch>` with `pytest.log` and JUnit
`results.xml`, including failed runs. Core/Memory TLC artifacts are named
`ci-tlc-<group>` and contain bounds, verdicts and counterexamples. Retention is
14 days. Extra pytest flags can be passed through the batch runner, for example:

```bash
python scripts/run_test_batch.py core-a -ra --tb=short --junitxml=test-logs/core-a.xml
```

Full TLC and model/runtime conformance have a separate
[nightly workflow](../.github/workflows/tlc-nightly.yml), also available through
manual dispatch. Nightly limits the model matrix to two runners and retains the
existing per-model two-worker, 2 GiB bounds. An inventory-only pass does not mean
TLC ran; see [Formal Checks](../spec/README.md) for expected mutant/witness results.
Report skipped checks and platform limits.

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
