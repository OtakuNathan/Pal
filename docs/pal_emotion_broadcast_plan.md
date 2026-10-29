# Emotion Broadcast and Tool Surface Consolidation Plan

Date: 2026-10-15
Status: Implemented into source code per the confirmed scope; see [implementation notes](pal_tool_consolidation_implementation.md) for targeted verification. Not yet installed or activated.

## Goals and Scope

Unify the emotion actions of the entity face panel and the desktop avatar into a single broadcast entry point. The model only expresses an emotion; it is not responsible for selecting devices, scheduling animation duration, or restoring the display state.

This round covers `st7789_face` and `desktop_avatar_emotion`. Both keep their independent device implementations, configuration, lifecycle, and diagnostics; what is consolidated is the model-facing action entry point. The retired OLED emotion is out of scope, and OLED status keeps its status display responsibility.

## Established Public Contract

Keep only one emotion action tool:

```json
{
  "name": "show_emotion",
  "arguments": {
    "emotion": "happy"
  }
}
```

- The only input is `emotion`; no `duration`, device selection, or channel parameters are provided.
- By default it broadcasts to all mounted, enabled emotion renderers, including multiple desktop avatar endpoints.
- The emotion vocabulary is the union of the supported values reported by these renderers, not the intersection, and the face vocabulary is not used as a global gate.
- Each renderer handles its own local aliases and state mappings, e.g. `sleeping` / `sleepy`.
- If a renderer does not recognize the emotion, it is skipped directly; its current display is not changed and it is not automatically replaced with another emotion.
- Connection, transport, and execution failures are still reported as failures and must not be swallowed as "unsupported".

The current three public action entry points are consolidated into one:

| Existing entry point | Handling |
| --- | --- |
| `show_emotion` | Keep the name, change it to unified broadcast semantics, and remove the public duration parameter |
| `show_panel_emotion` | Withdraw from the model tool surface; panel rendering remains an internal capability |
| `show_expression` | Withdraw from the model tool surface; its broadcast responsibility migrates to the unified entry point |

This is expected to reduce two resident action tools. The old names must not remain searchable public aliases, otherwise duplicate selection still occurs. Necessary internal compatibility methods may be kept, but are not published as model tools.

## Runtime Responsibilities

Each device runtime manages on its own:

- Animation duration, looping, and end timing.
- The base state restored after an animation ends.
- The priority between manual emotions and thinking, working, sleeping, and error states.
- The device's own supported expressions and emotion aliases.

Different devices are not required to use the same animation lengths. The unified emotion tool also does not re-implement the state machines devices already have. Day-to-day activity states such as thinking / working continue to be driven by runtime events, without additional model calls.

## Dispatch and Plugin Boundaries

Add an independent lightweight coordinator entry point, with rendering capability provided by the two device plugins, avoiding the face plugin holding the global broadcast tool. After the face plugin is disabled or uninstalled, the desktop avatar should still be able to use the unified entry point on its own, and vice versa.

During implementation, first determine the ownership and registration interface of the coordinator entry point, preferring a standalone plugin or an existing stable extension point. Constraints:

1. The coordinator entry point only handles registering renderers, aggregating the vocabulary, broadcasting, and collecting results.
2. Each plugin handles its own hardware, sidecar, or channel endpoint communication; face no longer iterates over and calls desktop endpoints on behalf of the desktop plugin.
3. The vocabulary is reported by the plugins, and the public tool description or allowed values are generated from the current registry; the same vocabulary is not manually duplicated across multiple modules.
4. On plugin uninstall, disable, or reload, promptly revoke old registrations to avoid duplicate broadcasts or calling objects from an old generation.
5. Generating the tool contract only reads declarations; it does not start devices or generate the vocabulary through live hardware probing.
6. A failure in one recipient does not prevent other recipients from attempting execution.

## Return Results

Return enough information to judge the outcome of this broadcast, while keeping it brief:

- Which renderers accepted the request.
- Which were skipped because they do not support the emotion.
- Which had actual failures and a brief reason.

"Accepted the request" does not mean the screen has been verified to finish rendering; do not use unconfirmed displayed success claims.

| Situation | External meaning |
| --- | --- |
| At least one recipient accepted, others unsupported | Accepted; unsupported recipients skipped |
| Partially accepted, partially failed execution | Partial success, failure information retained |
| All recipients unsupported | Not executed, reason: unsupported; must not claim it was displayed |
| No mounted, enabled recipients | Unavailable, reason: no renderers |
| All recipients failed execution | Failure, report the actual reasons |

Do not return internal IPC identifiers, registry generation, transport cursors, or full error stack traces to the model.

## Basis in the Current Implementation

- `~/.pal/plugins/community/st7789_face/face_introspection.py`: already has panel tools and `show_expression` broadcast, but the coordination logic belongs to the face, and the panel vocabulary is validated before broadcasting.
- `../desktop_avatar/plugin/desktop_avatar_emotion_introspection.py`: already has `show_emotion`, exposing duration; it currently selects the target by the current desktop session or the only available desktop endpoint.
- The new plan explicitly changes the above "current/only desktop endpoint" semantics to broadcasting to all enabled render endpoints.
- Currently face checks against files in the installation directory. Before implementation, determine the authoritative source location and packaging method; merely modifying the installation directory must not be the final deliverable.

## Implementation Order

1. Determine the module ownership of the coordination entry point, define the minimal render endpoint interface, supported vocabulary, and three result types: accepted, unsupported, failed.
2. Hook the face and desktop rendering and alias mappings into this interface, preserving each runtime's duration and state priority policies.
3. Publish a unified `show_emotion` with only the `emotion` parameter, using the union of registered vocabularies to describe available emotions.
4. Retire the other two action tools, and update related guidance, tool hints, plugin documentation, and references to the old entry points.
5. Generate the tool inventory and verify that the resident entry points have indeed been reduced from three to one; retain necessary device diagnostics.
6. Only after completing targeted regression and model tool-selection verification, schedule plugin activation.

## Acceptance Criteria

- The model sees only one emotion action tool, and the schema no longer contains duration or device selection parameters.
- Works correctly with only face installed, only desktop installed, and both installed.
- Every desktop endpoint receives exactly one broadcast, with no omissions or duplicate deliveries.
- Emotions exclusive to device A and exclusive to device B can both enter the unified entry point; unsupported devices skip and keep their original display.
- When all devices are unsupported or there are no render endpoints, no false display success is returned.
- When a single device is disconnected, other devices can still accept the request, and failures are not hidden.
- The priorities of automatic activity state, manual emotion, and system error state, as well as animation recovery behavior, remain effective.
- reload, deactivation, and uninstall leave no stale registrations; after remounting, the vocabulary union updates correctly.
- Search and model selection no longer oscillate among three similar action tools. Evaluations should cover everyday emotions and device-exclusive emotions, distinguishing retrieval hits from device execution results.

## Verification and Activation Boundaries

Writing this document to disk does not mean the implementation or live behavior has changed. During implementation, first run targeted tests covering the involved plugins and the coordination entry point; full checks are left to GitHub CI.

Before activation, read `pal.self.maintenance` as required by the repository. Updates to existing plugin code take effect via `manage_plugin(operation="reload")`; rescan only discovers metadata, and attach does not substitute for reload. If the coordination entry point involves changes to resident core / execution implementations, arrange host activation separately; defer if Pal is busy.

This plan does not proceed simultaneously with tool consolidation in other modules such as Shell or Proactive.

Decisions for other modules are recorded separately, see [Plan for Migrating Memory Backend Maintenance into the Pal CLI](pal_memory_backend_cli_plan.md).
