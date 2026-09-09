from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pal.cli_paths import default_runtime_root, RUNTIME_ROOT_HELP

from pal.provider_install import (
    ProviderInstallError,
    inspect_provider_wheel,
)


def configure_provider_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="provider_command", required=True)
    install_parser = commands.add_parser(
        "install",
        help="Install one or more channel-provider wheels into a Pal runtime",
    )
    install_parser.add_argument("wheels", nargs="+", type=Path, help="Provider .whl artifact(s)")
    install_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=default_runtime_root(),
        help=RUNTIME_ROOT_HELP,
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall a provider when the same version is already present",
    )


def run_provider_cli(args: argparse.Namespace) -> int:
    if args.provider_command != "install":
        return 2
    runtime_root = Path(args.runtime_root).expanduser().resolve(strict=False)
    try:
        inspected = [inspect_provider_wheel(Path(wheel_path)) for wheel_path in args.wheels]
        provider_ids = [item.provider_id for item in inspected]
        if len(set(provider_ids)) != len(provider_ids):
            raise ProviderInstallError("one install command must not contain duplicate provider ids")
        for wheel in inspected:
            from pal.packages.service import PackageService
            from pal.provider_install import _installed_provider_version
            target = runtime_root / "channel/providers" / wheel.provider_id
            if _installed_provider_version(target) == wheel.provider_version and not args.force:
                raise ProviderInstallError("Provider version is already installed; pass --force to reinstall")
            result = PackageService(runtime_root).install(wheel.wheel_path)
            print(f"Installed provider {result['id']} {result['version']} to {target}")
            print(f"Wheel SHA-256: {result['sha256']}")
        print(
            "Provider files are installed. Run channel_provider_rescan in the running Pal "
            "instance (or restart Pal) to activate the new generation."
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"provider install failed: {exc}", file=sys.stderr)
        return 2
    return 0
