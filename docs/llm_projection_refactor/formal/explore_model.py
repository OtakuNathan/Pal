#!/usr/bin/env python3
"""Finite-state executable companion to EndpointProjection.tla.

This is an independently written Python abstraction, NOT TLC and NOT a parser
or verifier for the .tla file. It checks the same listed safety ideas for a
bounded model. It must never substitute for running TLC on the delivered spec.
No network or third-party packages are required.
"""
from __future__ import annotations
import argparse
from collections import deque
from dataclasses import dataclass, replace
from itertools import combinations
import json
from typing import Any

EMPTY = frozenset()
@dataclass(frozen=True)
class Work:
    calls: frozenset[str] = EMPTY
    started: frozenset[str] = EMPTY
    results: frozenset[str] = EMPTY
    native_calls: frozenset[str] = EMPTY
    token: tuple[Any, ...] | None = None

@dataclass(frozen=True)
class Slot:
    generation: int = 0
    fence: int = 0
    endpoint: str = 'A'
    attempt: int = 0
    phase: str = 'Idle'
    history: tuple[Any, ...] = ()
    native: tuple[Any, ...] = ()
    cache: tuple[Any, ...] = ()
    sent: tuple[Any, ...] = ()
    work: Work = Work()

@dataclass(frozen=True)
class State:
    slots: tuple[Slot, ...]
    mail: frozenset[tuple[Any, ...]] = EMPTY

@dataclass(frozen=True)
class Bounds:
    scopes: int = 1
    calls: tuple[str, ...] = ('a', 'b')
    generations: int = 1
    fences: int = 1
    attempts: int = 2
    blocks: int = 2
    buggy: bool = False

def key(s: int, x: Slot) -> tuple[Any, ...]:
    return (s, x.generation, x.fence, x.endpoint, x.attempt)

def project(s: int, x: Slot) -> tuple[Any, ...]:
    return tuple((s, x.generation, x.endpoint, b, n)
                 for b, n in zip(x.history, x.native))

def prefix(a: tuple, b: tuple) -> bool:
    return a == b[:len(a)]

def put(st: State, s: int, x: Slot, mail=None) -> State:
    return State(st.slots[:s] + (x,) + st.slots[s+1:],
                 st.mail if mail is None else mail)

def check(st: State, b: Bounds) -> str | None:
    for s, x in enumerate(st.slots):
        w = x.work
        if not (0 <= x.generation <= b.generations and
                0 <= x.fence <= b.fences and 0 <= x.attempt <= b.attempts
                and len(x.history) <= b.blocks and len(x.native) == len(x.history)
                and w.results <= w.started <= w.calls <= frozenset(b.calls)):
            return 'TypeOK'
        if any(block[1] != block[2] for block in x.history):
            return 'ClosedHistory'
        for block, n in zip(x.history, x.native):
            if n is not None and not (n[0][0] == s and n[0][1] == x.generation
                    and n[0][3] == x.endpoint and n[1] == block[1]):
                return 'NativeAligned'
        if not prefix(x.cache, project(s, x)) or not prefix(x.sent, project(s, x)):
            return 'PrefixSound'
        if x.phase in ('Streaming', 'Tools', 'Committable') and not (
                w.token == key(s, x) and w.native_calls == w.calls):
            return 'DraftAligned'
        if len({block[0] for block in x.history}) != len(x.history):
            return 'NoDuplicateCommit'
    return None

