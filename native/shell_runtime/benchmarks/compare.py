"""Local shell-adapter comparison; no model or resident Pal involvement.

Run with a Release extension and prototype/python plus Pal/src on PYTHONPATH.
Outputs raw samples and metadata as JSON. Both adapters retain full output;
native uses file handoff (inline_limit=0). Core/model/pagination are excluded.
"""
import asyncio
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from pal.execution.shell_exec import ShellExecTool
from pal_shell_prototype import ShellRuntime, TERMINAL


def summary(samples):
    values = sorted(samples)
    return {"median_ms": statistics.median(values),
            "p95_ms": values[math.ceil(len(values) * .95) - 1],
            "max_ms": max(values), "samples_ms": samples}


async def main():
    native = ShellRuntime()
    with tempfile.TemporaryDirectory(prefix="pal-shell-comparison-") as output:
        old = ShellExecTool(shell_path="/bin/bash", output_root=Path(output))

        async def invoke(kind, command):
            if kind == "python":
                result = await old.ainvoke({"cmd": command, "timeout_ms": 10000})
                assert result.structured["returncode"] == 0, result
                return result.structured
            result = await native.run(command, timeout_ms=10000)
            assert result["returncode"] == 0 and result["session_id"] == 0, result
            return result

        report = {"platform": platform.platform(), "python": sys.version,
                  "shell": "/bin/bash -lc", "build": "use Release; caller supplies module path",
                  "scope": "ShellExecTool.ainvoke versus ShellRuntime.run; excludes Core/model",
                  "cases": {}}
        try:
            for _ in range(3):
                for kind in ("python", "native"):
                    await invoke(kind, "true")
            cases = [("true", "true", 50, 0), ("stdout_64KiB", "head -c 65536 /dev/zero", 20, 65536),
                     ("stdout_8MiB", "head -c 8388608 /dev/zero", 10, 8388608)]
            for name, command, repeats, output_size in cases:
                values = {kind: {"wall": [], "host_cpu": []} for kind in ("python", "native")}
                for index in range(repeats):
                    order = ("python", "native") if index % 2 == 0 else ("native", "python")
                    for kind in order:
                        cpu, wall = time.process_time(), time.perf_counter()
                        result = await invoke(kind, command)
                        values[kind]["wall"].append((time.perf_counter() - wall) * 1000)
                        values[kind]["host_cpu"].append((time.process_time() - cpu) * 1000)
                        if kind == "native":
                            assert result["stdout_total"] == output_size
                            assert len(result["stdout_bytes"]) == output_size
                        else:
                            assert len(result["stdout"]) == output_size
                report["cases"][name] = {
                    kind: {metric: summary(samples) for metric, samples in measurements.items()}
                    for kind, measurements in values.items()}

            report["heartbeat"] = {}
            for kind in ("python", "native"):
                lags = []
                stop = asyncio.Event()
                async def heartbeat():
                    while not stop.is_set():
                        start = time.perf_counter()
                        await asyncio.sleep(.005)
                        lags.append(max(0, (time.perf_counter() - start - .005) * 1000))
                ticker = asyncio.create_task(heartbeat())
                for _ in range(5):
                    await invoke(kind, "sleep .2")
                stop.set()
                await ticker
                report["heartbeat"][kind] = summary(lags)

            start = time.perf_counter()
            result = await native.run("sleep .3; printf done", wait_ms=10)
            report["background"] = {"initial_response_ms": (time.perf_counter() - start) * 1000,
                                    "initial_status": result["status"]}
            assert result["session_id"]
            terminal = await native.read(result["session_id"], wait_ms=5000)
            assert terminal["status"] in TERMINAL and terminal["stdout"] == "done"
            report["background"]["terminal_response_ms"] = (time.perf_counter() - start) * 1000
            print(json.dumps(report, indent=2))
        finally:
            await native.close()


if __name__ == "__main__":
    asyncio.run(main())
