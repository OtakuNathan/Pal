"""Turn-scoped routing for optional, ephemeral execution UI."""
from __future__ import annotations

from pal.core.turn_events import TURN_START, TURN_END


class ToolActivityRouter:
    def __init__(self, runtime):
        self.runtime = runtime
        self.routes = {}

    def __call__(self, topic, event):
        turn_id = str(event.get("turn_id") or "")
        if not turn_id:
            return
        if topic == TURN_START:
            endpoint_id = str(event.get("endpoint_id") or "")
            endpoint = self.runtime.get_endpoint(endpoint_id)
            if not getattr(endpoint, "supports_tool_activity", False):
                return
            route = (endpoint_id, dict(event.get("reply_target") or {}))
            self.routes[turn_id] = route
            self._send(route, {"action": "begin", "turn_id": turn_id})
        elif topic == TURN_END:
            route = self.routes.pop(turn_id, None)
            if route is not None:
                self._send(route, {"action": "end", "turn_id": turn_id})

    def open_sink(self, turn_id):
        route = self.routes.get(turn_id)
        if route is None:
            return None

        def emit(payload):
            if self.routes.get(turn_id) is route:
                self._send(route, payload)
        return emit

    def _send(self, route, payload):
        endpoint = self.runtime.get_endpoint(route[0])
        if endpoint is None or not getattr(endpoint, "supports_tool_activity", False):
            return
        hub = self.runtime.get_endpoint_hub(route[0])
        if hub is None or hub.state != "attached":
            return
        # Skip detached/disabled providers and bound this optional queue. No
        # hub backlog is needed for an execution UI that expires with its turn.
        if not endpoint.enabled or not endpoint.attached:
            return
        for queued in list(endpoint.status_outbox):
            if queued.kind != "tool_activity" or queued.payload.get("turn_id") != payload["turn_id"]:
                continue
            if payload["action"] == "end" or (
                payload["action"] == "call" and queued.payload.get("call_id") == payload.get("call_id")
            ):
                endpoint.status_outbox.remove(queued)
        if len(endpoint.status_outbox) >= 100:
            return
        self.runtime.queue_endpoint_status(route[0], "tool_activity", payload=payload, reply_target=route[1])
