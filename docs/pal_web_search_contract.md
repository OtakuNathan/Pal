# Pal Web Search Contract

> Goal: define the responsibilities and object model of the `web_search` subsystem, and how it integrates with the capability forest.

## Goals

`web_search` is responsible for giving `Pal` web search capability.

It is not a general-purpose HTTP client, nor is it a browser.

It is responsible for:

- structured web search
- multi-provider fallback (Brave Search, DuckDuckGo)
- provider health checks and status reporting
- lifecycle management of search providers

## Owns

- web search provider registry
- search query execution
- provider fallback chain
- active provider selection
- provider health / auth status

## Does Not Own

- HTTP transport implementation (each provider is responsible for its own)
- LLM invocation decisions
- post-processing of search results
- channel transport

## Core Objects

### WebSearchProvider

A search provider is a replaceable backend.

Currently implemented:

- `BraveSearchProvider` — Brave Search API, requires an API key
- `DuckDuckGoSearchProvider` — DuckDuckGo Instant Answer API, no key required, serves as a best-effort fallback

Each provider implements the `WebSearchProviderPort` protocol:

```python
class WebSearchProviderPort(Protocol):
    provider_kind: str
    def search(self, query: str, *, max_results: int, ...) -> WebSearchResult: ...
```

### WebSearchService

The core service layer. Responsible for:

- maintaining the provider registry
- selecting providers by priority and enabled status
- executing searches, automatically falling back to the next provider on failure
- tracking recent errors (`last_errors`)

### WebSearchProviderModel

The database model, storing provider configuration:

- `provider_id` — unique identifier
- `provider_kind` — which implementation it corresponds to
- `enabled` — whether it is enabled
- `priority` — priority (lower number means higher priority)
- `settings_blob` — provider-specific configuration
- `auth_material_blob` — authentication material (e.g. API key)

## Integration with the Capability Forest

`web_search` registers into the capability forest via `IntrospectionProvider`.

### Model Tool Surface

| Tool | Entry point | Purpose |
|------|------|------|
| `search_web(query, limit?, region?, safe_search?)` | always-on direct call | Search; the service selects the provider and performs fallback |
| `inspect_web_search()` | indirect read-only | No parameters; returns diagnostics for the Hub and all providers at once |

Diagnostics always use the same return structure, without switching semantics based on parameters, and without paging or truncating providers:

- Hub: mounted/degraded, provider count, enabled count, configured preferred and effective preferred.
- `providers`: each provider's public configuration, whether auth is complete, and recent request health.
- `diagnostics`: observation time, source, and sections that are partially unavailable; an independent failure
  to read a single provider's auth/health does not lose other known information.

This is passive observation of configuration and recent requests; it does not initiate network probes. The effective preferred means the first position in the current candidate order,
and does not guarantee that provider's network or auth availability; search results report the backend actually executed.
Authentication material values are not returned. Model-visible error reports have tracebacks removed and are limited to 600 characters per entry;
the full recent request report remains in the resident service and is not overwritten by summaries.

The old `inspect_web_search_state`, `list_web_search_providers`, and
`inspect_web_search_provider` are no longer published. The original provider maintenance handler remains available for internal
compatibility calls; configuration, authentication, enable/disable, and preferred switching are done via the `pal web-search` CLI, and model
tools do not provide these write operations.

## Plugin Integration

`web_search` is loaded as a first-party plugin via `plugins_builtin/web_search/`.

`plugin.toml` declares plugin metadata, and `build_plugin(context: PluginBuildContext)` in `runtime.py` creates the service and registers it with the core context.

## Supervisor Default Configuration

The `supervisor`'s `seed_defaults()` presets two providers:

1. `brave_search_default` — Brave Search, priority 0, requires an API key to be configured
2. `duckduckgo_search_default` — DuckDuckGo, priority 10, no key required

The active provider defaults to `brave_search_default`.

## Invariants

- `web_search` provides a fallback-capable provider family.
- When a search fails, the next provider is tried automatically.
- Sensitive information in a provider's `auth_material_blob` must not be exposed in plaintext to the LLM.
- Search results are normalized before being returned to the LLM.

## Non-Goals

- Post-processing and summarization policies for search results are not defined in this document
- Search caching policies are not defined in this document
- Rate limiting implementation is not defined in this document
