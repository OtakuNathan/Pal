#!/usr/bin/env python3
"""Build the panel's artwork assets from the desktop_avatar provider art.

The desktop companion's DEFAULT artwork is the raster skin, not the pal2d SVG
skin. client/js/pal-raster-art.js holds three layers:

  base         -- body plate (head + torso + legs)
  arm-overlay  -- the posable arm: upper arm, forearm and four hand poses
  eye-overlay  -- pure vector face (built at runtime by face_art.py)

The arm cannot ship as one document. Rendering that combined layer costs
~120 ms per frame at *any* target resolution, because librsvg re-parses and
re-decodes the embedded PNGs every time -- far too slow for a 20 fps panel.
So the arm is emitted as separate parts; the runtime rasterises each one ONCE
into a cairo surface and then blits it with cairo transforms. See
face_art.RasterFace.

Outputs under assets/:
  pal-body.svg        base layer + static (screen-right) arm   cached
  pal-arm-upper.svg   shoulder joint + upper arm               rotates at shoulder
  pal-arm-forearm.svg elbow joint + forearm                    rotates at elbow
  pal-hand-*.svg      wrist + one hand pose                    rotates at wrist
  pal-cola-hand.svg   hand + can + straw (drinking)            blitted, faded

Every part is self-contained: librsvg on this image will not load external
image references at all (file:// and bare relative paths both render empty --
verified), so textures are embedded as data URIs.

Re-run this after the provider art changes.
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
from pathlib import Path

from PIL import Image

import face_art  # shares the ctypes librsvg/cairo helpers

PLUGIN_DIR = Path(__file__).resolve().parent
CLIENT_DIR = Path("/home/nathan/.pal/channel/providers/desktop_avatar/client")
ART_JS = CLIENT_DIR / "js" / "pal-raster-art.js"
RASTER = CLIENT_DIR / "assets" / "model" / "pal-raster"
ASSETS = PLUGIN_DIR / "assets"

# The panel is 240x320 and the character fills the frame; embedding at 2x keeps
# the plate crisp without paying for the original 887 px on every start.
BODY_EMBED_HEIGHT = 640
# The hand atlas is declared 1536x1024 in the SVG and ends up about 100 px on
# the panel, so half resolution is still generous.
HAND_EMBED_WIDTH = 512
# The cola sprite lands on about 90x104 user units (roughly 32x38 panel
# pixels), so 256 px of embedded width is already an 8x oversample.
COLA_EMBED_WIDTH = 256

VIEW_BOX = "0 0 591.333 887"
SVG_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="' + VIEW_BOX + '">'
)

WRIST = '<ellipse cx="155" cy="575" rx="16" ry="10" fill="#10212d"/>'

# The posable arm is the screen-left one drawn inside shoulder-joint; its
# sibling <use ... clip-path="url(#right-cut)"/> is the screen-right arm, which
# never moves and therefore rides along with the cached body.
STATIC_ARM = '<use href="#arm-texture" clip-path="url(#right-cut)"/>'
UPPER_ARM = (
    '<circle cx="199" cy="415" r="18" fill="#111c24"/>'
    '<use href="#arm-texture" clip-path="url(#upper-cut)"/>'
)
FOREARM = (
    '<circle cx="177" cy="483" r="14" fill="#142029"/>'
    '<use href="#arm-texture" clip-path="url(#forearm-cut)"/>'
)
# defs the static arm needs, injected into the body document
BODY_EXTRA_DEFS = ("arm-texture", "right-cut")

_TAG_OPEN = re.compile(r"<([A-Za-z][\w:.-]*)(?=[\s/>])")
_TAG_CLOSE = re.compile(r"</([A-Za-z][\w:.-]*)\s*>")


def js_string_literal(raw: str, marker: str) -> str:
    """Read the JS string literal that starts immediately after ``marker``.

    The artwork literal is no longer the last thing in its statement: the
    avatar now injects the cola sprite with ``.replace('<use id="rest-hand"',
    ...)`` *before* ``.replaceAll("__ASSETS__", assets)``. Anchoring the regex
    on that suffix silently stopped matching, so scan for the closing quote.
    """
    at = raw.index(marker) + len(marker)
    if not raw.startswith('"', at):
        raise SystemExit(f"{marker!r} is not followed by a string literal")
    out: list[str] = []
    i = at + 1
    while i < len(raw):
        char = raw[i]
        if char == "\\":
            out.append(raw[i : i + 2])
            i += 2
            continue
        if char == '"':
            return json.loads('"' + "".join(out) + '"')
        out.append(char)
        i += 1
    raise SystemExit(f"unterminated string literal after {marker!r}")


def extract_layers() -> dict[str, str]:
    raw = ART_JS.read_text(encoding="utf-8")
    artwork = js_string_literal(raw, "export const artwork = ")
    layers = dict(re.findall(r'<svg class="([^"]+)"[^>]*>(.*?)</svg>', artwork, re.S))
    for required in ("base", "arm-overlay"):
        if required not in layers:
            raise SystemExit(f"missing '{required}' layer in {ART_JS}")
    return layers


def extract_element(text: str, element_id: str) -> str:
    """Return the element carrying id=element_id, with balanced nesting."""
    marker = f'id="{element_id}"'
    at = text.find(marker)
    if at < 0:
        raise SystemExit(f"element id={element_id!r} not found")
    start = text.rfind("<", 0, at)
    first_end = text.find(">", start)
    if first_end < 0:
        raise SystemExit(f"malformed element id={element_id!r}")
    if text[first_end - 1] == "/":  # self-closing: nothing to balance
        return text[start : first_end + 1]

    depth = 0
    i = start
    while i < len(text):
        open_match = _TAG_OPEN.search(text, i)
        close_match = _TAG_CLOSE.search(text, i)
        if close_match and (not open_match or close_match.start() < open_match.start()):
            depth -= 1
            i = close_match.end()
            if depth == 0:
                return text[start:i]
        elif open_match:
            end = text.find(">", open_match.end())
            if end < 0:
                break
            if text[end - 1] != "/":
                depth += 1
            i = end + 1
        else:
            break
    raise SystemExit(f"unbalanced element id={element_id!r}")


def data_uri(name: str, *, width: int | None = None, height: int | None = None) -> str:
    image = Image.open(RASTER / name)
    if height:
        image = image.resize((round(image.width * height / image.height), height), Image.LANCZOS)
    elif width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def write_asset(name: str, defs: str, content: str) -> None:
    leftovers = sorted(set(re.findall(r"__ASSETS__[A-Za-z0-9._-]+", defs + content)))
    if leftovers:
        raise SystemExit(f"{name}: unsubstituted asset reference(s): {leftovers}")
    text = SVG_OPEN + defs + content + "</svg>"
    path = ASSETS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  {name:22} {len(text) / 1e3:7.0f} KB")


def _outline_mask(path_d: str, size: tuple[int, int]) -> "Image.Image":
    """Rasterise the avatar's cola outline into an alpha mask."""
    width, height = size
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><path d="{path_d}" fill="#ffffff"/></svg>'
    )
    rendered = face_art.render_image(svg, width, height).convert("L")
    return rendered.point(lambda value: 255 if value > 128 else 0)


