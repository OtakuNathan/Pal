"""Qualified names and executable ownership rules shared by publishers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class RegistrationKey:
    domain: str
    name: str

    def __post_init__(self) -> None:
        for part in (self.domain, self.name):
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part):
                raise ValueError(f"Invalid registration identity: {self.domain}:{self.name}")

    @property
    def qualified(self) -> str:
        return f"{self.domain}:{self.name}"

    @classmethod
    def parse(cls, value: str) -> RegistrationKey:
        domain, separator, name = value.partition(":")
        if not separator:
            raise ValueError(f"Registration identity must be domain:name: {value}")
        return cls(domain, name)


class OwnerRelation(StrEnum):
    VACANT = "vacant"
    SELF = "self"
    OTHER = "other"


class OwnershipEvent(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"


OWNERSHIP_TRANSITIONS = {
    (OwnerRelation.VACANT, OwnershipEvent.CLAIM): OwnerRelation.SELF,
    (OwnerRelation.VACANT, OwnershipEvent.RELEASE): OwnerRelation.VACANT,
    (OwnerRelation.SELF, OwnershipEvent.RELEASE): OwnerRelation.VACANT,
    (OwnerRelation.OTHER, OwnershipEvent.RELEASE): OwnerRelation.OTHER,
}


def owner_relation(current: object | None, expected: object) -> OwnerRelation:
    return (OwnerRelation.VACANT if current is None else
            OwnerRelation.SELF if current is expected else OwnerRelation.OTHER)


def reduce_ownership(relation: OwnerRelation, event: OwnershipEvent) -> OwnerRelation:
    try:
        return OWNERSHIP_TRANSITIONS[relation, event]
    except KeyError:
        raise ValueError(f"Registration name is already owned ({relation})") from None
