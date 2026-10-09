"""File rollback must consult retained runtime ownership, not attached status."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pal.packages.integration import RuntimeActivation
from pal.packages.process import PackageError


@pytest.mark.parametrize("early", [False, True])
def test_package_switch_must_clean_failed_unattached_plugin(early):
    host = SimpleNamespace(
        _record=lambda name: SimpleNamespace(attached=False, enabled=True),
        generations={} if early else {"demo": object()},
        _pending_rollbacks={"demo": object()} if early else {},
        detach=Mock(return_value={"status": "error", "cleanup_pending": True}),
    )
    with pytest.raises(PackageError, match="could not be detached"):
        RuntimeActivation(host).before("plugin", "demo")
    host.detach.assert_called_once_with("demo")


@pytest.mark.parametrize("cleanup_blocked", [False, True])
def test_cancelled_activation_restores_files_only_after_candidate_cleanup(tmp_path, cleanup_blocked):
    from pal.packages.service import PackageService
    source, target = tmp_path / "source", tmp_path / "plugins/community/demo"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "version").write_text("new")
    (target / "version").write_text("old")
    calls = []
    def before(*args):
        calls.append("before")
        if len(calls) > 1 and cleanup_blocked:
            raise RuntimeError("cleanup pending")
        return {}
    def after(*args):
        raise asyncio.CancelledError("activation cancelled")
    activation = SimpleNamespace(gate=nullcontext, before=before, after=after,
                                 restore=Mock())
    service = PackageService(tmp_path, activation=activation)
    with pytest.raises(PackageError if cleanup_blocked else asyncio.CancelledError):
        service._publish(source, target, {"kind": "plugin", "id": "demo"})
    assert (target / "version").read_text() == ("new" if cleanup_blocked else "old")
    assert len(calls) == 2
    assert activation.restore.call_count == (0 if cleanup_blocked else 1)


def test_provider_build_failure_without_discovery_still_blocks_file_switch():
    manager = SimpleNamespace(discovered_runtime_providers={}, runtime_provider_handles={"demo": object()},
        _provider_hub_ids=lambda name: [], runtime=SimpleNamespace(get_endpoint=lambda name: None),
        _stop_provider_transports=Mock(return_value=([], [])),
        _unload_runtime_provider=Mock(return_value=["cleanup pending"]))
    host = SimpleNamespace(context=SimpleNamespace(require_port=lambda key: manager))
    with pytest.raises(PackageError, match="could not be unloaded"):
        RuntimeActivation(host).before("provider", "demo")
    manager._unload_runtime_provider.assert_called_once_with("demo")
