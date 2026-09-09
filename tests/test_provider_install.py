from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pal.provider_install import (
    ProviderInstallError,
    inspect_provider_wheel,
    install_provider_wheel,
)


def _write_provider_wheel(
    path: Path,
    *,
    provider_id: str = "example",
    provider_version: str = "1.2.3",
    metadata_version: str | None = None,
    extra_members: dict[str, str] | None = None,
) -> None:
    package = f"pal_provider_{provider_id.replace('-', '_')}"
    distribution = f"pal-channel-provider-{provider_id}"
    dist_info = f"{distribution.replace('-', '_')}-{metadata_version or provider_version}.dist-info"
    manifest = (
        f'provider_id = "{provider_id}"\n'
        'entrypoint = "runtime.py"\n'
        f'version = "{provider_version}"\n'
        "enabled = true\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/provider.toml", manifest)
        archive.writestr(f"{package}/runtime.py", "def build_channel_provider(context):\n    return context\n")
        archive.writestr(f"{package}/helper.py", "VALUE = 1\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\n"
            f"Name: {distribution}\n"
            f"Version: {metadata_version or provider_version}\n\n",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        for name, content in dict(extra_members or {}).items():
            archive.writestr(name, content)


class ProviderInstallTests(unittest.TestCase):
    def test_inspect_and_install_provider_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wheel_path = root / "example.whl"
            runtime_root = root / "runtime"
            _write_provider_wheel(wheel_path)

            wheel = inspect_provider_wheel(wheel_path)
            result = install_provider_wheel(wheel_path, runtime_root=runtime_root)

            self.assertEqual(wheel.provider_id, "example")
            self.assertEqual(wheel.provider_version, "1.2.3")
            self.assertEqual(
                result.target_dir,
                runtime_root.resolve() / "channel" / "providers" / "example",
            )
            self.assertEqual((result.target_dir / "helper.py").read_text(), "VALUE = 1\n")
            receipt = json.loads((result.target_dir / ".pal-provider-install.json").read_text())
            self.assertEqual(receipt["provider_id"], "example")
            self.assertEqual(receipt["provider_version"], "1.2.3")
            self.assertEqual(receipt["wheel_sha256"], result.wheel_sha256)

    def test_update_archives_previous_provider_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "runtime"
            first = root / "first.whl"
            second = root / "second.whl"
            _write_provider_wheel(first, provider_version="1.0.0")
            _write_provider_wheel(second, provider_version="2.0.0")
            first_result = install_provider_wheel(first, runtime_root=runtime_root)
            (first_result.target_dir / "local-note.txt").write_text("preserve me")

            second_result = install_provider_wheel(second, runtime_root=runtime_root)

            self.assertEqual(second_result.provider_version, "2.0.0")
            self.assertIsNotNone(second_result.archived_previous_dir)
            assert second_result.archived_previous_dir is not None
            self.assertEqual(
                (second_result.archived_previous_dir / "local-note.txt").read_text(),
                "preserve me",
            )
            self.assertEqual(
                (second_result.target_dir / "provider.toml").read_text().split('version = "')[1].split('"')[0],
                "2.0.0",
            )

    def test_same_version_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wheel_path = root / "example.whl"
            runtime_root = root / "runtime"
            _write_provider_wheel(wheel_path)
            install_provider_wheel(wheel_path, runtime_root=runtime_root)

            with self.assertRaisesRegex(ProviderInstallError, "already installed"):
                install_provider_wheel(wheel_path, runtime_root=runtime_root)
            result = install_provider_wheel(wheel_path, runtime_root=runtime_root, force=True)
            self.assertIsNotNone(result.archived_previous_dir)

    def test_rejects_metadata_and_manifest_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wheel_path = Path(temp_dir) / "bad.whl"
            _write_provider_wheel(
                wheel_path,
                provider_version="1.0.0",
                metadata_version="2.0.0",
            )

            with self.assertRaisesRegex(ProviderInstallError, "does not match"):
                inspect_provider_wheel(wheel_path)

    def test_rejects_unsafe_wheel_member(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wheel_path = Path(temp_dir) / "bad.whl"
            _write_provider_wheel(
                wheel_path,
                extra_members={"../escape.py": "bad"},
            )

            with self.assertRaisesRegex(ProviderInstallError, "unsafe wheel member"):
                inspect_provider_wheel(wheel_path)


if __name__ == "__main__":
    unittest.main()


def test_common_installer_preserves_legacy_provider_receipt(tmp_path):
    from pal.packages.service import PackageService
    from pal.provider_install import _RECEIPT_FILENAME
    wheel_path = tmp_path / "example.whl"
    _write_provider_wheel(wheel_path)
    runtime = tmp_path / "runtime"
    result = PackageService(runtime).install(wheel_path)
    receipt = json.loads((runtime / "channel/providers/example" / _RECEIPT_FILENAME).read_text())
    assert receipt["provider_id"] == "example"
    assert receipt["wheel_sha256"] == result["sha256"]
    assert receipt["distribution_version"] == "1.2.3"


def test_live_common_install_rejects_real_provider_factory_failure(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import pytest
    from pal.channel.provider_manager import ChannelEndpointProviderManager
    from pal.channel.repository import ChannelEndpointRepository
    from pal.channel.runtime import ChannelRuntime
    from pal.packages.integration import RuntimeActivation
    from pal.packages.process import PackageError
    from pal.packages.service import PackageService
    runtime = tmp_path / "runtime"
    manager = ChannelEndpointProviderManager(ChannelRuntime(), ChannelEndpointRepository(), runtime)
    host = SimpleNamespace(runtime_root=runtime, context=SimpleNamespace(
        require_port=lambda name: manager,
        execution_runtime=SimpleNamespace(lifecycle_gate=SimpleNamespace(write=nullcontext)),
    ))
    wheel_path = tmp_path / "example.whl"
    # The fixture returns its build context, which is deliberately not a provider.
    _write_provider_wheel(wheel_path)
    service = PackageService(runtime, activation=RuntimeActivation(host))
    with pytest.raises(PackageError, match="Provider discovery failed"):
        service.install(wheel_path)
    assert service.status(name="example", kind="provider")["items"][0]["status"] == "failed"
    assert "example" not in manager.discovered_runtime_providers
    assert not (runtime / "channel/providers/example").exists()
