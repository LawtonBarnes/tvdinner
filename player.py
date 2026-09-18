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
  single video. No SKIP overlay and no guide flash on this transition
  (confirmed bug, 2026-09-17) -- it should read as one continuous channel,
  not a series of user-visible skips. See _advance_to_next_program() and
  app.py's enter_playback() (which paints the framebuffer black once on
  entry so nothing stale can show through if mpv ever drops DRM master
  mid-transition).

Channel-changing here is deliberately independent state from the guide's
own NavigationState -- selecting OK on a highlighted program starts
playback there, but panning through channels via remote while *playing*
doesn't retroactively move the guide's own cursor (matches the mental
model: the guide is where you browse, playback is where you watch).

**Per-file gain compensation (2026-09-17):** the indexer's volumedetect
pass measured wildly inconsistent peak levels across the library (these
are YouTube rips, not a mastered source) -- 37% of files already sit at
0 dB peak (zero headroom) while the quietest is over 20 dB below that.
_load_current() applies a static, boost-only gain per file so quiet
tracks get turned up to roughly match the loud ones -- this is NOT a
live/dynamic auto-leveler (the user was explicit about not wanting
that): the gain is computed once from the indexer's stored peak value
and set as a fixed mpv audio filter for that file's entire playback,
identical in spirit to the fleet's line-level-PCM-enforced convention
elsewhere ([[project_bars]]/[[project_mcbrain]]) -- a fixed level
decided in advance, not something that reacts to the audio as it plays.
"""

INDICATOR_DURATION_S = 2.0
CH_INDICATOR_DURATION_S = INDICATOR_DURATION_S * 2  # CH indicator stays up ~double as long (user request)

# Boost-only gain compensation target -- see the module docstring. Tracks
# already at or above this peak get zero adjustment (nothing to gain, and
# never attenuated); quieter ones get turned up toward it.
TARGET_PEAK_DB = -1.0
# Safety ceiling on the computed boost itself, independent of the target
# above -- guards against a bad/corrupt peak_volume_db value ever producing
# an absurd gain. The real library's worst case (Megadeth - Symphony Of
# Destruction) only needs +20.3dB, comfortably under this.
MAX_GAIN_DB = 24.0


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

    def _gain_for(self, program):
        peak = program.get("peak_volume_db")
        if peak is None:
            return 0.0  # no measurement (old cache entry, or a failed probe) -- leave it alone
        return min(MAX_GAIN_DB, max(0.0, TARGET_PEAK_DB - peak))

    def start(self, media_root, channel_index, program_index):
        self.media_root = media_root
        self.channel_index = channel_index
        self.program_index = program_index
        self.mpv.start()
        self._load_current(show_channel_indicator=True)

    def _load_current(self, show_channel_indicator=False):
        program = self._program()
        self.mpv.set_audio_gain(self._gain_for(program))
        self.mpv.load_file(self._media_path(self.media_root, program))
        self.mpv.set_pause(False)
        self.overlays.hide("pause")
        if show_channel_indicator:
            channel_number = f"{self._channel()['number']:02d}"
            self.overlays.show("ch", f"CH {channel_number}", (0, 255, 0, 255), duration=CH_INDICATOR_DURATION_S)

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

    def _advance_to_next_program(self):
        """Natural continuation to the next program in the same channel --
        no SKIP overlay (that's reserved for the user actually pressing
        LEFT/RIGHT), since this should read as one continuous channel."""
        count = len(self._channel()["programs"])
        self.program_index = (self.program_index + 1) % count
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
        self._advance_to_next_program()

    def stop(self):
        self.overlays.hide_all()
        self.mpv.close()
