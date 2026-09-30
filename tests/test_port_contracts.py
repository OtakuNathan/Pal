from typing import Protocol

import pytest

from pal.shared.ports import PortContractError, PortKey, PortRef, PortRegistry


class EchoPort(Protocol):
    async def echo(self, text: str, *, suffix: str = "") -> str: ...


ECHO = PortKey[EchoPort]("test:echo", EchoPort)


class Echo:
    async def echo(self, text: str, *, suffix: str = "") -> str:
        return text + suffix


def test_optional_registration_replacement_and_retirement():
    registry = PortRegistry()
    ref = PortRef(registry, ECHO)
    assert registry.optional(ECHO) is None
    first, second = Echo(), Echo()
    registry.register(ECHO, first)
    assert ref.current is first
    registry.register(ECHO, second)
    assert ref.current is second
    del registry[ECHO.name]
    with pytest.raises(KeyError, match="test:echo"):
        _ = ref.current
    assert registry.optional(ECHO) is None


@pytest.mark.parametrize("candidate", [
    object(),
    type("SyncEcho", (), {"echo": lambda self, text, suffix="": text})(),
])
def test_rejects_missing_and_wrong_execution_kind_without_replacing(candidate):
    registry = PortRegistry()
    good = Echo()
    registry.register(ECHO, good)
    with pytest.raises(PortContractError, match="test:echo"):
        registry[ECHO.name] = candidate
    assert registry.require(ECHO) is good


def test_rejects_incompatible_keyword_and_additional_required_argument():
    class MissingKeyword:
        async def echo(self, text):
            raise AssertionError("validation must not execute the method")

    class ExtraRequired:
        async def echo(self, text, extra, *, suffix=""):
            raise AssertionError("validation must not execute the method")

    for value in (MissingKeyword(), ExtraRequired()):
        with pytest.raises(PortContractError, match="test:echo.echo: incompatible signature"):
            ECHO.validate(value)


def test_optional_parameter_cannot_become_required():
    class RequiredSuffix:
        async def echo(self, text, *, suffix):
            raise AssertionError("validation must not execute the method")

    with pytest.raises(PortContractError, match="suffix"):
        ECHO.validate(RequiredSuffix())


def test_conflicting_key_does_not_reinterpret_published_service():
    class OtherPort(Protocol):
        def reset(self) -> None: ...

    registry = PortRegistry()
    registry.register(ECHO, Echo())
    with pytest.raises(PortContractError, match="conflicting contract"):
        registry.require(PortKey[OtherPort](ECHO.name, OtherPort))


def test_invalid_module_does_not_publish_any_ports():
    from pal.core.main_context import MainContext
    from pal.core.module_registry import ModuleHandle

    context = MainContext()
    handle = ModuleHandle("test", "detachable", ports={"other": object(), "echo": object()},
                          port_contracts=(ECHO,))
    with pytest.raises(PortContractError, match="test:echo"):
        context.register_module(handle)
    assert context.module_registry.get("test") is None
    assert dict(context.port_registry) == {}


def test_retired_handle_cannot_remove_replacement():
    from pal.core.main_context import MainContext
    from pal.core.module_registry import ModuleHandle

    context = MainContext()
    old = ModuleHandle("test", "detachable", ports={"echo": Echo()}, port_contracts=(ECHO,))
    new = ModuleHandle("test", "detachable", ports={"echo": Echo()}, port_contracts=(ECHO,))
    context.register_module(old)
    assert context.unregister_module(old)
    context.register_module(new)
    assert not context.unregister_module(old)
    assert context.require_port(ECHO) is new.ports["echo"]
