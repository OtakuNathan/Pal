from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RuntimeConfig:
    """Runtime tuning constants. Loaded from {runtime_root}/config.toml, fallback to defaults."""

    runtime_root: Path | None = None

    # read limits
    max_lines_to_read: int = 2_000
    default_max_output_tokens: int = 25_000
    max_output_size_bytes: int = 262_144

    # tool / prompt budget
    default_max_result_size_chars: int = 50_000
    max_tool_result_tokens: int = 100_000
    max_tool_results_per_message_chars: int = 200_000
    active_tool_result_preview: int = 1_000
    tool_result_pager_retention_user_turns: int = 5
    chars_per_token: float = 3.5
    context_margin_factor: float = 0.05
    context_margin_cap: int = 16_384
    context_margin_min: int = 1_024
    fallback_max_output_tokens: int = 4_096

    # stagnation
    stagnation_repeat_threshold: int = 3
    stagnation_oscillation_window: int = 4

    # llm retry
    llm_base_retry_delay_ms: int = 500
    llm_max_retry_delay_ms: int = 32_000
    llm_stale_connection_settle_ms: int = 300
    llm_endpoint_retry_attempts: int = 3
    llm_compaction_retry_attempts: int = 3
    llm_max_output_recovery_attempts: int = 3
    llm_request_timeout_seconds: float = 180.0
    llm_compaction_timeout_seconds: float = 180.0

    # memory
    keep_recent_tool_messages: int = 5
    embedding_ollama_remote_base_urls: tuple[str, ...] = ()
    embedding_ollama_local_base_url: str = "http://127.0.0.1:11434"
    embedding_ollama_model_name: str = "bge-m3"
    embedding_ollama_keep_alive: str = "5m"
    embedding_ollama_remote_timeout_seconds: float = 8.0
    embedding_ollama_local_timeout_seconds: float = 120.0
    embedding_ollama_fallback_cooldown_seconds: float = 3600.0

    @classmethod
    def load(cls, runtime_root: Path | None) -> RuntimeConfig:
        if runtime_root is None:
            return cls()
        root = Path(runtime_root)
        config_path = Path(runtime_root) / "config.toml"
        if not config_path.exists():
            return cls(runtime_root=root)
        try:
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
        except Exception:
            return cls(runtime_root=root)
        if not isinstance(raw, dict):
            return cls(runtime_root=root)
        kwargs: dict = {"runtime_root": root}
        cls._apply_section(kwargs, raw, "read", {
            "max_lines_to_read": int,
            "default_max_output_tokens": int,
            "max_output_size_bytes": int,
        })
        cls._apply_section(kwargs, raw, "budget", {
            "default_max_result_size_chars": int,
            "max_tool_result_tokens": int,
            "max_tool_results_per_message_chars": int,
            "active_tool_result_preview": int,
            "tool_result_pager_retention_user_turns": int,
            "chars_per_token": float,
            "context_margin_factor": float,
            "context_margin_cap": int,
            "context_margin_min": int,
            "fallback_max_output_tokens": int,
        })
        cls._apply_section(kwargs, raw, "stagnation", {
            "repeat_threshold": ("stagnation_repeat_threshold", int),
            "oscillation_window": ("stagnation_oscillation_window", int),
        })
        cls._apply_section(kwargs, raw, "llm", {
            "base_retry_delay_ms": ("llm_base_retry_delay_ms", int),
            "max_retry_delay_ms": ("llm_max_retry_delay_ms", int),
            "stale_connection_settle_ms": ("llm_stale_connection_settle_ms", int),
            "endpoint_retry_attempts": ("llm_endpoint_retry_attempts", int),
            "compaction_retry_attempts": ("llm_compaction_retry_attempts", int),
            "max_output_recovery_attempts": ("llm_max_output_recovery_attempts", int),
            "request_timeout_seconds": ("llm_request_timeout_seconds", float),
            "compaction_timeout_seconds": ("llm_compaction_timeout_seconds", float),
        })
        cls._apply_section(kwargs, raw, "memory", {
            "keep_recent_tool_messages": int,
            "embedding_ollama_local_base_url": str,
            "embedding_ollama_model_name": str,
            "embedding_ollama_keep_alive": str,
            "embedding_ollama_remote_timeout_seconds": float,
            "embedding_ollama_local_timeout_seconds": float,
            "embedding_ollama_fallback_cooldown_seconds": float,
        })
        memory_section = raw.get("memory")
        if isinstance(memory_section, dict):
            remote_urls = memory_section.get("embedding_ollama_remote_base_urls")
            if remote_urls is None:
                remote_urls = memory_section.get("embedding_ollama_remote_base_url")
            if remote_urls is not None:
                kwargs["embedding_ollama_remote_base_urls"] = cls._string_tuple(remote_urls)
        return cls(**kwargs)

    @staticmethod
    def _apply_section(
        kwargs: dict,
        raw: dict,
        section: str,
        mapping: dict[str, type | tuple[str, type]],
    ) -> None:
        section_data = raw.get(section)
        if not isinstance(section_data, dict):
            return
        for toml_key, field_spec in mapping.items():
            if toml_key not in section_data:
                continue
            if isinstance(field_spec, tuple):
                field_name, field_type = field_spec
            else:
                field_name, field_type = toml_key, field_spec
            value = section_data[toml_key]
            try:
                kwargs[field_name] = field_type(value)
            except (ValueError, TypeError):
                pass

    @staticmethod
    def _string_tuple(value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            items = value.replace("\n", ",").split(",")
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            return ()
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return tuple(normalized)

    @classmethod
    def defaults(cls) -> RuntimeConfig:
        return cls()
