"""Typed service identities and validation at the module publication boundary.

Reflection belongs here, where a generation is admitted, rather than in callers
guessing which methods an already published service happens to implement.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Generic, TypeVar, cast


T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
_MISSING = object()


class PortContractError(TypeError):
    """A candidate service cannot implement its declared communication port."""


@dataclass(frozen=True)
class PortKey(Generic[T_co]):
    name: str
    contract: object

    def __post_init__(self) -> None:
        if not self.name or ":" not in self.name:
            raise ValueError("port names must be qualified as module:port")
        if not isinstance(self.contract, type):
            raise TypeError("port contract must be a protocol class")

    def validate(self, value: object) -> None:
        contract = self.contract
        assert isinstance(contract, type)
        for base in reversed(contract.__mro__):
            for name in base.__annotations__ if "__annotations__" in vars(base) else ():
                if not name.startswith("_") and getattr(value, name, _MISSING) is _MISSING:
                    raise PortContractError(f"{self.name}: missing field {name}")
        for name, member in inspect.getmembers(contract):
            if isinstance(member, property) and inspect.getattr_static(value, name, _MISSING) is _MISSING:
                raise PortContractError(f"{self.name}: missing property {name}")
        for name, expected in inspect.getmembers(contract, inspect.isfunction):
            if name.startswith("_"):
                continue
            actual = getattr(value, name, None)
            if not callable(actual):
                raise PortContractError(f"{self.name}: missing method {name}")
            if inspect.iscoroutinefunction(expected) != inspect.iscoroutinefunction(actual):
                raise PortContractError(f"{self.name}.{name}: synchronous/asynchronous contract mismatch")
            try:
                expected_signature = inspect.signature(expected)
                signature = inspect.signature(actual)
                parameters = tuple(expected_signature.parameters.values())[1:]
                for include_optional in (False, True):
                    for use_keywords in (False, True):
                        args: list[object] = []
                        kwargs: dict[str, object] = {}
                        for parameter in parameters:
                            if not include_optional and parameter.default is not inspect.Parameter.empty:
                                continue
                            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                                args.append(_MISSING)
                            elif parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
                                if use_keywords:
                                    kwargs[parameter.name] = _MISSING
                                else:
                                    args.append(_MISSING)
                            elif parameter.kind == inspect.Parameter.KEYWORD_ONLY:
                                kwargs[parameter.name] = _MISSING
                            elif parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                                if not any(p.kind == parameter.kind for p in signature.parameters.values()):
                                    raise TypeError("variadic positional arguments required")
                            elif parameter.kind == inspect.Parameter.VAR_KEYWORD:
                                if not any(p.kind == parameter.kind for p in signature.parameters.values()):
                                    raise TypeError("variadic keyword arguments required")
                        signature.bind(*args, **kwargs)
            except (TypeError, ValueError) as error:
                raise PortContractError(f"{self.name}.{name}: incompatible signature: {error}") from error


class PortRegistry(MutableMapping[str, object]):
    """One identity-checked registry, shared by typed callers and manifest lookup."""

    def __init__(self, initial: Mapping[str, object] | None = None) -> None:
        self._values: dict[str, object] = dict(initial or {})
        self._contracts: dict[str, PortKey[object]] = {}

    def declare(self, key: PortKey[object]) -> None:
        previous = self._contracts.get(key.name)
        if previous is not None and previous.contract is not key.contract:
            raise PortContractError(f"{key.name}: conflicting contract declaration")
        if key.name in self._values:
            key.validate(self._values[key.name])
        self._contracts[key.name] = key

    def validate(self, name: str, value: object) -> None:
        key = self._contracts.get(name)
        if key is not None:
            key.validate(value)

    def register(self, key: PortKey[T], value: T) -> None:
        self.declare(key)
        self[key.name] = value

    def require(self, key: PortKey[T]) -> T:
        if key.name not in self._contracts:
            self.declare(key)
        elif self._contracts[key.name].contract is not key.contract:
            raise PortContractError(f"{key.name}: conflicting contract lookup")
        return cast(T, self._values[key.name])

    def optional(self, key: PortKey[T]) -> T | None:
        if key.name not in self._values:
            self.declare(key)
            return None
        return self.require(key)

    def __getitem__(self, name: str) -> object:
        return self._values[name]

    def __setitem__(self, name: str, value: object) -> None:
        self.validate(name, value)
        self._values[name] = value

    def __delitem__(self, name: str) -> None:
        del self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True)
class PortRef(Generic[T_co]):
    """Resolve the published generation on use; never retain a retired service."""

    registry: PortRegistry
    key: PortKey[T_co]

    @property
    def current(self) -> T_co:
        return self.registry.require(self.key)
