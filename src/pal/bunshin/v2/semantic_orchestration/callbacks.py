from __future__ import annotations
from typing import Any, Awaitable, Callable, Mapping
from pal.bunshin.v2.process_lifecycle import WorkerProcessOwner
from pal.shared import BunshinInvocationPack


HumanReviewPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]


WorkerEventPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]


WorkflowEventPublisher = Callable[[Mapping[str, Any]], None]


BrokerRunRegistrar = Callable[[str, str, BunshinInvocationPack, WorkerProcessOwner], None]


BrokerRunUnregistrar = Callable[[str, bool], None]


SkillInjector = Callable[[str], Mapping[str, str]]
