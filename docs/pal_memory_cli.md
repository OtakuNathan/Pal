# Memory maintenance CLI

Daily memory tools keep `recall_memory`, `remember_memory`, `update_memory`,
`forget_memory` and `inspect_memory_state`. Backend maintenance is owned by CLI;
the model directory has no equivalent maintenance aliases.

## Resident owner

Flags follow the operation. Use the actual runtime directory:

```sh
pal memory providers --runtime-root /path/to/runtime --json
pal memory inspect sqlite_vec_l3 --runtime-root /path/to/runtime --json
pal memory refresh-indexes sqlite_vec_l3 --limit 32 --retry-failed --runtime-root /path/to/runtime --json
pal memory swap sqlite_vec_l3 --runtime-root /path/to/runtime --json
pal memory swap-status JOB_ID --runtime-root /path/to/runtime --json
pal memory swap-cancel JOB_ID --runtime-root /path/to/runtime --json
pal memory swap-resume JOB_ID --runtime-root /path/to/runtime --json
```

`attach` and `detach` require a running owner and an idle Pal. Detaching the
active backend through this CLI requires a completed managed swap first.
`swap` prepares a private database in the background, synchronizes concurrent
writes and publishes a verified generation atomically. Index building follows
publication; failures retain the selected data and can be resumed. The former
`select` command has been removed. See [managed swap](pal_memory_swap.md).
`swap-cancel` persists a pause until an explicit `swap-resume`; restart and backend
registration do not restart a paused job. Committed data remains selected.
Inspection/refresh hold a generation
lease until their worker finishes, including cancellation. An owner timeout has
an unknown outcome; inspect owner state before retrying. It never triggers direct
writes from a second process. Offline `providers`/`inspect` can read the published
SQLite generation; other storage providers require their resident owner.

## Offline embedding migration

Stop the host externally before planning or running a migration. Commands hold
its runtime lease and reject a live host. Prepare a JSON patch containing only
settings to change:

```json
{"model_name":"bge-m3","model_revision":"reviewed-model-revision"}
```

Supported keys are `model_name`, `model_revision`, `text_processing_version`,
`local_base_url` and `remote_base_urls`. Revision/text-processing identifiers
identify the intended vector space; they do not download a revision or install
new text-processing code. Pin the model/backend implementation separately.
URLs must use HTTP(S) and cannot contain embedded credentials. Omitted settings
and unrelated TOML configuration are preserved.

```sh
pal memory embedding plan --patch-file /path/to/embedding.json --runtime-root /path/to/runtime --json
pal memory embedding migrate --patch-file /path/to/embedding.json --batch-size 32 --runtime-root /path/to/runtime --json
pal memory embedding status --runtime-root /path/to/runtime --json
pal memory embedding resume --runtime-root /path/to/runtime --json
```

The migration clones an unpublished memory generation. Provider/model, revision,
text-processing or configuration-fingerprint changes discard source vectors and
metadata before rebuilding, even when dimensions match. Endpoint configuration
is part of that fingerprint. It rebuilds bounded batches,
checks unchanged memory content/references and complete finite vectors, then checks
sample retrieval before publishing the candidate and updating configuration.

An incomplete journal blocks host startup, including interruption after publication
but before configuration replacement. Keep the host stopped and use `resume`.
A completed batch is retained, so recovery does not recompute it. The source
memory generation, existing workflow pins and a private configuration backup are
preserved. Journal status distinguishes completion from activation: `on_start`
means the host must next start with the new generation/configuration. An arbitrary
concurrent configuration change requires reconciliation before resume.

These commands change offline storage and future startup behavior. They do not
restart Pal or claim a running process has adopted the new configuration.
