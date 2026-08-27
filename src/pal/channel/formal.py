from __future__ import annotations

import hashlib
import json

from pal.channel.lifecycle import (
    EndpointHubAction,
    EndpointHubSnapshot,
    EndpointHubState,
    iter_endpoint_hub_reducer_edges,
)


def _snapshot_cells(snapshot: EndpointHubSnapshot) -> tuple[str | bool, ...]:
    return (
        snapshot.state.value,
        snapshot.physical_present,
        snapshot.transport_present,
        snapshot.published,
        snapshot.publish_when_ready,
    )


def _reducer_rows() -> list[tuple[str | bool, ...]]:
    return sorted(
        (
            *_snapshot_cells(source),
            buffer_empty,
            action.value,
            *_snapshot_cells(target),
        )
        for source, buffer_empty, action, target in iter_endpoint_hub_reducer_edges()
    )


def _reducer_digest() -> str:
    return hashlib.sha256(
        json.dumps(_reducer_rows(), sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _tla_atom(item: str | bool) -> str:
    if isinstance(item, bool):
        return "TRUE" if item else "FALSE"
    return json.dumps(item)


def _tla_set(rows: list[tuple[str | bool, ...]]) -> str:
    lines = ["{"]
    for index, row in enumerate(rows):
        suffix = "," if index + 1 < len(rows) else ""
        lines.append("    <<" + ", ".join(_tla_atom(item) for item in row) + ">>" + suffix)
    lines.append("}")
    return "\n".join(lines)


def render_endpoint_hub_implementation_relation() -> str:
    states = "{" + ", ".join(json.dumps(state.value) for state in EndpointHubState) + "}"
    actions = "{" + ", ".join(json.dumps(action.value) for action in EndpointHubAction) + "}"
    reducer = _tla_set(_reducer_rows())
    return "\n".join(
        (
            "------------- MODULE EndpointHubImplementationReducer -------------",
            "EXTENDS FiniteSets",
            "",
            "\\* Generated from pal.channel.lifecycle.reduce_endpoint_hub; do not edit.",
            f"ReducerDigest == {json.dumps(_reducer_digest())}",
            "",
            f"HubStates == {states}",
            f"HubActions == {actions}",
            f"HubReducerEdges == {reducer}",
            "HubStep(sourceState, sourcePhysical, sourceTransport, sourcePublished,",
            "        sourceIntent, bufferEmpty, action, targetState, targetPhysical,",
            "        targetTransport, targetPublished, targetIntent) ==",
            "    <<sourceState, sourcePhysical, sourceTransport, sourcePublished,",
            "      sourceIntent, bufferEmpty, action, targetState, targetPhysical,",
            "      targetTransport, targetPublished, targetIntent>> \\in HubReducerEdges",
            "",
            "ReducerWellFormed ==",
            "    \\A edge \\in HubReducerEdges :",
            "        /\\ edge[1] \\in HubStates",
            "        /\\ edge[2] \\in BOOLEAN",
            "        /\\ edge[3] \\in BOOLEAN",
            "        /\\ edge[4] \\in BOOLEAN",
            "        /\\ edge[5] \\in BOOLEAN",
            "        /\\ edge[6] \\in BOOLEAN",
            "        /\\ edge[7] \\in HubActions",
            "        /\\ edge[8] \\in HubStates",
            "        /\\ edge[9] \\in BOOLEAN",
            "        /\\ edge[10] \\in BOOLEAN",
            "        /\\ edge[11] \\in BOOLEAN",
            "        /\\ edge[12] \\in BOOLEAN",
            "ReducerNeverPublishesBeforeReady ==",
            "    \\A edge \\in HubReducerEdges :",
            "        edge[11] =>",
            "            /\\ edge[8] = \"attached\"",
            "            /\\ edge[9]",
            "            /\\ edge[10]",
            "            /\\ edge[6]",
            "ReducerRemovalOwnsNothing ==",
            "    \\A edge \\in HubReducerEdges :",
            "        edge[8] \\in {\"absent\", \"removing\"} =>",
            "            /\\ ~edge[9]",
            "            /\\ ~edge[10]",
            "            /\\ ~edge[11]",
            "            /\\ ~edge[12]",
            "",
            "=============================================================================",
            "",
        )
    )


__all__ = ["render_endpoint_hub_implementation_relation"]
