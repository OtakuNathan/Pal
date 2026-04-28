"""CLI orchestration for ``pal setup``."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

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


def _resolve_pal_bin() -> str:
    pal_bin = shutil.which("pal")
    if pal_bin:
        return pal_bin
    return sys.executable + " -m pal.main"


def _generate_service_content(
    *,
    pal_bin: str,
    runtime_root: Path,
) -> str:
    runtime_path = runtime_root.as_posix()
    return (
        "[Unit]\n"
        "Description=Pal Agent Runtime\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"Type=simple\n"
        f"ExecStart={pal_bin} run --runtime-root {runtime_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"StandardOutput=append:{runtime_path}/pal.log\n"
        f"StandardError=append:{runtime_path}/pal.log\n"
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


def _prompt_service_setup(runtime_root: Path) -> str | None:
    """Ask user if they want to register a systemd service. Returns service name or None."""
    print(f"\n{'=' * 50}")
    print("  Service Registration")
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
    )

    print()
    print("  Service file content:")
    for line in content.strip().splitlines():
        print(f"    {line}")
    print()

    if not ask_yes_no("  Register and start this service?", default=True):
        return None

    # Write the service file
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    service_path.write_text(content, encoding="utf-8")

    if _register_and_start_service(service_name, service_path):
        print(f"  Service {service_name} registered and started.")
        return service_name
    else:
        print(f"  Service file written to {service_path} but could not be activated.")
        print("  Activate manually: systemctl --user enable --now " + service_name)
        return service_name


def run_setup_wizard() -> int:
    print(_BANNER)

    result = run_interactive_wizard()
    if result is None:
        print("\n  Setup cancelled.")
        return 1

    runtime_root, collected = result

    # ------------------------------------------------------------------
    # Handle existing DB
    # ------------------------------------------------------------------
    db_path = runtime_root / DEFAULT_DB_FILENAME
    service = WizardService()

    if db_path.exists():
        print(f"\n  A database already exists at {db_path}")
        choice = ask_yes_no("  Overwrite (delete + recreate)?", default=False)
        if choice:
            db_path.unlink()
            print("  Old database deleted.")
        else:
            update = ask_yes_no("  Update (upsert in place)?", default=True)
            if not update:
                print("  Aborting.")
                return 1
            print("  Will update existing database.")

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
        print(f"    Log:     {runtime_root}/pal.log")
    else:
        print(f"\n    Run: pal run --runtime-root {runtime_root}")
    return 0
