"""Render Pal's real (raster) avatar on the Pi without a browser.

The desktop companion defaults to the *raster* skin: a pre-rendered body plate
plus a vector eye overlay (client/js/pal-raster-*.js). This module reproduces
that split so the panel stays fast on a Pi 4:

  * the body plate is rendered once into a cached cairo surface, because
    librsvg re-decoding the embedded plate every frame costs ~300 ms
  * each frame only the pure-vector eye overlay is re-rendered (single-digit
    milliseconds) and composited over the cached body

Pose and expression maths are a port of pal-raster-animation.js so the panel
face and the on-screen pet agree.

Rendering goes through librsvg + libcairo via ctypes (both ship with
Raspberry Pi OS; the PyGObject Rsvg typelib is NOT installed here). Keeping
every ctypes call on the calling thread matters: doing this from a worker
thread segfaults the interpreter.

Register this module through build_assets.py: assets/pal-body.svg holds the
static body layer with the plate embedded as a data URI.
"""

from __future__ import annotations

import ctypes
import io
import math
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
BODY_SVG = PLUGIN_DIR / "assets" / "pal-body.svg"
UPPER_SVG = PLUGIN_DIR / "assets" / "pal-arm-upper.svg"
FOREARM_SVG = PLUGIN_DIR / "assets" / "pal-arm-forearm.svg"
# The posable (screen-left) arm is split into parts at build time so the
# runtime can rasterise each one once and then rotate it with cairo. Rendering
# the combined arm layer instead costs ~120 ms/frame at any resolution, because
# librsvg re-decodes the embedded PNGs on every parse.
HAND_SVGS = {
    "rest": PLUGIN_DIR / "assets" / "pal-hand-rest.svg",
    "ok": PLUGIN_DIR / "assets" / "pal-hand-ok.svg",
    "wave": PLUGIN_DIR / "assets" / "pal-hand-wave.svg",
    "scratch": PLUGIN_DIR / "assets" / "pal-hand-scratch.svg",
    "pinch": PLUGIN_DIR / "assets" / "pal-hand-pinch.svg",
}
# The cyber cola is one rigid sprite in the avatar -- hand, can and straw in a
# single image (assets/model/pal-raster/cola-hand.png). The panel blits that
# same sprite, so the grip cannot drift from the desktop avatar's; only the
# outline mask is baked in at build time (see build_assets.py).
COLA_SVG = PLUGIN_DIR / "assets" / "pal-cola-hand.svg"

# Joint centres in SVG user units; pal-raster-animation.js rotates about these.
SHOULDER = (199.0, 415.0)
ELBOW = (177.0, 483.0)
WRIST = (155.0, 575.0)

BACKGROUND = (10, 14, 24)
VIEW_BOX = (591.333, 887.0)

# The panel mirrors the desktop avatar's raster skin (pal-raster-avatar.js),
# which is the source of truth for the expression set. Keep the state list,
# aliases, labels, eye/mouth kinds and durations in step with that file:
# anything the avatar can show should render here too, and vice versa.
ALIASES = {
    "sleepy": "sleeping", "err": "error", "laugh": "happy", "clap": "happy",
    "excited": "happy", "proud": "smirk",
    "complain": "angry", "stretching": "greeting",
    # Wire aliases the avatar's sidecar also accepts on push_state.
    "embarrassed": "awkward", "smug": "smirk", "playful": "cheeky",
    "surprised": "shock", "wave": "greeting",
    "snack": "snacking", "drink": "drinking", "stretch": "stretching",
}
STATES = (
    "standby", "thinking", "working", "sleeping", "greeting", "curious",
    "love", "wink", "shock", "happy", "cheeky", "smirk", "snacking",
    "drinking", "sad", "confused", "panic", "angry", "error", "shy",
    "awkward", "crying", "bored", "gloomy", "celebrate", "agree",
)

# state -> (label, eye kind, mouth kind)
PRESETS: dict[str, tuple[str, str, str]] = {
    "greeting": ("打招呼 · 挥手", "happy", "smile"),
    "standby": ("待机", "round", "smile"),
    "thinking": ("思考", "round", "flat"),
    "working": ("工作", "round", "smile"),
    "sleeping": ("睡觉", "closed", "flat"),
    "curious": ("好奇 · 星星眼", "star", "small"),
    "love": ("喜欢 · 爱心眼", "heart", "smile"),
    "wink": ("Wink · 0_<", "wink", "smirk"),
    "shock": ("惊醒", "wide", "o"),
    "happy": ("开心", "happy", "grin"),
    "cheeky": ("耍贱 · 吐舌", "playful", "tongue"),
    "smirk": ("坏笑", "sly", "smirk"),
    "snacking": ("赛博零食", "happy", "chew"),
    "drinking": ("赛博可乐", "round", "small"),
    "sad": ("难过", "pleading", "pout"),
    "error": ("工具失败 · 崩溃脸", "cross", "open"),
    "shy": ("害羞 · 脸红", "happy", "small"),
    "awkward": ("尴尬 · 偷吃被发现", "cookie", "small"),
    "crying": ("哭 · 眼泪", "round", "sad"),
    "bored": ("无聊 · 挠头", "heavy", "flat"),
    "gloomy": ("阴郁 · 小乌云", "heavy", "sad"),
    "celebrate": ("庆祝 · 烟花", "happy", "grin"),
    "agree": ("同意 · OK 手势", "happy", "smile"),
    "confused": ("疑惑 · 问号眼", "question", "small"),
    "panic": ("懵圈 · 大头螺旋眼", "spiral", "o"),
    "angry": ("生气", "round", "sad"),
}

# Base (persistent) states never auto-expire. Every other state plays for its
# duration and then hands control back to the last base state -- this mirrors
# finish() / select(base) in pal-raster-animation.js.
BASE_STATES = ("standby", "thinking", "working", "sleeping")


def state_duration(state: str) -> float:
    if state == "drinking":
        return 5.2
    if state in ("greeting", "agree", "confused", "bored", "gloomy"):
        return 4.2
    if state in ("snacking", "celebrate"):
        return 3.6
    if state == "shock":
        return 1.1
    return 2.6


