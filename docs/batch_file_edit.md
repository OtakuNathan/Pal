# Batch file edits with per-item results

The public tool edits one local file:

```python
edit_file(file_path="src/example.py", edits=[
    {"old_string": "first exact block", "new_string": "replacement"},
    {"old_string": "second exact block", "new_string": ""},
])
```

`edits` must be nonempty. Each item requires both strings and may specify
`replace_all=True`; the default requires a unique match. All items match the same
original read snapshot, including when a replacement happens to contain another
item's search text. Valid items apply even when other items fail. Items passing
matching/read validation are then checked for overlap. Adjacent ranges are
allowed; all items involved in an overlap fail, including nested overlaps
and overlapping occurrences within a `replace_all` item. A failed `replace_all`
item applies none of its occurrences. Empty search text and unchanged edits fail.
If the remaining edits together make no change, no write occurs.

Every affected range must have been delivered through the existing read authority.
Validation collects all item failures before a single compare-and-swap write of
the valid subset. A stale file at commit prevents all writes. Pre-write failures
report no applied indices. A write/durability error may happen after replacement;
that result explicitly reports uncertainty and requires reading the current file
before retrying. Invalid top-level/tool-schema arguments reject the call before execution.

Results list zero-based `applied_edit_indices` and `failed_edits`, each with its
index and reason. `edit_count` counts applied items and `match_count` counts their
replacements. Partial success is an applied tool result, not a failed mutation
that the harness should retry. Only failed items need further attention.
Overlaps include conflicting indices. After a partial write, original diagnostic
ranges are explicitly labeled `original_*`; `current_match_line_ranges` locates
the failed item's search text in the resulting file. These locations do not
grant read authority or automatically retry that item.

The same UTF-8/BOM/newline handling, digest checks and advisory-lock/CAS guarantees
apply as before. Updated read authority accounts for each replacement's separate
line delta. The applied/failed report precedes the diff. Pagination of the report
alone grants no new diff coverage; unchanged
inherited ranges retain their existing provenance.

Old top-level `old_string`/`new_string` arguments are no longer a public tool
contract. Single replacements use the same array with one item. The internal
`_plan_exact_edit` helper resolves one item without writing or updating authority.
Multiple files use separate calls and do not form a cross-file transaction.

Regression cases cover partial success, all-failed batches, original-snapshot
matching, nested overlap, external modification at commit, partial-read authority,
CRLF/BOM, transformed diagnostics and post-edit authority, and runtime output
validation/effect reporting.
