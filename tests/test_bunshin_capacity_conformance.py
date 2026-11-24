"""Compare the bounded TLC capacity graph with public semaphore operations."""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from pal.bunshin.coroutine_runtime import CoroutineRunSemaphore, NodeSlot
from scripts.run_tlc import ROOT, accepted, ensure_jar, load_inventory


@dataclass(frozen=True)
class Occupancy:
    active: frozenset[str]
    reserved: frozenset[str]
    unsettled: frozenset[str]

    @classmethod
    def observe(cls, pool: CoroutineRunSemaphore, pending_receipts: set[str]):
        # The public status view combines held and active nodes. Observe the
        # recorded predecessor too, so a stale reservation cannot hide there.
        active = frozenset(slot.node_id for slot in pool._active_nodes.values())
        assert len(active) == pool.active_count, "multiple permits on the same node"
        held = pool._reservations
        reserved = frozenset(slot.node_id for slot in held)
        assert active.isdisjoint(reserved)
        assert {slot.node_id for slot in pool.reserved_nodes} == active | reserved
        return cls(active, reserved, frozenset(slot.node_id for slot, previous in held.items()
                                               if previous in pending_receipts))


def projected_tlc_graph(dot: Path):
    states = {}
    edges = []
    for line in dot.read_text().splitlines():
        edge = re.match(r'^(-?\d+) -> (-?\d+) \[label="(\w+)"', line)
        if edge:
            edges.append(edge.groups())
            continue
        state = re.match(r'^(-?\d+) \[label=', line)
        if state:
            fields = dict(re.findall(r'\b(permits|reserved|unsettled) = \{([^}]*)\}', line))
            states[state[1]] = Occupancy(*(
                frozenset(filter(None, fields[key].split(", ")))
                for key in ("permits", "reserved", "unsettled")
            ))
    graph = {state: set() for state in states.values()}
    for source, target, action in edges:
        if states[source] != states[target]:
            graph[states[source]].add((action, states[target]))
    return graph


async def runtime_successors(source: Occupancy, workers: set[str], capacity: int):
    successors = set()
    slots = {name: NodeSlot("workflow", "epoch", 1, name) for name in workers}
    for name in workers:
        operations = ["Spawn"]
        if name in source.active:
            operations.extend(["Release", "ReserveSettled", "ReserveUnsettled"])
        if name in source.reserved:
            operations.append("YieldNode")
        if name in source.unsettled:
            operations.append("SettlePrevious")
        for operation in operations:
            # Rebuild the source via public operations for every independent probe.
            unsettled = set(source.unsettled)
            keep = set(slots.values())
            pool = CoroutineRunSemaphore(capacity, keep.__contains__, lambda previous: previous not in unsettled)
            permits = {}
            for current in sorted(source.active | source.reserved):
                permit = await asyncio.wait_for(pool.acquire(f"{current}/first", node=slots[current]), 1)
                permit.assignment_id = current
                if current in source.reserved:
                    await permit.release(reserve_successor=True)
                else:
                    permits[current] = permit
            assert Occupancy.observe(pool, unsettled) == source

            action = operation
            if operation == "Spawn":
                waiter = asyncio.create_task(pool.acquire(f"{name}/next", node=slots[name]))
                await asyncio.sleep(0)  # Let acquire enter the uncontended condition, or park there.
                if not waiter.done():
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                else:
                    waiter.result()
            elif operation == "YieldNode":
                keep.remove(slots[name])
                await pool.reconcile_reservations()
            elif operation == "SettlePrevious":
                unsettled.remove(name)
                await pool.reconcile_reservations()
            else:
                action = "Release"
                if operation == "ReserveUnsettled":
                    unsettled.add(name)
                await permits[name].release(reserve_successor=operation != "Release")

            target = Occupancy.observe(pool, unsettled)
            if target != source:
                successors.add((action, target))
    return successors


def assert_capacity_conformance(dot: Path, config: Path):
    graph = projected_tlc_graph(dot)
    settings = config.read_text()
    workers = set(re.search(r"Workers = \{([^}]+)\}", settings)[1].split(", "))
    capacity = int(re.search(r"Capacity = (\d+)", settings)[1])
    # Require witnesses even if a later model edit accidentally disables handoff.
    assert any(state.unsettled for state in graph)
    assert any(len(state.reserved) == capacity for state in graph)

    async def compare():
        for source, expected in graph.items():
            actual = await runtime_successors(source, workers, capacity)
            assert actual == expected, (source, actual - expected, expected - actual)

    asyncio.run(compare())


@pytest.mark.parametrize("suffix", ["", "IgnoreReservation", "ReuseCheckpoint"],
                         ids=["runtime-conformance", "overbook-mutant", "stale-checkpoint-mutant"])
def test_process_capacity_tlc(tmp_path, suffix):
    jar_path = os.environ.get("TLA2TOOLS_JAR")
    if not jar_path:
        pytest.skip("Set TLA2TOOLS_JAR to run the pinned model checker")
    manifest = load_inventory()
    jar = Path(jar_path).resolve()
    ensure_jar(jar, manifest["tool"])
    config = f"ProcessCapacityLifecycle{suffix}.cfg"
    case = next(case for case in manifest["cases"] if case["config"] == f"spec/bunshin/{config}")
    module = Path(case["module"])
    shutil.copy(ROOT / module, tmp_path)
    shutil.copy(ROOT / case["config"], tmp_path)
    dump = [] if suffix else ["-dump", "dot,actionlabels", "capacity"]
    run = subprocess.run(
        ["java", "-XX:+UseParallelGC", "-jar", str(jar), "-workers", "1", "-cleanup",
         *dump, "-config", config, module.name],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
    )
    output = run.stdout + run.stderr
    assert accepted(case, run.returncode, output), output
    if not suffix:
        assert_capacity_conformance(tmp_path / "capacity.dot", tmp_path / config)
