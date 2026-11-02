from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pal.foundation import ArtifactIngestor

from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.tool_facade import EffectReceipt, ToolExecutionError, ToolGuidance
from pal.execution.tool_semantics import INDIRECT_EXTERNAL_READ, INDIRECT_EXTERNAL_WRITE, INDIRECT_UNSAFE_LOCAL_WRITE
from pal.shared import OPERATION_NAMESPACE, RuntimeStatus, capability_action, capability_node
from pal.shared.tool_protocol import EffectOutcome, RetryDirective

from .client import AndroidError, AndroidHostClient
from .devices import AndroidDeviceRouter
from .models import (ActInput, ActOutput, DevicesOutput, EmptyInput, ObserveInput,
                     QueryInput, QueryOutput, ScreenOutput, DeviceInput, AppsOutput,
                     ScreenshotInput, ScreenshotOutput)


def compact_node(node):
    """Keep hierarchy and actionable facts; omit empty/default framework fields."""
    if isinstance(node, list):
        return [compact_node(child) for child in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key in ("text", "id", "desc", "class", "bounds"):
        if node.get(key) not in (None, "", [], {}):
            out[key] = node[key]
    for key in ("clickable", "long_clickable", "checkable", "checked", "focused",
                "selected", "scrollable", "editable", "password"):
        if node.get(key) is True:
            out[key] = True
    for key in ("enabled", "visible"):
        if node.get(key) is False:
            out[key] = False
    if node.get("password"):
        for key in ("text", "desc"):
            if key in out:
                out[key] = "[password]"
    if node.get("children"):
        out["children"] = [compact_node(child) for child in node["children"]]
    return out


def screen(result: dict) -> dict:
    # Missing/malformed state is an observation failure, never an empty success.
    return ScreenOutput.model_validate({
        **{key: result[key] for key in ("package", "activity", "width", "height")},
        "tree": compact_node(result["tree"]),
        **{key: result[key] for key in ("workspace", "generation", "truncated") if key in result},
    }).model_dump()


def guidance(purpose: str, use_when: str, terms: tuple[str, ...]) -> ToolGuidance:
    return ToolGuidance(
        purpose=purpose, use_when=use_when,
        do_not_use_when="PC/browser control, arbitrary shell commands, or autonomous app business workflows.",
        search_domain="android", search_domain_aliases=("phone", "mobile"),
        search_domain_description="Observe and operate Android apps through a paired accessibility host.",
        search_objects=("phone",), search_terms=terms,
        search_intents=("操作安卓手机", "查看手机页面"),
        failure_next_steps="Inspect current state with accessibility observe, or android_screenshot when the tree is unavailable. Never blindly replay an action batch.",
    )


def success(data: dict, *, applied: bool = False, hint: str = "") -> CapabilityResult:
    return CapabilityResult(
        status=RuntimeStatus.OK, structured=data,
        llm_text=json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED) if applied else None,
        recovery_hint=hint,
    )


@capability_node(namespace=OPERATION_NAMESPACE, scope="module", kind="module",
                 source="plugin:android_browser", target_kind="module")
