from __future__ import annotations

import json
from pathlib import Path


DEFAULT_MEMORY_EMBEDDING_MODEL = "bge-m3"

_MANAGED_MEMORY_KEYS = {
    "embedding_ollama_remote_base_url",
    "embedding_ollama_remote_base_urls",
    "embedding_ollama_model_name",
}


def upsert_memory_embedding_config(
    runtime_root: Path,
    *,
    remote_ollama_base_urls: list[str] | tuple[str, ...],
    model_name: str = DEFAULT_MEMORY_EMBEDDING_MODEL,
) -> None:
    config_path = Path(runtime_root) / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    remote_urls = _normalize_urls(remote_ollama_base_urls)
    normalized_model = str(model_name or DEFAULT_MEMORY_EMBEDDING_MODEL).strip() or DEFAULT_MEMORY_EMBEDDING_MODEL
    managed_lines: list[str] = []
    if remote_urls:
        managed_lines.append(f"embedding_ollama_remote_base_urls = {_toml_string_array(remote_urls)}")
    if normalized_model != DEFAULT_MEMORY_EMBEDDING_MODEL:
        managed_lines.append(f"embedding_ollama_model_name = {_toml_string(normalized_model)}")
    updated = _replace_memory_embedding_keys(existing, managed_lines)
    if updated == existing:
        return
    if not updated.strip():
        if config_path.exists():
            config_path.write_text("", encoding="utf-8")
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(updated, encoding="utf-8")


def _replace_memory_embedding_keys(existing: str, managed_lines: list[str]) -> str:
    lines = existing.splitlines()
    output: list[str] = []
    in_memory = False
    saw_memory = False
    inserted = False

    for line in lines:
        section = _section_name(line)
        if section is not None:
            if in_memory and not inserted:
                _append_managed_lines(output, managed_lines)
                inserted = True
            in_memory = section == "memory"
            saw_memory = saw_memory or in_memory
            output.append(line)
            continue

        if in_memory and _line_key(line) in _MANAGED_MEMORY_KEYS:
            continue
        output.append(line)

    if in_memory and not inserted:
        _append_managed_lines(output, managed_lines)
        inserted = True

    if not saw_memory and managed_lines:
        if output and output[-1].strip():
            output.append("")
        output.append("[memory]")
        _append_managed_lines(output, managed_lines)

    return "\n".join(output).rstrip() + ("\n" if output or managed_lines else "")


def _append_managed_lines(output: list[str], managed_lines: list[str]) -> None:
    if not managed_lines:
        return
    if output and output[-1].strip():
        output.append("")
    output.extend(managed_lines)


def _section_name(line: str) -> str | None:
    text = line.strip()
    if not text.startswith("[") or text.startswith("[["):
        return None
    end = text.find("]")
    if end < 0:
        return None
    tail = text[end + 1 :].strip()
    if tail and not tail.startswith("#"):
        return None
    return text[1:end].strip()


def _line_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    return stripped.split("=", 1)[0].strip()


def _normalize_urls(values: list[str] | tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = str(value or "").strip().rstrip("/")
        if not url:
            continue
        if "://" not in url:
            url = f"http://{url}"
        if url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"