MOUTHS = {
    "open": "M 281 316 Q 296 305 311 316 L 309 333 Q 296 343 283 333 Z",
    "smile": "M 281 315 Q 296 329 311 315",
    "flat": "M 287 318 Q 296 318 305 318",
    "small": "M 290 316 Q 296 321 302 316",
    "smirk": "M 281 318 Q 301 327 312 312",
    "sad": "M 283 323 Q 296 312 309 323",
    "o": "M 296 310 C 306 310 306 328 296 328 C 286 328 286 310 296 310",
    "chew": "M 285 316 Q 296 332 307 316 Q 296 311 285 316",
    "grin": "M 279 316 Q 296 320 313 316 C 312 334 281 334 279 316 Z",
    "pout": "M 290 322 Q 296 313 302 322",
    "tongue": "M 292 319 Q 303 320 315 314 L 313 330 C 309 340 296 337 295 330 Z",
}

# The snack chip lives in the eye overlay and is dragged around by the pinch
# hand, so its position depends on the arm pose (see _snack_anchor).
SNACK_SHAPE = (
    '<rect x="-19" y="-15" width="38" height="30" rx="5" fill="#101c36" '
    'stroke="#ff7dea" stroke-width="3"/>'
    '<path d="M -26 -9 h 7 M -26 0 h 7 M -26 9 h 7 M 19 -9 h 7 M 19 0 h 7 M 19 9 h 7" '
    'stroke="#53f8ed" stroke-width="3"/>'
    '<path d="M 1 -11 l -8 13 h 7 l -1 9 l 9 -13 h -7 Z" fill="#73fff0"/>'
)

# The cola sprite is painted in the avatar's own frame: translate(WRIST), then
# scale(.1), then translate(-COLA_ANCHOR). Solving the arm so that COLA_STRAW_TIP
# lands on MOUTH_CENTRE is what puts the straw on the mouth at both sizes.
COLA_SCALE = 0.1
COLA_ANCHOR = (250.0, 1020.0)
COLA_STRAW_TIP = (1068.0, 133.0)
MOUTH_CENTRE = (296.0, 318.0)

BLINK_CYCLE_S = 4.4
BLINK_MS = 315
# pal-raster-animation.js only *starts* a blink when the current state's eye
# kind is round or wide, so winking / happy / star / heart / closed poses never
# blink. Getting this wrong made the winking pose blink its open eye too.
BLINKING_KINDS = ("round", "wide")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ease(t: float) -> float:
    t = _clamp01(t)
    return t * t * (3 - 2 * t)


def _ramp(value: float, start: float, end: float) -> float:
    """Eased 0->1 transition between two times, clamped outside the window."""
    if end <= start:
        return 1.0 if value >= end else 0.0
    return _ease((value - start) / (end - start))


def _cola_opacity(age: float) -> float:
    """Port of the avatar's colaOpacity ramp (age in milliseconds).

    The avatar fades the sprite in over 450 ms, holds it through the sip, then
    fades it out; its resting hand is drawn at 1 - this opacity, so the grip
    that appears is the sprite's own.
    """
    return _ramp(age, 0, 450) * (1 - _ramp(age, 4400, 4700))


def normalise_state(value: str | None) -> str:
    name = str(value or "").strip().lower()
    # Aliases can chain (stretch -> stretching -> greeting), so follow them until
    # they settle rather than resolving a single hop and giving up.
    seen: set[str] = set()
    while name in ALIASES and name not in seen:
        seen.add(name)
        name = ALIASES[name]
    return name if name in PRESETS else "standby"


def is_valid(value: str | None) -> bool:
    name = str(value or "").strip().lower()
    return name in PRESETS or name in ALIASES


def _blink_openness(clock: float, kind: str, *, reduced: bool) -> float:
    if kind not in BLINKING_KINDS or reduced:
        return 1.0
    phase = (clock % BLINK_CYCLE_S) * 1000.0
    start = BLINK_CYCLE_S * 1000.0 - BLINK_MS
    if phase < start:
        return 1.0
    t = phase - start
    if t < 100:
        return 1.0 - _ease(t / 100)
    if t < 135:
        return 0.0
    return _ease((t - 135) / 180)


# --------------------------------------------------------------------------
# expression art (ported from pal-raster-animation.js)
# --------------------------------------------------------------------------
def _star(cx: int) -> str:
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = 13 if i % 2 else 30
        points.append(f"{cx + math.cos(angle) * radius:.1f},{279 + math.sin(angle) * radius:.1f}")
    return (f'<g class="eye-symbol" data-cx="{cx}"><polygon points="{" ".join(points)}" '
            'fill="#7cffff" stroke="#20bdff" stroke-width="2"/></g>')


def _heart(cx: int) -> str:
    return (f'<g class="eye-symbol" data-cx="{cx}">'
            f'<path transform="translate({cx} 278)" d="M 0 24 C -48 -5 -24 -39 0 -16 '
            'C 24 -39 48 -5 0 24 Z" fill="#ff78d4" stroke="#ffb1ea" stroke-width="2"/></g>')


def _question(cx: int, y: int) -> str:
    return (f'<g class="question-eye" transform="translate({cx} {y})">'
            '<path d="M -16 -13 C -17 -34 20 -35 19 -14 C 18 -2 1 -3 1 10"/>'
            '<circle cx="1" cy="23" r="3.5" fill="#31e6ff" stroke="none"/></g>')


def _spiral(cx: int) -> str:
    parts = []
    for i in range(85):
        angle = i / 84 * math.pi * 4.6
        radius = 2 + i / 84 * 25
        parts.append(f"{'L' if i else 'M'} {math.cos(angle) * radius:.2f} {math.sin(angle) * radius:.2f}")
    return (f'<g class="spiral-eye" data-cx="{cx}" transform="translate({cx} 279)">'
            f'<path d="{" ".join(parts)}" stroke="#31e6ff" stroke-width="4" fill="none"/></g>')


