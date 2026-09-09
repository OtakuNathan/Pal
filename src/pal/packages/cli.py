from __future__ import annotations

import argparse
import json
from pathlib import Path

from pal.cli_paths import default_runtime_root, RUNTIME_ROOT_HELP

from pal.packages.archive import build
from pal.packages.service import PackageService


def configure_package_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="package_command", required=True)
    builder = commands.add_parser("build", help="Build a .palpkg from package.toml and a wheel project")
    builder.add_argument("source", type=Path)
    builder.add_argument("--output", type=Path, default=Path("dist"))
    installer = commands.add_parser("install", help="Install and prepare isolated packages")
    installer.add_argument("paths", nargs="+", type=Path)
    preparer = commands.add_parser("prepare", help="Prepare installed or built-in package dependencies")
    preparer.add_argument("name", nargs="?")
    preparer.add_argument("--all-builtin", action="store_true")
    preparer.add_argument("--kind", choices=("plugin", "provider", "builtin"), default="plugin")
    status = commands.add_parser("status", help="Show installation results")
    status.add_argument("name", nargs="?")
    status.add_argument("--kind", choices=("plugin", "provider", "builtin"), default="plugin")
    for command in (installer, preparer, status):
        command.add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)


def run_package_cli(args: argparse.Namespace) -> int:
    try:
        if args.package_command == "build":
            print(build(args.source, args.output))
            return 0
        service = PackageService(args.runtime_root)
        if args.package_command == "install":
            for path in args.paths:
                print(json.dumps(service.install(path), ensure_ascii=False))
            print("Prepared packages are installed. Rescan/attach through the running owner, or start Pal, to activate them.")
        elif args.package_command == "prepare":
            if args.all_builtin:
                from pal.plugins.host import _source_plugins_root
                names = [path.parent.name for path in sorted(_source_plugins_root().glob("*/plugin.toml"))]
                errors = []
                for name in names:
                    try:
                        print(json.dumps(service.prepare(name, kind="builtin"), ensure_ascii=False))
                    except Exception as exc:
                        errors.append({"id": name, "error": str(exc)})
                if errors:
                    print(json.dumps({"errors": errors}, ensure_ascii=False))
                    return 2
            elif args.name:
                print(json.dumps(service.prepare(args.name, kind=args.kind), ensure_ascii=False))
            else:
                raise ValueError("prepare requires a name or --all-builtin")
        else:
            print(json.dumps(service.status(name=args.name, kind=args.kind), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
