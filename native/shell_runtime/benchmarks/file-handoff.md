# Full-output file handoff — 2026-09-09

Current implementation, Linux arm64 Raspberry Pi, CPython 3.13.5, Release native
extension and static libuv 1.50.0. Both adapters run `/bin/bash -lc` and retain
complete output. Native uses file transport (`inline_limit=0`). The comparison
includes adapter materialization/cleanup but excludes Core, pagination and models.
Three warmup pairs precede samples; Python/native execution order alternates.
Commands are sequential. This is one local run, not a controlled performance study.

| Workload | Samples each | Python median / p95 | Native median / p95 |
| --- | ---: | ---: | ---: |
| true | 50 | 33.97 / 34.26 ms | 29.51 / 31.10 ms |
| stdout_64KiB | 20 | 33.97 / 34.29 ms | 31.99 / 33.52 ms |
| stdout_8MiB | 10 | 83.07 / 90.66 ms | 65.60 / 73.61 ms |

Host CPU medians (excluding child CPU):

- true: Python 2.55 ms; native 3.96 ms.
- stdout_64KiB: Python 2.55 ms; native 4.56 ms.
- stdout_8MiB: Python 19.56 ms; native 23.91 ms.

A 5 ms asyncio heartbeat during five `sleep .2` commands had p95 lateness of 0.30 ms (Python) and 0.34 ms (native).
Both remained responsive. This does not measure response under sustained load.

One `sleep .3; printf done` call with `wait_ms=10` returned its live session in 44.59 ms and the terminal result in 382.92 ms.
The wait budget starts after successful spawn; it is not an end-to-end deadline.

Both paths retain full content, but adapter result formats still differ.
No RSS, power, model latency, production UX, or macOS claim follows from this run.
The earlier head/tail results are historical and must not be used for the
current implementation. Correctness acceptance includes budget transport
boundaries, middle-page recovery after file deletion, failed handoff retry,
PTY output and close cleanup.

Reproduce from the repository root after building Release:

```bash
PYTHONPATH=/tmp/pal-native-shell-release:native/shell_runtime/python:src \
  python3 native/shell_runtime/benchmarks/compare.py > /tmp/pal-shell-comparison.json
```

[Raw samples](file-results-2026-09-09.json).
