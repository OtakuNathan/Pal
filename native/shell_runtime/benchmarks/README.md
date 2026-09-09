> Historical result: this report measured the earlier head/tail prototype.
> That output policy has been removed. Its timings do not describe the current
> full-output implementation; see [file-handoff.md](file-handoff.md).

# Raspberry Pi shell comparison — 2026-09-09

Local Linux arm64 / CPython 3.13.5, Release C++ extension, static libuv 1.50.0.
Both adapters launch `/bin/bash -lc`. After three warmup pairs, execution order
alternates between Python and native. Commands are sequential; no resident Pal
or paid model is involved. Wall time includes spawn, execution, output handling
and adapter result delivery. Host CPU time excludes child-process CPU.

| Workload | Samples per adapter | Python median / p95 | Native median / p95 |
| --- | ---: | ---: | ---: |
| true | 50 | 34.11 / 34.44 ms | 28.70 / 30.29 ms |
| stdout_64KiB | 20 | 34.25 / 34.76 ms | 30.77 / 33.02 ms |
| stdout_8MiB | 10 | 91.92 / 96.48 ms | 50.12 / 65.06 ms |

Host CPU medians (Python → native):
- true: 2.71 → 3.15 ms.
- stdout_64KiB: 2.73 → 3.60 ms.
- stdout_8MiB: 28.33 → 18.50 ms.

During five `sleep .2` commands, a 5 ms asyncio heartbeat remained responsive
in both implementations. Its p95 scheduling lateness was
0.31 ms for Python and 0.36 ms for native.
This is a small idle-wait probe, not a production responsiveness/load test.

For `sleep .3; printf done` with a 10 ms native wait budget, the initial live-session
response arrived in 19.15 ms and the final result in
334.39 ms. Initial latency also includes startup/bridge work.

## Interpretation and limitations

- Short commands improved modestly in this run, while native host CPU was slightly higher.
- Large-output policies differ: Python writes temporary files and returns all 8 MiB;
  native drains the stream but retains only a 512 KiB head and 512 KiB tail.
  The timing therefore compares current implementations, not equal-output language speed.
- The first native run used a sliding string tail and took 121.14 ms median for
  8 MiB, versus 90.53 ms for Python. Each small read shifted the full tail.
  Replacing it with an in-place ring buffer removed this avoidable copying.
  A byte-order regression test checks repeated wraps with nonuniform output.
- This is one host/session with small samples and no control over other machine
  workloads. It does not establish model latency, power usage, RSS, production UX,
  macOS performance, or a general C++ versus Python speedup.
- Session handoff, PTY input and deferred completion are functional improvements;
  they are not proof that existing asyncio-based shell calls blocked Pal.

## Reproduce

From the repository root, build the prototype in Release mode as described in
[the runtime README](../README.md), then:

```bash
PYTHONPATH=/tmp/pal-native-shell-release:native/shell_runtime/python:src \
  python3 native/shell_runtime/benchmarks/compare.py > /tmp/pal-shell-comparison.json
```

[Raw samples and metadata](results-2026-09-09.json).
