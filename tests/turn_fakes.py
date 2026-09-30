"""Construct complete turn state for tests that exercise individual effects."""
from pal.core.turns import TurnContinuation


def continuation(*, turn_id="test-turn", program=None, correlation_id=None, **state):
    def idle_program():
        yield from ()

    return TurnContinuation(
        turn_id=turn_id,
        program=program if program is not None else idle_program(),
        correlation_id=correlation_id if correlation_id is not None else turn_id,
        **state,
    )


def require_port(ports, key):
    from pal.shared.ports import PortKey

    return ports[key.name if isinstance(key, PortKey) else key]


class TurnIOFixture:
    def __init__(self, *, scope=None, binding=None, capabilities=None):
        self.scope = scope
        self.binding = binding if binding is not None else {}
        self.capabilities = dict(capabilities or {})

    def artifact_scope_for_turn(self, turn_id):
        return self.scope

    def capture_delivery_binding(self, turn_id):
        return dict(self.binding) if turn_id else {}

    def llm_capabilities_for_turn(self, turn_id):
        return dict(self.capabilities)

    async def send_attachment_for_turn(self, turn_id, attachment):
        raise AssertionError("test must provide its attachment delivery behavior")
