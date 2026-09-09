"""Conversation-scoped unpacked Chromium extension configuration."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


def inspect_extension(directory: str) -> dict:
    if not str(directory).strip():
        raise ValueError("A local extension directory is required")
    path = Path(directory).expanduser().resolve(strict=True)
    if not path.is_dir() or any(c in str(path) for c in (',', '\x00', '\n', '\r')):
        raise ValueError('Extension must be a local directory without commas or control characters')
    manifest_path = path / 'manifest.json'
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError('Extension manifest exceeds 1 MiB')
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get('manifest_version') != 3:
        raise ValueError('Only unpacked Manifest V3 extensions are supported')
    if not isinstance(manifest.get('name'), str) or not isinstance(manifest.get('version'), str):
        raise ValueError('Extension manifest requires name and version strings')
    # Chromium derives unpacked IDs from the manifest public key, or absolute path.
    identity = base64.b64decode(manifest['key'], validate=True) if manifest.get('key') else str(path).encode()
    digest = hashlib.sha256(identity).hexdigest()[:32]
    extension_id = ''.join(chr(ord('a') + int(c, 16)) for c in digest)
    return {
        'id': extension_id, 'path': str(path), 'name': manifest['name'], 'version': manifest['version'],
        'permissions': manifest.get('permissions', []), 'host_permissions': manifest.get('host_permissions', []),
        'manifest_url': f'chrome-extension://{extension_id}/manifest.json',
    }


def read_extensions(profile_dir: Path) -> list[dict]:
    path = profile_dir / 'extensions.json'
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list) or len(data) > 16:
        raise ValueError('Invalid browser extension configuration')
    if any(not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ('id', 'path', 'name', 'version')) for item in data):
        raise ValueError('Invalid browser extension entry')
    return data


def validate_extension_url(url: str, extensions: list[dict]) -> str:
    parsed = urlparse(url)
    if parsed.scheme != 'chrome-extension' or parsed.netloc not in {item['id'] for item in extensions}:
        raise ValueError('Only pages belonging to a mounted extension may use chrome-extension URLs')
    if len(url) > 8192:
        raise ValueError('browser URL exceeds 8192 characters')
    return url
