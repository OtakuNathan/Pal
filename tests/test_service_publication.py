"""Publication updates cached references without executing consumer callbacks."""
from typing import Protocol
import pytest
from pal.shared.ports import PortKey, PortRegistry, PortContractError

class ValuePort(Protocol):
    def read(self) -> int: ...

VALUE = PortKey[ValuePort]("sample:value", ValuePort)
OTHER = PortKey[ValuePort]("sample:other", ValuePort)

class Value:
    def __init__(self, value): self.value = value
    def read(self): return self.value

def test_observed_slot_updates_without_consumer_lookup(monkeypatch):
    ports = PortRegistry()
    ref = ports.observe(VALUE)
    assert ref.optional is None
    first, second = Value(1), Value(2)
    ports.publish({VALUE.name: first})
    monkeypatch.setattr(ports, 'require', lambda _: pytest.fail('consumer looked up a service'))
    assert ref.current is first
    retained = ref.current
    ports.publish({VALUE.name: second})
    assert ref.current is second
    assert retained.read() == 1
    ports.withdraw({VALUE.name: first})
    assert ref.current is second
    ports.withdraw({VALUE.name: second})
    with pytest.raises(KeyError): _ = ref.current

def test_invalid_batch_does_not_publish_partial_candidate():
    ports = PortRegistry()
    first, other = ports.observe(VALUE), ports.observe(OTHER)
    old = Value(1)
    ports.publish({VALUE.name: old})
    with pytest.raises(PortContractError):
        ports.publish({VALUE.name: Value(2), OTHER.name: object()})
    assert first.current is old
    assert other.optional is None
