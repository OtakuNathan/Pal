from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_first_party_provider_projects_match_runtime_manifests() -> None:
    required_files = {
        "telegram": {
            "__init__.py",
            "provider.toml",
            "runtime.py",
            "endpoint.py",
            "interaction_store.py",
        },
        "websocket_bridge": {
            "__init__.py",
            "provider.toml",
            "runtime.py",
            "protocol.py",
            "sidecar.py",
            "sidecar_main.py",
        },
    }
    for provider_id, filenames in required_files.items():
        provider_root = ROOT / "providers" / provider_id
        with (provider_root / "pyproject.toml").open("rb") as stream:
            project = dict(tomllib.load(stream).get("project") or {})
        with (provider_root / "provider.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        assert manifest["provider_id"] == provider_id
        assert project["version"] == manifest["version"]
        assert project["name"].startswith("pal-channel-provider-")
        assert project["authors"] == [{"name": "Nathan Wu (OtakuNathan)"}]
        assert project["license"] == {"file": "LICENSE"}
        assert project["readme"] == "README.md"
        assert "License :: OSI Approved :: MIT License" in project["classifiers"]
        assert (provider_root / "LICENSE").is_file()
        assert filenames <= {path.name for path in provider_root.iterdir() if path.is_file()}
        assert tomllib.loads(
            (provider_root / "pyproject.toml").read_text(encoding="utf-8")
        )["tool"]["setuptools"]["include-package-data"] is False


def test_release_scripts_keep_providers_out_of_runtime_overlay() -> None:
    build_script = (ROOT / "scripts/build_package.sh").read_text(encoding="utf-8")
    install_script = (ROOT / "scripts/install_package.sh").read_text(encoding="utf-8")

    assert "build_provider_packages.sh" in build_script
    assert 'mkdir -p "$install_bundle_dir/providers"' in build_script
    assert '"$provider_dist_dir"/pal_channel_provider_*.whl' in build_script
    assert "websocket_overlay_dir" not in build_script
    assert "telegram_overlay_dir" not in build_script
    assert '"$pal_bin" provider install' in install_script
    assert '"$providers_dir"/pal_channel_provider_*.whl' in install_script


def test_installer_stops_active_service_before_replacing_virtualenv() -> None:
    installer = ROOT / "scripts/install_package.sh"
    source = installer.read_text(encoding="utf-8")

    assert source.index('systemctl --user stop "$service_name"') < source.index(
        '"$python_bin" -m venv --clear "$venv_dir"'
    )
    subprocess.run(["bash", "-n", str(installer)], check=True)
