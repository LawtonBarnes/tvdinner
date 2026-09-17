"""Direct /dev/fb0 writer, duplicated from bars.py's FrameBuffer (per this
project's no-shared-library convention -- see project_mcbrain memory).

Screen updates go straight into /dev/fb0's memory rather than through
SDL/DRM for the EPG: the vc4-fkms-v3d driver rejects the async page flips
SDL issues on every flip() call, causing a visible ~1s blank on every
update on this hardware. pygame here runs fully headless (SDL_VIDEODRIVER
=dummy, no display.set_mode) purely to build surfaces/render fonts; the
keyboard is read directly via evdev instead of through SDL's input path.

mpv's own --vo=drm output (used during video playback) is unrelated to
this -- it takes real DRM/KMS master directly, bypassing this legacy
fbdev compatibility layer entirely, so the two never fight over the
display as long as they're never both actively writing at once (they
aren't -- see player.py's handoff).
"""

import fcntl
import os
import mmap

import numpy as np
import pygame

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01


class FrameBuffer:
    def __init__(self, dev="/dev/fb0"):
        from pathlib import Path
        sys_dir = Path("/sys/class/graphics") / Path(dev).name
        self.width, self.height = (int(x) for x in (sys_dir / "virtual_size").read_text().split(","))
        self.bpp = int((sys_dir / "bits_per_pixel").read_text())
        self.stride = int((sys_dir / "stride").read_text())
        self.bypp = self.bpp // 8
        self.row_bytes = self.width * self.bypp
        size = self.stride * self.height
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        if self.bpp not in (16, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")

    def write_surface(self, surface):
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        arr = pygame.surfarray.pixels3d(surface).transpose(1, 0, 2)  # (H, W, RGB) uint8
        if self.bpp == 16:
            r = arr[:, :, 0].astype(np.uint16) >> 3
            g = arr[:, :, 1].astype(np.uint16) >> 2
            b = arr[:, :, 2].astype(np.uint16) >> 3
            raw = ((r << 11) | (g << 5) | b).astype("<u2").tobytes()
        else:
            alpha = np.zeros((self.height, self.width, 1), dtype=np.uint8)
            raw = np.concatenate([arr[:, :, ::-1], alpha], axis=2).astype(np.uint8).tobytes()

        if self.stride == self.row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.height):
                self.mm.seek(y * self.stride)
                self.mm.write(raw[y * self.row_bytes : (y + 1) * self.row_bytes])

    def fill_black(self):
        """Paints the whole framebuffer black. Used once when entering
        playback mode: mpv's --vo=drm takes real DRM master directly, but if
        it ever momentarily drops master during a file transition (observed
        as a split-second flash of the GUIDE, since fbdev still held the
        last-rendered guide frame), black shows through instead of stale
        guide content."""
        self.mm.seek(0)
        self.mm.write(b"\x00" * (self.stride * self.height))

    def close(self):
        self.mm.close()
        os.close(self.fd)


def enter_console_graphics_mode():
    """Returns (tty_fd, enabled). Call restore_console_text_mode() with the
    same values on exit."""
    import sys
    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
        fcntl.ioctl(tty_fd, KDSETMODE, KD_GRAPHICS)
        return tty_fd, True
    except OSError as exc:
        print(f"Console graphics mode not available: {exc}", file=sys.stderr)
        return None, False


def restore_console_text_mode(tty_fd, enabled):
    if enabled:
        fcntl.ioctl(tty_fd, KDSETMODE, KD_TEXT)
        os.write(tty_fd, b"\033[2J\033[H")
    if tty_fd is not None:
        os.close(tty_fd)
