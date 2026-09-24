# Worker-upgrade sample: checklist cleanup and tool efficiency

The captured request in `/tmp/pal/pal.log` included the completed cloud-worker
upgrade followed by a thank-you turn. Its history contains 20 tool rounds and
40 calls. Two rounds only updated the checklist. The final checklist tool result
reported `cleared=true, active=false` but emitted `action=check`; Telegram therefore
edited/pinned the completed card instead of deleting it.

## Changes

- Final check and fully completed upsert emit one independent channel clear event. Telegram unpins
  and deletes its tracked card; an absent target does not create a notification.
- Recalled bodies are delivered by tool results and their output snapshots only. L2 records
  remain stored, and compaction keeps its existing continuity mechanism. The
  renderer no longer silently omits selected hits after the third record.
- Recall guidance no longer requires a lookup for every ordinary tool error.
- Skill search uses real terms, not function-word substring matches. The existing
  remote manual covers upgrades as well as enrollment. Search does not demand
  injection of an unrelated manual.
- The native plugin owns summary/detail target discovery and selected-target
  refresh. Remote workers and their wire protocol are unchanged.

## Offline output comparison

These are character counts, not tokenizer counts, latency measurements, or a
claim that the model was rerun. The same captured target data was projected
through the new summary formatter and serialized consistently as compact JSON.

| Captured result | Previous JSON | Summary: all targets | Summary: cloud only |
| --- | ---: | ---: | ---: |
| Before upgrade | 4,772 | 1,684 | 279 |
| After upgrade | 6,130 | 1,905 | 500 |

Four automatically appended memory-context messages contained 22,791 characters
in total. New requests no longer append these L2-derived blocks. Existing L1
history is not rewritten; recall results themselves retain their requested bodies.
The model's browser/API detour and unnecessary memory lookups are not counted as
eliminated model rounds without a subsequent real sample.

## Verification and activation

Regression coverage includes real capability echoes through the core's streaming
and non-streaming echo conversion into Telegram, both completion operations,
thread isolation, repeated clears and delete retries; full recall pagination;
Pal/Bunshin absence of duplicate L2 prompt bodies; scenario search; and selected
remote refresh without probing unrelated targets.

The existing completed Telegram card was matched to the captured final checklist
and the bot identity, then unpinned and deleted. Core memory changes require an
external Pal restart. Updating runtime provider/plugin files alone does not mean
that code is loaded. Full CI runs separately in the background; no paid model
probe or release publication is part of this change.

Follow-up regression: a valid 4,537-character completed checklist used to be
silently dropped by core's 4,000-character echo gate. Cleanup now uses a separate
`channel_event`, while the display echo is bounded around the current work cursor.
Tests exercise both completion operations and streaming modes with a long plan,
and verify that the full tool pipeline preserves the event under an output budget.
