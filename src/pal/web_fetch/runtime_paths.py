"""Browser tooling paths and installation probes independent of service startup."""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

PLAYWRIGHT_CLI_PACKAGE = "@playwright/cli"
PLAYWRIGHT_CLI_VERSION = "0.1.19"
NODE_MINIMUM_MAJOR = 20


@dataclass(frozen=True)
class BrowserRuntimePaths:
    runtime_root: Path

    @property
    def root(self) -> Path:
        return Path(self.runtime_root) / "data" / "web_fetch"

    @property
    def tooling_root(self) -> Path:
        return self.root / "tooling"

    @property
    def tooling_current(self) -> Path:
        return self.tooling_root / "current"

    @property
    def cli(self) -> Path:
        return self.tooling_current / "node_modules" / ".bin" / "playwright-cli"

    @property
    def browser_cache(self) -> Path:
        return self.root / "browsers"

    @property
    def cli_cache(self) -> Path:
        return self.root / "cli-cache"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def config(self) -> Path:
        return self.workspace / ".playwright" / "cli.config.json"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def temporary(self) -> Path:
        return self.root / "tmp"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    def prepare(self) -> None:
        for path in (
            self.root,
            self.tooling_root,
            self.browser_cache,
            self.cli_cache,
            self.workspace,
            self.output,
            self.temporary,
            self.profiles,
            self.config.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.root.chmod(0o700)
            self.profiles.chmod(0o700)


def _installed_cli_version(paths: BrowserRuntimePaths) -> str:
    if not paths.cli.is_file():
        return ""
    package_json = paths.tooling_current / "node_modules" / "@playwright" / "cli" / "package.json"
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise ValueError(f"Invalid Playwright CLI package metadata: {package_json}")
    return payload["version"]


def _chromium_installed(paths: BrowserRuntimePaths) -> bool:
    package = paths.tooling_current / "node_modules" / "playwright-core" / "browsers.json"
    try:
        entries = json.loads(package.read_text())["browsers"]
        revision = next(item["revision"] for item in entries if item["name"] == "chromium")
    except FileNotFoundError:
        return False
    for browser_root in paths.browser_cache.glob(f"chromium-{revision}"):
        for name in ("chrome", "chrome.exe", "Chromium", "Google Chrome for Testing"):
            if any(candidate.is_file() for candidate in browser_root.rglob(name)):
                return True
    return False
