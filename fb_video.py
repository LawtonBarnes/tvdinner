"""Shared pipeline for the video modes that draw the picture themselves,
straight into /dev/fb0, instead of handing it to mpv -- ASCII mode
(ascii_video.py), PIXEL mode (pixel_video.py) and DOT MASK mode
(mask_video.py).

How it works:
  * mpv still plays the file, audio only (--vid=no), with the usual gain
    compensation, pause/mute, and end-of-file auto-advance -- its playback
    clock (time-pos) is what every frame is timed against.
  * ffmpeg decodes the video, already shrunk to the mode's own small grid
    (one pixel per character cell, or per chunky pixel) at a fixed frame
    rate, into a pipe (cheap -- the expensive part of a full-size decode is
    the scaling/output, not the reading).
  * The subclass turns each small frame into a full framebuffer frame in
    numpy (_compose), written to the framebuffer as RGB565.

Playback indicators (CH, SKIP, PAUSE, MUTE) normally go through mpv's
overlay-add -- here this class stands in for that interface
(add_overlay/remove_overlay), so overlay.py is unchanged.

Subclasses set self.cols/self.rows (the decode size) before calling
FramebufferVideo.__init__, and implement _compose(buf) -> (fb.height,
fb.width) uint16 RGB565. PIX_FMT/BYTES_PER_PIXEL/SCALE_FLAGS pick what
ffmpeg hands over (shrinking modes want area-averaged rgb24; a full-size
mode can take the framebuffer's own RGB565 directly).
"""

import queue
import shutil
import subprocess
import threading
import time

import numpy as np

FPS = 30
QUEUE_FRAMES = 8

# The overlay coordinates/sizes in overlay.py are for the 720x480 frame
# mpv normally fills; scale them to whatever this framebuffer is.
OVERLAY_SPACE = (720, 480)


class FramebufferVideo:
    PIX_FMT = "rgb24"
    BYTES_PER_PIXEL = 3
    SCALE_FLAGS = "area"

    def __init__(self, fb):
        if shutil.which("ffmpeg") is None:
            raise OSError("ffmpeg not installed")
        self.fb = fb
        self.proc = None
        self.reader = None
        self.frames = queue.Queue(maxsize=QUEUE_FRAMES)
        self.pending = None
        self.shown_index = -1
        self.base = None  # last composed frame (H, W) uint16, before overlays
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
        frame_bytes = self.cols * self.rows * self.BYTES_PER_PIXEL
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-an", "-sn",
             "-vf", f"fps={FPS},scale={self.cols}:{self.rows}:flags={self.SCALE_FLAGS}",
             "-pix_fmt", self.PIX_FMT, "-f", "rawvideo", "pipe:1"],
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
        raise NotImplementedError

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
