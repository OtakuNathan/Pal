# Provider Continuation Contract Matrix

**Status: P0 factual baseline + items pending verification (TBD). Unknowns are always marked TBD, never filled with none.**
**Date: 2026-09-18. Baseline: `7e0b1f74`. Data sources: `~/.pal/pal.sqlite3` table `llm_endpoints` (read-only queries, no keys), `~/.pal/config.toml` active endpoints, and the official continuation documentation referenced by PLAN.md [W1]–[W3].**

## Usage Rules

1. `provider_id`/endpoint_id serve only as **configured policy selectors**; full compatibility is not inferred from vendor names (PLAN §7).
2. The same vendor + shape can differ due to model version, gateway, or thinking/tool configuration; this matrix records one row per endpoint.
3. TBD items must not be rewritten as conclusions until verified in P2 with real fixtures (spec 10.4: sanitized, no new paid calls).
4. This matrix covers only the ContinuationContract axis; PromptCacheProfile (implicit/hybrid/explicit) is a separate axis, recorded elsewhere.

## Active Fleet (11 enabled endpoints)

| endpoint_id | model_id | wire_shape | Required native continuation (verified/pending verification) | Behavior when missing (target) |
|---|---|---|---|---|
| **deepseek-v4.1-flash ★ACTIVE** | deepseek-flash | anthropic_messages | Anthropic-compat thinking blocks: when thinking is enabled, the complete unmodified thinking block must be returned (including signature/redacted, original order) [W2 analogy]; DeepSeek thinking+tools requires reasoning_content to be returned in full [W3]. **TBD-P2**: whether this gateway's anthropic-compat actually issues a signature field (fixture verification) | ContinuationUnavailable; must not silently strip thinking and continue |
| deepseek-reasoner | deepseek-v4.1-pro | anthropic_messages | Same family as above; reasoner is a thinking-first model, so [W3] constraints are stricter. **TBD-P2**: signature issuance behavior | Same as above |
| glm-5.3 | glm-5.3 | openai_completion | **TBD-P2**: the reasoning field continuation requirements for Zhipu chat-completions have not been checked (Zhipu official docs have not yet been consulted and cross-referenced as W1–W3 were; response_hooks.py already has a Zhipu normalizer usable as a reference on the decoding side) | Treated conservatively as RequiredNative until verified |
| glm-5.3-bigmodel | glm-5.3 | openai_completion | Same as glm-5.3 | Same as above |
| glm-5.3-bigmodel-anthropic | glm-5.3 | anthropic_messages | Same family, anthropic shape. **TBD-P2**: thinking block/signature behavior | Same as above |
| glm-5.3-flash | glm-5.3-flash | openai_completion | Same as glm-5.3 | Same as above |
| glm-5.3-flash-bigmodel | glm-5.3-flash | openai_completion | Same as glm-5.3 | Same as above |
| openrouter-gpt-5.6-luna | openai/gpt-5.6-luna | openai_response | OpenAI reasoning: stateless continuation relies on `encrypted_content` in output items; storing only the readable summary is insufficient [W1]. **TBD-P2**: integrity of encrypted_content passthrough on openrouter | Same as above |
| openrouter-gpt-5.6-terra | openai/gpt-5.6-terra | openai_response | Same as luna | Same as above |
| openrouter-gpt-5.6-sol | openai/gpt-5.6-sol | openai_response | Same as luna | Same as above |
| gpt-6-astra | openai/gpt-6-astra | openai_response | Same [W1] family. **Note**: the decision to switch astra's cache route (automatic/hybrid/explicit) has been shelved by Nathan (2026-09-17, case_f3f42d3777b1); this matrix only records the continuation contract and does not trigger a cache route change | Same as above |

★ACTIVE: both `endpoint_id` and `review_endpoint_id` in `~/.pal/config.toml` are deepseek-v4.1-flash. **The active endpoint uses the anthropic_messages shape, so [W2]/[W3]-type contracts directly constrain the production path** — this is the practical basis for the priority of this refactor.

## Mapping from Current State (v1 mechanism) to Target (v2)

- Current state: each assistant message's `ReplayEnvelope` (shape+endpoint+model+payload) is the de facto native storage, persisted wholesale with the L1 snapshot and zeroed out on protocol repair during same-endpoint replay. It "stores" data for all three shapes in the table above, but **does not validate field completeness per protocol** (e.g., it never checks whether encrypted_content/signature exist), nor does it distinguish required/optional.
- Target (PLAN §3/§7): `NativeContinuationStore` performs typed, validated storage per ContinuationContract; unknown fields must not masquerade as known; switching endpoints destroys active lineage data.
- Migration (PLAN §8.2): legacy ReplayEnvelope data is migrated by filtering on actual active binding and protocol consistency; missing required fields are explicitly reported, with no fabricated empty signatures.

## Verification Obligations (before entering P2)

1. Each TBD row must produce a sanitized fixture or be explicitly marked "cannot be verified offline; requires separate P7 canary authorization".
2. Fixtures come in two kinds: synthetic-opaque (proves only "not modified") and provider-real (claims provider-side acceptability); only the latter counts as contract evidence.
3. Whenever any row of this matrix changes from TBD to a conclusion, the fixture file and test name must be cited.
