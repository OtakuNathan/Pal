from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4
from typing import TYPE_CHECKING, Any, Callable

from pal.skill.decorators import skill
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.web_fetch.tool_models import (
    BrowserActionOutput,
    BrowserInspectionInput,
    BrowserSessionManageInput,
    BrowserCheckInput,
    BrowserClickInput,
    BrowserDialogInput,
    BrowserDragInput,
    BrowserEvaluateInput,
    BrowserFillInput,
    BrowserFindInput,
    BrowserHistoryInput,
    BrowserInspectLayoutInput,
    BrowserNetworkInput,
    BrowserPressInput,
    BrowserReadInput,
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
from pal.execution.tool_facade import (
    NextToolHint,
    ToolGuidance,
    ToolRejectedError,
)
from pal.execution.contracts import CapabilityResult
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
from pal.shared.inspection import inspection_snapshot
from pal.shared.diagnostics import exception_report
from pal.web_fetch.browser_service import BrowserServiceError, browser_session_key
from pal.web_fetch.service import WebFetchService
from pal.web_fetch.tools import BrowserScreenshotTool, browser_tool_error

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


_BROWSER_SKILL_MANUAL = """# Stateful Browser Use

Use the browser capabilities for JavaScript-rendered pages and interactive UI work.

1. Use `read_browser_page` to open a URL and read its rendered text and links in one call.
   Read beyond the preview using rg/read_file on text_file.file_path. Reuse the captured
   content; do not follow it with read_browser_page unless content is missing
   or has changed. Use read_browser_page without url to reread the current page after interaction.
2. Use `capture_browser_snapshot` or `find_browser_text` to obtain current element refs.
3. Call the narrow interaction capability such as `click_browser_target` or `fill_browser_field`.
4. Inspect the changed page again; refs may become stale after any action.
   Click results include open tabs when reported by the browser. Popups do not
   automatically become current: use manage_browser_tabs list/select, then read or snapshot.
   If a popup appears later, list tabs again; do not repeat the click blindly.
5. Use `capture_browser_screenshot` when pixel evidence is useful. When registration=registered,
   core can attach the image when the selected model supports vision; do not import it again.
   If registration is unavailable or failed, follow the result's recovery guidance for the saved
   artifact.local_cached_path. If registration=unknown, reconcile with search_artifacts before
   importing again; omit query to browse this conversation's artifacts. Recover the saved image
   rather than capturing a replacement solely because registration or inline delivery failed.

The browser profile belongs to the current conversation. `manage_browser_session(operation=close)` releases live
processes but keeps login state; `manage_browser_session(operation=clear_cache)` clears HTTP cache while retaining
cookies and site storage; `manage_browser_session(operation=reset, confirm=true)` deliberately deletes the profile. If browser
navigation or reading fails and raw HTTP is sufficient, the main Pal may use `run_shell`
with curl. Curl cannot replace clicks, JavaScript state, dialogs, or rendered layout.

Never invent element refs, automatically repeat a failed write action, expose cookies,
or use browser tools for local files. `evaluate_browser_script` runs a JavaScript function
inside the page with the logged-in origin's privileges; prefer read-only expressions and
use it only when snapshot/read/layout evidence is not enough. `manage_browser_network_capture` observes
fetch/XHR traffic via an injected hook: start it after navigation (hooks reset on every
navigation), interact with the page, then read entries. For local extension development, use manage_browser_extension to mount/reload/unmount
an unpacked Manifest V3 directory. Changes close current tabs but preserve profile data;
navigate afterward and verify content scripts or a mounted chrome-extension:// page.
Use inspect_browser_status to inspect configuration and observed service workers.
`drag_browser_target` supports inspected element refs or viewport coordinates; after a
failed drag, inspect the page before deciding whether to repeat it. For ordinary HTTP/API
requests use run_shell with curl. To reuse browser login state, evaluate_browser_script
can execute fetch within the page; browser origin/CORS rules still apply. Write requests
require authorized intent; reconcile uncertain server outcomes before retrying.
After browser recovery, refresh refs and focus before retrying an interaction.
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
        "read_browser_page", "capture_browser_snapshot", "find_browser_text",
        "click_browser_target", "fill_browser_field", "type_browser_text", "press_browser_key",
        "hover_browser_target", "select_browser_option", "set_browser_checked_state", "scroll_browser",
        "resize_browser_viewport", "navigate_browser_history", "manage_browser_tabs", "handle_browser_dialog",
        "evaluate_browser_script", "manage_browser_network_capture", "inspect_browser_layout",
        "capture_browser_screenshot", "inspect_browser_status", "manage_browser_session",
        "manage_browser_extension",
        "drag_browser_target",
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
            search_objects=('status', 'session', 'extension', 'extensions'),
            search_terms=('state', 'health', 'show', 'check', 'configuration', 'capabilities', 'worker', 'workers'),
            search_intents=('inspect browser extension configuration and observed workers',),
            purpose="Inspect browser host and current session health, extension summaries, or one selected extension's complete configured state and observed workers.",
            use_when="Omit extension_id for passive host/session health and extension summaries. Supply extension_id for that extension's complete state. Inspection does not start the sidecar or browser. Configuration and worker observations do not prove extension behavior.",
            do_not_use_when="Reading or interacting with a page.",
            failure_next_steps="If dependencies are installing, continue with other work and retry later.",
        ),
        aliases=("inspect_browser_status",),
        InputModel=BrowserInspectionInput,
        execution=INDIRECT_LOCAL_READ,
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = {**self.service.health(), "mounted": self.mounted, "degraded": self.degraded}
        sections = {}
        try:
            key, persistent = self._scope(call)
        except ValueError:
            if call.args.get("extension_id"):
                raise ToolRejectedError("Extension inspection requires a conversation scope", error_code="missing_execution_scope")
        else:
            sections["session"] = lambda: self.service.execute(
                session_key=key, action="inspect", args=dict(call.args), persistent=persistent, timeout_ms=15000)
        payload = inspection_snapshot(payload, sections=sections, source="browser_host")
        failed = bool(call.args.get("extension_id") and payload["diagnostics"]["partial"])
        return _result(RuntimeStatus.ERROR if failed else RuntimeStatus.OK, "Browser inspection", payload)


    def navigate(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        return self._action(call, "navigate", "Browser navigated")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="read",
        guidance=ToolGuidance(
            search_objects=('page', 'pages', 'text', 'link', 'links', 'url', 'urls'),
            search_terms=('show', 'get', 'fetch', 'web', 'content', 'webpage', 'webpages', 'open', 'visit', 'navigate', 'url', 'urls', 'website', 'websites'),
            purpose="Open a webpage URL and read rendered text, metadata and links, or read the current conversation browser page.",
            use_when="Supply url to open and read a webpage in one call. Omit url to reread the current page after interaction or content_status=unavailable. Reuse returned text_file and links_file snapshots for content beyond the preview; a separate read is unnecessary unless content changed or is missing.",
            do_not_use_when="Reading beyond a preview: use rg/read_file on the returned text_file. Searching the web (use search_web), reading local files, or calling an API that curl can handle directly.",
            failure_next_steps="For page_restore_failed, supply a new HTTP(S) url to skip the saved page without clearing login data. Do not edit last_url. For readable non-JavaScript content, the main Pal may use run_shell with curl. Bunshin roles must report the bounded web evidence gap instead.",
            next_tool_hints=(
                NextToolHint(name="capture_browser_snapshot", use_when="Current controls and element refs are needed before interaction."),
                NextToolHint(name="find_browser_text", use_when="Locate particular page text or controls."),
                NextToolHint(name="inspect_browser_status", use_when="Browser startup, session or extension diagnosis is needed."),
                NextToolHint(name="manage_browser_extension", use_when="Mount, reload or unmount a local Manifest V3 extension."),
            ),
        ),
        InputModel=BrowserReadInput,
        OutputModel=BrowserActionOutput,
        aliases=("read_browser_page",),
        metadata={"canonical_path": "op_browser_read", "omit_family_in_canonical": True},
        execution=DIRECT_EXTERNAL_READ,
    )
    def read(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        if self.read_delegate is not None:
            result = self.read_delegate(dict(call.args))
            return self._document_result(call, result) if result.status == RuntimeStatus.OK else result
        return self._action(call, "read", "Browser page content")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="snapshot",
        guidance=ToolGuidance(search_intents=('inspect accessibility tree and element references', 'capture page controls snapshot'),
            search_objects=('snapshot', 'snapshots', 'element', 'elements'),
            search_terms=('accessibility', 'tree', 'inspect',),
            purpose="Capture a bounded accessibility snapshot containing current element refs.",
            use_when="Locating interactive controls before acting or verifying a changed page.",
            do_not_use_when="Pixel-level evidence is required (use capture_browser_screenshot).",
            failure_next_steps="Use find_browser_text for a narrower result, or navigate to a valid page first.",
        ),
        InputModel=BrowserSnapshotInput,
        OutputModel=BrowserActionOutput,
        aliases=("capture_browser_snapshot",),
        metadata={"canonical_path": "op_browser_snapshot", "omit_family_in_canonical": True},
        execution=INDIRECT_EXTERNAL_READ,
    )
    def snapshot(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        return self._action(call, "snapshot", "Browser snapshot")

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        action_name="find",
        guidance=ToolGuidance(
            search_objects=('text', 'expression', 'expressions'),
            search_terms=('search', 'regex', 'regexp',),
            purpose="Find text or a regular expression in the current browser snapshot.",
            use_when="A full snapshot would be too large or a particular control needs locating.",
            do_not_use_when="Both text and regex are available; provide exactly one.",
            failure_next_steps="Refresh capture_browser_snapshot if the page changed, then search again.",
        ),
        InputModel=BrowserFindInput,
        examples=({"text": "Sign in"},),
        OutputModel=BrowserActionOutput,
        aliases=("find_browser_text",),
        metadata={"canonical_path": "op_browser_find", "omit_family_in_canonical": True},
        execution=INDIRECT_EXTERNAL_READ,
    )
    def find(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        return self._action(call, "find", "Browser matches")

    def _write_action(self, call: IntrospectionCall, action: str) -> IntrospectionResult:
        return self._action(call, action, f"Browser {action} completed")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="click", guidance=ToolGuidance(search_objects=('target', 'targets', 'locator', 'locators'), search_terms=('element', 'elements', 'button', 'buttons', 'link', 'links',), purpose="Click a current snapshot ref or unique locator.", use_when="The requested UI action is authorized and its target was inspected.", do_not_use_when="The target is guessed or the prior result is uncertain.", failure_next_steps="Do not retry automatically. Inspect the current page and use manage_browser_tabs list if a popup opened or the page appears unchanged; select the intended tab before reading it.", next_tool_hints=(NextToolHint(name="manage_browser_tabs", use_when="Inspect or select a popup or new tab after clicking."),)), InputModel=BrowserClickInput, OutputModel=BrowserActionOutput, aliases=("click_browser_target",), metadata={"canonical_path": "op_browser_click", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def click(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "click")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="fill", guidance=ToolGuidance(search_intents=('set input field value', 'replace text in browser form field', '替换网页输入框内容'), search_objects=('field', 'fields'), search_terms=('input', 'form', 'forms', 'submit'), purpose="Replace the value of an editable target, optionally submitting it.", use_when="Filling a known form control.", do_not_use_when="The target has not been inspected.", failure_next_steps="Do not retry automatically; inspect the current page first."), InputModel=BrowserFillInput, OutputModel=BrowserActionOutput, aliases=("fill_browser_field",), metadata={"canonical_path": "op_browser_fill", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def fill(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "fill")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="type", guidance=ToolGuidance(search_intents=('type text with keyboard into input', 'append text by typing keystrokes', '逐键输入文字'), search_objects=('text', 'element', 'elements'), search_terms=('keyboard',), purpose="Type into the currently focused editable element.", use_when="Focus is already established and keystroke-like entry matters.", do_not_use_when="A target can be filled directly.", failure_next_steps="Inspect the page before deciding whether to repeat."), InputModel=BrowserTypeInput, OutputModel=BrowserActionOutput, aliases=("type_browser_text",), metadata={"canonical_path": "op_browser_type", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def type_text(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "type")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="press", guidance=ToolGuidance(search_objects=('key', 'keys'), search_terms=('keyboard',), purpose="Press one keyboard key in the current page.", use_when="Keyboard interaction is required.", do_not_use_when="The focused target is unknown.", failure_next_steps="Inspect the page before retrying."), InputModel=BrowserPressInput, OutputModel=BrowserActionOutput, aliases=("press_browser_key",), metadata={"canonical_path": "op_browser_press", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def press(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "press")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="hover", guidance=ToolGuidance(search_objects=('target', 'targets', 'locator', 'locators'), search_terms=('element', 'elements',), purpose="Hover a current snapshot ref or unique locator.", use_when='Revealing hover-only UI. Capture a fresh browser snapshot when interaction changes visible content or makes existing refs stale.', do_not_use_when="No target has been inspected.", failure_next_steps="Capture a new snapshot after the hover."), InputModel=BrowserTargetInput, OutputModel=BrowserActionOutput, aliases=("hover_browser_target",), metadata={"canonical_path": "op_browser_hover", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def hover(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "hover")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="select", guidance=ToolGuidance(search_intents=('select dropdown option value', 'choose browser select menu item'), search_objects=('option', 'options', 'value', 'values', 'dropdown', 'dropdowns'), purpose="Select a value in a known dropdown.", use_when="A snapshot identifies a select control and desired value.", do_not_use_when="The option value is unknown.", failure_next_steps="Inspect the page before retrying."), InputModel=BrowserSelectInput, OutputModel=BrowserActionOutput, aliases=("select_browser_option",), metadata={"canonical_path": "op_browser_select", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def select(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "select")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="check", guidance=ToolGuidance(search_objects=('checkbox', 'checkboxes', 'radio', 'state', 'states'), search_terms=('uncheck', 'toggle'), search_term_rules=({'terms': ('check',), 'requires_any': ('checkbox', 'checkboxes', 'radio')},), purpose="Set a checkbox or radio target's checked state.", use_when="Ensure a known checkbox or radio control is checked or unchecked. Set checked to the desired value; its current checked value may be unknown. A current ref or known unique locator is sufficient; no preliminary state read is needed solely to learn whether it is already checked.", do_not_use_when="The desired checked value is unspecified, or the target is not a checkable control. Blindly toggling an unknown current value; this tool sets a desired value rather than inverting it.", failure_next_steps="Inspect the page before retrying."), InputModel=BrowserCheckInput, OutputModel=BrowserActionOutput, aliases=("set_browser_checked_state",), metadata={"canonical_path": "op_browser_check", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def check(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "check")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="scroll", guidance=ToolGuidance(search_terms=('move', 'up', 'down', 'left', 'right', 'wheel', 'below', 'above'), search_intents=('move down the current page',), search_objects=('page', 'pages'), purpose="Scroll the current page by wheel deltas.", use_when='More of the rendered page must be exposed. Capture a fresh browser snapshot when scrolling changes the content needed for the next interaction.', do_not_use_when="A direct target is already visible.", failure_next_steps="Take a fresh snapshot after scrolling."), InputModel=BrowserScrollInput, OutputModel=BrowserActionOutput, aliases=("scroll_browser",), metadata={"canonical_path": "op_browser_scroll", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def scroll(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "scroll")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="drag", guidance=ToolGuidance(search_intents=('drag slider captcha handle to unlock', '拖动滑块完成安全验证', 'perform drag and drop between two elements', 'press mouse at one point and move it to another'), search_objects=('slider', 'sliders', 'handle', 'handles', 'target', 'targets', 'pointer'), search_terms=('drag', 'drop', 'mousemove', 'mousedown', 'mouseup', 'swipe', 'captcha', 'puzzle'), purpose="Drag between two inspected elements, or perform a pointer drag across viewport coordinates with interpolated steps.", use_when="Slider verifications, drag-and-drop lists, and range inputs. Element mode uses start_target/end_target; coordinate mode (start_x/start_y/end_x/end_y with steps) fits slider tracks whose end position is not an element and moves the pointer through human-like intermediate points.", do_not_use_when="A click or hover suffices, or the endpoints are guessed rather than inspected.", failure_next_steps="Stale refs or moved geometry break drags; re-snapshot or re-inspect layout, then retry with fresh endpoints."), InputModel=BrowserDragInput, examples=({"start_target": "e12", "end_target": "e15"}, {"start_x": 480, "start_y": 418, "end_x": 800, "end_y": 418, "steps": 12}), OutputModel=BrowserActionOutput, aliases=("drag_browser_target",), metadata={"canonical_path": "op_browser_drag", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def drag(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        return self._write_action(call, "drag")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="resize", guidance=ToolGuidance(search_terms=('set', 'window', 'screen', 'size', 'dimensions', 'width', 'height', 'resolution'), search_intents=('set browser window dimensions',), search_objects=('viewport', 'viewports'), purpose="Resize the current browser viewport.", use_when='Checking responsive behavior at a known viewport size. Capture a fresh browser snapshot when the viewport change alters layout or element refs.', do_not_use_when="No layout change is needed.", failure_next_steps="Inspect layout or capture a snapshot after resizing."), InputModel=BrowserResizeInput, OutputModel=BrowserActionOutput, aliases=("resize_browser_viewport",), metadata={"canonical_path": "op_browser_resize", "omit_family_in_canonical": True}, execution=INDIRECT_CONTROL)
    def resize(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "resize", "Browser resized")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="history", guidance=ToolGuidance(search_enum_fields=("operation",), search_terms=("page", "pages", "refresh"), search_objects=('page', 'pages'), purpose="Go back, go forward, or reload the current browser page.", use_when='Navigating browser history without a new URL. Capture a fresh browser snapshot after history navigation before using old element refs.', do_not_use_when="A specific URL is known (use read_browser_page).", failure_next_steps="Inspect the current URL and snapshot after navigation."), InputModel=BrowserHistoryInput, OutputModel=BrowserActionOutput, aliases=("navigate_browser_history",), metadata={"canonical_path": "op_browser_history", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_READ)
    def history(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "history", "Browser history updated")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="tabs", guidance=ToolGuidance(search_enum_fields=("operation",), search_terms=("create", "open"), search_objects=('tab', 'tabs'), purpose="List, create, select, or close tabs in the current browser session.", use_when="A workflow genuinely needs multiple pages.", do_not_use_when="One page is sufficient.", failure_next_steps="List tabs to reconcile the current state."), InputModel=BrowserTabsInput, OutputModel=BrowserActionOutput, aliases=("manage_browser_tabs",), metadata={"canonical_path": "op_browser_tabs", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def tabs(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "tabs")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="dialog", guidance=ToolGuidance(search_enum_fields=("operation",), search_objects=('dialog', 'dialogs'), search_terms=('alert', 'confirm', 'prompt',), purpose="Accept or dismiss the currently open browser dialog.", use_when="A known page dialog blocks the authorized workflow.", do_not_use_when="No dialog was observed.", failure_next_steps="Inspect the page rather than retrying blindly."), InputModel=BrowserDialogInput, OutputModel=BrowserActionOutput, aliases=("handle_browser_dialog",), metadata={"canonical_path": "op_browser_dialog", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def dialog(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._write_action(call, "dialog")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="inspect_layout", guidance=ToolGuidance(search_objects=('layout', 'layouts', 'geometry'), search_terms=('css', 'style', 'styles', 'bounds', 'bounding', 'box', 'element', 'elements'), purpose="Inspect computed layout and geometry for a bounded selector on the current page.", use_when="Diagnosing CSS or rendered geometry without relying on pixels. For layout problems, compare relevant computed styles, bounding geometry and gaps before and after the change; include nested or edge-case content when the affected behavior requires it.", do_not_use_when="Only text content is needed.", failure_next_steps="Verify the selector using capture_browser_snapshot, then retry."), InputModel=BrowserInspectLayoutInput, OutputModel=BrowserActionOutput, aliases=("inspect_browser_layout",), metadata={"canonical_path": "op_browser_inspect_layout", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_READ)
    def inspect_layout(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "inspect_layout", "Browser layout inspection")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="evaluate", guidance=ToolGuidance(search_objects=('script', 'scripts', 'function', 'functions'), search_terms=('execute', 'run', 'javascript', 'js',), purpose="Evaluate a JavaScript function in the current page and return its result.", use_when="Reading page-internal state such as localStorage, sessionStorage, or JS globals that snapshots cannot expose.", do_not_use_when="The value is available via read_browser_page, capture_browser_snapshot, or inspect_browser_layout.", failure_next_steps="Check that func is a function expression such as () => document.title; navigation resets page state."), InputModel=BrowserEvaluateInput, OutputModel=BrowserActionOutput, aliases=("evaluate_browser_script",), metadata={"canonical_path": "op_browser_evaluate", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_WRITE)
    def evaluate(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        return self._action(call, "evaluate", "Browser evaluation")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="network", guidance=ToolGuidance(search_enum_fields=("operation",), search_objects=('capture', 'captures', 'request', 'requests', 'response', 'responses'), search_terms=('api', 'traffic', 'xhr'), purpose="Observe fetch/XHR API traffic of the current page through an injected JavaScript hook.", use_when='Capturing API requests, headers, or tokens the page sends; start after navigating, interact, then read entries. Navigation retires the capture hook; after navigation run operation=start and confirm installed=true before capturing more traffic.', do_not_use_when="Raw HTTP via curl is sufficient, or no page interaction is expected.", failure_next_steps="Navigation removes the hook; run operation=start again and confirm installed=true."), InputModel=BrowserNetworkInput, OutputModel=BrowserActionOutput, aliases=("manage_browser_network_capture",), metadata={"canonical_path": "op_browser_network", "omit_family_in_canonical": True}, execution=INDIRECT_EXTERNAL_READ)
    def network(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "network", "Browser network")

    def fetch(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        return self._action(call, "fetch", "Browser fetch")

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", action_name="screenshot", guidance=ToolGuidance(search_objects=('screenshot', 'screenshots'), search_terms=('image', 'picture', 'png',), purpose="Capture the current page or target as a PNG and register its artifact for automatic core delivery. Vision-capable models receive pixels within image budgets; other models receive the artifact reference.", use_when="Pixel-level visual evidence is required.", do_not_use_when="Text or geometry evidence is sufficient.", failure_next_steps="Check inspect_browser_status and page state; failed calls return no screenshot.", next_tool_hints=(NextToolHint(name="import_artifact", use_when="Automatic artifact registration was unavailable and the artifact owner has recovered; pass artifact.local_cached_path as path. Do not reimport an already registered screenshot."),)), InputModel=BrowserScreenshotInput, OutputModel=BrowserActionOutput, aliases=("capture_browser_screenshot",), metadata={"canonical_path": "op_browser_screenshot", "omit_family_in_canonical": True, "async_required": True}, execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    async def screenshot(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            key, persistent = self._scope(call)
        except ValueError as exc:
            raise ToolRejectedError(str(exc), error_code="missing_execution_scope",
                recovery_hint="Use browser tools within a conversation execution scope.") from exc
        return await BrowserScreenshotTool(self.service).ainvoke(
            dict(call.args),
            session_key=key,
            persistent=persistent,
            runtime=call.meta.get("execution_runtime"),
            turn_id=str(call.meta.get("turn_id") or "manual"),
        )

    def extensions(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "extensions", "Browser extensions")

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="module", action_name="extension_manage",
        guidance=ToolGuidance(search_enum_fields=("operation",),
            search_objects=('extension', 'extensions'),
            purpose="Mount, reload, or unmount an unpacked local Manifest V3 extension for this conversation's browser.",
            use_when='The user requests extension development or installation. For mount supply a directory containing manifest.json; for reload/unmount supply extension_id from inspect_browser_status. Unmount remains available after the source directory is deleted. After mount/reload, navigate to a test page and verify behavior.',
            do_not_use_when="Installing Chrome Web Store packages, using temporary Bunshin browser scopes, or assuming configuration proves the extension works. This closes current browser tabs; persistent login data is retained.",
            failure_next_steps="Correct manifest/path errors before retrying. After success navigate to a test page to launch with the new configuration, then verify behavior. Unmount remains available if the source directory was deleted.",
        ),
        InputModel=BrowserExtensionManageInput, OutputModel=BrowserActionOutput,
        aliases=("manage_browser_extension",), execution=INDIRECT_UNSAFE_LOCAL_WRITE,
        examples=({"operation": "mount", "path": "/tmp/pal-test-extension"},),
    )
    def extension_manage(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "extension_manage", "Browser extension configuration updated")

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="module", action_name="manage_session",
        guidance=ToolGuidance(
            search_objects=('session', 'sessions', 'cache', 'profile'),
            search_terms=('close', 'shutdown', 'clear', 'http', 'reset', 'cookies', 'login'),
            search_enum_fields=('operation',),
            purpose="Close the conversation browser, clear its HTTP cache, or explicitly reset its profile.",
            use_when="Choose close to release the browser while keeping login/profile; clear_cache to clear HTTP cache while retaining cookies and site storage; reset to close and delete the profile, with confirm=true. No operation is chosen implicitly.",
            do_not_use_when="Ordinary page navigation or failure recovery. Cache clearing does not log out or delete service-worker CacheStorage. Reset is only for intentional profile deletion.",
            failure_next_steps="Inspect browser health and the actual outcome before retrying; never escalate cache cleanup to profile reset automatically.",
        ), InputModel=BrowserSessionManageInput, OutputModel=BrowserActionOutput,
        aliases=("manage_browser_session",), execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    def manage_session(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        operation = call.args["operation"]
        if operation == "reset":
            return self.reset(call)
        return self._action(call, operation, "Browser session maintenance")

    def clear_cache(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "clear_cache", "Browser HTTP cache cleared")

    def close(self, call: IntrospectionCall) -> IntrospectionResult:
        return self._action(call, "close", "Browser closed")

    def reset(self, call: IntrospectionCall) -> IntrospectionResult:
        if call.args.get("confirm") is not True:
            raise ToolRejectedError("confirm must be true", error_code="confirmation_required",
                recovery_hint="Set confirm=true only if clearing the conversation's cookies, login state, and browser profile is intended.")
        return self._action(call, "reset", "Browser profile reset")

    def _action(self, call: IntrospectionCall, action: str, title: str) -> IntrospectionResult | CapabilityResult:
        try:
            key, persistent = self._scope(call)
        except ValueError as exc:
            raise ToolRejectedError(str(exc), error_code="missing_execution_scope",
                recovery_hint="Use browser tools within a conversation execution scope.") from exc
        try:
            payload = self.service.execute(
                session_key=key,
                action=action,
                args=dict(call.args),
                persistent=persistent,
                timeout_ms=int(call.args.get("timeout_ms") or 15000),
            )
        except BrowserServiceError as exc:
            worker_scope = bool(call.meta.get("broker_run_id"))
            raise browser_tool_error(
                action, exc, allow_curl=not worker_scope, worker_scope=worker_scope,
            ) from exc
        if action in {"navigate", "read", "fetch"}:
            result = IntrospectionResult(status=RuntimeStatus.OK, text=title, structured=payload, llm_text=title)
            if call.meta.get("broker_run_id"):
                # The receiving worker owns the readable output file. Host paths
                # are not usable inside its sandbox; transport raw text once.
                return replace(result, llm_text="Browser content captured for worker delivery")
            return self._document_result(call, result)
        if "_full_text" in payload or "_full_files" in payload:
            result = IntrospectionResult(status=RuntimeStatus.OK, text=title, structured=payload, llm_text=title)
            return self._document_result(call, result, nested_document=False)
        return _result(RuntimeStatus.OK, title, payload)

    def _document_result(self, call: IntrospectionCall, result: IntrospectionResult, *, nested_document: bool = True) -> IntrospectionResult | CapabilityResult:
        payload = dict(result.structured or {})
        document_key = "response" if nested_document and "response" in payload and "document" not in payload else "document"
        document = dict(payload.get(document_key) or {}) if nested_document else payload
        files = dict(document.pop("_full_files", {}) or {})
        if "_full_text" in document:
            files["text_file"] = document.pop("_full_text")
        if not files:
            return replace(result, llm_text=render_titled_structured_for_llm(result.text, payload))
        refs = []
        runtime = call.meta.get("execution_runtime")
        for label, text in files.items():
            try:
                if runtime is None:
                    raise RuntimeError("Output storage is unavailable for this caller")
                call_id = getattr(call.meta.get("tool_call"), "call_id", None) or uuid4().hex
                lifetime = runtime.logical_context_for_turn(call.meta.get("turn_id") or call_id).execution_lifetime_id
                ref = runtime.result_snapshots.capture(text, call_id=call_id, lifetime=lifetime,
                    coverage=f"captured {label.removesuffix('_file')} content")
                refs.append(ref)
                document[label] = {"file_path": ref.path, "size_bytes": ref.size_bytes,
                                  "sha256": ref.digest, "read_only": True}
            except (OSError, RuntimeError) as exc:
                document[label + "_error"] = exception_report(exc)
                # The browser already returned the full capture. Keep it for
                # ordinary result budgeting when dedicated storage fails.
                document[label + "_content"] = text
        document["next_step"] = (
            "Use rg/read_file on the returned content files for captured output beyond the preview. "
            "These are snapshots; element refs may be stale after page changes; "
            "do not repeat actions or scripts just to recover output."
        )
        if len(refs) != len(files):
            document["next_step"] += " Saving one or more content files failed; their full captures are included in this result for normal output delivery. Resolve output storage before requesting further captures."
        if nested_document:
            payload[document_key] = document
        return CapabilityResult(status=result.status, text=result.text, structured=payload,
                       llm_text=render_titled_structured_for_llm(result.text, payload), snapshot_refs=tuple(refs))

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