def _cookie(cx: int) -> str:
    return (
        f'<g class="cookie-eye">'
        f'<path d="M {cx-26} 281 Q {cx-23} 251 {cx} 255 Q {cx+25} 251 {cx+27} 279 '
        f'Q {cx+9} 294 {cx-26} 281" fill="#28dfff" stroke="#31e6ff" stroke-width="2"/>'
        f'<path d="M {cx+7} 264 Q {cx-3} 273 {cx+8} 281" stroke="#0876ad" stroke-width="5" fill="none"/>'
        f'<path d="M {cx-16} 265 L {cx-10} 274 M {cx-9} 261 L {cx-3} 270" stroke="#b9ffff" stroke-width="2"/>'
        "</g>"
    )


def _concept_eye(cx: int, shape: str, look_x: float = 0.0, look_y: float = 0.0) -> str:
    """Cut the iris silhouette with an upper lid instead of squashing the whole
    circle, so the pupil keeps its round shape. Port of conceptEye() in
    pal-raster-animation.js.

    ``shape`` is a closed path in pupil-local space (origin at the eye centre);
    the iris and pupil are painted through it as a clip.
    """
    lid = f"expression-lid-{cx}"
    return (
        f'<g class="concept-eye" transform="translate({cx} 279)">'
        f'<defs><clipPath id="{lid}"><path d="{shape}"/></clipPath></defs>'
        f'<g clip-path="url(#{lid})" stroke="none">'
        '<circle r="30" fill="url(#iris)"/>'
        f'<g transform="translate({look_x:g} {look_y:g})">'
        '<circle r="14" fill="url(#pupil)"/>'
        '<circle cx="5" cy="-8" r="4" fill="#e8ffff"/>'
        '<circle cx="-5" cy="7" r="1.8" fill="#68dcff" opacity=".65"/>'
        "</g></g></g>"
    )


# Concept-eye silhouettes (pupil-local space, origin at the eye centre).
ROUND_EYE = "M -29 0 C -29 -39 29 -39 29 0 C 29 36 -29 36 -29 0 Z"
HEAVY_EYE = "M -29 0 L 29 0 C 29 35 -29 35 -29 0 Z"
SLY_EYE_LEFT = "M -28 -17 L 28 -4 C 28 34 -31 34 -28 -17 Z"
SLY_EYE_RIGHT = "M -28 -4 L 28 -17 C 31 34 -28 34 -28 -4 Z"
PLEADING_LOOK_Y = -16.0


def _eye_art(kind: str) -> str:
    if kind == "cross":
        return '<path d="M 218 260 L 254 296 M 254 260 L 218 296 M 338 260 L 374 296 M 374 260 L 338 296"/>'
    if kind == "cookie":
        return _cookie(236) + _cookie(356)
    if kind == "question":
        return _question(236, 276) + _question(356, 282)
    if kind == "spiral":
        return _spiral(236) + _spiral(356)
    if kind == "star":
        return _star(236) + _star(356)
    if kind == "heart":
        return _heart(236) + _heart(356)
    if kind == "happy":
        return '<path d="M 211 283 Q 236 253 261 283 M 331 283 Q 356 253 381 283"/>'
    if kind == "closed":
        return '<path d="M 211 279 Q 236 285 261 279 M 331 279 Q 356 285 381 279"/>'
    if kind == "heavy":
        return _concept_eye(236, HEAVY_EYE, 5, 0) + _concept_eye(356, HEAVY_EYE, 5, 0)
    if kind == "sly":
        return _concept_eye(236, SLY_EYE_LEFT, 9, -6) + _concept_eye(356, SLY_EYE_RIGHT, -9, -6)
    if kind == "pleading":
        return (
            _concept_eye(236, ROUND_EYE, 9, PLEADING_LOOK_Y)
            + _concept_eye(356, ROUND_EYE, -9, PLEADING_LOOK_Y)
        )
    if kind == "playful":
        # Left eye open, right eye squashed shut with a long lash, as in
        # pal-raster-animation.js.
        return (
            _concept_eye(236, ROUND_EYE, 9, -2)
            + '<path d="M 329 284 Q 352 255 379 271 Q 358 268 337 281 Q 354 277 366 284" stroke-width="5"/>'
        )
    if kind == "wink":
        # The '<' mark is emitted by the animation block in build_eye_svg, which
        # also owns its opacity and vertical squash. Emitting it here as well
        # produced two overlapping marks: the static one stayed fully opaque
        # while the animated one faded in and out.
        return ""
    return ""


def _brows(state: str) -> tuple[str, str]:
    if state in ("angry", "smirk"):
        return "M 216 229 Q 235 232 254 241", "M 338 241 Q 357 232 376 229"
    if state in ("curious", "confused", "wink", "cheeky"):
        return "M 215 227 Q 232 217 250 223", "M 342 235 Q 360 232 376 238"
    if state in ("sad", "crying", "awkward", "gloomy"):
        return "M 218 240 Q 238 237 250 228", "M 342 228 Q 354 237 374 240"
    if state == "bored":
        return "M 218 244 Q 233 238 249 243", "M 343 243 Q 359 238 374 244"
    if state == "shock":
        return "M 218 224 Q 234 214 250 220", "M 342 220 Q 358 214 374 224"
    return "M 218 235 Q 234 226 250 230", "M 342 230 Q 358 226 374 235"


def _sparkles(progress: float, colour: str, ox: float = 296, oy: float = 175) -> str:
    out = []
    for i in range(7):
        phase = (progress + i / 7) % 1
        angle = i * 2.39996
        radius = 70 + phase * 60
        x = ox + math.cos(angle) * radius
        y = oy + math.sin(angle) * radius * 0.6 - phase * 28
        scale = math.sin(phase * math.pi) * 0.9
        out.append(
            f'<path transform="translate({x:.1f} {y:.1f}) scale({scale:.3f})" '
            'd="M 0 -8 L 2 -2 L 8 0 L 2 2 L 0 8 L -2 2 L -8 0 L -2 -2 Z" '
            f'fill="{colour}" opacity="{math.sin(phase * math.pi):.3f}"/>'
        )
    return "".join(out)


