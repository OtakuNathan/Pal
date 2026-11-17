"""Dreaming's durable failure-to-silent-turn policy."""
from dataclasses import asdict
import json

from pal.control.runtime_contracts import ControlRuntimePort
from pal.memory.dreaming.contracts import DreamingFailureNotice


def schedule_failure_reminder(host: ControlRuntimePort, notice: DreamingFailureNotice) -> str | None:
    return host.ensure_silent_once(
        proactive_id=f"memory-dreaming-failure-{notice.run_id}",
        goal="Record this dreaming failure as a pending user notice in shared L1.",
        method=(
            "The user has NOT been notified. Produce a short handoff note, in the user's "
            "language, for the next ordinary user conversation to mention before answering "
            "their message. Include the run id, failure summary and diagnostic log path. "
            "These are facts at the time of failure, not a claim about the current runtime. "
            "Output is intentionally discarded by the null channel; your turn remains in L1. "
            "Do not call tools, send a message, retry dreaming or change configuration. "
            "Do not claim the user has already been told. Failure facts: "
            + json.dumps(asdict(notice), ensure_ascii=False)
        ),
    )
