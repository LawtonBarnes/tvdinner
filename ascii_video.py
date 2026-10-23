"""ASCII mode: plays the video as colored text characters on black, drawn
straight into /dev/fb0, instead of handing the picture to mpv.

Made for PRODUCTION's 2x2 CRT wall at XGA (1024x768): with the Linux
console's 8x8 VGA font that's a 128x96 character grid across the wall,
64x48 per tube -- a soft SD source turned into crisp lettering the CRTs
render sharply. Enabled per machine by settings.ini ascii_mode (see
settings.example.ini); off everywhere else.

How it works (the decode/timing/overlay pipeline is fb_video.py's, shared
with PIXEL mode): ffmpeg shrinks the video to one pixel per character
cell; each cell's brightness picks a character from a density ramp built
from the font itself (glyphs sorted by how many pixels they light), and
the character is tinted with the cell's own color at full brightness, so
brightness is carried by the letter shape and hue by its color. The whole
frame is composed in numpy from a glyph atlas (no per-character drawing).
"""

import gzip
import os

import numpy as np

from fb_video import FramebufferVideo

FONT_DIR = "/usr/share/consolefonts"
DEFAULT_FONT = "Lat15-VGA8"


def font_path(name):
    """settings.ini ascii_font: a console font name (e.g. Lat15-VGA14,
    looked up in FONT_DIR) or a full path to a .psf/.psf.gz file."""
    if os.sep in name:
        return name
    return os.path.join(FONT_DIR, name if name.endswith((".psf", ".psf.gz")) else name + ".psf.gz")


FONT_FILE = font_path(DEFAULT_FONT)
# Brightness below this (0-255) is drawn as empty black, so dark areas of
# the picture really go black instead of a field of dots.
BLACK_LEVEL = 24
# Default color boost (settings.ini ascii_saturation): how hard each
# character's tint is pushed from white toward its pure hue. Smoky, stage-lit
# SD music videos are mostly pale tints, and composite video mutes those
# further -- at 1.0 the wall read as "nearly monochrome" (user, 2026-10-16).
DEFAULT_SATURATION = 3.0
# Default leading trim (settings.ini ascii_line_trim): pixel rows cut from
# each character cell's blank margin above/below the glyphs, so lines of
# text sit closer together and more of them fit. Never cuts lit pixels,
# and always leaves at least 1 black row between lines.
DEFAULT_LINE_TRIM = 0

