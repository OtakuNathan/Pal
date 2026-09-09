from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from pal.behavior.decorators import skill
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.web_fetch.tool_models import (
    BrowserActionOutput,
    BrowserCheckInput,
    BrowserClickInput,
    BrowserDialogInput,
    BrowserEvaluateInput,
    BrowserFillInput,
    BrowserFindInput,
    BrowserHistoryInput,
    BrowserInspectLayoutInput,
    BrowserNavigateInput,
    BrowserNetworkInput,
    BrowserPressInput,
    BrowserReadInput,
    BrowserResetInput,
    BrowserExtensionManageInput,
    BrowserResizeInput,
    BrowserScreenshotInput,
    BrowserScrollInput,
    BrowserSelectInput,
    BrowserSnapshotInput,
    BrowserTabsInput,
    BrowserTargetInput,
    BrowserTypeInput,
)
from pal.execution.tool_facade import NextToolHint, ToolGuidance
from pal.execution.tool_semantics import (
    DIRECT_EXTERNAL_READ,
    INDIRECT_CONTROL,
    INDIRECT_EXTERNAL_READ,
    INDIRECT_EXTERNAL_WRITE,
    INDIRECT_LOCAL_READ,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.web_fetch.browser_service import BrowserServiceError, browser_session_key
from pal.web_fetch.service import WebFetchService
from pal.web_fetch.tools import BrowserScreenshotTool

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


_BROWSER_SKILL_MANUAL = """# Stateful Browser Use

Use the browser capabilities for JavaScript-rendered pages and interactive UI work.

1. Use `browser_navigate` as the browser discovery entry point. Its next-tool hints lead to
   page inspection and extension tools via `read_tool` / `call_tool`. If the current page
   already suffices, discover and call `browser_read` directly; no extra navigation is required.
2. Use `browser_snapshot` or `browser_find` to obtain current element refs.
3. Call the narrow interaction capability such as `browser_click` or `browser_fill`.
4. Inspect the changed page again; refs may become stale after any action.
5. Use `browser_screenshot` only when pixel evidence is useful.

The browser profile belongs to the current conversation. `browser_close` releases live
processes but keeps login state; `browser_clear_cache` clears HTTP cache while retaining
cookies and site storage; `browser_reset` deliberately deletes the profile. If browser
navigation or reading fails and raw HTTP is sufficient, the main Pal may use `run_shell`
with curl. Curl cannot replace clicks, JavaScript state, dialogs, or rendered layout.

Never invent element refs, automatically repeat a failed write action, expose cookies,
or use browser tools for local files. `browser_evaluate` runs a JavaScript function
inside the page with the logged-in origin's privileges; prefer read-only expressions and
use it only when snapshot/read/layout evidence is not enough. `browser_network` observes
fetch/XHR traffic via an injected hook: start it after navigation (hooks reset on every
navigation), interact with the page, then read entries. For local extension development, use browser_extension_manage to mount/reload/unmount
an unpacked Manifest V3 directory. Changes close current tabs but preserve profile data;
navigate afterward and verify content scripts or a mounted chrome-extension:// page.
Use browser_extensions to inspect configuration and observed service workers.
Uploads, cookie/storage editing,
request interception or modification, traces, videos, PDF and the Playwright dashboard
are not part of this capability surface.
"""


@dataclass(frozen=True)
class WebFetchModuleSnapshot:
    browser: dict[str, Any]
    mounted: bool = True
    degraded: bool = False


@skill(
    skill_id="pal.web.browser",
    title="Stateful Browser Use",
    summary="Navigate, inspect, and safely interact with rendered web pages in a conversation-scoped browser.",
    manual_text=_BROWSER_SKILL_MANUAL,
    activation_terms=(
        "browser", "web page", "click website", "fill form", "rendered page",
        "screenshot website", "inspect layout", "playwright",
    ),
    capability_refs=(
        "browser_navigate", "browser_read", "browser_snapshot", "browser_find",
        "browser_click", "browser_fill", "browser_type", "browser_press",
        "browser_hover", "browser_select", "browser_check", "browser_scroll",
        "browser_resize", "browser_history", "browser_tabs", "browser_dialog",
        "browser_evaluate", "browser_network", "browser_inspect_layout",
        "browser_screenshot", "browser_status", "browser_close", "browser_clear_cache", "browser_reset",
        "browser_extensions", "browser_extension_manage",
    ),
    metadata={"internal": True, "plugin_id": "web_fetch"},
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:web_fetch",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:web_fetch",
    target_kind="module",
)
@dataclass
class WebFetchIntrospectionProvider:
    service: WebFetchService
    read_delegate: Callable[[dict[str, object]], IntrospectionResult] | None = None
    module_id: str = "web_fetch"
    mounted: bool = True
    degraded: bool = False

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            purpose="Show Playwright CLI, sidecar, profile, and browser-session health.",
            use_when="Diagnosing browser startup, dependency installation, or session failures.",
            do_not_use_when="Reading or interacting with a page.",
            failure_next_steps="If dependencies are installing, continue with other work and retry later.",
        ),
        aliases=("browser_status",),
        execution=INDIRECT_LOCAL_READ,
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.service.health()
        payload.update({"mounted": self.mounted, "degraded": self.degraded})
        return _result(RuntimeStatus.OK, "Browser status", payload)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="navigate",
        guidance=ToolGuidance(
            purpose="Open a URL in the current conversation's full Chromium browser. It supports interactive pages and loading local Manifest V3 extensions, including extensions you develop and test here.",
            use_when="Starting an interactive browser workflow or changing pages.",
            do_not_use_when="Only raw HTTP/API content is needed.",
            failure_next_steps="An explicit URL skips saved-page restoration. For page_restore_failed, provide a new HTTP(S) URL; do not edit last_url or reset login data. For startup failures inspect browser_status. For target-page failures check the URL/network; the main Pal may use run_shell with curl when raw HTTP content suffices.",
            next_tool_hints=(
                NextToolHint(name="browser_read", use_when="Read rendered text, metadata, and links; omit url to use the current page."),
                NextToolHint(name="browser_snapshot", use_when="Inspect controls and obtain element refs before interacting."),
                NextToolHint(name="browser_find", use_when="Locate specific text or controls without a full snapshot."),
                NextToolHint(name="browser_status", use_when="Inspect browser health or diagnose startup failures."),
                NextToolHint(name="browser_extension_manage", use_when="Mount, reload, or unmount a local Manifest V3 extension you are developing or using."),
                NextToolHint(name="browser_extensions", use_when="Inspect configured extensions and observed service workers."),
            ),
        ),
        InputModel=BrowserNavigateInput,
        OutputModel=BrowserActionOutput,
        aliases=("browser_navigate",),
        metadata={"canonical_path": "op_browser_navigate", "omit_family_in_canonical": True},
        execution=DIRECT_EXTERNAL_READ,
    )
    def navigate(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "navigate", "Browser navigated")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="read",
        guidance=ToolGuidance(
            purpose="Read rendered text, metadata, and links from the current conversation's browser page.",
            use_when="Reading a specific rendered page; provide url to navigate first or omit it to read the current page.",
            do_not_use_when="Searching the web (use search_web), reading local files, or calling an API that curl can handle directly.",
            failure_next_steps="For page_restore_failed, supply a new HTTP(S) url here or use browser_navigate; this skips the saved page without clearing login data. Do not edit last_url. For readable non-JavaScript content, the main Pal may use run_shell with curl. Bunshin roles must report the bounded web evidence gap instead.",
        ),
        InputModel=BrowserReadInput,
        OutputModel=BrowserActionOutput,
        aliases=("browser_read",),
        metadata={"canonical_path": "op_browser_read", "omit_family_in_canonical": True},
        execution=INDIRECT_EXTERNAL_READ,
    )
    def read(self, call: IntrospectionCall) -> IntrospectionResult:
        if self.read_delegate is not None:
            return self.read_delegate(dict(call.args))
        return self._action(call, "read", "Browser page content")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="snapshot",
        guidance=ToolGuidance(
            purpose="Capture a bounded accessibility snapshot containing current element refs.",
            use_when="Locating interactive controls before acting or verifying a changed page.",
            do_not_use_when="Pixel-level evidence is required (use browser_screenshot).",
            failure_next_steps="Use browser_find for a narrower result, or navigate to a valid page first.",
        ),
        InputModel=BrowserSnapshotInput,
        OutputModel=BrowserActionOutput,
        aliases=("browser_snapshot",),
        metadata={"canonical_path": "op_browser_snapshot", "omit_family_in_canonical": True},
        execution=INDIRECT_EXTERNAL_READ,
    )
    def snapshot(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "snapshot", "Browser snapshot")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="find",
        guidance=ToolGuidance(
            purpose="Find text or a regular expression in the current browser snapshot.",
            use_when="A full snapshot would be too large or a particular control needs locating.",
            do_not_use_when="Both text and regex are available; provide exactly one.",
            failure_next_steps="Refresh browser_snapshot if the page changed, then search again.",
        ),
        InputModel=BrowserFindInput,
        OutputModel=BrowserActionOutput,
        aliases=("browser_find",),
        metadata={"canonical_path": "op_browser_find", "omit_family_in_canonical": True},
        execution=INDIRECT_EXTERNAL_READ,
    )
    def find(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "find", "Browser matches")

    def _write_action(self, call: IntrospectionCall, action: str) -> IntrospectionResult:
        return self._action(call, action, f"Browser {action} completed")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="click", guidance=ToolGuidance(purpose="Click a current snapshot ref or unique locator.", use_when="The requested UI action is authorized and its target was inspected.", do_not_use_when="The target is guessed or the prior result is uncertain.", failure_next_steps="Do not retry automatically; inspect the current page first."), InputModel=BrowserClickInput, OutputModel=BrowserActionOutput, aliases=("browser_click",), metadata={"canonical_path": "op_browser_click", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def click(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "click")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="fill", guidance=ToolGuidance(purpose="Replace the value of an editable target, optionally submitting it.", use_when="Filling a known form control.", do_not_use_when="The target has not been inspected.", failure_next_steps="Do not retry automatically; inspect the current page first."), InputModel=BrowserFillInput, OutputModel=BrowserActionOutput, aliases=("browser_fill",), metadata={"canonical_path": "op_browser_fill", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def fill(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "fill")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="type", guidance=ToolGuidance(purpose="Type into the currently focused editable element.", use_when="Focus is already established and keystroke-like entry matters.", do_not_use_when="A target can be filled directly.", failure_next_steps="Inspect the page before deciding whether to repeat."), InputModel=BrowserTypeInput, OutputModel=BrowserActionOutput, aliases=("browser_type",), metadata={"canonical_path": "op_browser_type", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def type_text(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "type")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="press", guidance=ToolGuidance(purpose="Press one keyboard key in the current page.", use_when="Keyboard interaction is required.", do_not_use_when="The focused target is unknown.", failure_next_steps="Inspect the page before retrying."), InputModel=BrowserPressInput, OutputModel=BrowserActionOutput, aliases=("browser_press",), metadata={"canonical_path": "op_browser_press", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def press(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "press")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="hover", guidance=ToolGuidance(purpose="Hover a current snapshot ref or unique locator.", use_when="Revealing hover-only UI.", do_not_use_when="No target has been inspected.", failure_next_steps="Capture a new snapshot after the hover."), InputModel=BrowserTargetInput, OutputModel=BrowserActionOutput, aliases=("browser_hover",), metadata={"canonical_path": "op_browser_hover", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def hover(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "hover")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="select", guidance=ToolGuidance(purpose="Select a value in a known dropdown.", use_when="A snapshot identifies a select control and desired value.", do_not_use_when="The option value is unknown.", failure_next_steps="Inspect the page before retrying."), InputModel=BrowserSelectInput, OutputModel=BrowserActionOutput, aliases=("browser_select",), metadata={"canonical_path": "op_browser_select", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def select(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "select")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="check", guidance=ToolGuidance(purpose="Set a checkbox or radio target's checked state.", use_when="A known checkable control must change.", do_not_use_when="The target state is unknown.", failure_next_steps="Inspect the page before retrying."), InputModel=BrowserCheckInput, OutputModel=BrowserActionOutput, aliases=("browser_check",), metadata={"canonical_path": "op_browser_check", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def check(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "check")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="scroll", guidance=ToolGuidance(purpose="Scroll the current page by wheel deltas.", use_when="More of the rendered page must be exposed.", do_not_use_when="A direct target is already visible.", failure_next_steps="Take a fresh snapshot after scrolling."), InputModel=BrowserScrollInput, OutputModel=BrowserActionOutput, aliases=("browser_scroll",), metadata={"canonical_path": "op_browser_scroll", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def scroll(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "scroll")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="resize", guidance=ToolGuidance(purpose="Resize the current browser viewport.", use_when="Checking responsive behavior at a known viewport size.", do_not_use_when="No layout change is needed.", failure_next_steps="Inspect layout or capture a snapshot after resizing."), InputModel=BrowserResizeInput, OutputModel=BrowserActionOutput, aliases=("browser_resize",), metadata={"canonical_path": "op_browser_resize", "omit_family_in_canonical": True}, execution=INDIRECT_CONTROL)
    def resize(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "resize", "Browser resized")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="history", guidance=ToolGuidance(purpose="Go back, go forward, or reload the current browser page.", use_when="Navigating browser history without a new URL.", do_not_use_when="A specific URL is known (use browser_navigate).", failure_next_steps="Inspect the current URL and snapshot after navigation."), InputModel=BrowserHistoryInput, OutputModel=BrowserActionOutput, aliases=("browser_history",), metadata={"canonical_path": "op_browser_history", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_READ)
    def history(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "history", "Browser history updated")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="tabs", guidance=ToolGuidance(purpose="List, create, select, or close tabs in the current browser session.", use_when="A workflow genuinely needs multiple pages.", do_not_use_when="One page is sufficient.", failure_next_steps="List tabs to reconcile the current state."), InputModel=BrowserTabsInput, OutputModel=BrowserActionOutput, aliases=("browser_tabs",), metadata={"canonical_path": "op_browser_tabs", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def tabs(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "tabs")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="dialog", guidance=ToolGuidance(purpose="Accept or dismiss the currently open browser dialog.", use_when="A known page dialog blocks the authorized workflow.", do_not_use_when="No dialog was observed.", failure_next_steps="Inspect the page rather than retrying blindly."), InputModel=BrowserDialogInput, OutputModel=BrowserActionOutput, aliases=("browser_dialog",), metadata={"canonical_path": "op_browser_dialog", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def dialog(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "dialog")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="inspect_layout", guidance=ToolGuidance(purpose="Inspect computed layout and geometry for a bounded selector on the current page.", use_when="Diagnosing CSS or rendered geometry without relying on pixels.", do_not_use_when="Only text content is needed.", failure_next_steps="Verify the selector using browser_snapshot, then retry."), InputModel=BrowserInspectLayoutInput, OutputModel=BrowserActionOutput, aliases=("browser_inspect_layout",), metadata={"canonical_path": "op_browser_inspect_layout", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_READ)
    def inspect_layout(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "inspect_layout", "Browser layout inspection")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="evaluate", guidance=ToolGuidance(purpose="Evaluate a JavaScript function in the current page and return its result.", use_when="Reading page-internal state such as localStorage, sessionStorage, or JS globals that snapshots cannot expose.", do_not_use_when="The value is available via browser_read, browser_snapshot, or browser_inspect_layout.", failure_next_steps="Check that func is a function expression such as () => document.title; navigation resets page state."), InputModel=BrowserEvaluateInput, OutputModel=BrowserActionOutput, aliases=("browser_evaluate",), metadata={"canonical_path": "op_browser_evaluate", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def evaluate(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "evaluate", "Browser evaluation")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="network", guidance=ToolGuidance(purpose="Observe fetch/XHR API traffic of the current page through an injected JavaScript hook.", use_when="Capturing API requests, headers, or tokens the page sends; start after navigating, interact, then read entries.", do_not_use_when="Raw HTTP via curl is sufficient, or no page interaction is expected.", failure_next_steps="Navigation removes the hook; run operation=start again and confirm installed=true."), InputModel=BrowserNetworkInput, OutputModel=BrowserActionOutput, aliases=("browser_network",), metadata={"canonical_path": "op_browser_network", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_READ)
    def network(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "network", "Browser network")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="screenshot", guidance=ToolGuidance(purpose="Capture the current page or target as a managed image artifact.", use_when="Pixel-level visual evidence is required.", do_not_use_when="Text or geometry evidence is sufficient.", failure_next_steps="Check browser_status and page state; failed calls return no artifact."), InputModel=BrowserScreenshotInput, OutputModel=BrowserActionOutput, aliases=("browser_screenshot",), metadata={"canonical_path": "op_browser_screenshot", "omit_family_in_canonical": True, "async_required": True}, execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    async def screenshot(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            key, persistent = self._scope(call)
        except ValueError as exc:
            return _result(RuntimeStatus.INVALID, "Browser screenshot failed", {"error": {"code": "missing_execution_scope", "message": str(exc)}})
        return await BrowserScreenshotTool(self.service).ainvoke(
            dict(call.args),
            session_key=key,
            persistent=persistent,
            runtime=call.meta.get("execution_runtime"),
            turn_id=str(call.meta.get("turn_id") or "manual"),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="extensions",
        guidance=ToolGuidance(
            purpose="Inspect configured local browser extensions and observed extension service workers.",
            use_when="Developing an extension or checking its configuration, ID, permissions, and manifest URL.",
            do_not_use_when="Treating a configured entry or missing worker as proof of extension success or failure.",
            failure_next_steps="Use browser_status for browser health. Verify content scripts on the target page and open the mounted extension's manifest URL to check loading.",
        ),
        OutputModel=BrowserActionOutput, aliases=("browser_extensions",),
        execution=INDIRECT_LOCAL_READ,
    )
    def extensions(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "extensions", "Browser extensions")

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="module", action_name="extension_manage",
        guidance=ToolGuidance(
            purpose="Mount, reload, or unmount an unpacked local Manifest V3 extension for this conversation's browser.",
            use_when="The user requests extension development or installation. For mount supply a directory containing manifest.json; for reload/unmount supply extension_id from browser_extensions.",
            do_not_use_when="Installing Chrome Web Store packages, using temporary Bunshin browser scopes, or assuming configuration proves the extension works. This closes current browser tabs; persistent login data is retained.",
            failure_next_steps="Correct manifest/path errors before retrying. After success navigate to a test page to launch with the new configuration, then verify behavior. Unmount remains available if the source directory was deleted.",
        ),
        InputModel=BrowserExtensionManageInput, OutputModel=BrowserActionOutput,
        aliases=("browser_extension_manage",), execution=INDIRECT_UNSAFE_LOCAL_WRITE,
        examples=({"operation": "mount", "path": "/tmp/pal-test-extension"},),
    )
    def extension_manage(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "extension_manage", "Browser extension configuration updated")

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="module", action_name="clear_cache",
        guidance=ToolGuidance(
            purpose="Clear the current conversation browser's HTTP cache, retaining cookies and site storage.",
            use_when="The user requests browser cache cleanup or stale cached resources need to be discarded.",
            do_not_use_when="Logging out, deleting cookies, removing service-worker CacheStorage, or resetting the profile. Cache clearing does not fix an unreachable URL.",
            failure_next_steps="Inspect browser_status on failure. A failed result does not confirm cleanup; do not reset the profile. Navigate or reload afterward only when the task needs it.",
        ),
        OutputModel=BrowserActionOutput, aliases=("browser_clear_cache",),
        metadata={"canonical_path": "op_browser_clear_cache", "omit_family_in_canonical": True},
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    def clear_cache(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "clear_cache", "Browser HTTP cache cleared")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="close", guidance=ToolGuidance(purpose="Close the current conversation's live browser while retaining its profile.", use_when="The live browser is no longer needed but login state should remain.", do_not_use_when="The profile must also be removed (use browser_reset).", failure_next_steps="Closing an already closed session is harmless."), OutputModel=BrowserActionOutput, aliases=("browser_close",), metadata={"canonical_path": "op_browser_close", "omit_family_in_canonical": True}, execution=INDIRECT_CONTROL)
    def close(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "close", "Browser closed")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="reset", guidance=ToolGuidance(purpose="Close the current browser and permanently delete its conversation profile.", use_when="The user explicitly wants cookies, login state, and browser profile data cleared.", do_not_use_when="Only live resources need releasing (use browser_close).", failure_next_steps="A deleted profile cannot be recovered; navigate again to create a clean one."), InputModel=BrowserResetInput, OutputModel=BrowserActionOutput, aliases=("browser_reset",), metadata={"canonical_path": "op_browser_reset", "omit_family_in_canonical": True}, execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    def reset(self, call: IntrospectionCall) -> IntrospectionResult:
        if call.args.get("confirm") is not True:
            return _result(RuntimeStatus.INVALID, "Browser reset rejected", {"error": {"code": "confirmation_required", "message": "confirm must be true"}})
        return self._action(call, "reset", "Browser profile reset")

    def _action(self, call: IntrospectionCall, action: str, title: str) -> IntrospectionResult:
        try:
            key, persistent = self._scope(call)
            payload = self.service.execute(
                session_key=key,
                action=action,
                args=dict(call.args),
                persistent=persistent,
                timeout_ms=int(call.args.get("timeout_ms") or 15000),
            )
        except ValueError as exc:
            return _result(RuntimeStatus.INVALID, f"{title} failed", {"error": {"code": "missing_execution_scope", "message": str(exc)}})
        except BrowserServiceError as exc:
            error = exc.to_dict()
            if error["curl_applicable"] and not call.meta.get("broker_run_id"):
                error["fallback_hint"] = "Use run_shell with curl only when raw HTTP content is sufficient."
            return _result(RuntimeStatus.ERROR, f"{title} failed", {"error": error})
        return _result(RuntimeStatus.OK, title, payload)

    @staticmethod
    def _scope(call: IntrospectionCall) -> tuple[str, bool]:
        broker_run_id = str(call.meta.get("broker_run_id") or "").strip()
        if broker_run_id:
            return browser_session_key(f"bunshin:{broker_run_id}"), False
        turn_id = str(call.meta.get("turn_id") or "").strip()
        runtime = call.meta.get("execution_runtime")
        if runtime is not None and turn_id:
            context = runtime.logical_context_for_turn(turn_id)
            return browser_session_key(context.execution_lifetime_id), True
        if turn_id:
            return browser_session_key(f"local:{turn_id}"), True
        raise ValueError("browser action has no conversation execution scope")


def _result(status: str, title: str, payload: dict[str, Any]) -> IntrospectionResult:
    return IntrospectionResult(
        status=status,
        text=title.lower(),
        structured=payload,
        llm_text=render_titled_structured_for_llm(title, payload),
    )


def inspect_web_fetch(provider: WebFetchIntrospectionProvider) -> WebFetchModuleSnapshot:
    return WebFetchModuleSnapshot(
        browser=provider.service.health(),
        mounted=provider.mounted,
        degraded=provider.degraded,
    )


def register_with_core(
    context: MainContext,
    service: WebFetchService,
    *,
    read_delegate: Callable[[dict[str, object]], IntrospectionResult] | None = None,
) -> ModuleHandle:
    provider = WebFetchIntrospectionProvider(service=service, read_delegate=read_delegate)
    handle = ModuleHandle(
        module_id="web_fetch",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        ports={"web_fetch": service},
        shutdown_sync=service.shutdown_sync,
        shutdown_async=service.shutdown_async,
    )
    context.register_module(handle)
    return handle


__all__ = [
    "WebFetchIntrospectionProvider",
    "WebFetchModuleSnapshot",
    "inspect_web_fetch",
    "register_with_core",
]
