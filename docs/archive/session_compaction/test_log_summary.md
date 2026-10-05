# Historical test log summary

These results belong to the session-compaction work recorded in [DELIVERY.md](DELIVERY.md).
They are historical evidence, not a regression result for the current checkout.
Failed runs remain identified as failed; individual reruns do not establish a
successful full-suite run. See the delivery report for the original analysis.

The 17 raw root-level logs were moved byte-for-byte to the ignored local directory
`test-logs/legacy/`. New test output belongs under `test-logs/` or outside the
checkout, not in version control. Full original logs also remain available in Git
at commit `c477be47e5b6f63f016f06a4dfc5925a6ab2acc5`:

```sh
git show c477be47e5b6f63f016f06a4dfc5925a6ab2acc5:logs_full_regression.txt
```

For logs containing multiple single-test runs, every pytest result footer is
retained below. Some red-baseline files contain handwritten failure observations
rather than pytest output; their original text remains in the raw log.

| Original root filename | Recorded pytest result(s) |
| --- | --- |
| `logs_flake_rerun_af51d74fix.txt` | 20 passed, 2 warnings, 6 subtests passed in 7.89s |
| `logs_full_regression.txt` | 22 failed, 3050 passed, 7 skipped, 48 warnings, 486 subtests passed in 1575.64s (0:26:15) |
| `logs_full_regression_4b14ce4_review_fixes.txt` | 22 failed, 3098 passed, 7 skipped, 48 warnings, 533 subtests passed in 1377.37s (0:22:57) |
| `logs_full_regression_7d182fd_s1_seam_fix.txt` | 22 failed, 3100 passed, 7 skipped, 48 warnings, 557 subtests passed in 1359.85s (0:22:39) |
| `logs_full_regression_95373ef_review_fixes.txt` | 22 failed, 3134 passed, 8 skipped, 48 warnings, 491 subtests passed in 1356.59s (0:22:36) |
| `logs_full_regression_af51d74fix.txt` | 25 failed, 3134 passed, 7 skipped, 48 warnings, 491 subtests passed in 1547.80s (0:25:47) |
| `logs_full_regression_c9cb2d2_review_fixes.txt` | 22 failed, 3094 passed, 7 skipped, 48 warnings, 498 subtests passed in 1371.83s (0:22:51) |
| `logs_full_regression_idle_rerun.txt` | 22 failed, 3071 passed, 7 skipped, 48 warnings, 491 subtests passed in 1454.48s (0:24:14) |
| `logs_full_regression_n1head.txt` | 22 failed, 3098 passed, 7 skipped, 48 warnings, 486 subtests passed in 1512.85s (0:25:12) |
| `logs_full_regression_n4head.txt` | 22 failed, 3124 passed, 7 skipped, 48 warnings, 491 subtests passed in 1577.99s (0:26:17) |
| `logs_full_regression_wholesource_removal.txt` | 24 failed, 3069 passed, 7 skipped, 48 warnings, 491 subtests passed in 1412.22s (0:23:32) |
| `logs_n11_prefix_red.txt` | Captured failure notes; no pytest result footer. |
| `logs_n1_flake_rerun.txt` | 1 passed, 2 warnings in 6.07s; 1 passed, 2 warnings in 6.37s; 1 passed, 2 warnings in 5.16s; 1 passed, 2 warnings in 5.42s; 1 passed, 2 warnings in 5.83s; 1 passed, 2 warnings in 3.62s; 1 passed, 2 warnings in 5.59s; 1 passed, 2 warnings, 3 subtests passed in 4.84s; 1 passed, 2 warnings in 5.05s; 1 passed, 2 warnings in 4.23s; 1 passed, 2 warnings in 4.51s; 1 passed, 2 warnings in 4.89s; 1 passed, 2 warnings in 5.10s; 1 passed, 2 warnings in 5.07s; 1 passed, 2 warnings in 5.17s; 1 passed, 2 warnings in 5.16s; 1 passed, 2 warnings in 5.68s; 1 passed, 2 warnings in 3.89s; 1 passed, 2 warnings in 3.57s |
| `logs_n1_prefix_red.txt` | Captured failure notes; no pytest result footer. |
| `logs_n2_prefix_red.txt` | Captured failure notes; no pytest result footer. |
| `logs_n4_flake_rerun.txt` | 1 passed, 2 warnings in 5.99s; 1 passed, 2 warnings in 6.33s; 1 passed, 2 warnings in 5.92s; 1 passed, 2 warnings in 5.95s; 1 passed, 2 warnings in 6.48s; 1 passed, 2 warnings in 3.64s; 1 passed, 2 warnings in 5.52s; 1 passed, 2 warnings, 3 subtests passed in 5.45s; 1 passed, 2 warnings in 5.71s; 1 passed, 2 warnings in 4.09s; 1 passed, 2 warnings in 4.42s; 1 passed, 2 warnings in 5.83s; 1 passed, 2 warnings in 5.48s; 1 passed, 2 warnings in 5.24s; 1 passed, 2 warnings in 5.27s; 1 passed, 2 warnings in 4.91s; 1 passed, 2 warnings in 5.57s; 1 passed, 2 warnings in 3.76s; 1 passed, 2 warnings in 3.47s |
| `logs_v3_review_95373ef_draft_red_baseline.txt` | 9 failed, 2 warnings in 3.77s |

