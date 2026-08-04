from __future__ import annotations

import base64
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


def derive_runtime_fernet_key(
    *,
    runtime_root: str | Path,
    purpose: str,
    include_hostname: bool = False,
) -> bytes:
    """Derive a purpose-separated deployment-local Fernet key."""

    parts: list[str] = []
    if include_hostname:
        parts.append(socket.gethostname())
    parts.extend((getpass.getuser(), str(Path(runtime_root)), str(purpose)))
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        ":".join(parts).encode("utf-8"),
        b"pal_v2_secret_salt",
        200_000,
    )
    return base64.urlsafe_b64encode(digest)


@dataclass(frozen=True)
class EncryptedJsonEnvelopeCodec:
    """Authenticated encryption for JSON runtime artifacts."""

    runtime_root: Path
    purpose: str

    def encrypt(self, value: Mapping[str, Any]) -> str:
        from cryptography.fernet import Fernet

        plaintext = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return Fernet(self._key()).encrypt(plaintext).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        from cryptography.fernet import Fernet, InvalidToken

        try:
            plaintext = Fernet(self._key()).decrypt(str(token).encode("ascii"))
            decoded = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("encrypted JSON envelope is invalid or was tampered with") from exc
        if not isinstance(decoded, dict):
            raise ValueError("encrypted JSON envelope must contain an object")
        return dict(decoded)

    def _key(self) -> bytes:
        master = _load_or_create_runtime_master_key(self.runtime_root)
        digest = hmac.new(
            master,
            str(self.purpose).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest)


def ensure_runtime_snapshot_key(runtime_root: str | Path) -> Path:
    """Materialize the runtime key before entering a read-only sandbox."""

    _load_or_create_runtime_master_key(runtime_root)
    return Path(runtime_root) / "data" / "security" / "runtime_state.key"


def _load_or_create_runtime_master_key(runtime_root: str | Path) -> bytes:
    """Return a deployment-local random key without a concurrent rotation race."""

    root = Path(runtime_root)
    directory = root / "data" / "security"
    key_path = directory / "runtime_state.key"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(PermissionError):
        directory.chmod(0o700)
    if key_path.is_file():
        return _read_runtime_master_key(key_path)

    encoded = base64.urlsafe_b64encode(secrets.token_bytes(32))
    temporary = directory / f".{key_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, key_path)
        except FileExistsError:
            pass
        else:
            _fsync_directory(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    with contextlib.suppress(PermissionError):
        key_path.chmod(0o600)
    return _read_runtime_master_key(key_path)


def _read_runtime_master_key(path: Path) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("runtime snapshot master key is unreadable") from exc
    if len(decoded) != 32:
        raise ValueError("runtime snapshot master key is invalid")
    return decoded


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
