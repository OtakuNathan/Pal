# DeepSeek v4.1 Flash vs. GLM 5.3 Tool Recall Comparison

| Metric | GLM 5.3 | DeepSeek v4.1 Flash |
| --- | --- | --- |
| Primary tool Top-3 on first single search | 103/109 (94.5%) | 106/109 (97.2%) |
| First model response search batch recall (including parallel) | 103/109 (94.5%) | 108/109 (99.1%) |
| Final tool and invocation method correct | 109/109 | 109/109 |
| Chinese action tools and key parameters correct | 20/20 | 20/20 |
| Negative cases with no corresponding capability correctly terminated | 10/10 | 9/10 |
| Error-correction scenarios ultimately passed | 4/4 | 4/4 |
| Primary tool average model response count (including final selection) | 2.055 | 2.018 |

DeepSeek had 21 primary tool samples that issued parallel searches in the first response. Therefore, the first single search and the first parallel search batch are scored separately; parallel calls within the same turn are not counted as multiple turns.

The misses on the first single search were rebind_bunshin_task_delivery, search_artifacts, and search_tools, all of which initially chose the wrong owner domain and then recovered; among these, the former and the latter were already recalled in the first parallel search batch.

The only failure was negative-profile-delete: there is no dedicated tool for deleting a built-in Bunshin profile definition. The model did not choose a wrong alternative tool, but performed 15 searches across 6 responses, reaching the evaluation cap, and did not promptly acknowledge that no corresponding tool exists. This is a stopping-behavior failure, not an interface failure.

The catalog, questions, prompt, Top-3, source hashes, and sampling settings are all identical to the GLM round validation. The 36 role tools served only as an additional stress test and are not counted in the primary metrics. Current endpoint deepseek-v4.1-flash, with configuration model_id=deepseek-flash, provider=deepseek, wire_shape=anthropic_messages.

This is a one-run-per-model coverage evaluation based on purpose-derived questions, and cannot be treated as an independent natural-request success rate or as proof of determinism across runs. Business calls were not executed; only tools/invocation methods/schema/key parameters were verified. External dynamic contributions and code regressions are out of scope for this round.

The raw model records are saved in the locally ignored directory `test-logs/discovery-{glm53,ds41flash}-current-20261015/`; this report contains no API authentication material.
