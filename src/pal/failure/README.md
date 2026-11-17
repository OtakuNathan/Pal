# failure

Owns failure classification, recovery policy, safe-mode prompts, bounded repair
and verification, diagnostic reports and user feedback. Recovery declares a
sanitized tool surface appropriate to the failure domain; tool observations are
identified by call ID and credential-redacted before model projection.

The module publishes `FAILURE_RECOVERY`. Core coordinates the call and supplies
model/tool execution and diagnostic facilities through `CORE_CONTROL`; Core does
not build recovery prompts, select repair tools or write memory projections.

Successful repairs become generic `MemoryCase` requests. Memory owns the L3
commit and L2 projection. A failed or unavailable save is reported separately
from the successful repair; it never creates a synthetic committed L2 case.
