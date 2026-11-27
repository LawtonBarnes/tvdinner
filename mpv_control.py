"""mpv subprocess + JSON IPC control. Confirmed working config on this
hardware (Pi 3B+, vc4-fkms-v3d): --vo=drm (legacy modesetting -- gpu-next
hits a real GLSL shader-compiler bug on this Mesa/V3D driver version, dead
end, don't retry it) + --hwdec=v4l2m2m-copy (hardware H.264 decode via
/dev/video10, confirmed live) + --keepaspect=no (640x480 source
deliberately stretched to fill the real 720x480 display, matching the
non-square-pixel compensation the EPG side also does). Some frame drops
are expected/acceptable on this source material (low-quality YouTube rips)
-- confirmed acceptable by the user watching a real CRT, not just assumed.

Runs as a plain subprocess (not wrapped in its own openvt) -- the whole
TV DINNER process is already the active tty1 session (STRINGS launches it
via openvt, see the launcher script), so a child process inherits that
and can become DRM master itself with no extra ceremony.
"""

import json
import os
import re
import selectors
import socket
import subprocess
import time

MPV_ARGS = [
    "mpv",
    "--vo=drm",
    "--fullscreen",
    "--no-osc",
    "--no-input-default-bindings",
    "--no-input-terminal",
    "--keepaspect=no",
    "--hwdec=v4l2m2m-copy",
    "--idle=yes",  # stay alive between files instead of exiting, we drive it explicitly
]

# ASCII mode (ascii_video.py draws the picture itself): mpv only plays the
# audio and reports its clock -- no video decode, and it never takes DRM
# master, so the framebuffer stays ours.
AUDIO_ONLY_ARGS = [
    "mpv",
    "--vid=no",
    "--vo=null",
    "--no-osc",
    "--no-input-default-bindings",
    "--no-input-terminal",
    "--idle=yes",
]
TIME_POS_OBSERVER_ID = 1

SOCKET_PATH = "/tmp/tvdinner-mpv.sock"
SOCKET_CONNECT_TIMEOUT_S = 5
SOCKET_CONNECT_POLL_S = 0.05


def console_drm_mode():
    """The console's own mode as an mpv --drm-mode value, but only when the
    connected screen's PREFERRED mode is a different size; otherwise None
    (mpv's default, the preferred mode, is already right -- HIDEF keeps its
    native 1080i that way).

    Why: mpv --vo=drm switches to the preferred mode. QUAD's OREI EDID
    prefers 3840x2160, so every video put the wall at 4K and every gap
    between videos dropped back to the 1024x768 console -- the OREI
    re-synced (color bars) on each song change."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as f:
            width, height = (int(v) for v in f.read().strip().split(","))
    except (OSError, ValueError):
        return None
    drm = "/sys/class/drm"
    try:
        connectors = sorted(os.listdir(drm))
    except OSError:
        return None
    for name in connectors:
        try:
            with open(os.path.join(drm, name, "status")) as f:
                if f.read().strip() != "connected":
                    continue
            with open(os.path.join(drm, name, "modes")) as f:
                preferred = f.readline().strip()  # first line = preferred
        except OSError:
            continue
        # composite on the Pi 3B+ puppets reports "FIXED_MODE", not a size:
        # leave those alone
        match = re.match(r"(\d+)x(\d+)", preferred)
        if not match:
            continue
        if (int(match.group(1)), int(match.group(2))) != (width, height):
            return f"{width}x{height}@60"
        return None
    return None


class MpvController:
    def __init__(self, audio_only=False, pillarbox=False, x_offset=0):
        """pillarbox: a 16:9 screen (framebuffer.WIDE_ASPECT) -- mpv keeps
        the source aspect (4:3 lands centered with black bars, NEVER
        stretched) and overlays shift right by x_offset into the picture."""
        self.audio_only = audio_only
        self.pillarbox = pillarbox
        self.x_offset = x_offset
        self.proc = None
        self.sock = None
        self._next_request_id = 1
        self._pending = {}  # request_id -> None (fire-and-forget; we don't block on replies)
        self._partial = b""  # an incomplete trailing line from the last recv

    def start(self):
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)
        self.proc = subprocess.Popen(
            self._args() + [f"--input-ipc-server={SOCKET_PATH}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + SOCKET_CONNECT_TIMEOUT_S
        while time.monotonic() < deadline:
            if os.path.exists(SOCKET_PATH):
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(SOCKET_PATH)
                    self.sock.setblocking(False)
                    if self.audio_only:
                        # time-pos arrives as property-change events -- the
                        # clock ASCII mode times its frames against
                        self._send(["observe_property", TIME_POS_OBSERVER_ID, "time-pos"])
                    return
                except OSError:
                    pass
            time.sleep(SOCKET_CONNECT_POLL_S)
        raise RuntimeError("mpv IPC socket never became available")

    def fileno(self):
        return self.sock.fileno()

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def _send(self, command):
        request_id = self._next_request_id
        self._next_request_id += 1
        payload = json.dumps({"command": command, "request_id": request_id}) + "\n"
        try:
            self.sock.sendall(payload.encode("utf-8"))
        except OSError:
            pass
        return request_id

    def load_file(self, path):
        self._send(["loadfile", path, "replace"])

    def set_pause(self, paused):
        self._send(["set_property", "pause", paused])

    def set_mute(self, muted):
        self._send(["set_property", "mute", muted])

    def set_audio_gain(self, db):
        """Static per-file gain compensation (from the indexer's volumedetect
        pass) -- not a live/dynamic auto-leveler, just a fixed dB boost
        decided once at load time. 0 (or near enough) clears the filter
        entirely rather than inserting a harmless-but-pointless unity-gain
        one."""
        if abs(db) > 0.05:
            self._send(["af", "set", f"volume=volume={db:.1f}dB"])
        else:
            self._send(["af", "clear"])

    def quit(self):
        self._send(["quit"])

    def _args(self):
        if self.audio_only:
            return AUDIO_ONLY_ARGS
        args = MPV_ARGS
        if self.pillarbox:
            args = ["--keepaspect=yes" if a == "--keepaspect=no" else a for a in args]
        mode = console_drm_mode()
        if mode:
            args = args + [f"--drm-mode={mode}"]
        return args

    def add_overlay(self, overlay_id, x, y, raw_path, width, height, stride):
        if not self.audio_only:
            x += self.x_offset
        self._send(["overlay-add", overlay_id, x, y, raw_path, 0, "bgra", width, height, stride])

    def remove_overlay(self, overlay_id):
        self._send(["overlay-remove", overlay_id])

    def read_events(self):
        """Non-blocking: returns a list of parsed JSON event dicts with an
        'event' key (property-change replies without one are ignored --
        we're fire-and-forget on those). Call only when fileno() is
        select-ready for read."""
        events = []
        try:
            buf = self.sock.recv(65536)
        except (BlockingIOError, OSError):
            return events
        if not buf:
            return events
        # A message can be split across two reads (more likely now that ASCII
        # mode observes time-pos, a steady stream of events) -- keep the
        # unfinished tail for next time instead of dropping it, which could
        # otherwise lose an end-file event and stall auto-advance.
        *lines, self._partial = (self._partial + buf).split(b"\n")
        for line in (raw.decode("utf-8", errors="replace") for raw in lines):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in msg:
                events.append(msg)
        return events

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
