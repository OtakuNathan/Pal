from __future__ import annotations

import hashlib
import re
import shutil
import stat
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


_PROVIDER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_WHEEL_FILES = 2_048
_MAX_WHEEL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_RECEIPT_FILENAME = ".pal-provider-install.json"


class ProviderInstallError(ValueError):
    """The supplied wheel is not a valid Pal channel-provider artifact."""


@dataclass(frozen=True)
class ProviderWheel:
    wheel_path: Path
    distribution: str
    distribution_version: str
    provider_id: str
    provider_version: str
    payload_root: PurePosixPath
    payload_files: tuple[PurePosixPath, ...]
    wheel_sha256: str


def inspect_provider_wheel(wheel_path: Path) -> ProviderWheel:
    path = Path(wheel_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".whl":
        raise ProviderInstallError(f"provider artifact must be a .whl file: {path}")

    wheel_sha256 = _sha256_file(path)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProviderInstallError(f"invalid provider wheel: {exc}") from exc
    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > _MAX_WHEEL_FILES:
            raise ProviderInstallError(
                f"provider wheel contains too many files ({len(members)} > {_MAX_WHEEL_FILES})"
            )
        total_size = sum(max(int(item.file_size), 0) for item in members)
        if total_size > _MAX_WHEEL_UNCOMPRESSED_BYTES:
            raise ProviderInstallError(
                "provider wheel exceeds the uncompressed size limit "
                f"({total_size} > {_MAX_WHEEL_UNCOMPRESSED_BYTES})"
            )
        member_paths = tuple(_safe_member_path(item) for item in members)
        if len(set(member_paths)) != len(member_paths):
            raise ProviderInstallError("provider wheel contains duplicate member paths")
        manifests = [item for item in member_paths if item.name == "provider.toml"]
        if len(manifests) != 1:
            raise ProviderInstallError(
                f"provider wheel must contain exactly one provider.toml; found {len(manifests)}"
            )
        manifest_path = manifests[0]
        payload_root = manifest_path.parent
        if not payload_root.parts or payload_root.name.endswith(".dist-info"):
            raise ProviderInstallError("provider.toml must live in a provider payload directory")
        payload_files = tuple(
            item
            for item in member_paths
            if item != payload_root and item.is_relative_to(payload_root)
        )
        manifest = _read_provider_manifest(archive.read(str(manifest_path)))
        provider_id = str(manifest.get("provider_id") or "").strip()
        provider_version = str(manifest.get("version") or "").strip()
        entrypoint = _safe_relative_manifest_path(
            str(manifest.get("entrypoint") or "").strip(),
            field_name="entrypoint",
        )
        if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
            raise ProviderInstallError(f"invalid provider_id in provider.toml: {provider_id!r}")
        if not provider_version:
            raise ProviderInstallError("provider.toml version is required")
        if payload_root / entrypoint not in payload_files:
            raise ProviderInstallError(f"provider entrypoint is missing from wheel: {entrypoint}")
        distribution, distribution_version = _read_wheel_metadata(archive, member_paths)
        if distribution_version != provider_version:
            raise ProviderInstallError(
                "wheel metadata version does not match provider.toml: "
                f"{distribution_version!r} != {provider_version!r}"
            )
    return ProviderWheel(
        wheel_path=path,
        distribution=distribution,
        distribution_version=distribution_version,
        provider_id=provider_id,
        provider_version=provider_version,
        payload_root=payload_root,
        payload_files=payload_files,
        wheel_sha256=wheel_sha256,
    )


def _extract_provider_payload(wheel: ProviderWheel, target_dir: Path) -> None:
    with zipfile.ZipFile(wheel.wheel_path) as archive:
        for member_path in wheel.payload_files:
            relative = member_path.relative_to(wheel.payload_root)
            if not relative.parts or "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            destination = target_dir.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(str(member_path)) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            destination.chmod(0o644)


def _safe_member_path(item: zipfile.ZipInfo) -> PurePosixPath:
    name = item.filename
    if "\\" in name:
        raise ProviderInstallError(f"wheel member uses a non-portable path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ProviderInstallError(f"unsafe wheel member path: {name!r}")
    file_type = (item.external_attr >> 16) & 0o170000
    if file_type == stat.S_IFLNK:
        raise ProviderInstallError(f"provider wheel must not contain symlinks: {name!r}")
    return path


def _read_provider_manifest(raw: bytes) -> dict[str, object]:
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProviderInstallError(f"invalid provider.toml: {exc}") from exc
    if not isinstance(value, dict):
        raise ProviderInstallError("provider.toml must contain a TOML table")
    return value


def _read_wheel_metadata(
    archive: zipfile.ZipFile,
    member_paths: tuple[PurePosixPath, ...],
) -> tuple[str, str]:
    metadata_paths = [
        path
        for path in member_paths
        if path.name == "METADATA" and path.parent.name.endswith(".dist-info")
    ]
    if len(metadata_paths) != 1:
        raise ProviderInstallError(
            f"provider wheel must contain exactly one dist-info/METADATA; found {len(metadata_paths)}"
        )
    message = BytesParser().parsebytes(archive.read(str(metadata_paths[0])))
    distribution = str(message.get("Name") or "").strip()
    version = str(message.get("Version") or "").strip()
    if not distribution or not version:
        raise ProviderInstallError("wheel METADATA must declare Name and Version")
    return distribution, version


def _safe_relative_manifest_path(value: str, *, field_name: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProviderInstallError(f"provider.toml {field_name} must be a safe relative path")
    return path


def _installed_provider_version(target_dir: Path) -> str:
    manifest_path = target_dir / "provider.toml"
    if not manifest_path.is_file():
        return ""
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ""
    return str(manifest.get("version") or "").strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
