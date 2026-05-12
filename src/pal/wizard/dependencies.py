from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from pal.memory.embedding import DEFAULT_OLLAMA_MODEL_NAME, OllamaEmbeddingProvider


CHECK_STATUS_OK = "ok"
CHECK_STATUS_INFO = "info"
CHECK_STATUS_WARN = "warn"
CHECK_STATUS_MISSING = "missing"
CHECK_STATUS_ERROR = "error"

BLOCKING_STATUSES = {CHECK_STATUS_MISSING, CHECK_STATUS_ERROR}


@dataclass(frozen=True)
class WizardDependencyCheck:
    check_id: str
    title: str
    status: str
    detail: str
    required: bool = True
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return bool(self.required and self.status in BLOCKING_STATUSES)

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
            "fix": self.fix,
            "blocking": self.blocking,
        }


def collect_dependency_checks() -> tuple[WizardDependencyCheck, ...]:
    checks: list[WizardDependencyCheck] = [
        _check_python_version(),
        _check_python_package("litellm", "litellm", "LLM endpoint calls"),
        _check_python_package("playwright", "playwright", "rendered web fetch"),
        _check_python_package("python-telegram-bot", "telegram", "Telegram channel"),
        _check_python_package("sqlite-vec", "sqlite_vec", "vector memory backend", required=False),
        _check_playwright_chromium(),
        _check_git(),
        _check_ollama_embedding(),
        _check_service_manager(),
    ]
    return tuple(checks)


def dependency_report() -> dict[str, object]:
    checks = collect_dependency_checks()
    blocking = [check for check in checks if check.blocking]
    warnings = [check for check in checks if check.status == CHECK_STATUS_WARN]
    return {
        "ok": not blocking,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "checks": [check.to_dict() for check in checks],
    }


def _check_python_version() -> WizardDependencyCheck:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 11):
        return WizardDependencyCheck(
            check_id="python.version",
            title="Python version",
            status=CHECK_STATUS_OK,
            detail=f"Python {text}",
        )
    return WizardDependencyCheck(
        check_id="python.version",
        title="Python version",
        status=CHECK_STATUS_ERROR,
        detail=f"Python {text}; Pal requires Python >= 3.11",
        fix="Install Python 3.11 or newer, then reinstall the Pal wheel.",
    )


def _check_python_package(distribution_name: str, import_name: str, purpose: str, *, required: bool = True) -> WizardDependencyCheck:
    if importlib.util.find_spec(import_name) is not None:
        return WizardDependencyCheck(
            check_id=f"python.package.{distribution_name}",
            title=f"Python package: {distribution_name}",
            status=CHECK_STATUS_OK,
            detail=f"Import `{import_name}` is available for {purpose}.",
            required=required,
        )
    return WizardDependencyCheck(
        check_id=f"python.package.{distribution_name}",
        title=f"Python package: {distribution_name}",
        status=CHECK_STATUS_MISSING if required else CHECK_STATUS_WARN,
        detail=f"Import `{import_name}` is not available; needed for {purpose}.",
        required=required,
        fix=f"pip install pal-v2 or pip install {distribution_name}",
    )


def _check_playwright_chromium() -> WizardDependencyCheck:
    if importlib.util.find_spec("playwright") is None:
        return WizardDependencyCheck(
            check_id="playwright.chromium",
            title="Playwright Chromium",
            status=CHECK_STATUS_MISSING,
            detail="Playwright package is missing, so Chromium cannot be checked.",
            fix="pip install pal-v2 && python -m playwright install chromium",
        )
    executable = _find_playwright_chromium_executable()
    if executable is not None:
        return WizardDependencyCheck(
            check_id="playwright.chromium",
            title="Playwright Chromium",
            status=CHECK_STATUS_OK,
            detail=f"Chromium executable found at {executable}",
            required=False,
        )
    return WizardDependencyCheck(
        check_id="playwright.chromium",
        title="Playwright Chromium",
        status=CHECK_STATUS_WARN,
        detail="No Playwright Chromium executable was found in the standard browser cache. Plain HTTP fetch still works as fallback.",
        required=False,
        fix="python -m playwright install chromium",
    )


