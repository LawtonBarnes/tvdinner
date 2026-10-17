"""ASCII mode: plays the video as colored text characters on black, drawn
straight into /dev/fb0, instead of handing the picture to mpv.

Made for PRODUCTION's 2x2 CRT wall at XGA (1024x768): with the Linux
console's 8x8 VGA font that's a 128x96 character grid across the wall,
64x48 per tube -- a soft SD source turned into crisp lettering the CRTs
render sharply. Enabled per machine by settings.ini ascii_mode (see
settings.example.ini); off everywhere else.

How it works:
  * mpv still plays the file, audio only (--vid=no), with the usual gain
    compensation, pause/mute, and end-of-file auto-advance -- its playback
    clock (time-pos) is what every frame is timed against.
  * ffmpeg decodes the video, already shrunk to one pixel per character
    cell at a fixed frame rate, into a pipe (cheap -- the expensive part of
    a full-size decode is the scaling/output, not the reading).
  * Each cell's brightness picks a character from a density ramp built
    from the font itself (glyphs sorted by how many pixels they light), and
    the character is tinted with the cell's own color at full brightness,
    so brightness is carried by the letter shape and hue by its color.
  * The whole frame is composed in numpy from a glyph atlas (no per-
    character drawing) and written to the framebuffer as RGB565.

Playback indicators (CH, SKIP, PAUSE, MUTE) normally go through mpv's
overlay-add -- here this class stands in for that interface
(add_overlay/remove_overlay), so overlay.py is unchanged.
"""

import gzip
import os
import queue
import shutil
import subprocess
import threading
import time

import numpy as np

FONT_DIR = "/usr/share/consolefonts"
DEFAULT_FONT = "Lat15-VGA8"


def font_path(name):
    """settings.ini ascii_font: a console font name (e.g. Lat15-VGA14,
    looked up in FONT_DIR) or a full path to a .psf/.psf.gz file."""
    if os.sep in name:
        return name
    return os.path.join(FONT_DIR, name if name.endswith((".psf", ".psf.gz")) else name + ".psf.gz")


FONT_FILE = font_path(DEFAULT_FONT)
FPS = 30
QUEUE_FRAMES = 8
# Brightness below this (0-255) is drawn as empty black, so dark areas of
# the picture really go black instead of a field of dots.
BLACK_LEVEL = 24
# Default color boost (settings.ini ascii_saturation): how hard each
# character's tint is pushed from white toward its pure hue. Smoky, stage-lit
# SD music videos are mostly pale tints, and composite video mutes those
# further -- at 1.0 the wall read as "nearly monochrome" (user, 2026-10-16).
DEFAULT_SATURATION = 3.0

# The overlay coordinates/sizes in overlay.py are for the 720x480 frame
# mpv normally fills; scale them to whatever this framebuffer is.
OVERLAY_SPACE = (720, 480)


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


