import asyncio
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.service import DreamingService
from pal.memory.storage import MemoryStorage


@pytest.fixture
def storage(tmp_path):
    value = MemoryStorage(tmp_path)
    value.create_initial()
    return value


def test_migrate_once_and_use_database_after_restart(tmp_path, storage):
    config = tmp_path / 'config.toml'
    config.write_text('[memory.dreaming]\nenabled = true\ninput_tokens = 9000\n')
    imported = DreamingConfig.load(tmp_path, storage=storage)
    assert imported.enabled and imported.input_tokens == 9000
    service = DreamingService(storage=storage, provider=None, llm=None, config=imported)
    service.configure({'enabled': False, 'input_tokens': 8000})
    config.write_text('invalid TOML')
    restored = DreamingConfig.load(tmp_path, storage=storage)
    assert not restored.enabled and restored.input_tokens == 8000


@pytest.mark.parametrize('patch', [{'enabled': 'false'}, {'neighbors': True}, {'unknown': 1},
                                   {'timezone': 'not/a/zone'}, {'retention_days': 1},
                                   {'endpoint_id': 'missing'}, {'request_timeout_seconds': float('inf')}])
def test_invalid_patch_does_not_change_database_or_live_config(storage, patch):
    service = DreamingService(storage=storage, provider=None, llm=None)
    service.configure({})
    before = asdict(service.config)
    with pytest.raises((ValueError, TypeError)):
        service.configure(patch)
    assert asdict(service.config) == before
    assert asdict(DreamingConfig.load(storage.root.parent, storage=storage)) == before


def test_disable_pending_automatic_and_reenable_future_schedule(storage):
    service = DreamingService(storage=storage, provider=None, llm=None, config=DreamingConfig(enabled=True))
    run = service.create_run(slot='old-month')
    service.configure({'enabled': False})
    assert service.status(run)['status'] == 'failed'
    assert service.status()['next_due'] is None
    service.configure({'enabled': True})
    from datetime import datetime, timezone
    assert datetime.fromisoformat(service.status()['next_due']) > datetime.now(timezone.utc)
    manual = service.create_run()
    service.configure({'enabled': False})
    assert service.status(manual)['status'] == 'scheduled'


def test_running_batch_captures_config_before_task_execution(storage):
    async def check():
        service = DreamingService(storage=storage, provider=None, llm=None)
        service.run = AsyncMock(return_value={})
        old = service.config
        started = service.start()
        service.configure({'input_tokens': 4000, 'enabled': False})
        assert service.status()['active_configuration']['input_tokens'] == old.input_tokens
        await service.task
        service.run.assert_awaited_once_with(started['run_id'], config=old)
        assert service.config.input_tokens == 4000
    asyncio.run(check())


def test_readonly_migration_does_not_write_source(tmp_path, storage):
    (tmp_path / 'config.toml').write_text('[memory.dreaming]\nenabled = true\n')
    readonly = MemoryStorage(tmp_path, read_only=True)
    assert DreamingConfig.load(tmp_path, storage=readonly).enabled
    with storage.connection() as db:
        assert db.execute("SELECT value FROM memory_settings WHERE key='dreaming_config'").fetchone() is None


def test_sleep_control_preserves_raw_json_and_avoids_llm(tmp_path, storage):
    import json
    import shlex
    from pal.core import PalCore
    from pal.control import ControlCommandInvocation
    from pal.memory.dreaming.runtime import register_dreaming
    core = PalCore()
    commands = {}
    core.context.port_registry['control:control'] = SimpleNamespace(register_command=lambda spec: commands.update({spec.name: spec}))
    service = register_dreaming(core, SimpleNamespace(repository=SimpleNamespace(catalog=storage)), None, tmp_path)
    core.state.memory_maintenance = True
    raw = '/dreaming configure {"enabled": true, "input_tokens": 8000}'
    invocation = ControlCommandInvocation(command_name='dreaming', argv=tuple(shlex.split(raw)[1:]), raw_text=raw)
    action = commands['dreaming'].handler(invocation)
    handle = core.context.module_registry.get('memory.dreaming')
    result = handle.control_action_handlers['memory_dreaming'](action)
    assert json.loads(result['message'])['enabled'] is True
    assert service.config.input_tokens == 8000


def test_stale_scheduled_event_does_not_restart_after_disable(storage):
    from pal.memory.dreaming.runtime import DreamingEventHandler
    from pal.foundation import EventEnvelope
    from unittest.mock import Mock
    service = DreamingService(storage=storage, provider=None, llm=None, config=DreamingConfig(enabled=True))
    run = service.create_run(slot='queued-before-disable')
    service.configure({'enabled': False})
    service.configure({'enabled': True})
    service.start = Mock()
    DreamingEventHandler(service).handle(EventEnvelope(event_kind='memory.dreaming', source_kind='memory', payload={'run_id': run}), None)
    service.start.assert_not_called()


def test_disable_with_active_dry_run_retires_pending_automatic_run(storage):
    from unittest.mock import Mock
    service = DreamingService(storage=storage, provider=None, llm=None, config=DreamingConfig(enabled=True))
    run = service.create_run(slot='automatic-pending-during-audit')
    service.task = Mock(done=lambda: False)
    service.run_id = None
    service.configure({'enabled': False})
    assert service.status(run)['status'] == 'failed'
