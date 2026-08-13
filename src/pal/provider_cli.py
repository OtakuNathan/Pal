from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pal.provider_install import (
    ProviderInstallError,
    inspect_provider_wheel,
    install_provider_wheel,
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
        default=Path("~/.pal"),
        help="Pal runtime root (default: ~/.pal)",
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
            result = install_provider_wheel(
                wheel.wheel_path,
                runtime_root=runtime_root,
                force=bool(args.force),
            )
            print(
                f"Installed provider {result.provider_id} {result.provider_version} "
                f"to {result.target_dir}"
            )
            if result.archived_previous_dir is not None:
                print(f"Archived previous provider at {result.archived_previous_dir}")
            print(f"Wheel SHA-256: {result.wheel_sha256}")
        print(
            "Provider files are installed. Run channel_provider_rescan in the running Pal "
            "instance (or restart Pal) to activate the new generation."
        )
    except (OSError, ProviderInstallError) as exc:
        print(f"provider install failed: {exc}", file=sys.stderr)
        return 2
    return 0