def successors(st: State, b: Bounds):
    for s, x in enumerate(st.slots):
        w = x.work
        if x.phase == 'Idle':
            p = project(s, x)
            if x.cache != p and prefix(x.cache, p):
                yield f'Sync({s})', put(st, s, replace(x, cache=p))
            if x.cache == p and x.attempt < b.attempts and len(x.history) < b.blocks:
                for n in range(len(b.calls)+1):
                    for cs in combinations(b.calls, n):
                        nx = replace(x, attempt=x.attempt+1, phase='Streaming', sent=x.cache)
                        k = key(s, nx)
                        nx = replace(nx, work=Work(calls=frozenset(cs),
                            native_calls=frozenset(cs), token=k))
                        yield f'Begin({s},{cs})', put(st, s, nx, st.mail | {k})
            if x.generation < b.generations:
                ep = 'B' if x.endpoint == 'A' else 'A'
                yield f'Switch({s},{ep})', put(st, s, replace(x,
                    generation=x.generation+1, endpoint=ep,
                    native=(None,)*len(x.history), cache=(), sent=()))
                yield f'Compact({s})', put(st, s, replace(x,
                    generation=x.generation+1, history=(), native=(), cache=(), sent=()))
            if x.cache:
                yield f'Evict({s})', put(st, s, replace(x, cache=()))
            if x.fence < b.fences:
                yield f'Restart({s})', put(st, s, replace(x, fence=x.fence+1, cache=()))
            yield f'Close({s})', put(st, s, replace(x, phase='Retired',
                native=(None,)*len(x.history), cache=(), sent=()))
        if x.phase == 'Streaming':
            yield f'CancelStream({s})', put(st, s, replace(x, phase='Idle', work=Work()))
        if x.phase == 'Tools':
            for c in sorted(w.calls - w.started):
                yield f'StartTool({s},{c})', put(st, s, replace(x,
                    work=replace(w, started=w.started | {c})))
            for c in sorted(w.started - w.results):
                rs = w.results | {c}
                yield f'FinishTool({s},{c})', put(st, s, replace(x,
                    phase='Committable' if rs == w.calls else 'Tools',
                    work=replace(w, results=rs)))
            if w.calls != w.started:
                cs = w.started
                yield f'Repair({s})', put(st, s, replace(x,
                    phase='Committable' if cs == w.results else 'Tools',
                    work=replace(w, calls=cs,
                        native_calls=w.native_calls if b.buggy else cs)))
        if x.phase == 'Committable' and w.calls == w.results == w.native_calls and w.token == key(s,x):
            block = (w.token, w.calls, w.results)
            native = (w.token, w.native_calls)
            yield f'Commit({s})', put(st, s, replace(x, phase='Idle', work=Work(),
                history=x.history+(block,), native=x.native+(native,)))
    for k in sorted(st.mail):
        s = k[0]
        x = st.slots[s]
        nx = x
        if x.phase == 'Streaming' and k == key(s,x):
            nx = replace(x, phase='Tools' if x.work.calls else 'Committable')
        yield f'Deliver({k})', put(st, s, nx, st.mail - {k})

def explore(b: Bounds, limit: int) -> dict:
    initial = State(tuple(Slot() for _ in range(b.scopes)))
    parents = {initial: (None, 'Init')}
    queue = deque([initial])
    edges = 0
    while queue:
        st = queue.popleft()
        for action, ns in successors(st,b):
            edges += 1
            failure = check(ns,b)
            if failure:
                path = [action]
                cur = st
                while cur is not None:
                    cur, previous = parents[cur]
                    path.append(previous)
                return dict(status='counterexample', invariant=failure,
                    states=len(parents), transitions=edges, trace=list(reversed(path)))
            if ns not in parents:
                if len(parents) >= limit:
                    return dict(status='state_limit', states=len(parents), transitions=edges)
                parents[ns] = (st,action)
                queue.append(ns)
    return dict(status='exhausted', states=len(parents), transitions=edges)

def isolation_smoke() -> None:
    """A late scope-0 callback cannot change scope-1, even on the same endpoint."""
    st = State((Slot(), Slot()))
    b = Bounds(scopes=2, calls=('a',), attempts=1)
    action, begun = next((a,s) for a,s in successors(st,b) if a == "Begin(0,('a',))")
    cancelled = next(s for a,s in successors(begun,b) if a == 'CancelStream(0)')
    restarted = next(s for a,s in successors(cancelled,b) if a == 'Restart(0)')
    delivered = next(s for a,s in successors(restarted,b) if a.startswith('Deliver('))
    assert delivered.slots == restarted.slots
    assert delivered.slots[1] == st.slots[1]
    assert check(delivered,b) is None

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-states',type=int,default=250000)
    parser.add_argument('--mutant',action='store_true')
    parser.add_argument('--scopes',type=int,default=1,choices=(1,2))
    args = parser.parse_args()
    bounds = Bounds(scopes=args.scopes, buggy=args.mutant,
        calls=('a','b') if args.scopes == 1 else ('a',),
        attempts=2 if args.scopes == 1 else 1,
        blocks=2 if args.scopes == 1 else 1)
    isolation_smoke()
    result = explore(bounds,args.max_states)
    result['engine'] = 'Python abstract model (not TLC)'
    print(json.dumps(result,indent=2,ensure_ascii=False))
    if args.mutant:
        raise SystemExit(0 if result['status'] == 'counterexample' else 2)
    raise SystemExit(0 if result['status'] == 'exhausted' else 2)
