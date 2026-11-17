"""Explicit composition of the optional cache-reminder module in tests."""
from contextlib import contextmanager
from dataclasses import dataclass, field

from pal.cache_reminder.runtime import register_with_core
from pal.core.capabilities import register_with_core as register_core
from tests.llm_fakes import NonStreamingLLM


@dataclass
class _Settings:
    values: dict[str, str] = field(default_factory=lambda: {
        "cache_warm_deadline_enabled": "true",
        "cache_warm_deadline_lead_seconds": "30",
        "cache_warm_deadline_min_prefix_tokens": "1024",
    })

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value
        return value

    @contextmanager
    def selection_transaction(self):
        previous = dict(self.values)
        try:
            yield
        except BaseException:
            self.values = previous
            raise


def attach_cache_reminder(core, settings=None):
    if core.context.module_registry.get("core") is None:
        register_core(core)
    llm = core.context.port_registry.get("llm:llm")
    if llm is None:
        llm = NonStreamingLLM()
        core.context.port_registry["llm:llm"] = llm
    if settings is None:
        settings = _Settings()
    core.context.port_registry["llm:settings"] = settings
    register_with_core(core.context)
    return core.context.port_registry["cache_reminder:reminder"]
