#!/usr/bin/env python3
"""Small, paid native compact/continuation check using Pal's existing auth path.

Reads one endpoint row in read-only mode. Does not select a resident model,
modify configuration, load resident history, or expose credential material.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.endpoint_factory import build_default_endpoint_invoker
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR
from pal.llm.runtime import EndpointResolver, LLMRuntime


class IsolatedSettings:
    def __init__(self, endpoint_id):
        self.endpoint_id = endpoint_id
        self.thinking = {}

    def get_active_llm_endpoint_id(self):
        return self.endpoint_id

    def get_think_level(self, endpoint_id):
        return self.thinking.get(endpoint_id)

    def set_think_level(self, endpoint_id, level):
        self.thinking[endpoint_id] = level


async def verify(root: Path, endpoint_id: str) -> None:
    db = sqlite3.connect((root / "pal.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    with db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(llm_endpoints)")}
        # This explicit model schema contains references, never stored keys.
        names = [item.name for item in fields(LLMEndpointSpec) if item.name in columns]
        row = db.execute(f"SELECT {', '.join(names)} FROM llm_endpoints WHERE endpoint_id=? AND enabled=1",
                         (endpoint_id,)).fetchone()
    db.close()
    if row is None:
        raise ValueError("configured enabled endpoint not found")
    values = dict(row)
    for key in ("capabilities_blob", "thinking_levels_blob", "input_modalities_blob", "output_modalities_blob"):
        if isinstance(values.get(key), str):
            values[key] = json.loads(values[key])
    endpoint = LLMEndpointSpec.from_value(values)
    invoker = build_default_endpoint_invoker(runtime_root=root, credentials=LLMCredentialResolver(
        secret_store=EncryptedFileSecretStore(root / "secrets.json")))
    runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint,)), IsolatedSettings(endpoint_id), endpoint_invoker=invoker,
                         config=SimpleNamespace(runtime_root=root, llm_endpoint_retry_attempts=1))
    try:
        if runtime.compaction_strategy() != "openai_responses":
            raise ValueError("selected endpoint is not configured for native compact")
        policy = GenerationPolicyIR(max_output_tokens=512, thinking_selection="lowest_supported")
        original = LLMRequestIR(messages=(
            LLMMessageIR(MessageRole.USER, (TextPartIR("Remember this test project identifier: PAL-LUNA-47. Its color is violet."),)),
            LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("The project is PAL-LUNA-47 and its color is violet."),)),
            LLMMessageIR(MessageRole.USER, (TextPartIR("Keep that identifier and color for my next question."),)),
            LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("Understood."),)),
        ), tools=(), policy=policy)
        replay, tokens, usage = await runtime.acompact_native(original, timeout_seconds=120)
        continuation = LLMRequestIR(messages=(
            LLMMessageIR(MessageRole.ASSISTANT, replay=replay),
            LLMMessageIR(MessageRole.USER, (TextPartIR("Reply with only the project identifier and its color."),)),
        ), tools=(), policy=policy)
        outcome = await runtime.agenerate(continuation)
        passed = "PAL-LUNA-47" in outcome.text and "violet" in outcome.text.lower()
        print(json.dumps({"endpoint": endpoint_id, "strategy": runtime.compaction_strategy(),
            "canonical_items": len(replay.payload["output"]), "compacted_input_tokens": tokens,
            "compact_usage": usage, "continuation": outcome.text, "passed": passed}, ensure_ascii=False))
        if not passed:
            raise RuntimeError("continuation did not preserve the test facts")
    finally:
        runtime.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--endpoint", default="openai-gpt-6-luna")
    args = parser.parse_args()
    asyncio.run(verify(args.runtime_root, args.endpoint))
