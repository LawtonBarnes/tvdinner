"""Playback-mode controller: owns the mpv subprocess + overlay state while
a video is playing, and implements the remote's playback-mode behavior
exactly as specified:

- UP/DOWN: change channel (increase/decrease channel number, wrapping),
  starting that channel's first program. Shows the CH indicator.
- LEFT/RIGHT: previous/next program within the CURRENT channel (wrapping).
  Shows the < SKIP / SKIP > indicator.
- OK: toggle pause. Paused shows a persistent red PAUSE (bottom center)
  until unpaused -- no indicator for the normal PLAY state.
- Vol Down: mute (persistent red MUTE, top left) until Vol Up (green VOL+
  for 2s, then clears) -- binary toggle, not multi-step gain (confirmed
  2026-09-16, matches the fleet's line-level-PCM-enforced convention).
- BACK: stop playback, signal the caller to return to the guide.
- Reaching the natural end of a file (not user-initiated) auto-advances to
  the next program in the same channel and keeps playing, same as a real
  TV channel continuing to its next scheduled program -- not a documented
  requirement, but the obvious behavior for a "channel" that isn't just a
  single video.

Channel-changing here is deliberately independent state from the guide's
own NavigationState -- selecting OK on a highlighted program starts
playback there, but panning through channels via remote while *playing*
doesn't retroactively move the guide's own cursor (matches the mental
model: the guide is where you browse, playback is where you watch).
"""

INDICATOR_DURATION_S = 2.0


class PlaybackController:
    def __init__(self, mpv, overlays, channels):
        self.mpv = mpv
        self.overlays = overlays
        self.channels = channels  # shared reference to the same channel list the guide uses
        self.channel_index = 0
        self.program_index = 0

    def _channel(self):
        return self.channels[self.channel_index]

    def _program(self):
        return self._channel()["programs"][self.program_index]

    def _media_path(self, media_root, program):
        return f"{media_root}/{program['relpath']}"

    def start(self, media_root, channel_index, program_index):
        self.media_root = media_root
        self.channel_index = channel_index
        self.program_index = program_index
        self.mpv.start()
        self._load_current(show_channel_indicator=True)

    def _load_current(self, show_channel_indicator=False):
        program = self._program()
        self.mpv.load_file(self._media_path(self.media_root, program))
        self.mpv.set_pause(False)
        self.overlays.hide("pause")
        if show_channel_indicator:
            channel_number = f"{self._channel()['number']:02d}"
            self.overlays.show("ch", f"CH {channel_number}", (0, 255, 0, 255), duration=INDICATOR_DURATION_S)

    def change_channel(self, step):
        self.channel_index = (self.channel_index + step) % len(self.channels)
        self.program_index = 0
        self._load_current(show_channel_indicator=True)

    def change_program(self, step):
        count = len(self._channel()["programs"])
        self.program_index = (self.program_index + step) % count
        label = "< SKIP" if step < 0 else "SKIP >"
        name = "skip_left" if step < 0 else "skip_right"
        self.overlays.show(name, label, (0, 255, 0, 255), duration=INDICATOR_DURATION_S)
        self._load_current(show_channel_indicator=False)

    def toggle_pause(self):
        # We track pause state ourselves rather than querying mpv (fire-and-
        # forget IPC, no request/response round trip needed for this).
        self._paused = not getattr(self, "_paused", False)
        self.mpv.set_pause(self._paused)
        if self._paused:
            self.overlays.show("pause", "PAUSE", (255, 0, 0, 255), duration=None)
        else:
            self.overlays.hide("pause")

    def mute(self):
        self.mpv.set_mute(True)
        self.overlays.hide("vol")
        self.overlays.show("mute", "MUTE", (255, 0, 0, 255), duration=None)

    def unmute(self):
        self.mpv.set_mute(False)
        self.overlays.hide("mute")
        self.overlays.show("vol", "VOL+", (0, 255, 0, 255), duration=INDICATOR_DURATION_S)

    def handle_end_of_file(self):
        """Called when mpv reports end-file with reason 'eof' (natural end,
        not a user-initiated load/stop) -- advance like a continuing channel."""
        self.change_program(1)

    def stop(self):
        self.overlays.hide_all()
        self.mpv.close()
