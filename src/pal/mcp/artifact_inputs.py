"""Adapt explicitly configured MCP string parameters from scoped artifacts."""
from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

from pal.execution.contracts import CapabilityCall
from pal.foundation.sidecar import MAX_SIDECAR_FRAME_BYTES


class McpArtifactInputError(ValueError):
    """A local pre-dispatch artifact or argument error."""


_REPRESENTATIONS = frozenset({'path', 'url', 'data_url', 'base64'})
_MAX_ENCODED_SOURCE_BYTES = 8 * 1024 * 1024
_ARTIFACT_REFERENCE = {
    'type': 'object',
    'properties': {'artifact_id': {'type': 'string', 'minLength': 1, 'pattern': r'\S'}},
    'required': ['artifact_id'],
    'additionalProperties': False,
}


def artifact_input_config(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise ValueError('artifact_inputs must map exact tool names to parameter representations')
    result = {}
    for tool, fields in value.items():
        if not isinstance(tool, str) or not tool.strip() or not isinstance(fields, dict):
            raise ValueError('artifact_inputs requires nonblank tool names and parameter maps')
        result[tool] = {}
        for field, representation in fields.items():
            if not isinstance(field, str) or not field.strip() or not isinstance(representation, str) or representation not in _REPRESENTATIONS:
                raise ValueError('artifact_inputs parameters require path, url, data_url or base64')
            result[tool][field] = representation
    return result


@dataclass(frozen=True)
class McpArtifactInputs:
    original_schema: dict[str, Any]
    fields: dict[str, str]

    def exposed_schema(self) -> dict[str, Any]:
        schema = deepcopy(self.original_schema)
        properties = schema.get('properties', {})
        for field, representation in self.fields.items():
            original = properties.get(field)
            if not isinstance(original, dict) or original.get('type') != 'string':
                raise ValueError(f'artifact_inputs.{field} requires an explicit top-level string property')
            properties[field] = {
                'anyOf': [original, deepcopy(_ARTIFACT_REFERENCE)],
                'description': (str(original.get('description') or '') +
                    f' May instead supply {{"artifact_id":"..."}} from this conversation; Pal resolves it as {representation} before dispatch.').strip(),
            }
        return schema

    def resolve(self, args: dict[str, Any], call: CapabilityCall,
                resolver: Callable[[str, str, CapabilityCall], str] | None) -> dict[str, Any]:
        resolved = dict(args)
        for field, representation in self.fields.items():
            value = resolved.get(field)
            if not isinstance(value, dict):
                continue
            artifact_id = value.get('artifact_id')
            if set(value) != {'artifact_id'} or not isinstance(artifact_id, str) or not artifact_id.strip():
                raise McpArtifactInputError(f'Invalid artifact reference for parameter {field}')
            if resolver is None:
                raise McpArtifactInputError('Artifact resolution is unavailable in this MCP host')
            try:
                resolved[field] = resolver(artifact_id, representation, call)
            except McpArtifactInputError:
                raise
            except Exception as exc:
                raise McpArtifactInputError('Artifact resolution failed before MCP dispatch') from exc
        # Keep the external schema authoritative, including constraints which
        # involve other fields. Do not echo converted bytes in diagnostics.
        error = next(Draft202012Validator(self.original_schema, format_checker=FormatChecker()).iter_errors(resolved), None)
        if error is not None:
            path = '/'.join(map(str, error.absolute_path)) or '<root>'
            raise McpArtifactInputError(f'Resolved MCP input is invalid at {path} ({error.validator})')
        if len(json.dumps(resolved, ensure_ascii=False).encode('utf-8')) > MAX_SIDECAR_FRAME_BYTES - 4096:
            raise McpArtifactInputError('Resolved MCP arguments exceed the transport size limit')
        return resolved


def resolve_artifact_input(context, artifact_id: str, representation: str, call: CapabilityCall) -> str:
    """Resolve owned local bytes, never source URLs that may embed credentials."""
    if representation not in _REPRESENTATIONS:
        raise McpArtifactInputError('Unsupported artifact representation')
    runtime = context.execution_runtime
    turn_io = runtime.turn_io
    turn_id = str(call.meta.get('turn_id') or '') or None
    scope = turn_io.artifact_scope_for_turn(turn_id) if turn_io is not None else None
    if not scope:
        raise McpArtifactInputError('artifact_scope_unavailable: invoke from the artifact conversation')
    service = context.port_registry.get('artifact:artifact')
    if service is None:
        raise McpArtifactInputError('Artifact service is unavailable')
    try:
        artifact = dict(service.info(artifact_id, str(scope)).get('artifact') or {})
        local = dict((artifact.get('metadata') or {}).get('local_file') or {})
        path_text = str(local.get('preferred_path') or '')
        if not path_text:
            raise McpArtifactInputError('Artifact has no available local representation')
        path = Path(path_text)
        if not path.is_file():
            raise McpArtifactInputError('Artifact local representation is unavailable')
        if representation == 'path':
            return str(path.resolve())
        # Read a bounded amount even if the file grows after stat().
        with path.open('rb') as source:
            raw = source.read(_MAX_ENCODED_SOURCE_BYTES + 1)
        if len(raw) > _MAX_ENCODED_SOURCE_BYTES:
            raise McpArtifactInputError('Artifact exceeds the 8 MiB inline conversion limit; use a path-capable tool')
        encoded = base64.b64encode(raw).decode('ascii')
        if representation == 'base64':
            return encoded
        mime = str(local.get('preferred_mime_type') or artifact.get('mime_type') or 'application/octet-stream')
        # URL adaptation uses a data URL. No file publishing, HTTP fetching or
        # forwarding of credential-bearing ingestion URLs is implied.
        return f'data:{mime};base64,{encoded}'
    except McpArtifactInputError:
        raise
    except (KeyError, ValueError, OSError) as exc:
        raise McpArtifactInputError('Artifact is unavailable in this conversation or its local file cannot be read') from exc
