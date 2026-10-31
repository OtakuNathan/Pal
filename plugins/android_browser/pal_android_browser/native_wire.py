"""Pal Android v1 boundary. The APK never consumes the legacy host protocol."""
from __future__ import annotations


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
