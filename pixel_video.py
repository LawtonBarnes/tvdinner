"""PIXEL mode: plays the video like an old computer or game console --
blocky pixels, a fixed limited palette, and ordered (Bayer) dithering to
fake the in-between colors -- drawn straight into /dev/fb0.

Made for PRODUCTION's 2x2 CRT wall at XGA (1024x768): the default
256x192 (the ZX Spectrum/MSX resolution) is exactly 4x4 framebuffer
pixels per chunky pixel there, 128x96 per tube. Other framebuffer sizes
stretch to fit (nearest neighbor, so the blocks stay hard-edged).

How it works (the decode/timing/overlay pipeline is fb_video.py's, shared
with ASCII mode): ffmpeg shrinks the video to the pixel grid; each frame
gets a color/contrast boost (smoky SD music videos are mostly pale tints
that a small palette would flatten to gray), then the 8x8 Bayer threshold
pattern is added and every pixel snaps to its nearest palette color.
Ordered dithering, not error diffusion, because its pattern stays put
from frame to frame -- error diffusion crawls and shimmers on video.

Nearest-color is a 65536-entry lookup table indexed by the dithered
color quantized to RGB565 (built once per palette), so even the 50-color
NES palette is a couple of milliseconds a frame.
"""

import numpy as np

from fb_video import FramebufferVideo


def _hexes(text):
    return np.array([[int(h[i:i + 2], 16) for i in (0, 2, 4)] for h in text.split()], dtype=np.float32)


# Name -> colors. Order here is the ABOUT screen's PALETTE cycle order.
PALETTES = {
    "NES": _hexes("000000 626262 0D2DA4 4412B9 6B03A6 7C0362 760F0D 5E2400 3B3B00 135300 006000 005A12 004D59 "
                  "ABABAB 2F62F4 7140FF A729F7 C925AE D3313F C64C05 9F6B00 6C8800 30A000 06A13C 009497 "
                  "FFFFFF 7EB1FF B78FFF EC74FF FF6DE7 FF7695 FF916A F4B04A CBCD35 91E64F 5DED87 4AE3D9 4E4E4E "
                  "C2DBFF DDCBFF F6C1FF FFBDF2 FFC2D5 FFCCB9 FFDCAC E8EBA4 C3F3AC A7F7C6 9CF2EF B8B8B8"),
    "PICO-8": _hexes("000000 1D2B53 7E2553 008751 AB5236 5F574F C2C3C7 FFF1E8 "
                     "FF004D FFA300 FFEC27 00E436 29ADFF 83769C FF77A8 FFCCAA"),
    "EGA": _hexes("000000 0000AA 00AA00 00AAAA AA0000 AA00AA AA5500 AAAAAA "
                  "555555 5555FF 55FF55 55FFFF FF5555 FF55FF FFFF55 FFFFFF"),
    "C64": _hexes("000000 FFFFFF 68372B 70A4B2 6F3D86 588D43 352879 B8C76F "
                  "6F4F25 433900 9A6759 444444 6C6C6C 9AD284 6C5EB5 959595"),
    "GAME BOY": _hexes("0F380F 306230 8BAC0F 9BBC0F"),
    "CGA": _hexes("000000 55FFFF FF55FF FFFFFF"),
}
DEFAULT_PALETTE = "NES"
DEFAULT_SIZE = (256, 192)

BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], dtype=np.float32) / 64 - 0.5
# Nearest-color distance weights (R, G, B) -- roughly how strongly the eye
# notices each channel's difference.
COLOR_WEIGHTS = np.array([2.0, 4.0, 3.0], dtype=np.float32)
# Pre-palette boost, picked from a comparison sheet of real frames
# (2026-10-23): saturation pushes pale tints toward their hue, contrast
# around mid-gray keeps darks from all landing on one color.
SATURATION = 1.6
CONTRAST = 1.15
LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _squash(text):
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def palette_name(name):
    """settings.ini pixel_palette -> a PALETTES key, ignoring case, spaces
    and dashes ("gameboy", "Pico8" ...); unknown names get the default."""
    for key in PALETTES:
        if _squash(key) == _squash(name):
            return key
    return DEFAULT_PALETTE


