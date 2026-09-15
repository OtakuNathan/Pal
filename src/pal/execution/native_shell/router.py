"""Resident target/session ownership; optional plugins supply only a backend port."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import itertools
from pathlib import Path
import tempfile
from uuid import uuid4

from .adapter import ShellRuntime, ShellRejected, Completion, TERMINAL
from .remote_contract import RemoteFailure


@dataclass
class Ticket:
    target: int
    epoch: str
    operation_id: str
    origin_turn: str
    native_id: int = 0
    output_id: str = ''
    public_id: int = 0
    kind: str = 'execution'


class ShellRouter(ShellRuntime):
    def __init__(self, *, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self.remote_ids = itertools.count(1 << 48)
        self.tickets = {}
        self.operations = {}
        self.remote_completions = {}
        self.cursors = {}
        self.poll_task = None
        self.cache = tempfile.TemporaryDirectory(prefix='pal-shell-output-')
        self.cache_files = {}
        self.cache_sizes = {}
        self.materialize_lock = asyncio.Lock()
        self.remote_foreground = set()
        self.operation_context = {}

    @property
    def remote_work(self):
        return bool(self.tickets or self.operations)

    @property
    def execution_work(self):
        return bool(self.tickets or any(t.kind == "execution" for t in self.operations.values()))

    def _port(self):
        if self.owner.remote_port is None:
            raise RemoteFailure('backend_unavailable', 'Remote plugin unavailable; attach it to access retained tickets')
        return self.owner.remote_port

    async def _rpc(self, ticket, method, params):
        port = self._port()
        result = await port.request(ticket.target, method, params, ticket.epoch or None)
        if self._closed or self.owner.closed or port is not self.owner.remote_port:
            raise RemoteFailure('backend_unavailable', 'Remote plugin changed while awaiting response', effect='unknown')
        return result

    def resume(self):
        if not self._closed and self.owner.remote_port and self.remote_work and (self.poll_task is None or self.poll_task.done()):
            self.poll_task = self.loop.create_task(self._poll())

    def _adopt(self, ticket, result):
        result = dict(result)
        if ticket.epoch and result['runtime_epoch'] != ticket.epoch:
            raise RemoteFailure('runtime_changed', 'Remote result belongs to another Runtime', effect='unknown')
        ticket.epoch = result['runtime_epoch']
        ticket.output_id = result.get('output_id', '')
        if result['session_id']:
            ticket.native_id = result['session_id']
            if not ticket.public_id:
                ticket.public_id = next(self.remote_ids)
            self.tickets[ticket.public_id] = ticket
        result.update(target=ticket.target, session_id=ticket.public_id,
                      operation_id=ticket.operation_id,
                      output_id=f'{ticket.target}:{ticket.epoch}:{ticket.output_id}')
        self.resume()
        return result

    async def _outcome(self, ticket, response):
        if response['state'] == 'approval_required':
            self.operations.pop(ticket.operation_id, None)
            self.operation_context.pop(ticket.operation_id, None)
            raise RemoteFailure('authorization_required', 'Execution has not started; request a new command approval')
        if response['state'] == 'pending':
            response = await self._rpc(ticket, 'query', {'operation_id': ticket.operation_id, 'wait_ms': 300000})
        if response.get('error'):
            error = response['error']
            if error.get('effect', 'unknown') == 'not_started':
                self.operations.pop(ticket.operation_id, None)
            raise RemoteFailure(error['code'], error['message'], effect=error.get('effect', 'unknown'), operation_id=ticket.operation_id)
        if response['state'] != 'complete' or response.get('result') is None:
            raise RemoteFailure('execution_unknown', 'Execution remains unresolved; query this operation', effect='unknown', operation_id=ticket.operation_id)
        return self._adopt(ticket, response['result'])

    async def run(self, cmd, *, target=0, sudo=False, delivery_context=None, **kwargs):
        if target == 0:
            if sudo:
                raise ShellRejected('sudo_unsupported: explicit privilege approval is remote-only')
            return {**await super().run(cmd, **kwargs), 'target': 0}
        if sudo:
            if kwargs.get('tty'):
                raise RemoteFailure('privilege_pty_unsupported', 'A single-command sudo grant cannot open a privileged PTY')
            return await self.privileged(target, 'sudo', cmd=cmd, delivery_context=delivery_context, **kwargs)
        self._port()  # Reject before allocating any request when the plugin is absent.
        async with self.tool_admission('external_write'):
            pass
        ticket = Ticket(target, '', uuid4().hex, kwargs.get('turn_id', ''))
        self.operations[ticket.operation_id] = ticket
        self._capture_context(ticket, cmd, kwargs.get('tty', False), delivery_context)
        submitted = False
        task = asyncio.current_task()
        self.remote_foreground.add(task)
        try:
            metadata = await self._rpc(ticket, 'metadata', {})
            ticket.epoch = metadata['runtime_epoch']
            args = {k: kwargs[k] for k in ('cwd', 'tty', 'timeout_ms') if kwargs.get(k) is not None}
            args['wait_ms'] = kwargs.get('wait_ms')
            if args['wait_ms'] is None:
                args['wait_ms'] = 1000 if args.get('tty') else 300000
            submitted = True
            response = await self._rpc(ticket, 'submit', {'operation_id': ticket.operation_id, 'cmd': cmd, **args})
            result = await self._outcome(ticket, response)
            return await self.materialize(result) if kwargs.get('load_output', True) else result
        except RemoteFailure as exc:
            exc.operation_id = ticket.operation_id
            if not submitted:
                exc.effect = 'not_started'
            if exc.effect == 'not_started':
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                exc.operation_id = ''
            raise
        finally:
            self.remote_foreground.discard(task)
            self.resume()

    def _capture_context(self, ticket, cmd, tty, context=None):
        core = self.owner.core
        continuation = core.state.active_turns.get(ticket.origin_turn) if core else None
        self.operation_context[ticket.operation_id] = {
            'origin_turn': ticket.origin_turn, 'binding': getattr(continuation, 'delivery_binding', None),
            'budget': None, 'cmd': cmd, 'tty': tty, 'committed': False, **(context or {}),
        }

    async def privileged(self, target, action, *, cmd='', delivery_context=None, **kwargs):
        port = self._port()
        ticket = Ticket(target, '', uuid4().hex, kwargs.get('turn_id', ''), kind=action if action == 'shutdown' else 'execution')
        self.operations[ticket.operation_id] = ticket
        self._capture_context(ticket, cmd, False, delivery_context)
        task = asyncio.current_task()
        self.remote_foreground.add(task)
        submitted = False
        try:
            metadata = await self._rpc(ticket, 'metadata', {})
            ticket.epoch = metadata['runtime_epoch']
            args = {'action': action, 'target': target}
            if action == 'sudo':
                args.update(cmd=cmd, cwd=kwargs.get('cwd', ''), wait_ms=kwargs.get('wait_ms') if kwargs.get('wait_ms') is not None else 300000,
                            timeout_ms=kwargs.get('timeout_ms'))
            else:
                identity = await port.call('identity', {})
                args['protected_machine_id'] = identity['machine_identity']
            prepared = await self._rpc(ticket, 'prepare_privileged', {'operation_id': ticket.operation_id, **args})
            args = prepared.get('normalized_args', args)
            await self.owner.approvals.request(ticket.origin_turn, target, args, prepared['approval'])
            if port is not self.owner.remote_port:
                raise RemoteFailure('backend_unavailable', 'Approval belongs to the detached plugin generation')
            submitted = True
            response = await port.call('approve', {'target': target, 'action': action, 'approval': prepared['approval']})
            if action == 'shutdown':
                if response['state'] == 'pending':
                    response = await self._rpc(ticket, 'query', {'operation_id': ticket.operation_id, 'wait_ms': 1000})
                if response.get('error'):
                    error = response['error']
                    raise RemoteFailure(error['code'], error['message'], effect=error.get('effect', 'unknown'))
                if (response.get('result') or {}).get('status') == 'accepted':
                    self.operations.pop(ticket.operation_id, None)
                    self.operation_context.pop(ticket.operation_id, None)
                return {'target': target, 'operation_id': ticket.operation_id, **(response.get('result') or {'status': 'unknown'})}
            result = await self._outcome(ticket, response)
            return await self.materialize(result) if kwargs.get('load_output', True) else result
        except RemoteFailure as exc:
            exc.operation_id = ticket.operation_id
            if not submitted:
                exc.effect = 'not_started'
            if exc.effect == 'not_started':
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                exc.operation_id = ''
            raise
        except asyncio.CancelledError:
            if not submitted:
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
            raise
        except Exception as exc:
            if not submitted:
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                raise RemoteFailure('privilege_prepare_failed',
                    'Privilege preparation failed before execution; inspect target compatibility and approval setup') from exc
            raise
        finally:
            self.remote_foreground.discard(task)
            self.resume()

    async def reconcile(self, operation_id):
        ticket = self.operations.get(operation_id)
        if ticket is None:
            raise RemoteFailure('invalid_operation', 'No operation owned by this shell scope')
        response = await self._rpc(ticket, 'query', {'operation_id': operation_id, 'wait_ms': 0})
        if response.get('result', {}) and 'session_id' not in response['result']:
            self.operations.pop(operation_id, None)
            self.operation_context.pop(operation_id, None)
            return {'target': ticket.target, 'operation_id': operation_id, **response['result']}
        return await self._outcome(ticket, response)

    async def session_snapshot(self, session_id, *, action='read', **kwargs):
        if session_id < 1 << 48:
            return {**await super().session_snapshot(session_id, action=action, **kwargs), 'target': 0}
        ticket = self.tickets.get(session_id)
        if ticket is None:
            raise ShellRejected('invalid_session: unknown remote session in this caller scope')
        if action == 'release':
            await self.release_output({'operation_id': ticket.operation_id, 'target': ticket.target, 'session_id': session_id})
            return {'session_id': session_id, 'target': ticket.target, 'status': 'released'}
        operation_id = uuid4().hex
        control = Ticket(ticket.target, ticket.epoch, operation_id, ticket.origin_turn,
                         ticket.native_id, ticket.output_id, ticket.public_id)
        self.operations[operation_id] = control
        try:
            response = await self._rpc(control, 'session', {'operation_id': operation_id, 'session_id': ticket.native_id,
                'action': action, **{k: v for k, v in kwargs.items() if v is not None}})
            result = await self._outcome(control, response)
            return result
        except RemoteFailure as exc:
            exc.operation_id = operation_id
            if exc.effect == 'not_started':
                self.operations.pop(operation_id, None)
            raise

    async def materialize(self, event):
        if not event.get('target'):
            return await super().materialize(event)
        ticket = self.operations[event['operation_id']]
        async with self.materialize_lock:
            total = sum(event.get(stream + '_total', 0) for stream in ('stdout', 'stderr'))
            key = event['output_id']
            if total > 8 * 1024 * 1024 or total < 0 or sum(self.cache_sizes.values()) - self.cache_sizes.get(key, 0) + total > 64 * 1024 * 1024:
                raise RemoteFailure('output_capacity', 'Local output cache full; deliver or release retained output', effect='applied')
            self.cache_sizes[key] = total  # Reserve before receiving any bytes.
            result = dict(event)
            for stream in ('stdout', 'stderr'):
                path = Path(self.cache.name) / (uuid4().hex + '.' + stream)
                self.cache_files.setdefault(key, []).append(path)
                try:
                    with path.open('xb') as file:
                        offset, size = 0, event.get(stream + '_total', 0)
                        while offset < size:
                            chunk = await self._rpc(ticket, 'output', {'snapshot': event['snapshot'], 'stream': stream,
                                'offset': offset, 'length': min(256 * 1024, size - offset)})
                            data = chunk['data']
                            if not isinstance(data, bytes) or not data or len(data) > min(256 * 1024, size-offset):
                                raise RemoteFailure('invalid_output', 'Remote output violated snapshot bounds', effect='applied')
                            file.write(data)
                            offset += len(data)
                    raw = await asyncio.to_thread(path.read_bytes)
                    result[stream + '_bytes'] = raw
                    result[stream] = raw.decode('utf-8', errors='replace')
                finally:
                    # Each materialization has one bounded transient file. The worker
                    # retains authoritative bytes until the existing host ack boundary.
                    path.unlink(missing_ok=True)
                    self.cache_files[key].remove(path)
            return result

    async def release_output(self, result):
        if not result.get('target'):
            return await super().release_output(result)
        ticket = self.operations.get(result['operation_id'])
        if ticket is None:
            return  # Already acknowledged locally; do not repeat the model delivery.
        await self._rpc(ticket, 'release', {'output_id': ticket.output_id})
        output_key = f'{ticket.target}:{ticket.epoch}:{ticket.output_id}'
        self.cache_sizes.pop(output_key, None)
        self.cache_files.pop(output_key, None)
        for op, item in list(self.operations.items()):
            if (item.target, item.epoch, item.output_id) == (ticket.target, ticket.epoch, ticket.output_id):
                self.operations.pop(op)
                self.operation_context.pop(op, None)
        self.tickets.pop(ticket.public_id, None)
        self._mark_consumed(ticket.public_id)
        self.remote_completions.pop(ticket.public_id, None)

    async def _poll(self):
        while not self._closed and self.owner.remote_port and self.remote_work:
            groups = {(t.target, t.epoch) for t in self.tickets.values()}
            for target, epoch in groups:
                ticket = next((t for t in self.tickets.values() if (t.target, t.epoch) == (target, epoch)), None)
                if ticket is None:
                    continue
                try:
                    response = await self._rpc(ticket, 'events', {'after': self.cursors.get((target, epoch), 0), 'wait_ms': 0})
                    for event in response['events']:
                        result = event['result']
                        owner = next((t for t in self.tickets.values() if t.target == target and t.epoch == epoch and t.native_id == result['session_id']), None)
                        if owner is None:
                            break  # Keep cursor before an unadopted completion.
                        adopted = self._adopt(owner, result)
                        if owner.public_id not in self._consumed:
                            self.remote_completions[owner.public_id] = Completion(owner.public_id, owner.origin_turn, adopted)
                            self.owner.notify()
                        self.cursors[(target, epoch)] = event['cursor']
                except RemoteFailure:
                    pass  # Reachability is not execution state. Tickets remain resident.
            await asyncio.sleep(1)

    def drain_completions(self):
        result = super().drain_completions()
        result.extend(self.remote_completions.values())
        self.remote_completions.clear()
        return result

    async def terminate(self, session_id):
        if session_id < 1 << 48:
            return await super().terminate(session_id)
        return await self.session_snapshot(session_id, action='terminate')

    async def _discard_cancelled_session(self, session_id):
        if session_id >= 1 << 48:
            return  # Remote cancellation must be reconciled, never discard its ticket.
        return await super()._discard_cancelled_session(session_id)

    async def close(self):
        remote_tasks = [task for task in self.remote_foreground if task is not asyncio.current_task()]
        for task in remote_tasks:
            task.cancel()
        await asyncio.gather(*remote_tasks, return_exceptions=True)
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        await super().close()
        self.cache.cleanup()
