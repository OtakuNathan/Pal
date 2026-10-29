# Browser Tool Surface Consolidation Plan

Date: 2026-10-15
Status: Implemented into source code per the confirmed scope; see the [implementation record](pal_tool_consolidation_implementation.md) for targeted verification. Not yet installed or activated.

## Design Principles

The goal is to reduce duplicate entry points and the parameter relationships the model must coordinate, while preserving clear semantics for everyday browsing operations. Reducing the number of tools does not necessarily improve how effectively the model uses them.

- When one enum can express several clear operations and the parameters are largely shared, they can be merged.
- Parameters with stable meaning that combine naturally can be kept, e.g. screenshot size and whether to capture the full page.
- When different operations require switching an entire set of required, mutually exclusive fields, defer merging.
- Recovery behavior that the runtime can infer or handle should not add model-facing parameters.
- Inspection by default queries module and current session summary; when an internal object is specified, it returns sufficient diagnostics for that object, and does not by default expand the details of every object.
- Keep state settings, evidence types, execution classifications, and failure semantics that have real meaning.

## Current Capability Baseline

According to the public capability declarations in `src/pal/web_fetch/capabilities.py`, Browser currently has 28 tools in total, of which only `navigate_browser` is a persistent direct tool and the remaining 27 are indirect tools. This is the source-code declaration count and does not represent a snapshot of the tools loaded in a running instance.

| Capability group | Existing tools | Count |
| --- | --- | ---: |
| Open and read | `navigate_browser`, `read_browser_page` | 2 |
| History navigation | `navigate_browser_history` | 1 |
| Page structure and layout | `capture_browser_snapshot`, `find_browser_text`, `inspect_browser_layout` | 3 |
| Screenshot | `capture_browser_screenshot` | 1 |
| Mouse and forms | `click_browser_target`, `hover_browser_target`, `drag_browser_target`, `scroll_browser`, `fill_browser_field`, `type_browser_text`, `press_browser_key`, `select_browser_option`, `set_browser_checked_state` | 9 |
| Tabs, viewport, dialogs | `manage_browser_tabs`, `resize_browser_viewport`, `handle_browser_dialog` | 3 |
| Script and network | `evaluate_browser_script`, `fetch_browser_url`, `manage_browser_network_capture` | 3 |
| Extensions | `inspect_browser_extensions`, `manage_browser_extension` | 2 |
| Health | `inspect_browser_status` | 1 |
| Session maintenance | `close_browser`, `clear_browser_cache`, `reset_browser` | 3 |
| Total | | **28** |

## Confirmed Consolidation Scope: 28 → 23

### Opening pages and reading body content: 2 → 1

Unify into a single page-read entry point: when a URL is provided, open and read it; when no URL is provided, read the current page. The final public name will be determined at implementation time; one persistent entry point is retained.

The two existing capabilities already share the navigation and body-extraction path underneath. Keep body, metadata, links, and the full text and link snapshot; continue using the existing file capability for content beyond the read preview.

When navigation has completed but body extraction failed, the current page should be read to recover the body content rather than navigating again. An explicit URL skips recovery from a corrupted stale page, and does not work around the problem by clearing login data.

The Bunshin/worker's existing `read_delegate` / broker path and role authorization must be preserved; the main tool surface consolidation must not expand Bunshin/worker permissions or break delegated reads.

### Module and extension diagnostics: 2 → 1

Consolidate `inspect_browser_status` and `inspect_browser_extensions` into a single inspection entry point:

- By default, return Browser runtime health, current session state, and a summary of configured extensions.
- When `extension_id` is specified, return the extension's full relevant configuration, observable run state, and diagnostic limitations.
- Module-level health checks remain usable when there is no session or a temporary Bunshin/worker does not support extensions, and do not fail wholesale because of the additional extension information.
- Do not start a browser to check status; distinguish configured state, whether the browser is running, actually observed workers, and unverified behavior.

A configured extension does not prove the extension behaves correctly; the absence of a worker does not prove extension failure either. Keep the hints about target-page and manifest verification. Extension mount / reload / unmount remains the responsibility of `manage_browser_extension` and is not merged into the diagnostic tool.

### Low-frequency session maintenance: 3 → 1

Unify into `manage_browser_session` with a single required `operation` enum:

| operation | Behavior |
| --- | --- |
| `close` | Close the current session's browser, keeping the profile and login state; idempotent success if already closed |
| `clear_cache` | Clear the HTTP cache, keeping cookies and site storage; does not imply refresh, logout, or deletion of service-worker CacheStorage |
| `reset` | Close and delete the current session's profile, including login state; continues to require the existing explicit `confirm=true` |

