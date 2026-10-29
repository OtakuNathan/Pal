# Cache TTL Estimate Reminder

The reminder is used to prompt the user to compact on demand during idle periods of a long conversation; it does not mean the provider has confirmed a cache hit.
The previous reminder relied directly on strict boundary evidence; that evidence is currently unavailable, leaving the reminder permanently in
`boundary_evidence_unavailable`. The reminder now uses an independent local estimate, and strict hot-cache entry keeps its original constraints.

Timing starts when a successful response has been fully returned and locally settled, using a monotonic clock; the UTC return time is for diagnostics only.
Time spent during request generation, turn submission, and message delivery is not re-counted into a full window.
Failures, cancellations, duplicate settlements, and late-arriving stale requests do not extend the current window. Only successful responses for the currently read resident scope and
the matching endpoint/model/provider/protocol/base URL are counted; responses from a Bunshin/worker or another endpoint do not renew it.
No reminder is given when the endpoint has caching disabled.

The TTL selection order is: the known cache TTL actually used by the request, exact model rules, provider policy, and the generic fallback.
Model rules have no entries yet; they will be added separately once models within the same provider are found to have differing TTLs.
Internal policy is centralized in `pal.llm.cache_reminder`, which does not change the request's cache parameters:

| Source | Current estimated window |
| --- | --- |
| OpenAI | 30 minutes |
| OpenRouter | 30 minutes (local estimate; actual value depends on the downstream provider) |
| Anthropic | 5 minutes; an explicitly requested TTL takes priority |
| DeepSeek | 1 hour (local early-reminder policy; the provider has not committed to a fixed TTL) |
| Other providers, including GLM without a configured TTL | 30 minutes |

The reminder fires 5 minutes early by default, and only when the context is at least 32,768 tokens. Unknown providers therefore remind about
25 minutes after a successful return. If the entire TTL is no longer than the lead time, the reminder window begins immediately.
The input token total returned by the provider is preferred; when missing, a local encoding estimate is used.
Reminder state is kept only in memory and subject to the coordinator capacity limit; after a process restart it waits for the next successful response.

New user activity clears old reminders; after a successful turn ends, rescheduling follows the latest response. An expired reminder is withdrawn, and the button remains subject to
the current epoch, expiration time, running turns, and one-time consumption validation. The reminder text explicitly marks it as an "estimate". Clicking this
estimated reminder performs a normal manual compact; the normal compact attempts legitimate prefix reuse according to existing rules, falling back to the cold path when necessary.
The estimate does not authorize a strict hot-cache compact, nor does it guarantee a cache hit or cost savings.

Read-only status and parameter configuration still use `pal core cache-reminder show/configure`, with no new model tools added.
This change includes resident core/LLM code; after the source is updated, running Pal instances must be restarted independently to load it;
plugin reload cannot activate these changes. This implementation did not restart online instances and did not modify the running configuration.

Policy basis: [OpenAI caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching),
[Anthropic caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[DeepSeek caching documentation](https://api-docs.deepseek.com/guides/kv_cache/),
[OpenRouter caching documentation](https://openrouter.ai/docs/guides/best-practices/prompt-caching).
These documents describe provider behavior; the scheduling estimates in the table above are not retention commitments by the providers.
