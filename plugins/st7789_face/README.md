# ST7789 face renderer

This versioned migration baseline was imported from the installed
`st7789_face` snapshot on 2026-10-15 because its original repository was not
located. `migration-origin.json` records the original file hashes for review.
If the original repository is identified, carry these changes back there.

The plugin now contributes a renderer to the independent `emotion` plugin.
Only `st7789_face_status` remains on this plugin's model tool surface.
The coordinator owns `show_emotion(emotion)`; the panel keeps its existing
sidecar animation timing, activity state and failure priority.

Build a reviewable artifact with:

```sh
pal package build plugins/st7789_face --output /tmp/pal-face-package
```

This is a host-Python package matching the existing installation: hardware
dependencies still belong to the target system. Building does not install,
start a device, or activate a generation. The new Pal `emotion` plugin must
be available before activating this renderer. Preserve target panel
configuration when preparing an installation; do not copy this baseline
over runtime-specific edits without comparing them first.
