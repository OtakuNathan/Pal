"""Dependency preparation without starting or pruning production browser sessions."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from pal.packages.process import PackageError, run_command
from pal.web_fetch.browser_service import (
    BrowserRuntimePaths, NODE_MINIMUM_MAJOR, PLAYWRIGHT_CLI_PACKAGE, PLAYWRIGHT_CLI_VERSION,
    _chromium_installed, _installed_cli_version,
)


NODE_VERSION = "24.19.0"


def child_env(paths: BrowserRuntimePaths) -> dict[str, str]:
    env = dict(os.environ)
    managed = paths.tooling_root / "node" / f"node-v{NODE_VERSION}" / "bin"
    if (managed / "node").is_file():
        env["PATH"] = str(managed) + os.pathsep + env.get("PATH", "")
    env.update(CI="1", NO_UPDATE_NOTIFIER="1", PLAYWRIGHT_BROWSERS_PATH=str(paths.browser_cache),
               XDG_CACHE_HOME=str(paths.cli_cache))
    return env


def node_major(env: dict[str, str]) -> int | None:
    node = shutil.which("node", path=env.get("PATH"))
    if not node:
        return None
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5, env=env)
        return int(result.stdout.strip().lstrip("v").split(".")[0]) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def inspect(runtime_root: Path) -> dict:
    paths = BrowserRuntimePaths(runtime_root)
    env = child_env(paths)
    major = node_major(env)
    cli = _installed_cli_version(paths)
    browser = _chromium_installed(paths)
    npm = bool(shutil.which("npm", path=env.get("PATH")))
    return {"ok": bool(major and major >= NODE_MINIMUM_MAJOR and npm and cli == PLAYWRIGHT_CLI_VERSION and browser),
            "node_major": major, "required_node_major": NODE_MINIMUM_MAJOR, "npm": npm,
            "cli_version": cli, "required_cli_version": PLAYWRIGHT_CLI_VERSION, "browser_installed": browser}


def _download(url: str, target: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


def _ensure_node(paths: BrowserRuntimePaths) -> None:
    env = child_env(paths)
    if (node_major(env) or 0) >= NODE_MINIMUM_MAJOR and shutil.which("npm", path=env.get("PATH")):
        return
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(platform.machine())
    if not system or not arch:
        raise PackageError("No managed Node build for this platform; install compatible Node and npm")
    target = paths.tooling_root / "node" / f"node-v{NODE_VERSION}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-install-", dir=target.parent) as temporary:
        root = Path(temporary)
        name = f"node-v{NODE_VERSION}-{system}-{arch}"
        archive = root / f"{name}.tar.gz"
        base = f"https://nodejs.org/dist/v{NODE_VERSION}"
        _download(f"{base}/SHASUMS256.txt", root / "checksums")
        checksums = {line.split()[1].lstrip("*"): line.split()[0] for line in (root / "checksums").read_text().splitlines() if len(line.split()) == 2}
        expected = checksums.get(archive.name)
        if not expected:
            raise PackageError("Official Node checksum is missing")
        _download(f"{base}/{archive.name}", archive)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise PackageError("Node download checksum mismatch")
        with tarfile.open(archive) as bundle:
            bundle.extractall(root / "unpacked", filter="data")
        candidate = root / "unpacked" / name
        if target.exists():
            shutil.rmtree(target)
        os.replace(candidate, target)
    if (node_major(child_env(paths)) or 0) < NODE_MINIMUM_MAJOR:
        raise PackageError("Managed Node does not meet the Playwright requirement")


def _ensure_cli(paths: BrowserRuntimePaths) -> None:
    if _installed_cli_version(paths) == PLAYWRIGHT_CLI_VERSION and paths.cli.is_file():
        return
    env = child_env(paths)
    npm = shutil.which("npm", path=env.get("PATH"))
    if not npm:
        raise PackageError("npm is missing after Node preparation")
    with tempfile.TemporaryDirectory(prefix="cli-install-", dir=paths.tooling_root) as temporary:
        candidate = Path(temporary) / "candidate"
        run_command([npm, "install", "--prefix", str(candidate), "--ignore-scripts", "--no-audit", "--no-fund",
                     "--omit=dev", f"{PLAYWRIGHT_CLI_PACKAGE}@{PLAYWRIGHT_CLI_VERSION}"], env=env)
        metadata = json.loads((candidate / "node_modules/playwright-core/package.json").read_text())
        if metadata.get("engines", {}).get("node") != ">=20":
            raise PackageError("Pinned Playwright Node requirement changed; update the dependency contract")
        old = Path(temporary) / "previous"
        if paths.tooling_current.exists():
            os.replace(paths.tooling_current, old)
        try:
            os.replace(candidate, paths.tooling_current)
        except Exception:
            if old.exists():
                os.replace(old, paths.tooling_current)
            raise


def _install_system_libraries(paths: BrowserRuntimePaths) -> None:
    if platform.system() != "Linux" or not shutil.which("apt-get"):
        raise PackageError("Browser system libraries are missing; automatic preparation requires a supported apt-based Linux platform")
    playwright = str(paths.tooling_current / "node_modules/.bin/playwright")
    env = child_env(paths)
    if os.geteuid() == 0:
        argv = [playwright, "install-deps", "chromium"]
    else:
        sudo = shutil.which("sudo")
        if not sudo:
            raise PackageError("Chromium system libraries need administrative privileges; sudo is unavailable")
        # Noninteractive sudo must never wait for a password in a background job.
        node = shutil.which("node", path=env.get("PATH"))
        argv = [sudo, "-n", node, str(paths.tooling_current / "node_modules/playwright/cli.js"), "install-deps", "chromium"]
    run_command(argv, env=env)


def verify(runtime_root: Path) -> dict:
    paths = BrowserRuntimePaths(runtime_root)
    state = inspect(runtime_root)
    if not state["ok"]:
        raise PackageError(f"Browser dependencies missing: {state}")
    with tempfile.TemporaryDirectory(prefix="pal-browser-verify-") as temporary:
        root = Path(temporary)
        extension = root / "extension"
        extension.mkdir()
        (extension / "manifest.json").write_text(json.dumps({"manifest_version": 3, "name": "Pal installation check", "version": "1.0", "background": {"service_worker": "worker.js"}}))
        (extension / "worker.js").write_text("chrome.runtime.onInstalled.addListener(() => {});")
        script = root / "verify.cjs"
        # Private scratch profile; this does not construct a production worker.
        script.write_text("""const { chromium } = require(process.argv[2]);
