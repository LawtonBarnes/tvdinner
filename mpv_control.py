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

SOCKET_PATH = "/tmp/tvdinner-mpv.sock"
SOCKET_CONNECT_TIMEOUT_S = 5
SOCKET_CONNECT_POLL_S = 0.05


class MpvController:
    def __init__(self):
        self.proc = None
        self.sock = None
        self._next_request_id = 1
        self._pending = {}  # request_id -> None (fire-and-forget; we don't block on replies)

    def start(self):
        if os.path.exists(SOCKET_PATH):
            os.remove(SOCKET_PATH)
        self.proc = subprocess.Popen(
            MPV_ARGS + [f"--input-ipc-server={SOCKET_PATH}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + SOCKET_CONNECT_TIMEOUT_S
        while time.monotonic() < deadline:
            if os.path.exists(SOCKET_PATH):
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(SOCKET_PATH)
                    self.sock.setblocking(False)
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

    def add_overlay(self, overlay_id, x, y, raw_path, width, height, stride):
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
        for line in buf.decode("utf-8", errors="replace").splitlines():
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