def _particles(state: str, age: float, reduced: bool) -> str:
    if state == "sleeping":
        out = []
        for i in range(3):
            phase = (age / 3200 + i / 3) % 1
            x = 373 + 14 * math.sin(phase * math.pi) + phase * 22
            y = 144 - phase * 102
            out.append(
                f'<text x="{x:.1f}" y="{y:.1f}" fill="#9adfff" font-family="monospace" '
                f'font-weight="700" font-size="{27 + phase * 17:.1f}" '
                f'opacity="{math.sin(phase * math.pi) * .85:.3f}">{"Z" if i == 0 else "z"}</text>'
            )
        return "".join(out)
    if state == "curious":
        return _sparkles(age / 1700, "#8affff")
    if state == "crying":
        out = []
        for i, x in enumerate((213, 379)):
            phase = 0.45 if reduced else (age / 1150 + i * 0.35) % 1
            out.append(
                f'<path transform="translate({x} {294 + phase * 43:.1f})" '
                'd="M 0 -8 C -2 -3 -9 3 -7 8 C -5 16 6 16 8 8 C 10 3 3 -4 0 -8 Z" '
                f'fill="#52cfff" stroke="#b6f4ff" stroke-width="1.5" opacity="{.9 - phase * .45:.3f}"/>'
            )
        return "".join(out)
    if state == "shy":
        opacity = 0.7 if reduced else 0.55 + 0.2 * math.sin(age / 330)
        return "".join(
            f'<g class="blush" opacity="{opacity:.3f}">'
            f'<ellipse cx="{x}" cy="311" rx="24" ry="10" fill="#ff739b"/>'
            f'<path d="M {x-12} 307 l -3 7 M {x} 307 l -3 7 M {x+12} 307 l -3 7" '
            'stroke="#ffc0d3" stroke-width="2"/></g>'
            for x in (225, 368)
        )
    if state == "celebrate":
        t = 850 if reduced else age
        out = []
        for j, cx in enumerate((185, 403, 296)):
            phase = (t / 1350 + j / 3) % 1
            cy = 80 if j == 2 else 155
            radius = 10 + phase * 75
            colour = ("#ff9edb", "#8cfff1", "#ffe897")[j]
            spokes = "".join(
                f'<path d="M {cx + math.cos(i * math.pi / 5) * radius * .7:.1f} '
                f'{cy + math.sin(i * math.pi / 5) * radius * .7:.1f} L '
                f'{cx + math.cos(i * math.pi / 5) * radius:.1f} '
                f'{cy + math.sin(i * math.pi / 5) * radius:.1f}"/>'
                for i in range(10)
            )
            out.append(
                f'<g stroke="{colour}" stroke-width="3.5" stroke-linecap="round" '
                f'opacity="{math.sin(phase * math.pi):.3f}">{spokes}</g>'
            )
        return "".join(out)
    return ""


def _head_effect(state: str, age: float, reduced: bool) -> str:
    if state == "thinking" and 1800 < age < 3200:
        phase = (age - 1800) / 1400
        glow = math.sin(phase * math.pi)
        return (
            f'<g transform="translate(374 86) scale({.8 + glow * .25:.3f})" opacity="{glow:.3f}" '
            'stroke="#ffe583" stroke-width="3" fill="none" stroke-linecap="round">'
            '<path d="M -9 12 C -9 4 -17 1 -17 -10 A 17 17 0 1 1 17 -10 C 17 1 9 4 9 12 Z" '
            'fill="#ffe583" fill-opacity=".3"/>'
            '<path d="M -8 18 H 8 M -5 24 H 5 M 0 -37 V -44 M -29 -27 L -35 -33 '
            'M 29 -27 L 35 -33 M -28 -7 H -36 M 28 -7 H 36"/></g>'
        )
    if state == "confused":
        return (
            '<text x="390" y="110" text-anchor="middle" fill="#a4eaff" font-family="sans-serif" '
            f'font-weight="700" font-size="34" transform="rotate({5 * math.sin(age / 240):.2f} 390 110)">'
            "???</text>"
        )
    if state == "angry":
        scale = 0.9 + 0.12 * math.sin(age / 120)
        return (
            f'<g transform="translate(393 150) scale({scale:.3f})" fill="none" stroke="#ff6f87" '
            'stroke-width="5" stroke-linecap="round">'
            '<path d="M -17 -5 Q -5 -5 -5 -17 M 5 -17 Q 5 -5 17 -5 M 17 5 Q 5 5 5 17 '
            'M -5 17 Q -5 5 -17 5"/></g>'
        )
    # The little dark cloud belongs to gloomy (阴郁); bored is the head-scratch
    # pose, which the arm rig draws instead of a head effect.
    if state == "gloomy":
        strands = "".join(
            f'<path d="M {x} 139 v {18 + (0 if reduced else 5 * math.sin(age / 250 + i)):.1f}"/>'
            for i, x in enumerate((349, 365, 381, 397, 413))
        )
        return (
            '<g class="gloom" fill="none" stroke="#929ed5" stroke-width="4" stroke-linecap="round">'
            '<path d="M 345 123 Q 331 108 341 98 Q 349 91 361 96 Q 370 80 386 91 Q 400 83 409 99 '
            'Q 428 100 421 115 Q 412 128 394 122 Z" fill="#363b66"/>' + strands + "</g>"
        )
    return ""


def _iris(cx: int, pupil_x: int, pupil_dx: float, pupil_dy: float, opacity: float) -> str:
    return (
        f'<circle cx="{cx}" cy="279" r="29" fill="url(#iris)"/>'
        f'<g class="pupil" transform="translate({pupil_dx:.2f} {pupil_dy:.2f})" opacity="{opacity:.3f}">'
        f'<circle cx="{pupil_x}" cy="279" r="14" fill="url(#pupil)"/>'
        f'<circle cx="{pupil_x + 5}" cy="270" r="5" fill="#e8ffff"/>'
        f'<circle cx="{pupil_x - 5}" cy="286" r="2" fill="#68dcff" opacity=".65"/></g>'
    )