class AsciiVideo:
    def __init__(self, fb, font_path=FONT_FILE, saturation=DEFAULT_SATURATION):
        if shutil.which("ffmpeg") is None:
            raise OSError("ffmpeg not installed")
        self.fb = fb
        self.saturation = max(0.0, float(saturation))
        bits, mapping = _load_psf(font_path)
        self.chars, glyphs = _build_ramp(bits, mapping)
        self.cell_h, self.cell_w = glyphs.shape[1:]
        self.cols = fb.width // self.cell_w
        self.rows = fb.height // self.cell_h
        self.glyphs = glyphs.astype(np.uint16)  # (levels, h, w) 0/1
        self.levels = len(glyphs)
        # Frame area actually covered by whole cells, centered.
        self.x0 = (fb.width - self.cols * self.cell_w) // 2
        self.y0 = (fb.height - self.rows * self.cell_h) // 2
        self.proc = None
        self.reader = None
        self.frames = queue.Queue(maxsize=QUEUE_FRAMES)
        self.pending = None
        self.shown_index = -1
        self.base = None  # last composed text frame (H, W) uint16, before overlays
        self.overlays = {}  # id -> (x, y, rgb565 (h, w), alpha mask (h, w))
        self.dirty = False
        self.clock_pos = None
        self.clock_at = 0.0
        self.paused = False

    # -- video pipeline ---------------------------------------------------

    def start(self, path):
        self.stop()
        self.frames = queue.Queue(maxsize=QUEUE_FRAMES)
        self.pending = None
        self.shown_index = -1
        self.clock_pos = None
        frame_bytes = self.cols * self.rows * 3
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-an", "-sn",
             "-vf", f"fps={FPS},scale={self.cols}:{self.rows}:flags=area",
             "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        proc, frames = self.proc, self.frames

        def read():
            # The queue stays small and blocks when full, so ffmpeg only
            # decodes a few frames ahead of what's on screen.
            index = 0
            while True:
                buf = proc.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    return
                while True:
                    try:
                        frames.put((index, buf), timeout=0.5)
                        break
                    except queue.Full:
                        if self.proc is not proc:
                            return  # stopped/restarted while waiting
                index += 1

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is not None:
            proc.kill()
            try:
                proc.stdout.close()
            except OSError:
                pass
            proc.wait()

    # -- clock (fed from mpv's time-pos property) --------------------------

    def set_time(self, pos):
        if pos is not None:
            self.clock_pos = float(pos)
            self.clock_at = time.monotonic()

    def set_paused(self, paused):
        if self.clock_pos is not None and not paused and self.paused:
            self.clock_at = time.monotonic()
        self.paused = paused

    def _now(self):
        if self.clock_pos is None:
            return None
        if self.paused:
            return self.clock_pos
        return self.clock_pos + (time.monotonic() - self.clock_at)

    # -- rendering --------------------------------------------------------

    def tick(self):
        """Call often (every loop iteration while playing): shows the
        frame that matches the audio clock, dropping late ones."""
        now = self._now()
        if now is not None:
            target = int(now * FPS)
            newest = None
            while True:
                if self.pending is None:
                    try:
                        self.pending = self.frames.get_nowait()
                    except queue.Empty:
                        break
                if self.pending[0] > target:
                    break
                newest, self.pending = self.pending, None
            if newest is not None and newest[0] != self.shown_index:
                self.shown_index = newest[0]
                self.base = self._compose(newest[1])
                self.dirty = True
        if self.dirty:
            self._present()
            self.dirty = False

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

    def _present(self):
        frame = self.base if self.base is not None else np.zeros((self.fb.height, self.fb.width), np.uint16)
        if self.overlays:
            frame = frame.copy()
            for x, y, pix, mask in self.overlays.values():
                h = min(pix.shape[0], self.fb.height - y)
                w = min(pix.shape[1], self.fb.width - x)
                if h <= 0 or w <= 0:
                    continue
                region = frame[y:y + h, x:x + w]
                region[mask[:h, :w]] = pix[:h, :w][mask[:h, :w]]
        self.fb.write_raw565(frame)

    # -- mpv overlay-add stand-in (see overlay.OverlayManager) -------------

    def add_overlay(self, overlay_id, x, y, raw_path, width, height, stride):
        with open(raw_path, "rb") as f:
            bgra = np.frombuffer(f.read(), dtype=np.uint8).reshape(height, stride // 4, 4)[:, :width]
        sx = self.fb.width / OVERLAY_SPACE[0]
        sy = self.fb.height / OVERLAY_SPACE[1]
        # nearest-neighbor scale to this framebuffer's size (keeps the
        # pixel font's hard edges)
        out_h, out_w = max(1, round(height * sy)), max(1, round(width * sx))
        ys = (np.arange(out_h) / sy).astype(np.intp).clip(0, height - 1)
        xs = (np.arange(out_w) / sx).astype(np.intp).clip(0, width - 1)
        img = bgra[ys][:, xs]
        b, g, r, a = (img[:, :, k].astype(np.uint16) for k in range(4))
        pix = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        self.overlays[overlay_id] = (round(x * sx), round(y * sy), pix, a > 127)
        self.dirty = True

    def remove_overlay(self, overlay_id):
        if self.overlays.pop(overlay_id, None) is not None:
            self.dirty = True
