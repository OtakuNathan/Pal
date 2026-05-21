from __future__ import annotations

import re


_XML_TAG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def render_xml_block(tag: str, content: object) -> str:
    normalized_tag = str(tag or "").strip()
    if not _XML_TAG_RE.match(normalized_tag):
        raise ValueError(f"invalid XML prompt tag: {normalized_tag!r}")
    body = str(content or "").strip()
    if not body:
        return ""
    return f"<{normalized_tag}>\n{body}\n</{normalized_tag}>"


def render_system_reminder(content: object) -> str:
    return render_xml_block("system-reminder", content)


def render_runtime_context_update(kind: object, content: object) -> str:
    raw_kind = str(kind or "runtime_context").strip() or "runtime_context"
    normalized_kind = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_kind).strip("_") or "runtime_context"
    body = str(content or "").strip()
    if not body:
        return ""
    return f'<runtime_context_update kind="{normalized_kind}">\n{body}\n</runtime_context_update>'
