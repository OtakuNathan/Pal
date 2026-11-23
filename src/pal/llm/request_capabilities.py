"""Bind exact-model sampling defaults without consulting provider identity."""
from __future__ import annotations

from typing import Any, Mapping

from pal.llm.ir import WireShape
from pal.shared.json_values import freeze_json_mapping


_MODEL_SAMPLING_POLICIES = {
    "gpt-6.2-astra": "unsupported",
    "gpt-6.2-sol": "unsupported",
    "gpt-6-astra": "unsupported",
    "gpt-6.1-sol": "unsupported",
    "gpt-6.1-terra": "requires_no_reasoning",
    "gpt-6-sol": "requires_no_reasoning",
    "gpt-6-luna": "requires_no_reasoning",
}


def bind_request_capabilities(
    wire_shape: WireShape, model_id: str, capabilities: Mapping[str, Any],
) -> Mapping[str, Any]:
    resolved = dict(capabilities)
    if wire_shape in {WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION}:
        default = _MODEL_SAMPLING_POLICIES.get(model_id.removeprefix("openai/"))
        if default is not None and resolved.get("sampling_policy") is None:
            resolved["sampling_policy"] = default
    return freeze_json_mapping(resolved)