_EYE_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 591.333 887">
<defs>
<radialGradient id="iris" cx="42%" cy="40%" r="65%"><stop stop-color="#b4ffff"/><stop offset=".42" stop-color="#28edff"/><stop offset=".76" stop-color="#03bdff"/><stop offset="1" stop-color="#0865df"/></radialGradient>
<radialGradient id="pupil"><stop stop-color="#061936"/><stop offset=".75" stop-color="#063b87"/><stop offset="1" stop-color="#075dd2"/></radialGradient>
<filter id="eye-glow" x="-50%" y="-60%" width="200%" height="220%"><feGaussianBlur stdDeviation="3"/><feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<clipPath id="left-lid"><ellipse cx="236" cy="279" rx="30" ry="{lry:.3f}"/></clipPath>
<clipPath id="right-lid"><ellipse cx="356" cy="279" rx="30" ry="{rry:.3f}"/></clipPath>
</defs>
<g filter="url(#eye-glow)">
<g clip-path="url(#left-lid)" opacity="{liris:.3f}">{left_iris}</g>
<g clip-path="url(#right-lid)" opacity="{riris:.3f}">{right_iris}</g>
<g fill="none" stroke="#20dfff" stroke-width="3.5" stroke-linecap="round" opacity="{closed_op:.3f}"><path d="M 210 281 Q 236 277 262 281"/><path d="M 330 281 Q 356 277 382 281"/></g>
</g>
<g filter="url(#eye-glow)"><g fill="none" stroke="#31e6ff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">{art}</g></g>
<g fill="none" stroke="#25dfff" stroke-linecap="round" filter="url(#eye-glow)">
<path stroke-width="5" d="{brow_left}"/>
<path stroke-width="5" d="{brow_right}"/>
<path stroke-width="3" fill="{mouth_fill}" d="{mouth}"{mouth_transform}/>
<path opacity=".6" stroke="{cheek}" stroke-width="2" d="M 213 311 l -4 4 M 221 311 l -4 4 M 229 311 l -4 4 M 363 311 l -4 4 M 371 311 l -4 4 M 379 311 l -4 4"/>
</g>
{snack}
<g filter="url(#eye-glow)">{particles}</g>
<g filter="url(#eye-glow)">{head_effect}</g>
<text x="296" y="100" text-anchor="middle" fill="#81eaff" font-size="25" font-weight="700" font-family="monospace">{caption}</text>
</svg>"""


class _Affine:
    """2D affine transform (a, b, c, d, e, f): x' = ax + cy + e, y' = bx + dy + f."""

    __slots__ = ("a", "b", "c", "d", "e", "f")

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, e=0.0, f=0.0) -> None:
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    @classmethod
    def translate(cls, tx: float, ty: float) -> "_Affine":
        return cls(1.0, 0.0, 0.0, 1.0, tx, ty)

    @classmethod
    def scale(cls, sx: float, sy: float) -> "_Affine":
        return cls(sx, 0.0, 0.0, sy, 0.0, 0.0)

    @classmethod
    def rotate(cls, degrees: float) -> "_Affine":
        r = math.radians(degrees)
        return cls(math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0.0, 0.0)

    @classmethod
    def rotate_about(cls, degrees: float, cx: float, cy: float) -> "_Affine":
        return cls.translate(cx, cy).then(cls.rotate(degrees)).then(cls.translate(-cx, -cy))

    def then(self, other: "_Affine") -> "_Affine":
        """Return self after other: other is applied first."""
        return _Affine(
            self.a * other.a + self.c * other.b,
            self.b * other.a + self.d * other.b,
            self.a * other.c + self.c * other.d,
            self.b * other.c + self.d * other.d,
            self.a * other.e + self.c * other.f + self.e,
            self.b * other.e + self.d * other.f + self.f,
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)


# The pinch hand carries the snack chip; pal-raster-animation.js maps a point in
# the hand atlas through the whole joint chain to find where it lands.
_PINCH_HAND = (
    _Affine.translate(155, 575)
    .then(_Affine.rotate(180))
    .then(_Affine.scale(-0.16, 0.145))
    .then(_Affine.translate(-1214, -755))
)


def _joint_chain(shoulder: float, elbow: float, wrist: float) -> _Affine:
    """Compose the three arm rotations, innermost joint last.

    The avatar nests shoulder > elbow > wrist as <g> elements; composing them in
    the same order lets any point in hand space be mapped to panel space.
    """
    return (
        _Affine.rotate_about(shoulder, *SHOULDER)
        .then(_Affine.rotate_about(elbow, *ELBOW))
        .then(_Affine.rotate_about(wrist, *WRIST))
    )


def _snack_anchor(shoulder: float, elbow: float, wrist: float) -> tuple[float, float]:
    chain = _joint_chain(shoulder, elbow, wrist).then(_PINCH_HAND)
    return chain.apply(1420.0, 330.0)


def _arm_angles(state: str, age: float) -> tuple[float, float, float, str]:
    """Port of animateArms() in pal-raster-animation.js (age in milliseconds)."""
    shoulder = elbow = wrist = 0.0
    wave = state in ("greeting", "agree")
    okay = state == "agree"
    eating = state == "snacking"
    drinking = state == "drinking"
    scratch = state in ("thinking", "confused", "bored") and age < 4200

    if wave:
        envelope = _ease(min(1.0, age / 750)) * (1 - _ease(max(0.0, min(1.0, (age - 3350) / 850))))
        swing = math.sin(max(0.0, age - 750) / 150)
        shoulder = 65 * envelope
        elbow = (100 + 5 * swing) * envelope
        wrist = (15 + 7 * swing) * envelope

    if scratch:
        def ramp(start: float, end: float) -> float:
            return _ease(max(0.0, min(1.0, (age - start) / (end - start))))

        # Fold the forearm before lifting the upper arm, then reverse the order
        # on return: this keeps the hand close instead of sweeping a long arc.
        fold = ramp(0, 600) * (1 - ramp(3650, 4200))
        lift = ramp(250, 950) * (1 - ramp(3100, 3750))
        contact = ramp(950, 1150) * (1 - ramp(2850, 3100))
        stroke = 5 * math.sin(max(0.0, age - 1150) * math.pi / 360) * contact
        shoulder = 65 * lift
        elbow = 120 * fold + stroke
        wrist = -10 * fold

    if eating:
        lift = _ease(min(1.0, age / 1250))
        lower = 1 - _ease(max(0.0, min(1.0, (age - 1850) / 1000)))
        envelope = lift * lower
        shoulder = -50 * envelope
        elbow = -125 * envelope
        wrist = 45 * envelope

    if drinking:
        # Two-phase: lift the can from the hip to chest height, then sip. The arm
        # is solved (two-link IK) so the straw tip lands on the mouth, and the
        # wrist absorbs the joint angles so the grip stays square to the can.
        hold = _ramp(age, 0, 450) * (1 - _ramp(age, 4700, 5150))
        sip = _ramp(age, 500, 1650) * (1 - _ramp(age, 3300, 4350))
        tilt = math.radians(-10)
        tip_x = (COLA_STRAW_TIP[0] - COLA_ANCHOR[0]) * COLA_SCALE
        tip_y = (COLA_STRAW_TIP[1] - COLA_ANCHOR[1]) * COLA_SCALE
        sip_x = MOUTH_CENTRE[0] - (tip_x * math.cos(tilt) - tip_y * math.sin(tilt))
        sip_y = MOUTH_CENTRE[1] - (tip_x * math.sin(tilt) + tip_y * math.cos(tilt))
        target_x = WRIST[0] + (185 - WRIST[0]) * hold + (sip_x - 185) * sip
        target_y = WRIST[1] + (555 - WRIST[1]) * hold + (sip_y - 555) * sip
        upper = math.hypot(-22, 68)
        forearm = math.hypot(-22, 92)
        to_target = math.hypot(target_x - SHOULDER[0], target_y - SHOULDER[1])
        cos_bend = (
            to_target * to_target - upper * upper - forearm * forearm
        ) / (2 * upper * forearm)
        bend = -math.acos(max(-1.0, min(1.0, cos_bend)))
        heading = math.atan2(target_y - SHOULDER[1], target_x - SHOULDER[0]) - math.atan2(
            forearm * math.sin(bend), upper + forearm * math.cos(bend)
        )
        shoulder = math.degrees(heading - math.atan2(68, -22))
        elbow = math.degrees(bend - math.atan2(92, -22) + math.atan2(68, -22))
        wrist = -10 * hold - shoulder - elbow

    if okay:
        hand = "ok"
    elif wave:
        hand = "wave"
    elif scratch:
        hand = "scratch"
    elif eating:
        hand = "pinch"
    else:
        hand = "rest"
    return shoulder, elbow, wrist, hand


def arm_pose(state: str, elapsed: float) -> tuple[float, float, float, str]:
    """Public wrapper: (shoulder, elbow, wrist, hand_name) for a state at `elapsed` seconds."""
    return _arm_angles(normalise_state(state), elapsed * 1000.0)


def build_eye_svg(state: str, elapsed: float, clock: float, *, reduced: bool = False) -> str:
    s = normalise_state(state)
    _label, kind, mouth_kind = PRESETS[s]
    age = elapsed * 1000.0

    openness = _blink_openness(clock, kind, reduced=reduced)
    resting = 0.40 if s == "bored" else 0.72 if s in ("thinking", "sad", "crying") else 1.0
    lid_ry = max(0.001, 30 * openness * resting)
    closed_op = (1 - openness) ** 10 if kind in ("round", "wide") else 0.0

    liris = 1.0 if kind in ("round", "wide") or kind == "wink" else 0.0
    riris = 1.0 if kind in ("round", "wide") else 0.0
    rry = lid_ry

    # The wink is keyed on the state, not the eye kind: cheeky now uses the
    # playful eyes, so only `wink` squashes its right eyelid.
    if s == "wink":
        if age < 320:
            closure = _ease(age / 320)
        elif age < 1400:
            closure = 1.0
        else:
            closure = 1 - _ease((age - 1400) / 480)
        rry = max(0.001, 30 * (1 - closure))
        riris = 1 - closure

    pupil_dx = pupil_dy = 0.0
    if s in ("thinking", "bored"):
        look = _ease(min(1.0, age / 700))
        pupil_dx = 7 * math.sin(age / 650) * look
        pupil_dy = -10 * look

    art = _eye_art(kind)
    if s in ("curious", "love"):
        period = 180 if s == "love" else 240
        pulse = 1 + 0.09 * math.sin(age / period)
        art = f'<g transform="translate(296 279) scale({pulse:.4f}) translate(-296 -279)">{art}</g>'
    if s == "wink":
        opacity = 0.0 if age < 320 else 1.0 if age < 1400 else 1 - _ease((age - 1400) / 480)
        scale_y = 0.45 + 0.55 * opacity
        art += (
            f'<path id="wink-mark" opacity="{opacity:.3f}" '
            f'transform="translate(356 279) scale(1 {scale_y:.3f}) translate(-356 -279)" '
            'd="M 371 263 L 345 279 L 371 292"/>'
        )
    if s == "panic":
        spin = age / 7
        art = art.replace(
            'transform="translate(236 279)"', f'transform="translate(236 279) rotate({spin:.2f})"'
        ).replace(
            'transform="translate(356 279)"', f'transform="translate(356 279) rotate({-spin:.2f})"'
        )

    brow_left, brow_right = _brows(s)
    particles = _particles(s, age, reduced)
    if s == "thinking" and 1800 < age < 3200:
        particles += _sparkles((age - 1800) / 1400, "#ffe583", 374, 95)

    caption = ""
    snack = ""
    mouth_transform = ""
    if s == "working":
        caption = "· · ·"
    elif s == "drinking":
        # Hand, can and straw are one sprite, blitted by RasterFace.render_image
        # through the same joint chain as the arm; only the caption belongs to
        # this vector overlay.
        if age > 2500:
            caption = "COLA +1"
    elif s == "snacking":
        # The chip rides the pinch hand, so it needs the arm pose for this frame.
        shoulder, elbow, wrist, _hand = _arm_angles(s, age)
        tip_x, tip_y = _snack_anchor(shoulder, elbow, wrist)
        swallowed = _clamp01((age - 1400) / 350)
        opacity = 0.0 if age >= 1750 else 1 - swallowed
        snack = (
            f'<g opacity="{opacity:.3f}" '
            f'transform="translate({tip_x:.1f} {tip_y:.1f}) scale({0.4 * (1 - swallowed):.3f})">'
            + SNACK_SHAPE
            + "</g>"
        )
        if age > 1450:
            chew = 0.7 + 0.3 * math.cos((age - 1450) / 90)
            mouth_transform = (
                f' transform="translate(296 319) scale(1 {chew:.3f}) translate(-296 -319)"'
            )
        if age > 2700:
            caption = "ENERGY +1 ⚡"

    return _EYE_TEMPLATE.format(
        lry=lid_ry, rry=rry, liris=liris, riris=riris, closed_op=closed_op,
        left_iris=_iris(236, 242, pupil_dx, pupil_dy, 1.0),
        right_iris=_iris(356, 352, pupil_dx, pupil_dy, 1.0),
        art=art, brow_left=brow_left, brow_right=brow_right,
        mouth=MOUTHS[mouth_kind], mouth_transform=mouth_transform,
        mouth_fill="#08273a" if s == "error" else "none",
        cheek="#ff78d4" if s == "love" else "#25dfff",
        particles=particles, head_effect=_head_effect(s, age, reduced),
        snack=snack, caption=caption,
    )


# --------------------------------------------------------------------------
# librsvg / cairo via ctypes
# --------------------------------------------------------------------------
_rsvg = ctypes.CDLL("librsvg-2.so.2")
_cairo = ctypes.CDLL("libcairo.so.2")
_gobject = ctypes.CDLL("libgobject-2.0.so.0")

_CAIRO_FORMAT_ARGB32 = 0


class _Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("width", ctypes.c_double), ("height", ctypes.c_double)]


_rsvg.rsvg_handle_new_from_data.restype = ctypes.c_void_p
_rsvg.rsvg_handle_new_from_data.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_void_p)]
_rsvg.rsvg_handle_render_document.restype = ctypes.c_int
_rsvg.rsvg_handle_render_document.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(_Rect), ctypes.POINTER(ctypes.c_void_p)]
_cairo.cairo_image_surface_create.restype = ctypes.c_void_p
_cairo.cairo_image_surface_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_cairo.cairo_create.restype = ctypes.c_void_p
_cairo.cairo_create.argtypes = [ctypes.c_void_p]
_cairo.cairo_destroy.argtypes = [ctypes.c_void_p]
_cairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
_cairo.cairo_surface_flush.argtypes = [ctypes.c_void_p]
_cairo.cairo_image_surface_get_data.restype = ctypes.c_void_p
_cairo.cairo_image_surface_get_data.argtypes = [ctypes.c_void_p]
_cairo.cairo_image_surface_get_stride.restype = ctypes.c_int
_cairo.cairo_image_surface_get_stride.argtypes = [ctypes.c_void_p]
_cairo.cairo_set_source_rgb.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_double]
_cairo.cairo_paint.argtypes = [ctypes.c_void_p]
_cairo.cairo_paint_with_alpha.argtypes = [ctypes.c_void_p, ctypes.c_double]
_cairo.cairo_set_source_surface.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
_cairo.cairo_save.argtypes = [ctypes.c_void_p]
_cairo.cairo_restore.argtypes = [ctypes.c_void_p]
_cairo.cairo_translate.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
_cairo.cairo_rotate.argtypes = [ctypes.c_void_p, ctypes.c_double]
_gobject.g_object_unref.argtypes = [ctypes.c_void_p]


def _handle(svg: bytes) -> int:
    error = ctypes.c_void_p()
    handle = _rsvg.rsvg_handle_new_from_data(svg, len(svg), ctypes.byref(error))
    if not handle:
        raise RuntimeError("librsvg could not parse the SVG document")
    return handle


def _render_onto(context: int, svg: bytes, width: int, height: int) -> None:
    handle = _handle(svg)
    try:
        viewport = _Rect(0.0, 0.0, float(width), float(height))
        error = ctypes.c_void_p()
        if not _rsvg.rsvg_handle_render_document(handle, context, ctypes.byref(viewport),
                                                 ctypes.byref(error)):
            raise RuntimeError("librsvg render_document reported failure")
    finally:
        _gobject.g_object_unref(handle)


def _surface_to_image(surface: int, width: int, height: int):
    from PIL import Image

    _cairo.cairo_surface_flush(surface)
    stride = _cairo.cairo_image_surface_get_stride(surface)
    pointer = _cairo.cairo_image_surface_get_data(surface)
    buffer = ctypes.string_at(pointer, stride * height)
    return Image.frombytes("RGB", (width, height), buffer, "raw", "BGRX", stride)


def pack_rgb565(image) -> bytes:
    import numpy as np

    array = np.asarray(image, dtype=np.uint16)
    packed = (
        ((array[:, :, 0] & 0xF8) << 8)
        | ((array[:, :, 1] & 0xFC) << 3)
        | (array[:, :, 2] >> 3)
    ).astype(">u2")
    return packed.tobytes()


class RasterFace:
    """Body plate cached once; arm parts and the vector face are redrawn."""

    def __init__(self, width: int = 240, height: int = 320, *, body_svg: Path | None = None) -> None:
        self.width = width
        self.height = height
        self._body = self._new_part((body_svg or BODY_SVG).read_bytes(), background=BACKGROUND)
        self._upper = self._new_part(UPPER_SVG.read_bytes())
        self._forearm = self._new_part(FOREARM_SVG.read_bytes())
        self._hands = {name: self._new_part(path.read_bytes()) for name, path in HAND_SVGS.items()}
        self._cola = self._new_part(COLA_SVG.read_bytes())
        # librsvg letterboxes the viewBox into the panel (xMidYMid/meet), so the
        # joint centres need the same mapping before cairo can rotate about them.
        self._scale = min(width / VIEW_BOX[0], height / VIEW_BOX[1])
        self._offset_x = (width - VIEW_BOX[0] * self._scale) / 2
        self._offset_y = (height - VIEW_BOX[1] * self._scale) / 2

    def _new_part(self, svg: bytes, *, background: tuple[int, int, int] | None = None) -> int:
        """Rasterise one asset into its own cairo surface (once)."""
        surface = _cairo.cairo_image_surface_create(_CAIRO_FORMAT_ARGB32, self.width, self.height)
        context = _cairo.cairo_create(surface)
        try:
            if background is not None:
                _cairo.cairo_set_source_rgb(context, *(c / 255 for c in background))
                _cairo.cairo_paint(context)
            _render_onto(context, svg, self.width, self.height)
        finally:
            _cairo.cairo_destroy(context)
        return surface

    def _panel_point(self, x: float, y: float) -> tuple[float, float]:
        return (x * self._scale + self._offset_x, y * self._scale + self._offset_y)

    @staticmethod
    def _blit(context: int, surface: int) -> None:
        _cairo.cairo_set_source_surface(context, surface, 0.0, 0.0)
        _cairo.cairo_paint(context)

    @staticmethod
    def _blit_alpha(context: int, surface: int, alpha: float) -> None:
        """Blit with opacity, for the cola sprite's fade and the hand crossfade."""
        if alpha <= 0.0:
            return
        _cairo.cairo_set_source_surface(context, surface, 0.0, 0.0)
        _cairo.cairo_paint_with_alpha(context, min(1.0, alpha))

    @staticmethod
    def _rotate_about(context: int, degrees: float, centre: tuple[float, float]) -> None:
        if not degrees:
            return
        _cairo.cairo_translate(context, centre[0], centre[1])
        _cairo.cairo_rotate(context, math.radians(degrees))
        _cairo.cairo_translate(context, -centre[0], -centre[1])

    def render_image(self, state: str, elapsed: float, clock: float, *, reduced: bool = False):
        surface = _cairo.cairo_image_surface_create(_CAIRO_FORMAT_ARGB32, self.width, self.height)
        context = _cairo.cairo_create(surface)
        try:
            _cairo.cairo_set_source_rgb(context, *(c / 255 for c in BACKGROUND))
            _cairo.cairo_paint(context)
            self._blit(context, self._body)

            # Nest the three joints so the forearm follows the shoulder and the
            # hand follows both, exactly like the nested <g> elements in the
            # provider's arm-overlay layer.
            shoulder, elbow, wrist, hand = arm_pose(state, elapsed)
            # The avatar fades its cola sprite in and hides its resting hand
            # for the same window (colaOpacity in pal-raster-animation.js).
            cola = _cola_opacity(elapsed * 1000.0) if normalise_state(state) == "drinking" else 0.0
            _cairo.cairo_save(context)
            self._rotate_about(context, shoulder, self._panel_point(*SHOULDER))
            self._blit(context, self._upper)
            _cairo.cairo_save(context)
            self._rotate_about(context, elbow, self._panel_point(*ELBOW))
            self._blit(context, self._forearm)
            _cairo.cairo_save(context)
            self._rotate_about(context, wrist, self._panel_point(*WRIST))
            if cola:
                # Sprite painted first, resting hand over it: the avatar inserts
                # #cyber-cola before #rest-hand inside the wrist group.
                self._blit_alpha(context, self._cola, cola)
                self._blit_alpha(context, self._hands[hand], 1.0 - cola)
            else:
                self._blit(context, self._hands[hand])
            _cairo.cairo_restore(context)
            _cairo.cairo_restore(context)
            _cairo.cairo_restore(context)

            _render_onto(context, build_eye_svg(state, elapsed, clock, reduced=reduced).encode("utf-8"),
                         self.width, self.height)
            return _surface_to_image(surface, self.width, self.height)
        finally:
            _cairo.cairo_destroy(context)
            _cairo.cairo_surface_destroy(surface)

    def render_rgb565(self, state: str, elapsed: float, clock: float, *, reduced: bool = False) -> bytes:
        return pack_rgb565(self.render_image(state, elapsed, clock, reduced=reduced))


