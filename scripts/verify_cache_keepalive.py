#!/usr/bin/env python3
"""Paid, isolated cache renewal probe. Never reads resident history or changes settings."""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint_factory import build_default_endpoint_invoker
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.response_evidence import WireResponseEvidence
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.llm.shapes.base import EncodedRequest
from pal.llm.transport_contracts import EncodedTransportRequest
from pal.llm.cache_capabilities import keepalive_profile, supports_suffix_instructions
from pal.llm.cache_keepalive import complete_usage


def probe(root: Path, endpoint_ids: list[str], *, cross_ttl: bool, effort: str = "low") -> bool:
    invoker = build_default_endpoint_invoker(runtime_root=root, credentials=LLMCredentialResolver(
        secret_store=EncryptedFileSecretStore(root / "secrets.json")))
    db = sqlite3.connect((root / "pal.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    def send(binding, encoded, stage):
        evidence = WireResponseEvidence(binding.context.wire_shape)
        try:
            request = EncodedTransportRequest(uuid4().hex, binding.context.wire_shape, 60,
                encoded.payload, False, extra_body=encoded.extra_body)
            terminal = {}
            with contextlib.closing(binding.frames(request)) as frames:
                for frame in frames:
                    evidence.observe(frame)
                    terminal = frame.payload
            usage = evidence.usage
            print(json.dumps({"endpoint": binding.spec.endpoint_id, "stage": stage,
                "input": usage.input_tokens, "read": usage.cached_input_tokens,
                "write": usage.cache_write_input_tokens, "output": usage.output_tokens,
                "fields": usage.reported_fields, "final": usage.final,
                "actual_provider": evidence.actual_provider, "status": terminal.get("status"),
                "stop_reason": terminal.get("stop_reason"), "content_blocks": len(terminal.get("content") or [])}, ensure_ascii=False), flush=True)
            return evidence
        except Exception as exc:
            print(json.dumps({"endpoint": binding.spec.endpoint_id, "stage": stage,
                "error": type(exc).__name__, "status": getattr(exc, "status_code", None)}, ensure_ascii=False), flush=True)
            return None

    def hit(evidence, target, limit, provider):
        return bool(evidence is not None and complete_usage(evidence.usage)
                    and evidence.usage.cached_input_tokens >= target
                    and not evidence.usage.cache_write_input_tokens
                    and evidence.usage.output_tokens <= limit
                    and (not provider or evidence.actual_provider == provider))

    pending = []
    verified = set()
    try:
        for endpoint_id in endpoint_ids:
            row = db.execute("SELECT * FROM llm_endpoints WHERE endpoint_id=? AND enabled=1", (endpoint_id,)).fetchone()
            if row is None:
                print(json.dumps({"endpoint": endpoint_id, "error": "missing"}), flush=True)
                continue
            values = dict(row)
            for key in ("capabilities_blob", "thinking_levels_blob", "input_modalities_blob", "output_modalities_blob"):
                if isinstance(values.get(key), str):
                    values[key] = json.loads(values[key])
            spec = LLMEndpointSpec.from_value(values)
            profile = keepalive_profile(spec, ("30m",) if spec.wire_shape == "openai_response" else ("5m",))
            if profile is None:
                print(json.dumps({"endpoint": endpoint_id, "error": "unsupported_profile"}), flush=True)
                continue
            binding = invoker._binding(spec)
            key = "pal-keepalive-probe-" + uuid4().hex
            prefix = key + "\n" + " alpha beta gamma delta epsilon zeta eta theta" * 270
            if spec.wire_shape == "openai_response":
                suffix = supports_suffix_instructions(binding.context.wire_shape, spec.model_id, spec.base_url)
                payload = {"model": spec.model_id, "input": [{"role": "developer", "content": [
                    {"type": "input_text", "text": prefix, "prompt_cache_breakpoint": {"mode": "explicit"}}]},
                    {"role": "user", "content": "Reply PING."}], "reasoning": {"effort": effort},
                    "max_output_tokens": 128, "store": False}
                extra = {"prompt_cache_options": {"mode": "explicit", "ttl": "30m"}, "prompt_cache_key": key}
                if spec.provider == "openrouter":
                    extra["session_id"] = key
                original = EncodedRequest(payload, extra_body=extra, instructions_are_suffix=suffix)
                ttl = 1800
            else:
                original = EncodedRequest({"model": spec.model_id, "max_tokens": 128,
                    "thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"},
                    "system": [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user", "content": "Reply PING."}]})
                ttl = 300
            heartbeat = binding.codec.prepare_keepalive(original, binding.context)
            if heartbeat is None:
                print(json.dumps({"endpoint": endpoint_id, "error": "codec_unsupported"}), flush=True)
                continue
            started = time.monotonic()
            seed = send(binding, original, "seed")
            if seed is None or not complete_usage(seed.usage) or seed.usage.cache_write_input_tokens <= 0:
                continue
            # Persistence may take a moment; no automatic paid retries.
            time.sleep(2)
            read = send(binding, heartbeat, "immediate")
            tokens = seed.usage.cache_write_input_tokens + seed.usage.cached_input_tokens
            if not hit(read, tokens, profile.output_limit, seed.actual_provider):
                continue
            if cross_ttl:
                pending.append((started + ttl - min(300, ttl // 5), started + ttl + 10,
                    binding, heartbeat, tokens, profile.output_limit, seed.actual_provider))
            else:
                verified.add(endpoint_id)
        events = [(due, "renew", after, binding, heartbeat, tokens, limit, provider)
                  for due, after, binding, heartbeat, tokens, limit, provider in pending]
        while events:
            events.sort(key=lambda x: x[0])
            due, stage, after, binding, heartbeat, tokens, limit, provider = events.pop(0)
            time.sleep(max(0, due-time.monotonic()))
            evidence = send(binding, heartbeat, stage)
            if hit(evidence, tokens, limit, provider):
                if stage == "renew":
                    events.append((after, "after_original_ttl", after, binding, heartbeat, tokens, limit, provider))
                else:
                    verified.add(binding.spec.endpoint_id)
        for endpoint_id in endpoint_ids:
            print(json.dumps({"endpoint": endpoint_id, "verified": endpoint_id in verified,
                              "cross_ttl": cross_ttl}, ensure_ascii=False), flush=True)
        return set(endpoint_ids) <= verified
    finally:
        db.close()
        invoker.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--endpoint", action="append", required=True)
    parser.add_argument("--cross-ttl", action="store_true")
    parser.add_argument("--effort", default="low", choices=("low", "medium", "high", "xhigh", "max"))
    args = parser.parse_args()
    raise SystemExit(0 if probe(args.runtime_root, args.endpoint, cross_ttl=args.cross_ttl, effort=args.effort) else 1)
