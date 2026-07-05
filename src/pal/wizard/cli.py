"""CLI orchestration for ``pal setup``."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

from pal.foundation.log_paths import pal_log_path, pal_log_root
from pal.wizard.dependencies import WizardDependencyCheck, collect_dependency_checks
from pal.wizard.prompts import ask, ask_yes_no, run_interactive_wizard
from pal.wizard.runtime import DEFAULT_DB_FILENAME, DEFAULT_PAL_ENTRYPOINT, WizardService


_BANNER = r"""
  ____          _       ____            _
 |  _ \ ___  __| |_   _/ ___| ___  _ __| |_
 | |_) / _ \/ _` | | | \___ \/ _ \| '__| __|
 |  __/  __/ (_| | |_| |___) | (_) | |  | |_
 |_|   \___|\__,_|\__, |____/ \___/|_|   \__|
                   |___/
  Interactive Setup Wizard
"""

_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_LAUNCHD_USER_DIR = Path.home() / "Library" / "LaunchAgents"
_SERVICE_ENV_PASSTHROUGH = (
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
)


def _resolve_pal_command() -> list[str]:
    invoked = Path(sys.argv[0])
    if invoked.name == "pal":
        if invoked.is_absolute() and invoked.exists():
            return [str(invoked)]
        if invoked.parent != Path("."):
            relative_invoked = (Path.cwd() / invoked).resolve()
            if relative_invoked.exists():
                return [str(relative_invoked)]
        invoked_on_path = shutil.which(str(invoked))
        if invoked_on_path:
            return [invoked_on_path]
    pal_bin = shutil.which("pal")
    if pal_bin:
        return [pal_bin]
    return [sys.executable, "-m", "pal.main"]


def _resolve_pal_bin() -> str:
    return shlex.join(_resolve_pal_command())


def _runtime_service_environment() -> dict[str, str]:
    environment: dict[str, str] = {"PYTHONUNBUFFERED": "1"}
    for key in _SERVICE_ENV_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _format_systemd_environment_value(key: str, value: str) -> str:
    escaped = f"{key}={value}".replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{escaped}"'


def _generate_service_content(
    *,
    pal_bin: str,
    runtime_root: Path,
    environment: dict[str, str] | None = None,
) -> str:
    runtime_path = runtime_root.as_posix()
    log_root = pal_log_root(runtime_root).as_posix()
    service_environment = environment or {"PYTHONUNBUFFERED": "1"}
    environment_lines = "\n".join(
        _format_systemd_environment_value(key, value)
        for key, value in service_environment.items()
        if value
    )
    return (
        "[Unit]\n"
        "Description=Pal Agent Runtime\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"Type=simple\n"
        f"ExecStartPre=/usr/bin/mkdir -p {log_root}\n"
        f"ExecStart={pal_bin} run --runtime-root {runtime_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        f"{environment_lines}\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _pick_service_name(runtime_root: Path) -> str:
    """Pick a service name that does not conflict with existing services."""
    # Default: pal.service
    # If pal.service exists and points to a different runtime_root, use pal@<name>.service
    default_name = "pal"
    candidate = f"{default_name}.service"
    candidate_path = _SYSTEMD_USER_DIR / candidate

    if not candidate_path.exists():
        return candidate

    # Check if existing pal.service already points to the same runtime_root
    try:
        content = candidate_path.read_text(encoding="utf-8")
        if runtime_root.as_posix() in content:
            return candidate
    except OSError:
        pass

    # Conflict — derive a name from the runtime_root directory name
    stem = runtime_root.name.replace(".", "").replace(" ", "-")
    if not stem or stem == "pal":
        stem = "pal2"
    candidate = f"pal@{stem}.service"
    if (_SYSTEMD_USER_DIR / candidate).exists():
        try:
            content = (_SYSTEMD_USER_DIR / candidate).read_text(encoding="utf-8")
            if runtime_root.as_posix() in content:
                return candidate
        except OSError:
            pass
        # Ask user
        override = ask(
            f"  Service file {candidate} already exists. Choose a different name",
            f"pal@{stem}-2",
        )
        return f"{override}.service"

    return candidate


def _register_and_start_service(service_name: str, service_path: Path) -> bool:
    """Write service file, daemon-reload, enable --now. Returns True on success."""
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service_path.read_text(), encoding="utf-8")

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", service_name],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Failed to register service: {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("  systemctl not found. Skipping service registration.")
        return False


def _prompt_systemd_service_setup(runtime_root: Path) -> str | None:
    """Ask user if they want to register a systemd service. Returns service name or None."""
    print(f"\n{'=' * 50}")
    print("  Service Registration (systemd)")
    print(f"{'=' * 50}\n")

    pal_bin = _resolve_pal_bin()
    service_name = _pick_service_name(runtime_root)
    service_path = _SYSTEMD_USER_DIR / service_name

    print(f"  Pal binary: {pal_bin}")
    print(f"  Service:    {service_name}")
    print(f"  Runtime:    {runtime_root}")

    if service_path.exists():
        print(f"  (File exists at {service_path})")

    content = _generate_service_content(
        pal_bin=pal_bin,
        runtime_root=runtime_root,
        environment=_runtime_service_environment(),
    )

    print()
    print("  Service file content:")
    for line in content.strip().splitlines():
        print(f"    {line}")
    print()

    if not ask_yes_no("  Register and start this service?", default=True):
        return None

    # Write the service file
    pal_log_root(runtime_root).mkdir(parents=True, exist_ok=True)
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    service_path.write_text(content, encoding="utf-8")

    if _register_and_start_service(service_name, service_path):
        print(f"  Service {service_name} registered and started.")
        return service_name
    else:
        print(f"  Service file written to {service_path} but could not be activated.")
        print("  Activate manually: systemctl --user enable --now " + service_name)
        return service_name


def _launchd_domain() -> str:
    getuid = getattr(os, "getuid", None)
    uid = getuid() if callable(getuid) else ""
    return f"gui/{uid}" if uid != "" else "gui/$UID"


def _sanitize_launchd_label_part(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return text or "pal"


def _pick_launchd_label(runtime_root: Path) -> str:
    default_label = "com.pal.runtime"
    default_path = _LAUNCHD_USER_DIR / f"{default_label}.plist"
    runtime_path = runtime_root.as_posix()
    if not default_path.exists():
        return default_label
    try:
        if runtime_path in default_path.read_text(encoding="utf-8"):
            return default_label
    except OSError:
        pass

    stem = _sanitize_launchd_label_part(runtime_root.name)
    if stem in {"pal", "runtime"}:
        stem = "pal2"
    candidate = f"com.pal.{stem}"
    candidate_path = _LAUNCHD_USER_DIR / f"{candidate}.plist"
    if not candidate_path.exists():
        return candidate
    try:
        if runtime_path in candidate_path.read_text(encoding="utf-8"):
            return candidate
    except OSError:
        pass

    override = ask(
        f"  LaunchAgent {candidate}.plist already exists. Choose a different label",
        f"{candidate}-2",
    )
    override = _sanitize_launchd_label_part(override)
    return override if override.startswith("com.") else f"com.pal.{override}"


def _generate_launchd_plist(
    *,
    label: str,
    pal_command: list[str],
    runtime_root: Path,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    runtime_path = runtime_root.as_posix()
    return {
        "Label": label,
        "ProgramArguments": [*pal_command, "run", "--runtime-root", runtime_path],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "WorkingDirectory": runtime_path,
        "EnvironmentVariables": dict(environment or {"PYTHONUNBUFFERED": "1"}),
    }


def _register_and_start_launchd(label: str, plist_path: Path) -> bool:
    domain = _launchd_domain()
    service_target = f"{domain}/{label}"
    try:
        subprocess.run(
            ["launchctl", "bootout", service_target],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["launchctl", "bootstrap", domain, plist_path.as_posix()],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["launchctl", "kickstart", "-k", service_target],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"  Failed to register LaunchAgent: {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("  launchctl not found. Skipping LaunchAgent registration.")
        return False


def _prompt_launchd_service_setup(runtime_root: Path) -> str | None:
    print(f"\n{'=' * 50}")
    print("  Service Registration (launchd)")
    print(f"{'=' * 50}\n")

    pal_command = _resolve_pal_command()
    label = _pick_launchd_label(runtime_root)
    plist_path = _LAUNCHD_USER_DIR / f"{label}.plist"
    plist_payload = _generate_launchd_plist(
        label=label,
        pal_command=pal_command,
        runtime_root=runtime_root,
        environment=_runtime_service_environment(),
    )

    print(f"  Pal command: {' '.join(pal_command)}")
    print(f"  Label:       {label}")
    print(f"  Runtime:     {runtime_root}")
    print(f"  Plist:       {plist_path}")
    if plist_path.exists():
        print(f"  (File exists at {plist_path})")

    print()
    print("  LaunchAgent ProgramArguments:")
    for item in plist_payload["ProgramArguments"]:
        print(f"    {item}")
    print()

    if not ask_yes_no("  Register and start this LaunchAgent?", default=True):
        return None

    pal_log_root(runtime_root).mkdir(parents=True, exist_ok=True)
    _LAUNCHD_USER_DIR.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as f:
        plistlib.dump(plist_payload, f, sort_keys=True)

    if _register_and_start_launchd(label, plist_path):
        print(f"  LaunchAgent {label} registered and started.")
        return label
    print(f"  LaunchAgent plist written to {plist_path} but could not be activated.")
    print(f"  Activate manually: launchctl bootstrap {_launchd_domain()} {plist_path}")
    return label


def _prompt_service_setup(runtime_root: Path) -> str | None:
    system = platform.system().lower()
    if system == "linux":
        return _prompt_systemd_service_setup(runtime_root)
    if system == "darwin":
        return _prompt_launchd_service_setup(runtime_root)
    print(f"\n{'=' * 50}")
    print("  Service Registration")
    print(f"{'=' * 50}\n")
    print(f"  Automatic service registration is not supported on {platform.system() or 'this platform'}.")
    print(f"  Run manually: pal run --runtime-root {runtime_root}")
    return None


def _render_dependency_check(check: WizardDependencyCheck) -> str:
    marker = {
        "ok": "OK",
        "info": "INFO",
        "warn": "WARN",
        "missing": "MISS",
        "error": "ERR",
    }.get(check.status, check.status.upper())
    suffix = " required" if check.required else " optional"
    lines = [f"  [{marker}] {check.title} ({suffix})", f"       {check.detail}"]
    if check.fix:
        lines.append(f"       Fix: {check.fix}")
    return "\n".join(lines)


def run_dependency_doctor() -> int:
    print("\n=== Pal Setup Doctor ===\n")
    checks = collect_dependency_checks()
    for check in checks:
        print(_render_dependency_check(check))
    blocking = [check for check in checks if check.blocking]
    warnings = [check for check in checks if check.status == "warn"]
    print()
    print(f"  Blocking: {len(blocking)}")
    print(f"  Warnings: {len(warnings)}")
    return 0 if not blocking else 2


def run_setup_wizard() -> int:
    print(_BANNER)

    service = WizardService()
    result = run_interactive_wizard(existing_loader=service.load_existing_wizard_data)
    if result is None:
        print("\n  Setup cancelled.")
        return 1

    runtime_root, collected = result

    # ------------------------------------------------------------------
    # Handle existing DB
    # ------------------------------------------------------------------
    db_path = runtime_root / DEFAULT_DB_FILENAME
    if db_path.exists():
        print(f"\n  Updating existing database at {db_path}")

    # ------------------------------------------------------------------
    # Provision
    # ------------------------------------------------------------------
    registration = service.provision_runtime(
        display_name=collected.identity.display_name,
        runtime_root=runtime_root,
        db_filename=DEFAULT_DB_FILENAME,
        pal_entrypoint=DEFAULT_PAL_ENTRYPOINT,
    )
    service.create_database(registration)
    service.provision_builtin_plugins(registration)
    service.seed_from_wizard(registration, collected)

    print(f"\n  Database configured at {db_path}")

    # ------------------------------------------------------------------
    # Systemd service
    # ------------------------------------------------------------------
    svc_name = _prompt_service_setup(runtime_root)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'=' * 50}")
    print("  Setup Complete")
    print(f"{'=' * 50}")
    print(f"    Home:    {runtime_root}")
    print(f"    DB:      {db_path}")
    if svc_name:
        print(f"    Service: {svc_name} (running)")
        print(f"    Log:     {pal_log_path(runtime_root)}")
    else:
        print(f"\n    Run: pal run --runtime-root {runtime_root}")
    return 0
