"""DOT MASK mode: normal full-color video multiplied by a CRT shadow-mask
pattern (assets/DOT-MASK.png -- soft red, green and blue phosphor dots on
black, made by the user in Photoshop as a Multiply layer), drawn straight
into /dev/fb0.

Made for PRODUCTION's 2x2 CRT wall at XGA (1024x768), the mask's own size.
On other framebuffer sizes the mask is cropped or tiled -- it's a repeating
pixel pattern, so it's never scaled.

How it works (the decode/timing/overlay pipeline is fb_video.py's, shared
with ASCII and PIXEL): ffmpeg scales the video to the full framebuffer size
and hands over RGB565 directly. Every mask pixel lights exactly one color
channel, at one of a few levels, so multiplying (with the brightness gain
that makes up for the mask's dark gaps) keeps just that channel's 5 or 6
bits and looks the result up in a small table: (mask shade, channel value)
-> finished RGB565 pixel, 12 shades x 64 values. A frame is one AND, one
per-pixel shift, one add and one table lookup -- ~19ms on a Pi 3B+, where a
general per-channel multiply was ~27ms and ffmpeg's own blend=multiply ran
at less than half real time.
"""

import numpy as np
import pygame

from fb_video import FramebufferVideo

DEFAULT_GAIN = 2.0
# (shift, field mask, max value) per RGB565 channel
CHANNELS = ((11, 0xF800, 31), (5, 0x07E0, 63), (0, 0x001F, 31))


def _load_mask(path, width, height):
    """Mask image -> (height, width, 3) uint8, cropped/tiled to size."""
    rgb = pygame.surfarray.array3d(pygame.image.load(path)).transpose(1, 0, 2)
    reps = (-(-height // rgb.shape[0]), -(-width // rgb.shape[1]), 1)
    return np.tile(rgb, reps)[:height, :width]


class MaskVideo(FramebufferVideo):
    PIX_FMT = "rgb565le"
    BYTES_PER_PIXEL = 2
    SCALE_FLAGS = "bilinear"  # scaling up, so area averaging buys nothing

    def __init__(self, fb, mask_path, gain=DEFAULT_GAIN):
        self.cols, self.rows = fb.width, fb.height
        super().__init__(fb)
        mask = _load_mask(mask_path, self.cols, self.rows).astype(np.uint16)
        channel = mask.argmax(axis=2)
        level = mask.max(axis=2)
        if ((mask > 0).sum(axis=2) > 1).any():
            # Only the brightest channel of a mixed-color pixel survives.
            print(f"{mask_path}: some pixels light more than one channel -- using the brightest", flush=True)
        self.field = np.array([c[1] for c in CHANNELS], dtype=np.uint16)[channel]
        self.field[level == 0] = 0
        self.shift = np.array([c[0] for c in CHANNELS], dtype=np.uint16)[channel]
        shades, shade_index = np.unique(channel * 256 + level, return_inverse=True)
        self.table_base = (shade_index.reshape(channel.shape) * 64).astype(np.uint16)
        values = np.arange(64)
        table = np.zeros((len(shades), 64), dtype=np.uint16)
        for i, shade in enumerate(shades):
            ch, lvl = divmod(int(shade), 256)
            shift, _, top = CHANNELS[ch]
            out = np.minimum(np.round(values * (lvl / 255.0) * gain), top).astype(np.uint16)
            out[values > top] = 0  # can't occur (5-bit channels), keeps the table tidy
            table[i] = out << shift
        self.table = table.reshape(-1)

    def _compose(self, buf):
        frame = np.frombuffer(buf, dtype="<u2").reshape(self.rows, self.cols)
        value = frame & self.field
        value >>= self.shift
        value += self.table_base
        return np.take(self.table, value)
