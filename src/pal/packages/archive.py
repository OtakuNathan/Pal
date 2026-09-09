from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path

from pal.packages.process import PackageError, run_command
from pal.provider_install import _safe_member_path


PROTOCOL = "pal.install.v1"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def valid_id(value: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise PackageError("Invalid package id")
    return value


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > 4096 or sum(item.file_size for item in members) > 256 * 1024 * 1024:
        raise PackageError("Package exceeds archive limits")
    names = [str(_safe_member_path(item)) for item in members]
    if len(set(names)) != len(names):
        raise PackageError("Duplicate archive paths")
    return [item for item in members if not item.is_dir()]


def _runtime_manifest(root: Path, kind: str, package_id: str, version: str) -> None:
    filename, id_key = ("plugin.toml", "plugin_id") if kind == "plugin" else ("provider.toml", "provider_id")
    payload = tomllib.loads((root / filename).read_text(encoding="utf-8"))
    if payload.get(id_key) != package_id or payload.get("version") != version:
        raise PackageError("Package and runtime manifest identity/version differ")
    if kind == "plugin" and payload.get("lifecycle_protocol") != "raii.v1":
        raise PackageError("Packaged plugins must implement raii.v1")
    entrypoint = str(payload.get("entrypoint", ""))
    if not entrypoint:
        raise PackageError("Runtime entrypoint is required")
    if kind == "plugin":
        relative = Path(*entrypoint.split("."))
        candidates = (root / relative.with_suffix(".py"), root / relative / "__init__.py")
    else:
        relative = Path(entrypoint) if entrypoint.endswith(".py") else Path(*entrypoint.split(".")).with_suffix(".py")
        candidates = (root / relative,)
    if not any(path.is_file() and path.resolve().is_relative_to(root.resolve()) for path in candidates):
        raise PackageError("Runtime entrypoint must exist inside host payload")


@dataclass(frozen=True)
class PackageArtifact:
    root: Path
    manifest: dict
    sha256: str

    @property
    def package_id(self) -> str:
        return self.manifest["id"]

    @property
    def kind(self) -> str:
        return self.manifest["kind"]


def inspect_extracted(root: Path, sha256: str) -> PackageArtifact:
    manifest = json.loads((root / "package.json").read_text())
    if manifest.get("protocol") != PROTOCOL or manifest.get("kind") not in {"plugin", "provider"}:
        raise PackageError("Unsupported package protocol or kind")
    valid_id(manifest["id"])
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise PackageError("Package version is required")
    for field in ("wheel", "hooks"):
        value = manifest.get(field)
        if value and (Path(value).name != value or not (root / value).is_file()):
            raise PackageError(f"Invalid {field} path")
    if manifest.get("python") not in {"venv", "host"}:
        raise PackageError("python must be venv or host")
    if manifest["python"] == "venv" and not manifest.get("wheel"):
        raise PackageError("venv packages require a backend wheel")
    if manifest["python"] == "host" and manifest.get("wheel"):
        raise PackageError("Backend wheels require an isolated venv")
    expected = manifest.get("files", {})
    actual = {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*")
              if p.is_file() and p != root / "package.json"}
    if expected != actual:
        raise PackageError("Package file checksums do not match")
    _runtime_manifest(root / "host", manifest["kind"], manifest["id"], manifest["version"])
    if manifest.get("wheel"):
        with zipfile.ZipFile(root / manifest["wheel"]) as wheel:
            entries = _members(wheel)
            metadata = [item for item in entries if item.filename.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise PackageError("Backend wheel must have one METADATA")
            version = BytesParser().parsebytes(wheel.read(metadata[0]))["Version"]
            if version != manifest["version"]:
                raise PackageError("Backend wheel version differs from package version")
    return PackageArtifact(root, manifest, sha256)


def unpack(path: Path, destination: Path) -> PackageArtifact:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(path) as archive:
            for member in _members(archive):
                target = destination / member.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return inspect_extracted(destination, digest(path))
    except Exception:
        shutil.rmtree(destination)
        raise


def build(source: Path, output: Path) -> Path:
    """package.toml declares kind/id/version, host_files and optional wheel/hooks."""
    source = source.resolve()
    config = tomllib.loads((source / "package.toml").read_text())
    valid_id(config["id"])
    with tempfile.TemporaryDirectory(prefix="pal-package-build-") as temporary:
        root = Path(temporary)
        host = root / "host"
        host.mkdir()
        for name in config.get("host_files", []):
            path = source / name
            if Path(name).is_absolute() or ".." in Path(name).parts or not path.resolve().is_relative_to(source):
                raise PackageError("host_files must stay inside the project")
            if path.is_symlink() or (path.is_dir() and any(child.is_symlink() for child in path.rglob("*"))):
                raise PackageError("host_files must not contain symlinks")
            target = host / name
            if path.is_dir():
                shutil.copytree(path, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        mode = config.get("python", "venv")
        wheel_name = None
        if mode == "venv":
            with tempfile.TemporaryDirectory(prefix="pal-wheel-build-") as wheel_tmp:
                run_command([sys.executable, "-m", "pip", "wheel", str(source), "--no-deps", "-w", wheel_tmp])
                wheels = list(Path(wheel_tmp).glob("*.whl"))
                if len(wheels) != 1:
                    raise PackageError("Expected exactly one backend wheel")
                wheel_name = wheels[0].name
                shutil.copy2(wheels[0], root / wheel_name)
        hooks = config.get("hooks")
        if hooks:
            if Path(hooks).name != hooks or not (source / hooks).resolve().is_relative_to(source):
                raise PackageError("hooks must name a project-local Python file")
            shutil.copy2(source / hooks, root / hooks)
        manifest = dict(protocol=PROTOCOL, id=config["id"], kind=config["kind"], version=config["version"],
                        python=mode, wheel=wheel_name, hooks=hooks)
        manifest["files"] = {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file()}
        (root / "package.json").write_text(json.dumps(manifest, indent=2))
        inspect_extracted(root, "build")
        output.mkdir(parents=True, exist_ok=True)
        target = output / f"{config['kind']}-{config['id']}-{config['version']}.palpkg"
        # Version must never act as an output path.
        if target.parent.resolve() != output.resolve():
            raise PackageError("Invalid package version")
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        return target