def cola_hand_group() -> str:
    """The avatar's cola sprite, ready for the runtime to blit.

    The avatar paints cola-hand.png inside <g id="cyber-cola"> under a clip
    path; librsvg will not clip an <image> reliably here, so the outline is
    rasterised once and baked into the embedded PNG's alpha channel instead.
    The sprite carries the gripping hand, the can and the straw as one image,
    exactly as the avatar paints them, so the panel's grip cannot drift from
    the desktop one.
    """
    group = extract_element(ART_JS.read_text(encoding="utf-8"), "cyber-cola")
    transform = re.search(r'transform="([^"]+)"', group)
    outline = re.search(r'<clipPath id="cola-hand-cut"><path d="([^"]+)"', group)
    if not transform or not outline:
        raise SystemExit("the cyber-cola group in pal-raster-art.js changed shape")

    sprite = Image.open(RASTER / "cola-hand.png").convert("RGBA")
    mask = _outline_mask(outline.group(1), sprite.size)
    sprite.putalpha(mask)
    box = mask.getbbox()
    if not box:
        raise SystemExit("the cola outline rasterised to nothing")
    crop = sprite.crop(box)
    factor = max(1.0, crop.width / COLA_EMBED_WIDTH)
    embed = crop.resize(
        (max(1, round(crop.width / factor)), max(1, round(crop.height / factor))),
        Image.LANCZOS,
    )
    buffer = io.BytesIO()
    embed.save(buffer, "PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    print(f"  {'cola sprite':22} cut {box}, embedded {embed.size}, uri {len(uri) / 1e3:.0f} KB")
    return (
        f'<g transform="{transform.group(1)} translate({box[0]} {box[1]})">'
        f'<image href="{uri}" width="{crop.width}" height="{crop.height}"/></g>'
    )


def main() -> int:
    layers = extract_layers()

    arms = layers["arm-overlay"]
    arms = arms.replace("__ASSETS__hand-poses.png?v=1", data_uri("hand-poses.png", width=HAND_EMBED_WIDTH))
    arms = arms.replace("__ASSETS__arm-texture.png?v=1", data_uri("arm-texture.png"))
    defs_match = re.search(r"<defs>.*?</defs>", arms, re.S)
    if not defs_match:
        raise SystemExit("expected a <defs> block in the arm layer")
    arm_defs = defs_match.group(0)

    # Body = base layer (its own defs) + the static arm, plus the two defs the
    # static arm borrows from the arm layer.
    body = layers["base"].replace(
        "__ASSETS__body-plate.png?v=1", data_uri("body-plate.png", height=BODY_EMBED_HEIGHT)
    )
    body_defs_match = re.search(r"<defs>.*?</defs>", body, re.S)
    if not body_defs_match:
        raise SystemExit("expected a <defs> block in the base layer")
    borrowed = "".join(extract_element(arm_defs, element_id) for element_id in BODY_EXTRA_DEFS)
    body = body.replace(
        body_defs_match.group(0), body_defs_match.group(0)[: -len("</defs>")] + borrowed + "</defs>", 1
    )
    write_asset("pal-body.svg", "", body + STATIC_ARM)

    # Posable arm, split so the runtime can rotate each joint independently.
    write_asset("pal-arm-upper.svg", arm_defs, UPPER_ARM)
    write_asset("pal-arm-forearm.svg", arm_defs, FOREARM)

    hand_markup = {
        "pal-hand-rest": '<use href="#arm-texture" clip-path="url(#hand-cut)"/>',
    }
    for part_id, name in (
        ("ok-hand", "pal-hand-ok"),
        ("wave-hand", "pal-hand-wave"),
        ("scratch-hand", "pal-hand-scratch"),
        ("pinch-hand", "pal-hand-pinch"),
    ):
        element = extract_element(arms[defs_match.end() :], part_id)
        hand_markup[name] = element.replace('opacity="0"', 'opacity="1"', 1)

    for name, content in hand_markup.items():
        write_asset(name + ".svg", arm_defs, WRIST + content)

    # Drinking uses one rigid hand-can-straw sprite, same as the avatar.
    write_asset("pal-cola-hand.svg", "", cola_hand_group())
    return 0


if __name__ == "__main__":
    sys.exit(main())