def parse_size(text):
    """settings.ini pixel_size, "WIDTHxHEIGHT" -> (w, h)."""
    try:
        w, h = (int(v) for v in str(text).lower().split("x"))
        if w > 0 and h > 0:
            return w, h
    except ValueError:
        pass
    return DEFAULT_SIZE


class PixelVideo(FramebufferVideo):
    def __init__(self, fb, palette=DEFAULT_PALETTE, size=DEFAULT_SIZE):
        self.cols, self.rows = size
        super().__init__(fb)
        # Framebuffer pixel -> grid pixel, nearest neighbor. When the
        # framebuffer is an exact multiple of the grid (the XGA wall) each
        # pixel is broadcast into a preallocated (rows, ry, cols, rx) block
        # instead -- 8ms vs 12ms fancy indexing (24ms for np.repeat) on a
        # Pi 3B+. That buffer is reused, so a composed frame is only valid
        # until the next _compose (fine: it's presented right away).
        self.blocks = None
        if fb.width % self.cols == 0 and fb.height % self.rows == 0:
            self.blocks = np.empty((self.rows, fb.height // self.rows, self.cols, fb.width // self.cols), np.uint16)
        self.ys = (np.arange(fb.height) * self.rows // fb.height).astype(np.intp)
        self.xs = (np.arange(fb.width) * self.cols // fb.width).astype(np.intp)
        self.bayer = np.tile(BAYER8, (self.rows // 8 + 1, self.cols // 8 + 1))[:self.rows, :self.cols, None]
        # The boost folded into one multiply-add, done in place:
        # (lum + (rgb - lum) * S - 128) * C + 128
        #   = rgb * S*C + lum * (1-S)*C + 128 - 128*C
        self.gain = np.float32(SATURATION * CONTRAST)
        self.luma_gain = (LUMA * np.float32((1 - SATURATION) * CONTRAST)).astype(np.float32)
        self.set_palette(palette)

    def set_palette(self, name):
        """Rebuilds the lookup table (~0.1s for NES on a Pi 3B+) -- only
        ever called from the guide/ABOUT, never mid-playback."""
        self.palette = palette_name(name)
        pal = PALETTES[self.palette]
        # Dither amplitude ~ the spacing between palette colors, so small
        # palettes get a stronger pattern. Carries the boost's constant term.
        self.offset = (self.bayer * (255.0 / np.cbrt(len(pal))) + 128.0 * (1 - CONTRAST)).astype(np.float32)
        # Every RGB565 color (bit-expanded back to 8 bits/channel) -> its
        # nearest palette color, already packed as RGB565.
        idx = np.arange(65536, dtype=np.uint32)
        r = (((idx >> 11) & 0x1F) * (255.0 / 31)).astype(np.float32)
        g = (((idx >> 5) & 0x3F) * (255.0 / 63)).astype(np.float32)
        b = ((idx & 0x1F) * (255.0 / 31)).astype(np.float32)
        best = np.zeros(65536, dtype=np.intp)
        best_d = np.full(65536, np.inf, dtype=np.float32)
        wr, wg, wb = COLOR_WEIGHTS
        for k, (pr, pg, pb) in enumerate(pal):
            d = wr * np.square(r - pr) + wg * np.square(g - pg) + wb * np.square(b - pb)
            closer = d < best_d
            best[closer] = k
            best_d[closer] = d[closer]
        pal565 = ((pal[:, 0].astype(np.uint16) >> 3) << 11) | ((pal[:, 1].astype(np.uint16) >> 2) << 5) \
            | (pal[:, 2].astype(np.uint16) >> 3)
        self.lut = pal565[best]
        self.dirty = True

    def _compose(self, buf):
        rgb = np.frombuffer(buf, dtype=np.uint8).reshape(self.rows, self.cols, 3).astype(np.float32)
        lum = rgb @ self.luma_gain
        rgb *= self.gain
        rgb += lum[:, :, None]
        rgb += self.offset
        np.clip(rgb, 0, 255, out=rgb)
        q = rgb.astype(np.uint16)
        index = (q[:, :, 0] >> 3) << 11
        index |= (q[:, :, 1] >> 2) << 5
        index |= q[:, :, 2] >> 3
        small = self.lut[index]
        if self.blocks is not None:
            self.blocks[...] = small[:, None, :, None]
            return self.blocks.reshape(self.fb.height, self.fb.width)
        return small[self.ys][:, self.xs]
