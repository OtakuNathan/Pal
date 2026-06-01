from __future__ import annotations

import base64
import getpass
import hashlib
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SecretRef:
    service: str
    account: str = "api-key"


class SecretStorePort(Protocol):
    def get_secret(self, ref: SecretRef) -> str | None:
        ...

    def set_secret(self, ref: SecretRef, secret: str) -> None:
        ...

    def delete_secret(self, ref: SecretRef) -> None:
        ...


@dataclass
class KeyringSecretStore:
    """Default system secret backend for PalV2 LLM credentials."""

    keyring_module: object | None = None
    macos_security_timeout_seconds: int = 60

    def _get_from_macos_security(self, ref: SecretRef) -> str | None:
        if sys.platform != "darwin":
            return None
        try:
            result = subprocess.run(
                (
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    ref.service,
                    "-a",
                    ref.account,
                    "-w",
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.macos_security_timeout_seconds,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        secret = result.stdout.strip()
        return secret or None

    def _load_keyring(self):
        if self.keyring_module is not None:
            return self.keyring_module
        try:
            import keyring  # type: ignore
        except Exception:
            return None
        self.keyring_module = keyring
        return keyring

    def get_secret(self, ref: SecretRef) -> str | None:
        secret = self._get_from_macos_security(ref)
        if secret:
            return secret
        keyring = self._load_keyring()
        if keyring is None:
            return None
        try:
            secret = keyring.get_password(ref.service, ref.account)
        except Exception:
            return None
        return str(secret) if secret else None

    def set_secret(self, ref: SecretRef, secret: str) -> None:
        keyring = self._load_keyring()
        if keyring is None:
            raise RuntimeError("system keyring is not available")
        keyring.set_password(ref.service, ref.account, secret)

    def delete_secret(self, ref: SecretRef) -> None:
        keyring = self._load_keyring()
        if keyring is None:
            raise RuntimeError("system keyring is not available")
        keyring.delete_password(ref.service, ref.account)


@dataclass
class InMemorySecretStore:
    secrets: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_secret(self, ref: SecretRef) -> str | None:
        return self.secrets.get((ref.service, ref.account))

    def set_secret(self, ref: SecretRef, secret: str) -> None:
        self.secrets[(ref.service, ref.account)] = secret

    def delete_secret(self, ref: SecretRef) -> None:
        self.secrets.pop((ref.service, ref.account), None)


def _derive_fernet_key(
    *,
    runtime_root: str,
    salt_extra: str = "pal_v2_secrets",
    include_hostname: bool = False,
) -> bytes:
    """Derive a Fernet key from machine identity + runtime root path.

    Deterministic per (username, runtime_root) so the same deployment always
    decrypts its own secrets without a password prompt. Hostname is deliberately
    excluded from the primary key because macOS hostnames may change after
    reboot or network changes.
    """

    parts = []
    if include_hostname:
        parts.append(socket.gethostname())
    parts.extend([getpass.getuser(), runtime_root, salt_extra])
    material = ":".join(parts)
    digest = hashlib.pbkdf2_hmac("sha256", material.encode(), b"pal_v2_secret_salt", 200_000)
    return base64.urlsafe_b64encode(digest)


class EncryptedFileSecretStore:
    """Encrypted JSON file secret store — drop-in replacement for KeyringSecretStore.

    File format (``{runtime_root}/secrets.json``):
    ``{ "service:account": { "service": "...", "account": "...", "encrypted": "..." } }``

    On first load, if a plaintext ``value`` field is found alongside no
    ``encrypted`` field, the store transparently migrates (encrypts) the entry.
    """

    def __init__(self, secrets_path: str | object) -> None:
        from pathlib import Path

        self._path = Path(str(secrets_path))
        self._fernet: object | None = None
        self._legacy_fernets: list[object] | None = None
        self._cache: dict[tuple[str, str], str] = {}
        self._loaded_mtime_ns: int | None = None
        self._load()

    # -- public API (SecretStorePort) ----------------------------------------

    def get_secret(self, ref: SecretRef) -> str | None:
        self._refresh_if_changed()
        return self._cache.get((ref.service, ref.account))

    def refresh(self) -> None:
        self._cache.clear()
        self._load()

    def set_secret(self, ref: SecretRef, secret: str) -> None:
        self._cache[(ref.service, ref.account)] = secret
        self._flush()

    def delete_secret(self, ref: SecretRef) -> None:
        self._cache.pop((ref.service, ref.account), None)
        self._flush()

    # -- internals -----------------------------------------------------------

    def _ensure_fernet(self):
        if self._fernet is not None and self._legacy_fernets is not None:
            return
        from cryptography.fernet import Fernet

        primary_key = _derive_fernet_key(runtime_root=str(self._path.parent))
        self._fernet = Fernet(primary_key)
        legacy_keys = [
            _derive_fernet_key(runtime_root=str(self._path.parent), include_hostname=True),
        ]
        self._legacy_fernets = [
            Fernet(key)
            for key in dict.fromkeys(legacy_keys)
            if key != primary_key
        ]

    def _load(self) -> None:
        import json

        if not self._path.exists():
            self._loaded_mtime_ns = None
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        dirty = False
        for _key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            service = str(entry.get("service") or "")
            account = str(entry.get("account") or "api-key")
            if not service:
                continue
            encrypted = entry.get("encrypted")
            if isinstance(encrypted, str) and encrypted:
                decrypted = self._decrypt_encrypted_value(encrypted)
                if decrypted is not None:
                    value, needs_rewrite = decrypted
                    self._cache[(service, account)] = value
                    dirty = dirty or needs_rewrite
                continue
            # Legacy plaintext migration
            plaintext = entry.get("value")
            if isinstance(plaintext, str) and plaintext:
                self._cache[(service, account)] = plaintext
                dirty = True
        if dirty:
            self._flush()
        else:
            self._mark_loaded()

    def _flush(self) -> None:
        import json

        self._ensure_fernet()
        data: dict[str, dict[str, str]] = {}
        for (service, account), secret in self._cache.items():
            encrypted = self._fernet.encrypt(secret.encode()).decode()  # type: ignore[union-attr]
            data[f"{service}:{account}"] = {
                "service": service,
                "account": account,
                "encrypted": encrypted,
            }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self._mark_loaded()

    def _mark_loaded(self) -> None:
        try:
            self._loaded_mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            self._loaded_mtime_ns = None

    def _refresh_if_changed(self) -> None:
        try:
            current_mtime_ns = self._path.stat().st_mtime_ns
        except OSError:
            current_mtime_ns = None
        if current_mtime_ns == self._loaded_mtime_ns:
            return
        self._cache.clear()
        self._load()

    def _decrypt_encrypted_value(self, encrypted: str) -> tuple[str, bool] | None:
        self._ensure_fernet()
        payload = encrypted.encode()
        try:
            return self._fernet.decrypt(payload).decode(), False  # type: ignore[union-attr]
        except Exception:
            pass
        for fernet in list(self._legacy_fernets or []):
            try:
                return fernet.decrypt(payload).decode(), True  # type: ignore[attr-defined]
            except Exception:
                continue
        return None
