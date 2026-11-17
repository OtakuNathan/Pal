"""Executable registration and ownership rules, also exported to TLA+."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RegistrationPhase(StrEnum):
    PREPARING = "preparing"
    REGISTERED = "registered"
    PUBLISHING = "publishing"
    ACTIVE = "active"
    WITHDRAWING = "withdrawing"
    CLEANING = "cleaning"
    CLOSED = "closed"


class RegistrationEvent(StrEnum):
    CLAIM = "claim"
    PUBLISH = "publish"
    COMMIT = "commit"
    RETIRE = "retire"
    ABANDON = "abandon"
    WITHDRAW = "withdraw"
    RETRY = "retry"
    CLOSE = "close"


REGISTRATION_TRANSITIONS = {
    (RegistrationPhase.PREPARING, True, RegistrationEvent.CLAIM): RegistrationPhase.REGISTERED,
    (RegistrationPhase.REGISTERED, True, RegistrationEvent.PUBLISH): RegistrationPhase.PUBLISHING,
    (RegistrationPhase.PUBLISHING, True, RegistrationEvent.COMMIT): RegistrationPhase.ACTIVE,
    **{(phase, True, RegistrationEvent.RETIRE): RegistrationPhase.WITHDRAWING
       for phase in (RegistrationPhase.PREPARING, RegistrationPhase.REGISTERED,
                     RegistrationPhase.PUBLISHING, RegistrationPhase.ACTIVE)},
    **{(phase, False, RegistrationEvent.ABANDON): RegistrationPhase.CLEANING
       for phase in (RegistrationPhase.PREPARING, RegistrationPhase.REGISTERED,
                     RegistrationPhase.PUBLISHING, RegistrationPhase.ACTIVE, RegistrationPhase.WITHDRAWING)},
    (RegistrationPhase.WITHDRAWING, False, RegistrationEvent.WITHDRAW): RegistrationPhase.CLEANING,
    (RegistrationPhase.WITHDRAWING, True, RegistrationEvent.RETRY): RegistrationPhase.WITHDRAWING,
    (RegistrationPhase.CLEANING, False, RegistrationEvent.RETRY): RegistrationPhase.CLEANING,
    (RegistrationPhase.CLEANING, False, RegistrationEvent.CLOSE): RegistrationPhase.CLOSED,
}


def reduce_registration(phase: RegistrationPhase, event: RegistrationEvent, *, owns_registration: bool) -> RegistrationPhase:
    try:
        return REGISTRATION_TRANSITIONS[phase, owns_registration, event]
    except KeyError:
        raise ValueError(f"Illegal registration transition: {phase}, {event}, owns={owns_registration}") from None


@dataclass
class RegistrationState:
    phase: RegistrationPhase = RegistrationPhase.PREPARING

    def advance(self, event: RegistrationEvent, *, owns_registration: bool) -> None:
        self.phase = reduce_registration(self.phase, event, owns_registration=owns_registration)

    @property
    def publishing(self) -> bool:
        return self.phase in {RegistrationPhase.PUBLISHING, RegistrationPhase.ACTIVE}
