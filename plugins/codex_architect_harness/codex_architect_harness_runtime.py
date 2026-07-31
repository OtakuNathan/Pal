from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.foundation.sidecar import python_subprocess_env
from pal.minion.harnesses import (
    CODEX_ARCHITECT_HARNESS_ID,
    HARNESS_LAUNCH_HOST,
    HARNESS_PROTOCOL_VERSION,
    MinionHarnessRegistry,
    MinionHarnessSpec,
)
from pal.plugins.contracts import PluginBuildContext


def _resolve_codex_binary() -> Path:
    configured = str(os.environ.get("PAL_CODEX_BIN") or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        sorted(
            Path.home().glob(".nvm/versions/node/*/bin/codex"),
            reverse=True,
        )
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.absolute()
    raise RuntimeError(
        "Codex Architect harness requires an executable Codex CLI; "
        "set PAL_CODEX_BIN or install Codex on PATH"
    )


@dataclass
class CodexArchitectHarnessBundle:
    plugin_dir: Path
    registry: MinionHarnessRegistry
    plugin_id: str = "codex_architect_harness"
    version: str = "0.1.0"

    def register_with_core(self, context) -> ModuleHandle:
        codex_bin = _resolve_codex_binary()
        version = subprocess.run(
            [str(codex_bin), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=python_subprocess_env(),
        ).stdout.strip()
        if not version.startswith("codex-cli "):
            raise RuntimeError(
                f"unexpected Codex CLI version response: {version}"
            )
        worker = (self.plugin_dir / "codex_architect_worker.py").resolve()
        if not worker.is_file():
            raise RuntimeError(f"Codex Architect worker is missing: {worker}")
        spec = MinionHarnessSpec(
            harness_id=CODEX_ARCHITECT_HARNESS_ID,
            protocol_version=HARNESS_PROTOCOL_VERSION,
            supported_roles=("architect",),
            priority=100,
            launch_kind=HARNESS_LAUNCH_HOST,
            worker_argv=(
                str(Path(sys.executable).resolve()),
                str(worker),
            ),
            config={
                "codex_bin": str(codex_bin),
                "effort": "high",
                "turn_timeout_seconds": 3000,
                "cli_version": version,
            },
        )
        self.registry.register(spec)
        handle = ModuleHandle(
            module_id=self.plugin_id,
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
        )
        handle.cleanup_callbacks.append(
            lambda: self.registry.unregister(CODEX_ARCHITECT_HARNESS_ID)
        )
        context.register_module(handle)
        return handle


def build_plugin(
    context: PluginBuildContext,
) -> CodexArchitectHarnessBundle:
    registry = context.services.get("minion_harness_registry")
    if not isinstance(registry, MinionHarnessRegistry):
        raise RuntimeError(
            "Codex Architect harness requires minion_harness_registry service"
        )
    if context.plugin_dir is None:
        raise RuntimeError("Codex Architect harness plugin_dir is required")
    return CodexArchitectHarnessBundle(
        plugin_dir=Path(context.plugin_dir),
        registry=registry,
    )
