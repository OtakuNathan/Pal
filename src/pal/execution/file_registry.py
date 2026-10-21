"""Worker-owned file authority; delivery synchronization is revision fenced.

This module is exported with the file engine. A transport authenticates the
caller and fences the worker epoch before entering this registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import threading

from pal.execution.file_read import FileReadTool, SessionFileVisibilityCache
from pal.execution.file_edit import FileEditTool
from pal.execution.file_write import FileWriteTool
from pal.execution.file_state import SessionFileStateCache
from pal.execution.session_state import FileDeliveryManifest, InMemoryLogicalExecutionState
from pal.execution.tool_facade import ToolExecutionError


class FileRegistryError(ValueError):
    pass


@dataclass
class _Scope:
    revision: int = -1
    fingerprint: str = ''
    backend: InMemoryLogicalExecutionState = field(default_factory=InMemoryLogicalExecutionState)
    staged: dict = field(default_factory=dict)
    retired_results: set = field(default_factory=set)
    retired_operations: set = field(default_factory=set)
    live: dict = field(default_factory=dict)
    visible: frozenset[str] | None = None
    closed: bool = False


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _validate_proof(original, delivered):
    """Delivery can remove proof, including partial lines, but cannot add it."""
    for key in ('file_key', 'digest', 'total_lines', 'operation', 'before_digest'):
        if delivered.get(key) != original.get(key):
            raise FileRegistryError('Delivery changed file identity or version')
    for key in ('empty_file', 'complete_file'):
        if delivered.get(key) and not original.get(key):
            raise FileRegistryError('Delivery added complete-file authority')
    for key in ('inherited_ranges', 'parent_result_ids'):
        if delivered.get(key) and delivered.get(key) != original.get(key):
            raise FileRegistryError('Delivery changed inherited authority')
    for span in delivered.get('spans', ()):
        if not any(
            all(span.get(k) == source.get(k) for k in ('start_line', 'end_line', 'line_length'))
            and source['visible_start_in_line'] <= span['visible_start_in_line']
            <= span['visible_end_in_line'] <= source['visible_end_in_line']
            for source in original.get('spans', ())
        ):
            raise FileRegistryError('Delivery added unseen source text')


class FileRegistry:
    def __init__(self, *, capacity=4096):
        self.scopes = {}
        self.capacity = capacity
        self.lock = threading.RLock()

    def _scope(self, scope):
        if not isinstance(scope, str) or not scope or len(scope) > 512:
            raise FileRegistryError('Invalid file scope')
        if scope not in self.scopes:
            if len(self.scopes) >= self.capacity:
                raise FileRegistryError('File scope capacity reached')
            self.scopes[scope] = _Scope()
        return self.scopes[scope]

    def sync(self, scope, revision, live, pending=(), closed=False, visible=None):
        with self.lock:
            state = self._scope(scope)
            if (type(revision) is not int or revision < 0 or not isinstance(live, dict)
                    or type(closed) is not bool or not isinstance(pending, (list, tuple))
                    or not all(isinstance(oid, str) and oid for oid in pending)
                    or (visible is not None and (not isinstance(visible, (list, tuple))
                        or not all(isinstance(rid, str) and rid for rid in visible)))):
                raise FileRegistryError('Invalid file synchronization')
            fingerprint = _fingerprint([live, list(pending), closed, visible])
            if revision < state.revision:
                raise FileRegistryError('Stale file synchronization revision')
            if revision == state.revision:
                if fingerprint != state.fingerprint:
                    raise FileRegistryError('Conflicting file synchronization revision')
                return {'revision': revision}
            if state.closed or closed and (live or pending):
                raise FileRegistryError('File scope is closed')
            backend = InMemoryLogicalExecutionState()
            backend.begin_input(execution_lifetime_id=scope, input_id=scope)
            for result_id, item in live.items():
                if (not isinstance(result_id, str) or not result_id or not isinstance(item, dict)
                        or not isinstance(item.get('operation_id'), str) or not item['operation_id']):
                    raise FileRegistryError('Invalid result identity')
                oid = item['operation_id']
                if result_id in state.retired_results or oid in state.retired_operations:
                    raise FileRegistryError('Retired authority cannot be resurrected')
                original = state.staged.get(oid)
                if original is None:
                    raise FileRegistryError('Unknown staged file result')
                manifest = FileDeliveryManifest.from_dict(item['manifest'])
                if manifest is None:
                    raise FileRegistryError('Invalid file manifest')
                delivery = manifest.to_dict()
                _validate_proof(original, delivery)
                if result_id in state.live and state.live[result_id] != item:
                    raise FileRegistryError('Committed result identity is immutable')
                delivery['result_id'] = result_id
                backend.record_delivery(execution_lifetime_id=scope, delivery=delivery)
            keep = set(pending) | {item['operation_id'] for item in live.values()}
            if keep & state.retired_operations:
                raise FileRegistryError('Retired operation cannot be staged again')
            state.retired_results.update(set(state.live) - set(live))
            state.retired_operations.update(set(state.staged) - keep)
            state.staged = {oid: manifest for oid, manifest in state.staged.items() if oid in keep}
            state.live = json.loads(json.dumps(live))
            state.visible = frozenset(visible) if visible is not None else None
            state.backend = backend
            state.revision, state.fingerprint, state.closed = revision, fingerprint, closed
            return {'revision': revision}

    def execute(self, scope, revision, operation_id, action, args):
        with self.lock:
            state = self._scope(scope)
            if type(revision) is not int or state.closed or revision != state.revision or revision < 0:
                raise FileRegistryError('Synchronize current file context before execution')
            if not isinstance(args, dict):
                raise FileRegistryError('File arguments must be an object')
            if operation_id in state.retired_operations or operation_id in state.staged:
                raise FileRegistryError('Operation was already executed; query its receipt')
            context = state.backend.context(scope)
            cache = SessionFileStateCache(state.backend, context, state.visible)
            if action == 'read':
                tool = FileReadTool(cache=cache,
                    visibility_cache=SessionFileVisibilityCache(state.backend, context, state.visible),
                    visibility_scope=scope, defer_delivery=True)
            elif action == 'edit':
                tool = FileEditTool(cache=cache)
            elif action == 'write':
                tool = FileWriteTool(cache=cache)
            else:
                raise FileRegistryError('Unknown file action')
            try:
                result = tool.invoke(args)
            except ToolExecutionError as exc:
                return {'status': 'error', 'llm_text': str(exc), 'text': str(exc),
                    'structured': {'error_code': exc.error_code, **exc.details},
                    'effect_receipt': {'outcome': exc.effect_receipt.outcome,
                        'receipt': exc.effect_receipt.receipt} if exc.effect_receipt else None,
                    'recovery_hint': exc.recovery_hint}
            # Both Pal and standalone result types are dataclasses. Receipt is
            # a facade model only in Pal, so serialize that boundary explicitly.
            payload = {key: getattr(result, key) for key in (
                'status', 'llm_text', 'text', 'structured', 'recovery_hint', 'context_delivery')}
            receipt = result.effect_receipt
            payload['effect_receipt'] = ({'outcome': receipt.outcome, 'receipt': receipt.receipt}
                                         if receipt else None)
            if result.context_delivery:
                state.staged[operation_id] = result.context_delivery
            return payload
