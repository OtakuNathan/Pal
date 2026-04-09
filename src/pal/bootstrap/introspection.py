from __future__ import annotations

from dataclasses import dataclass

from pal.bootstrap.service import RuntimeComposer


@dataclass(frozen=True)
class BootstrapSnapshot:
    runtime_composer_ready: bool = True


def inspect_bootstrap(runtime_composer: RuntimeComposer) -> BootstrapSnapshot:
    return BootstrapSnapshot(runtime_composer_ready=runtime_composer is not None)