@dataclass
class AndroidProvider:
    client: AndroidHostClient | AndroidDeviceRouter
    module_id: str = "android_browser"
    runtime_root: Path | None = None

    def read(self, command: str, args: dict | None = None, *, device: str = "_") -> dict:
        try:
            return self.client.request(command, args, device=device)
        except AndroidError as exc:
            raise ToolExecutionError(str(exc), error_code="android_read_failed", details=exc.details,
                                     retry=RetryDirective.SAFE) from exc

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="android", action_name="devices",
                       guidance=guidance("List paired Android devices and their readiness.", "Choose a connected phone.", ("list", "devices")),
                       InputModel=EmptyInput, OutputModel=DevicesOutput, aliases=("android_devices",),
                       execution=INDIRECT_EXTERNAL_READ)
    def devices(self, call: CapabilityCall) -> CapabilityResult:
        EmptyInput.model_validate(call.args)
        result = self.read("devices")
        # Exclude pairing/transport bookkeeping from model context.
        devices = [{k: d[k] for k in ("id", "name", "model", "sdk", "online", "ready", "leased") if k in d}
                   for d in result["value"]]
        return success({"devices": devices})

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="android", action_name="apps",
                       guidance=guidance("List launchable apps and their package names.",
                                         "Find an app package before launching it.", ("apps", "packages")),
                       InputModel=DeviceInput, OutputModel=AppsOutput, aliases=("android_apps",),
                       examples=({},), execution=INDIRECT_EXTERNAL_READ)
    def apps(self, call: CapabilityCall) -> CapabilityResult:
        args = DeviceInput.model_validate(call.args)
        return success({"apps": self.read("apps", device=args.device)["value"]})

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="android", action_name="observe",
                       guidance=guidance("Read a self-contained current app accessibility snapshot.",
                                         "Inspect app, page, text, controls and bounds. Offscreen/recycled content may be absent; scroll then observe.", ("observe", "screen", "tree")),
                       InputModel=ObserveInput, OutputModel=ScreenOutput, aliases=("android_observe",), examples=({},),
                       execution=INDIRECT_EXTERNAL_READ)
    def observe(self, call: CapabilityCall) -> CapabilityResult:
        args = ObserveInput.model_validate(call.args)
        return success(screen(self.read("dump", {"all_windows": args.all_windows}, device=args.device)))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="android", action_name="screenshot",
                       guidance=guidance("Capture the owned Android desktop as a PNG and attach its conversation artifact.",
                           "Inspect pixels when accessibility is incomplete or unavailable. Requires native Pal Android. Default image pixels match viewport tap coordinates; when resized, scale coordinates to viewport_width/viewport_height. Core delivers pixels only to vision-capable models; other models need a suitable image-processing capability.",
                           ("screenshot", "image", "vision", "pixels")),
                       InputModel=ScreenshotInput, OutputModel=ScreenshotOutput, aliases=("android_screenshot",),
                       examples=({},), metadata={"async_required": True}, execution=INDIRECT_UNSAFE_LOCAL_WRITE)
    async def screenshot(self, call: CapabilityCall) -> CapabilityResult:
        args = ScreenshotInput.model_validate(call.args)
        runtime = call.meta.get("execution_runtime")
        root = getattr(runtime, "runtime_root", None) or self.runtime_root
        if root is None:
            raise ToolExecutionError("Screenshot cache has no runtime root", error_code="android_cache_unavailable")
        result = self.read("screenshot", {"max_edge": args.max_edge}, device=args.device)
        png = result["png"]
        turn_id = str(call.meta.get("turn_id") or "manual")
        stored = ArtifactIngestor(Path(root)).store_bytes(channel_kind="android_browser", bucket_id=turn_id,
            file_name=f"android-{uuid4().hex[:12]}.png", content=png, mime_type="image/png")
        data = {key: value for key, value in result.items() if key not in {"ok", "png"}}
        data.update(device=args.device, path=stored.local_cached_path, artifact_id=None, registration="unavailable", registration_error=None)
        owner = getattr(runtime, "provider_registry", {}).get("artifact:artifact")
        messages = ()
        if owner is not None:
            try:
                imported = await owner.import_local_for_turn(stored, runtime=runtime, turn_id=turn_id, source_channel="android_browser")
                if imported.status == RuntimeStatus.OK:
                    data.update(artifact_id=imported.structured["artifact_id"], registration="registered")
                    messages = imported.context_messages
                else:
                    data.update(registration="failed", registration_error=imported.llm_text)
            except Exception as exc:
                data.update(registration="unknown", registration_error=f"{type(exc).__name__}: {exc}")
        hint = ("Image registered; do not import it again. Core attaches pixels only when the model supports vision."
                if data["registration"] == "registered" else
                "Image saved. Reconcile uncertain registration with search_artifacts; when registration is unavailable, import_artifact with path.")
        return CapabilityResult(status=RuntimeStatus.OK, structured=data,
            llm_text=json.dumps(data, ensure_ascii=False, separators=(",", ":")), context_messages=messages,
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED), recovery_hint=hint)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="android", action_name="query",
                       guidance=guidance("Find all current controls by resource id, text, description or class.",
                                         "Resolve a target or disambiguate duplicates; nth uses this match order. Results are live, not durable element handles.", ("find", "query", "controls")),
                       InputModel=QueryInput, OutputModel=QueryOutput, aliases=("android_query",),
                       examples=({"by": "textContains", "value": "设置"},), execution=INDIRECT_EXTERNAL_READ)
    def query(self, call: CapabilityCall) -> CapabilityResult:
        args = QueryInput.model_validate(call.args)
        result = self.read("find_all", {"by": args.by, "value": args.value, "timeout": 0}, device=args.device)
        return success({"matches": [compact_node(n) for n in result["value"]]})

    @capability_action(namespace=OPERATION_NAMESPACE, scope="module", family="android", action_name="act",
                       guidance=guidance("Execute up to eight explicit UI primitives, stop on error, then return a fresh screen.",
                                         "Tap/type/swipe/navigate in an observed Android app. Batch only known steps; leave page and business decisions to Pal. Coordinate tap is a fallback. A timeout may mean actions already ran; observe before retrying.", ("tap", "input", "swipe", "launch", "batch")),
                       InputModel=ActInput, OutputModel=ActOutput, aliases=("android_act",),
                       examples=({"steps": [{"cmd": "launch", "package": "com.android.settings"}]},),
                       execution=INDIRECT_EXTERNAL_WRITE)
    def act(self, call: CapabilityCall) -> CapabilityResult:
        args = ActInput.model_validate(call.args)
        try:
            result = self.client.request("batch", {
                "steps": [step.model_dump(exclude_unset=True) for step in args.steps], "stop_on_error": True,
            }, device=args.device)
        except AndroidError as exc:
            # Only this versioned protocol can attest rejection before effects.
            outcome = (EffectOutcome.NOT_APPLIED if exc.details.get("receipt_protocol") == "pal.android.v1"
                       and exc.details.get("effect") == "none" else EffectOutcome.UNKNOWN)
            raise ToolExecutionError(str(exc), error_code="android_action_uncertain", details=exc.details,
                                     effect_receipt=EffectReceipt(outcome=outcome),
                                     retry=RetryDirective.RECONCILE_FIRST,
                                     recovery_hint="Observe the current phone state; do not replay the batch blindly.") from exc
        results = result.get("results")
        valid = isinstance(results, list) and len(results) == len(args.steps) and all(
            isinstance(r, dict) and r.get("ok") is True for r in results)
        data = {"results": results if isinstance(results, list) else [], "screen": None, "snapshot_error": None}
        if "screen" in result and "snapshot_error" in result:
            # Pal Android captures the receipt and screen under one phone-side lock.
            try:
                data["screen"] = screen(result["screen"]) if result["screen"] is not None else None
                data["snapshot_error"] = result["snapshot_error"]
                if data["screen"] is None and not data["snapshot_error"]:
                    data["snapshot_error"] = "Host returned no snapshot"
            except (ValueError, KeyError, TypeError) as exc:
                data["snapshot_error"] = str(exc)
        else:
            try:
                # Best-effort settling is read-only; animations must not hide applied actions.
                try:
                    self.client.request("wait_idle", {"ms": 250, "timeout": 2}, device=args.device)
                except AndroidError:
                    pass
                data["screen"] = screen(self.client.request("dump", {}, device=args.device))
            except (AndroidError, ValueError, KeyError) as exc:
                data["snapshot_error"] = str(exc)
        if not valid:
            outcome = EffectOutcome.UNKNOWN
            if result.get("receipt_protocol") == "pal.android.v1" and isinstance(results, list) and results:
                effects = [step.get("effect") for step in results]
                if all(effect in {"applied", "none"} for effect in effects):
                    outcome = EffectOutcome.APPLIED if "applied" in effects else EffectOutcome.NOT_APPLIED
            raise ToolExecutionError("Android batch stopped or returned incomplete results", error_code="android_batch_partial",
                                     details=data, effect_receipt=EffectReceipt(outcome=outcome),
                                     retry=RetryDirective.RECONCILE_FIRST,
                                     recovery_hint="Inspect per-step receipts and current screen before continuing; do not replay completed steps.")
        return success(data, applied=True, hint="Actions completed. Observe again before continuing." if data["snapshot_error"] else "")