def _load_psf(path):
    """PSF1/PSF2 console font -> (glyph bitmaps (N, h, w) bool, {codepoint: glyph index})."""
    data = gzip.open(path).read() if path.endswith(".gz") else open(path, "rb").read()
    if data[:2] == b"\x36\x04":  # PSF1: always 8 px wide
        mode, height = data[2], data[3]
        count = 512 if mode & 0x01 else 256
        width, offset, bytes_per_row = 8, 4, 1
        table = data[offset + count * height:] if mode & 0x02 else b""
        has_table, utf8 = bool(mode & 0x02), False
    elif data[:4] == b"\x72\xb5\x4a\x86":  # PSF2
        header = np.frombuffer(data[4:32], dtype="<u4")
        offset, flags, count, size, height, width = (int(v) for v in header[1:7])
        bytes_per_row = (width + 7) // 8
        table = data[offset + count * size:]
        has_table, utf8 = bool(flags & 0x01), True
    else:
        raise ValueError(f"{path}: not a PSF font")
    raw = np.frombuffer(data, dtype=np.uint8, count=count * height * bytes_per_row, offset=offset)
    bits = np.unpackbits(raw.reshape(count, height, bytes_per_row), axis=2)[:, :, :width].astype(bool)

    mapping = {}
    if has_table:
        glyph = 0
        if utf8:
            for entry in table.split(b"\xff")[:count]:
                for cp in entry.split(b"\xfe")[0].decode("utf-8", "ignore"):
                    mapping.setdefault(ord(cp), glyph)
                glyph += 1
        else:
            codes = np.frombuffer(table[: len(table) // 2 * 2], dtype="<u2")
            in_sequence = False
            for code in codes.tolist():
                if code == 0xFFFF:
                    glyph += 1
                    in_sequence = False
                elif code == 0xFFFE:
                    in_sequence = True
                elif not in_sequence:
                    mapping.setdefault(code, glyph)
    else:
        mapping = {cp: cp for cp in range(min(count, 256))}
    return bits, mapping


def _build_ramp(bits, mapping, levels=20):
    """Printable ASCII glyphs ordered by lit-pixel coverage, thinned to
    `levels` steps spread evenly across the coverage range -> (chars, glyph
    bitmaps). Index 0 is always the blank space."""
    cands = []
    for cp in range(33, 127):
        g = mapping.get(cp)
        if g is not None:
            cands.append((int(bits[g].sum()), chr(cp), g))
    cands.sort()
    if not cands:
        raise ValueError("font has no printable ASCII glyphs")
    lo, hi = cands[0][0], cands[-1][0]
    chosen, used = [], set()
    for k in range(levels - 1):
        target = lo + (hi - lo) * k / max(1, levels - 2)
        best = min((c for c in cands if c[1] not in used), key=lambda c: abs(c[0] - target), default=None)
        if best is not None:
            used.add(best[1])
            chosen.append(best)
    chosen.sort()
    blank = np.zeros_like(bits[0])
    glyphs = np.stack([blank] + [bits[g] for _, _, g in chosen])
    return " " + "".join(ch for _, ch, _ in chosen), glyphs


def _trim_leading(glyphs, trim):
    """Cut up to `trim` blank pixel rows from the ramp's cells, alternating
    bottom/top, keeping at least 1 blank row between lines of text."""
    lit = np.flatnonzero(glyphs.any(axis=(0, 2)))
    if len(lit) == 0:
        return glyphs
    top, bottom = lit[0], glyphs.shape[1] - 1 - lit[-1]
    trim = max(0, min(int(trim), top + bottom - 1))
    cut_bottom = min(bottom, (trim + 1) // 2)
    cut_top = min(top, trim - cut_bottom)
    cut_bottom = trim - cut_top
    return glyphs[:, cut_top:glyphs.shape[1] - cut_bottom]


class AsciiVideo(FramebufferVideo):
    def __init__(self, fb, font_path=FONT_FILE, saturation=DEFAULT_SATURATION, line_trim=DEFAULT_LINE_TRIM):
        self.saturation = max(0.0, float(saturation))
        bits, mapping = _load_psf(font_path)
        self.chars, glyphs = _build_ramp(bits, mapping)
        glyphs = _trim_leading(glyphs, line_trim)
        self.cell_h, self.cell_w = glyphs.shape[1:]
        self.cols = fb.width // self.cell_w
        self.rows = fb.height // self.cell_h
        self.glyphs = glyphs.astype(np.uint16)  # (levels, h, w) 0/1
        self.levels = len(glyphs)
        # Frame area actually covered by whole cells, centered.
        self.x0 = (fb.width - self.cols * self.cell_w) // 2
        self.y0 = (fb.height - self.rows * self.cell_h) // 2
        super().__init__(fb)

    def _compose(self, buf):
        rgb = np.frombuffer(buf, dtype=np.uint8).reshape(self.rows, self.cols, 3).astype(np.float32)
        lum = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
        level = np.clip((lum - BLACK_LEVEL) * ((self.levels - 1) / (255.0 - BLACK_LEVEL)) + 0.5,
                        0, self.levels - 1).astype(np.intp)
        level[lum < BLACK_LEVEL] = 0
        # tint = the cell's hue at full brightness (brightness itself is
        # carried by which character it is)
        peak = np.maximum(rgb.max(axis=2, keepdims=True), 1.0)
        tint = rgb * (1.0 / peak)  # 0..1, brightest channel = 1
        # saturation boost: scale each channel's distance below full by the
        # gain, so pale tints head toward their pure hue (grays stay white)
        tint = np.clip(1.0 - (1.0 - tint) * self.saturation, 0.0, 1.0) * 255.0
        r = tint[:, :, 0].astype(np.uint16) >> 3
        g = tint[:, :, 1].astype(np.uint16) >> 2
        b = tint[:, :, 2].astype(np.uint16) >> 3
        color = (r << 11) | (g << 5) | b  # (rows, cols)
        cells = self.glyphs[level] * color[:, :, None, None]  # (rows, cols, h, w)
        text = cells.transpose(0, 2, 1, 3).reshape(self.rows * self.cell_h, self.cols * self.cell_w)
        if text.shape == (self.fb.height, self.fb.width):
            return text
        frame = np.zeros((self.fb.height, self.fb.width), dtype=np.uint16)
        frame[self.y0:self.y0 + text.shape[0], self.x0:self.x0 + text.shape[1]] = text
        return frame
