from __future__ import annotations
from pal.bunshin.runner_components.result_values import _memory_candidates_from_sink
from dataclasses import dataclass, field
from typing import Any
from pal.memory import MemoryService
from pal.plugins.l3 import MockL3Plugin


@dataclass
class MemoryResults:
    run_id: str
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)
    memory_candidate_sink: MockL3Plugin | None = field(default=None, init=False, repr=False)
    memory_generation_id: str = field(default="", init=False, repr=False)
    result_memory_service: MemoryService | None = field(default=None, init=False, repr=False)

    def runner_memory_candidate_sink(self) -> MockL3Plugin:
        if self.memory_candidate_sink is not None:
            return self.memory_candidate_sink
        sink = MockL3Plugin(provider_id=f"bunshin_run_{self.run_id}_memory_candidates")
        self.memory_candidate_sink = sink
        return sink

    def bind_generation(self, generation_id: str, service: MemoryService | None) -> None:
        self.memory_generation_id = generation_id
        self.result_memory_service = service

    def collect_candidates(self, sink: MockL3Plugin) -> None:
        self.memory_candidates[:] = _memory_candidates_from_sink(sink)

    def clear_candidates(self) -> None:
        self.memory_candidates.clear()