No implicit default maintenance action is set. Keep each action's execution constraints and error semantics; verify how existing execution classifications are represented at implementation time. The runtime's recovery and resource management paths continue to independently handle automatic fallback; profile deletion must not be introduced into ordinary failure recovery.

### Retiring the standalone browser fetch: 1 → 0

`fetch_browser_url` is removed from the model tool surface:

- Ordinary HTTP/API requests are made by the main Pal via `run_shell` calling curl.
- When reusing the browser login state is required, `evaluate_browser_script` executes fetch inside the page.
- In-page requests are still constrained by the browser's origin, cookies, and CORS rules; you cannot claim that plain curl is automatically equivalent.
- When a role has no shell or script permissions, continue to respect the original permission boundaries and report the capability gap; do not expand authorization via alternative paths.

This adjustment removes one entry point, but login-state requests require the model to generate JavaScript, so you cannot directly claim improved task accuracy. Before the removal, update the related guidance and recovery prompts; when a write request's outcome is uncertain, verify the actual state first rather than blindly retrying. Record the differences between dedicated tools and general scripting in parameter validation and error reporting, and include them in validation.

## Pending designs, not counted toward the 23-tool benefits

### Screenshot support for optional viewport size

Support the combined workflow of "resize the viewport then screenshot": when a size is provided, adjust the actual viewport first and then screenshot; when no size is provided, keep the current viewport. A size change affects responsive layouts and subsequent interactions; it is not just scaling the output image; re-fetch the page structure and valid refs when needed.

A call path that resizes without screenshotting still needs to be specified. Do not force every resize to generate an image just to remove `resize_browser_viewport`, and do not spread the size field to all session maintenance operations without design. Therefore the standalone resize is retained for now, and its removal is not counted as a confirmed benefit.

### Page snapshot and text filtering

`capture_browser_snapshot` returns the page structure and refs for interactive elements, not a pixel screenshot. If `find_browser_text` can be consolidated as a filtering capability over the same structural snapshot while preserving scope limits and valid refs, further merging is possible.

If the implementation merely becomes a snapshot/find mode branch and requires the model to coordinate two different parameter contracts, do not merge for now. This item also requires verifying the underlying semantics of the existing snapshot/find; do not presume the filtering behavior is equivalent.

## Boundaries retained this round

- `fill_browser_field`, `select_browser_option`, and `set_browser_checked_state` are not to be synthesized into a universal form tool that toggles between three parameter contracts. Setting checkbox state must not degrade into click toggling.
- `type_browser_text` and `press_browser_key` are not merged for now; the former types text into the current focus, the latter sends keys or key combinations.
- click, hover, drag, and scroll keep separate entry points, avoiding mixing targets, coordinates, and action-specific parameters.
- The structural snapshot, layout inspection, and screenshot provide control structure, style geometry, and pixel evidence respectively; do not return all content together by default.
- History navigation, tabs, dialogs, and extension management already have object-oriented enumerations; keep the existing boundaries.
- Script execution and network capture serve different purposes; do not force further merging.

## Implementation and validation

1. Align the final public names, input contracts, execution classification, and role projection before adjusting the public capabilities and search guidance; clean up old tool hints and manual references at the same time.
2. Preserve the ownership of the main-text snapshot, screenshot artifacts, session and login state, and the existing recovery paths.
3. Conduct targeted validation of open and re-read, navigation succeeding but main text failing, Bunshin/worker reads, session-less health checks, extension diagnostics, and the actual state changes of the three session maintenance operations.
4. Verify the removal of standalone fetch from the native schema, the search catalog, and the readable contracts, covering alternative flows and permission restrictions for both plain requests and browser login-state requests.
5. Use models to compare the original scheme with the consolidated scheme. Beyond first-attempt and follow-up search recall, also check operation selection, parameter validity, final page state, call rounds, and error recovery; success must not be judged solely by hits on old tool names or new entry points.
6. Run the relevant targeted tests locally, leaving full CI to GitHub. Do not claim the 23 tools are in effect or that accuracy has improved before the implementation is complete.

This document preserves the baseline and scope at review time; the implementation status of confirmed items is in the implementation record. Pending items remain out of scope for this round.

## Related module plans

- [Emotion broadcast and tool surface consolidation plan](pal_emotion_broadcast_plan.md)
- [Plan for migrating memory backend maintenance into the CLI](pal_memory_backend_cli_plan.md)
