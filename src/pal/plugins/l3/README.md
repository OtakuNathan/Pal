# plugins/l3

Owns:
- built-in L3 stub providers

Does not own:
- L1/L2 runtime memory
- memory service orchestration
- provider selection and publication (owned by Memory)
- plugin loading beyond the L3 contribution

Exposes:
- `NullL3Plugin`
- `MockL3Plugin`
- `SQLiteFTSL3Plugin`

The SQLite vector backend validates generation-local embedding provenance against
the catalog and configured provider. Ordinary calls never retarget vectors in
place. Private migration preparation clears derived vectors before binding a new
profile; current generations with a mismatched profile retain basic reads only.
New vectors are indexed in their write transaction, and superseded versions leave
the index in the same transaction as retirement. Maintaining an existing index
must succeed before committing; row counts are not the authority for membership.

Bunshin borrows its workflow generation read-only. Embedding configuration comes
from that generation rather than the current resident configuration. Index
maintenance belongs to the generation writer; roles do not clone the database.

Generation binding initializes and validates a private replacement before
publishing its repository and embedding state under the backend lock. Retired
repositories remain owned across close failures and are retried at binding and
shutdown. Construction failure releases the unpublished repository's connections
and reader lease before returning the error.