def render_image(svg_text: str, width: int, height: int):
    """Render a standalone SVG document (kept for diagnostics)."""
    from PIL import Image

    surface = _cairo.cairo_image_surface_create(_CAIRO_FORMAT_ARGB32, width, height)
    context = _cairo.cairo_create(surface)
    try:
        _cairo.cairo_set_source_rgb(context, *(c / 255 for c in BACKGROUND))
        _cairo.cairo_paint(context)
        _render_onto(context, svg_text.encode("utf-8"), width, height)
        return _surface_to_image(surface, width, height)
    finally:
        _cairo.cairo_destroy(context)
        _cairo.cairo_surface_destroy(surface)


if __name__ == "__main__":
    import time

    face = RasterFace()
    for _ in range(2):
        face.render_image("thinking", 0.5, 0.0)
    t0 = time.perf_counter()
    n = 12
    for i in range(n):
        face.render_image("thinking", 0.5, i * 0.05)
    dt = (time.perf_counter() - t0) / n * 1000
    print(f"body-cached pipeline: {dt:.1f} ms/frame -> {1000/dt:.1f} fps")

    states = ["standby", "thinking", "working", "sleeping",
              "happy", "sad", "wink", "shock",
              "curious", "love", "error", "celebrate"]
    from PIL import Image
    w, h = 240, 320
    sheet = Image.new("RGB", (w * 4, h * 3), (0, 0, 0))
    for i, s in enumerate(states):
        sheet.paste(face.render_image(s, 1.2, 0.4), ((i % 4) * w, (i // 4) * h))
    sheet.resize((sheet.width // 2, sheet.height // 2)).save("/tmp/raster-sheet.png")
    print("sheet -> /tmp/raster-sheet.png", states)
