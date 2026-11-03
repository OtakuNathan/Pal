"""Pal Android v1 boundary. The APK never consumes the legacy host protocol."""
from __future__ import annotations

import base64
import hashlib
import struct
import zlib


_SELECTORS = {
    "text": ("text", "exact"), "textContains": ("text", "contains"),
    "textMatches": ("text", "pattern"), "id": ("resource", "exact"),
    "desc": ("description", "exact"), "descContains": ("description", "contains"),
    "descMatches": ("description", "pattern"), "class": ("class", "exact"),
}


def selector(args: dict) -> dict:
    prop, kind = _SELECTORS[args["by"]]
    return {"property": prop, "match": {"kind": kind, "value": args["value"]}}


def action(step: dict) -> dict:
    match step["cmd"]:
        case "launch":
            return {"kind": "launch", "package": step["package"]}
        case "input" if step.get("by") == "clipboard":
            return {"kind": "paste", "package": step["package"], "text": step["text"]}
        case "input" if step.get("by") == "focus":
            return {"kind": "write_focused", "editor": step["editor"], "package": step["package"], "text": step["text"]}
        case "touch" | "input":
            target = {"selector": selector(step), "wait_ms": round(step.get("timeout", 5) * 1000)}
            if "nth" in step:
                target["ordinal"] = step["nth"]
            if step["cmd"] == "touch":
                return {"kind": "activate", "target": target}
            return {"kind": "write", "target": target, "text": step["text"],
                    "mode": "append" if step.get("append", False) else "replace"}
        case "tap":
            return {"kind": "tap", "x": step["x"], "y": step["y"]}
        case "swipe":
            return {"kind": "swipe", "direction": step["x1"], "duration_ms": step.get("ms", 300)}
        case "back" | "home":
            return {"kind": step["cmd"]}
        case "wait_idle":
            return {"kind": "settle", "quiet_ms": step.get("ms", 500),
                    "deadline_ms": round(step.get("timeout", 5) * 1000)}
        case _:
            raise ValueError("This action is not supported by Pal Android v1")


def node(item: dict) -> dict:
    state, actions = item["state"], item["actions"]
    return {"text": item["label"], "desc": item["description"], "id": item["resource"],
            "class": item["class"], "bounds": item["rect"],
            "clickable": "activate" in actions, "editable": "write" in actions,
            "scrollable": "scroll" in actions, **state}


def snapshot(value: dict) -> dict:
    nodes: dict[int, dict] = {}
    roots: list[dict] = []
    # Parent indices precede children in the canonical preorder. Reject broken
    # topology instead of silently rendering an incomplete current screen.
    for item in value["items"]:
        index, parent = item["index"], item["parent"]
        if type(index) is not int or index in nodes or (parent is not None and parent not in nodes):
            raise ValueError("Invalid frame topology")
        current = node(item)
        nodes[index] = current
        if parent is None:
            roots.append(current)
        else:
            nodes[parent].setdefault("children", []).append(current)
    surface, app = value["surface"], value["app"]
    return {"ok": True, "package": app["package"], "activity": app["activity"],
            "width": surface["width"], "height": surface["height"], "generation": surface["epoch"],
            "workspace": "pal", "truncated": value["truncated"], "tree": roots}


def receipt(value: dict) -> dict:
    steps = []
    for step in value["steps"]:
        kind = step["kind"]
        if kind not in {"applied", "rejected", "indeterminate"}:
            raise ValueError("Unknown action receipt")
        steps.append({"ok": kind == "applied", "effect": {
            "applied": "applied", "rejected": "none", "indeterminate": "unknown"}[kind],
            **({"fault": step["fault"]} if kind != "applied" else {})})
    observation = value["observation"]
    match observation["kind"]:
        case "snapshot":
            screen, error = snapshot(observation["frame"]), None
        case "unavailable":
            screen, error = None, observation["fault"]
        case _:
            raise ValueError("Unknown observation receipt")
    return {"ok": True, "results": steps, "screen": screen, "snapshot_error": error,
            "receipt_protocol": "pal.android.v1"}


def screenshot(value: dict) -> dict:
    surface, picture, app = value["surface"], value["image"], value["app"]
    for record, keys in ((surface, {"epoch", "width", "height"}),
                         (picture, {"mime", "width", "height", "sha256", "base64"}),
                         (app, {"package", "activity"})):
        if not isinstance(record, dict) or record.keys() != keys:
            raise ValueError("Invalid screenshot fields")
    if (set(value) != {"surface", "image", "app", "frame_age_ms"}
            or picture["mime"] != "image/png"
            or any(type(app[k]) is not str for k in app)
            or type(value["frame_age_ms"]) is not int or value["frame_age_ms"] < 0
            or any(type(n) is not int or n <= 0 for n in (*surface.values(), picture["width"], picture["height"]))
            or max(surface["width"], surface["height"]) > 4000
            or max(picture["width"], picture["height"]) > 1920
            or picture["width"] > surface["width"] or picture["height"] > surface["height"]):
        raise ValueError("Invalid screenshot metadata")
    png = base64.b64decode(picture["base64"], validate=True)
    if (not 33 <= len(png) <= 10 * 1024 * 1024 or png[:8] != b"\x89PNG\r\n\x1a\n"
            or png[8:16] != b"\x00\x00\x00\rIHDR"
            or struct.unpack(">II", png[16:24]) != (picture["width"], picture["height"])
            or zlib.crc32(png[12:29]) != struct.unpack(">I", png[29:33])[0]
            or hashlib.sha256(png).hexdigest() != picture["sha256"]):
        raise ValueError("Invalid screenshot PNG")
    return {"ok": True, "png": png, "package": app["package"], "activity": app["activity"],
            "width": picture["width"], "height": picture["height"],
            "viewport_width": surface["width"], "viewport_height": surface["height"],
            "generation": surface["epoch"], "frame_age_ms": value["frame_age_ms"],
            "sha256": picture["sha256"], "workspace": "pal"}


def focus(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid focus observation")
    if value.get("kind") == "ready":
        if (set(value) != {"kind", "editor", "package", "field_id"}
                or any(not isinstance(value[k], str) or not value[k] for k in ("editor", "package"))
                or type(value["field_id"]) is not int):
            raise ValueError("Invalid editor lease")
    elif value.get("kind") == "unavailable":
        fault = value.get("fault")
        if (set(value) != {"kind", "fault"} or not isinstance(fault, dict)
                or set(fault) != {"code", "message"} or any(not isinstance(v, str) for v in fault.values())):
            raise ValueError("Invalid focus fault")
    else:
        raise ValueError("Unknown focus observation")
    return {"ok": True, "focus": value}
