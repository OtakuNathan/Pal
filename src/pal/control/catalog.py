"""Coalesce command catalog changes into ordinary Core delivery work."""
from __future__ import annotations

from pal.control.contracts import ControlAction
from pal.control.service import ControlPlane
from pal.foundation import EventEnvelope
from pal.shared import EventKind, SourceKind


class ControlCatalogSource:
    source_id = "control.catalog"

    def __init__(self, control: ControlPlane):
        self.control = control
        self.published_revision = -1

    def prepare(self, context) -> bool:
        return self.published_revision != self.control.catalog_revision

    def drain(self, context) -> list[EventEnvelope]:
        self.published_revision = self.control.catalog_revision
        return [EventEnvelope(event_kind=EventKind.CONTROL_ACTION, source_kind=SourceKind.CONTROL,
                              payload=ControlAction(action_kind="publish_control_catalog", target_scope="control"))]