(async () => {
  const extension = process.argv[4];
  const context = await chromium.launchPersistentContext(process.argv[3], {
    channel: 'chromium', headless: true,
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]
  });
  try {
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', {timeout: 15000});
    const result = await worker.evaluate(() => chrome.runtime.getManifest().name);
    if (result !== 'Pal installation check') throw Error('Extension verification failed');
    const page = context.pages()[0] || await context.newPage();
    await page.setContent('<title>Pal browser ready</title>');
    if (await page.title() !== 'Pal browser ready') throw Error('Page verification failed');
  } finally { await context.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
        env = child_env(paths)
        run_command([shutil.which("node", path=env.get("PATH")), str(script),
                     str(paths.tooling_current / "node_modules/playwright"), str(root / "profile"), str(extension)], env=env, timeout=60)
    return {**state, "browser_launch_verified": True, "extension_verified": True}


def prepare(runtime_root: Path) -> dict:
    """Caller owns the common package lock, including runtime self-repair callers."""
    paths = BrowserRuntimePaths(runtime_root)
    paths.prepare()
    _ensure_node(paths)
    _ensure_cli(paths)
    if not _chromium_installed(paths):
        run_command([str(paths.tooling_current / "node_modules/.bin/playwright"), "install", "chromium", "--no-shell"], env=child_env(paths))
    try:
        return verify(runtime_root)
    except PackageError as exc:
        detail = str(exc).lower()
        if not any(marker in detail for marker in ("missing dependencies", "error while loading shared libraries", "host system is missing")):
            raise
        _install_system_libraries(paths)
        return verify(runtime_root)
