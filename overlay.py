"""Playback OSD overlays: VCR OSD Mono, 48px, green with a 1px black
outline by default (MUTE and PAUSE are explicitly red per the brief).
Rendered with Pillow to raw BGRA, written to /dev/shm (tmpfs -- fast,
no SD wear for something rewritten every time an indicator appears),
and pushed onto mpv's own video output via its overlay-add IPC command
-- mpv's built-in mechanism for compositing a bitmap over what it's
already rendering, so no separate window/compositor is needed.

Positions taken from TV_DINNER_OVERLAY_COORDINATES.csv -- these are in
the same 720x480 pixel space mpv's output actually fills (the 640x480
source is stretched to exactly 720x480, --keepaspect=no), so they're
used directly with no scaling.
"""

import os
import time

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "VCR_OSD_MONO_1.001.ttf"
FONT_SIZE = 48
GREEN = (0, 255, 0, 255)
RED = (255, 0, 0, 255)
BLACK = (0, 0, 0, 255)
OUTLINE_WIDTH = 1
SHM_DIR = "/dev/shm"

# name -> (x, y) top-left, from TV_DINNER_OVERLAY_COORDINATES.csv
POSITIONS = {
    "ch": (488, 53),
    "mute": (95, 53),
    "vol": (95, 53),
    "pause": (282, 384),
    "skip_left": (78, 384),
    "skip_right": (458, 384),
}

# name -> overlay-add id (must be distinct per concurrently-visible overlay;
# "mute"/"vol" share one id since they occupy the same slot and are never
# both shown at once)
OVERLAY_IDS = {
    "ch": 1,
    "mute": 2,
    "vol": 2,
    "pause": 3,
    "skip_left": 4,
    "skip_right": 5,
}


class OverlayRenderer:
    def __init__(self, asset_dir):
        self.font = ImageFont.truetype(os.path.join(asset_dir, FONT_PATH), FONT_SIZE)

    def render_bgra(self, text, color):
        # Oversize canvas, then crop tightly so mpv only composites the
        # actual glyph bounds (smaller overlay = cheaper to update rapidly).
        probe = Image.new("RGBA", (1, 1))
        bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=self.font, stroke_width=OUTLINE_WIDTH)
        w = bbox[2] - bbox[0] + 2 * OUTLINE_WIDTH + 2
        h = bbox[3] - bbox[1] + 2 * OUTLINE_WIDTH + 2
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((OUTLINE_WIDTH + 1 - bbox[0], OUTLINE_WIDTH + 1 - bbox[1]), text, font=self.font,
                   fill=color, stroke_width=OUTLINE_WIDTH, stroke_fill=BLACK)
        return img


class OverlayManager:
    """Tracks which named overlay slots are currently visible and their
    auto-hide deadlines. Call update() every loop iteration."""

    def __init__(self, mpv, renderer):
        self.mpv = mpv
        self.renderer = renderer
        self.hide_at = {}  # name -> monotonic deadline, or None for persistent
        self.visible = set()

    def show(self, name, text, color, duration=None):
        image = self.renderer.render_bgra(text, color)
        raw_path = os.path.join(SHM_DIR, f"tvdinner_overlay_{OVERLAY_IDS[name]}.raw")
        with open(raw_path, "wb") as f:
            f.write(image.tobytes("raw", "BGRA"))
        x, y = POSITIONS[name]
        w, h = image.size
        self.mpv.add_overlay(OVERLAY_IDS[name], x, y, raw_path, w, h, w * 4)
        self.visible.add(name)
        self.hide_at[name] = (time.monotonic() + duration) if duration else None

    def hide(self, name):
        if name in self.visible:
            self.mpv.remove_overlay(OVERLAY_IDS[name])
            self.visible.discard(name)
        self.hide_at.pop(name, None)

    def hide_all(self):
        for name in list(self.visible):
            self.hide(name)

    def update(self):
        now = time.monotonic()
        for name in list(self.visible):
            deadline = self.hide_at.get(name)
            if deadline is not None and now >= deadline:
                self.hide(name)
