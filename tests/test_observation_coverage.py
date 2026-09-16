"""Observation messages and their delivery proof share one L1 replacement."""
from unittest.mock import patch

import pytest

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService
from pal.memory.turn_ir import L1TurnProtocolError


def test_observation_commit_failure_and_retry_are_atomic():
    memory = MemoryService()
    before = memory.begin_l1_turn('t', user_text='work')
    message = LLMMessageIR(role=MessageRole.USER, message_id='observation',
                           parts=(TextPartIR('state'),))
    coverage = {'events': {'observation': {'message_id': 'observation'}}}
    with patch.object(memory.l1_store, 'replace', side_effect=OSError('storage unavailable')):
        with pytest.raises(OSError):
            memory.append_l1_user_contexts('t', (message,), coverage_namespace='test',
                coverage=coverage, expected_revision=before.revision)
    assert memory.active_l1_turn('t') is before
    memory.append_l1_user_contexts('t', (message,), coverage_namespace='test',
        coverage=coverage, expected_revision=before.revision)
    committed = memory.active_l1_turn('t')
    assert committed.metadata['observation_coverage']['test']['events']['observation']
    assert sum(m.message_id == 'observation' for m in committed.messages) == 1
    memory.append_l1_user_contexts('t', (message,), coverage_namespace='test',
        coverage=coverage, expected_revision=committed.revision)
    assert memory.active_l1_turn('t') is committed
    with pytest.raises(L1TurnProtocolError):
        memory.append_l1_user_contexts('t', (message,), coverage_namespace='test',
            coverage=coverage, expected_revision=before.revision)


def test_observations_cannot_split_a_tool_batch():
    from pal.shared.tool_protocol import new_tool_call
    memory = MemoryService()
    memory.begin_l1_turn('t', user_text='work')
    memory.upsert_l1_assistant('t', LLMMessageIR(role=MessageRole.ASSISTANT,
        parts=(new_tool_call(name='read_file', args={'path': 'a'}),)))
    before = memory.active_l1_turn('t')
    with pytest.raises(L1TurnProtocolError):
        memory.append_l1_user_contexts('t', (), coverage_namespace='test', coverage={})
    assert memory.active_l1_turn('t') is before
