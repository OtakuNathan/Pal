from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


RECEIPT = ".pal-package.json"


@dataclass(frozen=True)
class PackageEnvironment:
    """Interpreter selection for a plugin-owned sidecar, never host sys.path."""

    root: Path

    @property
    def python_executable(self) -> Path:
        python = self.root / "bin" / "python"
        if not python.is_file():
            raise RuntimeError("Package environment is missing; run package_prepare")
        # Do not resolve this symlink: Python needs the venv path to find pyvenv.cfg.
        return python

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__"):
            env.pop(key, None)
        env.update(VIRTUAL_ENV=str(self.root), PYTHONNOUSERSITE="1")
        env["PATH"] = str(self.root / "bin") + os.pathsep + env.get("PATH", "")
        return env


def installed_environment(directory: Path | None) -> PackageEnvironment | None:
    if directory is None:
        return None
    receipt = directory / RECEIPT
    if not receipt.is_file():
        return None  # Legacy hand-deployed packages have no environment contract.
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    root = payload.get("environment")
    if not root:
        return None
    environment = PackageEnvironment(Path(root))
    environment.python_executable
    return environment
