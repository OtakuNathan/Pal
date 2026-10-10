# State-machine review evidence (2026-10-09)

These reports and structured results describe the original review checkouts.
They do not establish a regression result for the current source. Historical
commands, absolute paths and log references in the reports remain unchanged.

The review findings are recorded in `review.txt`, `nightly_review.txt`,
`deep_review.txt`, `comprehensive_review.txt` and `precommit_review.txt`.
The final composite validation and its limitations are recorded in
[wide/validation.json](wide/validation.json) and
[wide/precommit-validation.json](wide/precommit-validation.json).
Structured results, model snapshots and the cleanup regression helper remain
here with the review evidence.

The 112 raw logs and one JUnit XML report were moved byte-for-byte to the ignored
local directory `test-logs/repository-cleanup-20261010/`, preserving their original
repository-relative paths underneath it. The original outputs are also available
in Git at the revision listed below; new raw output belongs under `test-logs/`.

```sh
git show 020f59e3d2f76f172c9ddab19179b212f1bbbe5b:docs/reviews/state_machine_20261009/wide/core-a-00.log
```

[raw_output_checksums.sha256](raw_output_checksums.sha256) records every relocated
output. To verify the local copies from the repository root:

```sh
(cd test-logs/repository-cleanup-20261010 && sha256sum -c ../../docs/reviews/state_machine_20261009/raw_output_checksums.sha256)
```
