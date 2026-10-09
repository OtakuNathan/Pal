from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_main_test_extra_includes_its_test_runner() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = dict(tomllib.load(stream).get("project") or {})

    test_dependencies = list(
        dict(project.get("optional-dependencies") or {}).get("test") or []
    )

    assert any(requirement.startswith("pytest>=") for requirement in test_dependencies)


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


@pytest.mark.parametrize("overlay", ["none", "explicit", "old_bundle", "missing"])
def test_installer_requires_an_overlay_only_when_explicitly_requested(tmp_path, monkeypatch, overlay):
    installer = tmp_path / "install-pal.sh"
    installer.write_text((ROOT / "scripts/install_package.sh").read_text())
    (tmp_path / "pal_v2-test.whl").write_bytes(b"fixture")
    # Stop at the Python prerequisite check, before any installation or setup.
    # Force the same branch on Linux and macOS without touching host services.
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
    uname.chmod(0o755)
    python = commands / "python-probe"
    python.write_text("#!/bin/sh\nprintf 'reached-python-check\\n'\nexit 41\n")
    python.chmod(0o755)
    monkeypatch.setenv("PAL_PYTHON", str(python))
    monkeypatch.setenv("PATH", str(commands) + ":" + os.environ["PATH"])
    args = ["bash", str(installer), "--no-service-restart", "--no-providers",
            "--runtime-root", str(tmp_path / "runtime"),
            "--install-root", str(tmp_path / "install"), "--bin-dir", str(tmp_path / "bin")]
    if overlay == "old_bundle":
        # A stale archive beside the installer must not be implicitly installed.
        (tmp_path / "pal_v2-runtime-root-overlay.tar.gz").mkdir()
    elif overlay in {"explicit", "missing"}:
        path = tmp_path / "custom.tar.gz"
        if overlay == "explicit":
            path.write_bytes(b"fixture")
        args.extend(["--overlay", str(path)])
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if overlay == "missing":
        assert result.returncode == 1
        assert "runtime-root overlay does not exist" in result.stderr
        assert "reached-python-check" not in result.stdout
    else:
        assert result.returncode == 41, result.stderr
        assert "reached-python-check" in result.stdout
    assert not (tmp_path / "install").exists()