## Original log checksums

SHA-256 allows local copies to be checked against the original Git blobs.

```text
08e651400cccd80dd37afdf428d4ee2943ceae8ceac15a3566049c026d5ccb65  logs_flake_rerun_af51d74fix.txt
d49e672bc95c0784c5c2abcb3ef1948b6ffa84bac6e6bd433c103659f6a76443  logs_full_regression.txt
005bd0948d2c16bac44babef83e17291abb4907cce09875705b37d4dba44a714  logs_full_regression_4b14ce4_review_fixes.txt
d72e9f6c85a64396d28aa20cd7e28564462b8e29c74c85ec749ece62ffd9b2f2  logs_full_regression_7d182fd_s1_seam_fix.txt
529fb8d9356b2d62f73ca4b4dd516326938d4b4cfbd3f4558588c9e252084c73  logs_full_regression_95373ef_review_fixes.txt
ef41a4bb5f7e1975c4d88194fcf736e9358056ed4c74ae014acaebbfafaa154d  logs_full_regression_af51d74fix.txt
a6a7bb4cf818bff78457d801cf5f9f7ab9c8bad6bc954ef72bc85951739969d9  logs_full_regression_c9cb2d2_review_fixes.txt
79332886ec361fecc6de465d87ba0282850846625651fca989042f304684a310  logs_full_regression_idle_rerun.txt
7cf29907d4f591ce6cf60d6b97dbb341af27520b945ba167f025394a80116751  logs_full_regression_n1head.txt
6f6f2c9354cc5f14cabf4b78e48613c216c9917f01438949bb65b0c171e2a8b9  logs_full_regression_n4head.txt
e2085ba413ecdcec260123a4071148c94cfb1b115b45b8d8ff9c51c13d0a2188  logs_full_regression_wholesource_removal.txt
4beb9be34bb052457be3a0b566b27b39c0299b7bb46bef324ab2832d06d3acf8  logs_n11_prefix_red.txt
a79e2adc4489e5e4fc7bc258d55b5bb64a393299668767d69f413308b29a3e89  logs_n1_flake_rerun.txt
2c9966f4905f32809c97bb5f147446fe192945ee680c41f40d6c69a80103f756  logs_n1_prefix_red.txt
09541f36952926fdcfc86d04de0f08e054386cae28110b43f8a9202971e18fb1  logs_n2_prefix_red.txt
dfec4920d302f7f89090b339ef868769187275c15859372c66afcaeb93bff578  logs_n4_flake_rerun.txt
f88d7a08664262ea1055ad68ed4e11f76cd032b5df013afd1ebe2050c6cb23dc  logs_v3_review_95373ef_draft_red_baseline.txt
```