def _find_playwright_chromium_executable() -> Path | None:
    roots: list[Path] = []
    env_path = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env_path and env_path != "0":
        roots.append(Path(env_path).expanduser())
    system = platform.system().lower()
    if system == "windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            roots.append(Path(local_app_data) / "ms-playwright")
    elif system == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")

    suffixes = (
        Path("chrome-win64") / "chrome.exe",
        Path("chrome-linux") / "chrome",
        Path("chrome-mac") / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
        Path("chrome-mac") / "Chromium.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
    )
    for root in roots:
        if not root.exists():
            continue
        for browser_dir in sorted(root.glob("chromium-*"), reverse=True):
            for suffix in suffixes:
                candidate = browser_dir / suffix
                if candidate.exists():
                    return candidate
    return None


def _check_git() -> WizardDependencyCheck:
    git = shutil.which("git")
    if git:
        return WizardDependencyCheck(
            check_id="tool.git",
            title="Git",
            status=CHECK_STATUS_OK,
            detail=f"git found at {git}",
            required=False,
        )
    return WizardDependencyCheck(
        check_id="tool.git",
        title="Git",
        status=CHECK_STATUS_WARN,
        detail="git is not on PATH. Coder minion repo workflows will be limited.",
        required=False,
        fix="Install git and make sure it is on PATH.",
    )


def _check_ollama_embedding() -> WizardDependencyCheck:
    ollama = shutil.which("ollama")
    if not ollama:
        return WizardDependencyCheck(
            check_id="embedding.ollama",
            title="Ollama embedding model",
            status=CHECK_STATUS_WARN,
            detail=f"ollama is not on PATH. Memory can run, but `{DEFAULT_OLLAMA_MODEL_NAME}` embeddings will not be available.",
            required=False,
            fix=f"Install Ollama, start it, then run: ollama pull {DEFAULT_OLLAMA_MODEL_NAME}",
        )
    provider = OllamaEmbeddingProvider(timeout_seconds=2.0)
    health = provider.health()
    if not health.get("healthy"):
        return WizardDependencyCheck(
            check_id="embedding.ollama",
            title="Ollama embedding model",
            status=CHECK_STATUS_WARN,
            detail=f"ollama found at {ollama}, but the local server is not healthy: {health.get('last_error') or 'unknown error'}",
            required=False,
            fix=f"Start Ollama, then run: ollama pull {DEFAULT_OLLAMA_MODEL_NAME}",
        )
    if not health.get("model_available"):
        return WizardDependencyCheck(
            check_id="embedding.ollama",
            title="Ollama embedding model",
            status=CHECK_STATUS_WARN,
            detail=f"Ollama is running, but `{DEFAULT_OLLAMA_MODEL_NAME}` is not installed.",
            required=False,
            fix=f"ollama pull {DEFAULT_OLLAMA_MODEL_NAME}",
        )
    return WizardDependencyCheck(
        check_id="embedding.ollama",
        title="Ollama embedding model",
        status=CHECK_STATUS_OK,
        detail=f"Ollama is running and `{DEFAULT_OLLAMA_MODEL_NAME}` is available.",
        required=False,
    )


def _check_service_manager() -> WizardDependencyCheck:
    system = platform.system().lower()
    if system == "linux":
        systemctl = shutil.which("systemctl")
        if systemctl:
            return WizardDependencyCheck(
                check_id="service.manager",
                title="Service manager",
                status=CHECK_STATUS_OK,
                detail=f"systemctl found at {systemctl}; setup can register a user service.",
                required=False,
            )
        return WizardDependencyCheck(
            check_id="service.manager",
            title="Service manager",
            status=CHECK_STATUS_WARN,
            detail="systemctl is not on PATH. setup can still write runtime files, but cannot enable a user service.",
            required=False,
            fix="Install systemd user service support, or run `pal run --runtime-root <path>` manually.",
        )
    if system == "darwin":
        launchctl = shutil.which("launchctl")
        if launchctl:
            return WizardDependencyCheck(
                check_id="service.manager",
                title="Service manager",
                status=CHECK_STATUS_OK,
                detail=f"launchctl found at {launchctl}; setup can register a user LaunchAgent.",
                required=False,
            )
        return WizardDependencyCheck(
            check_id="service.manager",
            title="Service manager",
            status=CHECK_STATUS_WARN,
            detail="launchctl is not on PATH. setup can still write runtime files, but cannot enable a LaunchAgent.",
            required=False,
            fix="Run `pal run --runtime-root <path>` manually.",
        )
    return WizardDependencyCheck(
        check_id="service.manager",
        title="Service manager",
        status=CHECK_STATUS_INFO,
        detail=f"Automatic service registration is not implemented for {platform.system() or 'this platform'}.",
        required=False,
        fix="pal run --runtime-root <path>",
    )
