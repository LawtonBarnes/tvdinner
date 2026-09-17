"""EPG navigation state machine -- pure logic, no pygame/hardware dependency
so it's cheap to verify with plain state traces before ever touching a
real screen.

Rules (all confirmed with the user):
- Highlight only ever occupies columns B/C/D x rows 2/3/4 (9 cells) --
  row 5 is a "peek" row, reached only by scrolling, never by direct highlight.
- LEFT/RIGHT move chronologically through the CURRENT channel's own program
  list: shifting the highlighted column while there's room (B<->C<->D), and
  once pinned to the far edge (B for LEFT, D for RIGHT), further presses
  advance that channel's own position pointer instead (scrolling its
  NOW/NEXT/LATER window) while the highlight stays pinned to that edge.
- UP/DOWN move the highlight between the 3 visible rows first; once pinned
  to the top (row 2, UP) or bottom (row 4, DOWN) edge, further presses
  scroll the channel list instead, while the highlight stays pinned to that
  edge row. The highlighted column (B/C/D) is preserved across channels --
  each channel remembers its own position pointer independently.
- Both the per-channel program list and the overall channel list wrap
  around circularly (modulo) rather than stopping at the ends -- confirmed
  2026-09-16: a channel with fewer than 3 programs cycles through what it
  has (e.g. a 2-program channel shows program 0/1/0) instead of leaving
  cells blank, and channel-list scrolling wraps top<->bottom the same way.
"""

COLUMN_OFFSETS = {"B": 0, "C": 1, "D": 2}
COLUMNS_IN_ORDER = ["B", "C", "D"]
TOP_ROW = 2
BOTTOM_ROW = 4
VISIBLE_ROWS = 4  # rows 2-5 shown on screen; only 2-4 are highlightable


class NavigationState:
    def __init__(self, channels):
        """channels: list of dicts, each with a 'programs' list (already
        sorted by mtime by the indexer). Order is ascending by channel
        number, like a normal channel dial -- NOT shuffled (only the
        programs within a channel are pre-shuffled, by the indexer)."""
        if not channels:
            raise ValueError("NavigationState needs at least one channel")
        self.channels = channels
        self.channel_window_start = [0] * len(channels)  # per-channel position pointer
        self.channel_scroll_offset = 0  # index of the topmost visible channel
        self.highlight_row = TOP_ROW
        self.highlight_col = "B"

    # -- internal helpers --------------------------------------------------

    def _channel_count(self):
        return len(self.channels)

    def _visible_channel_index(self, row):
        """row in 2..5 -> absolute channel index (circular)."""
        offset_in_window = row - TOP_ROW
        return (self.channel_scroll_offset + offset_in_window) % self._channel_count()

    def _current_channel_index(self):
        return self._visible_channel_index(self.highlight_row)

    def _program_count(self, channel_index):
        return len(self.channels[channel_index]["programs"])

    # -- navigation ----------------------------------------------------------

    def handle_up(self):
        if self.highlight_row > TOP_ROW:
            self.highlight_row -= 1
        else:
            self.channel_scroll_offset = (self.channel_scroll_offset - 1) % self._channel_count()

    def handle_down(self):
        if self.highlight_row < BOTTOM_ROW:
            self.highlight_row += 1
        else:
            self.channel_scroll_offset = (self.channel_scroll_offset + 1) % self._channel_count()

    def handle_left(self):
        channel_index = self._current_channel_index()
        count = self._program_count(channel_index)
        if self.highlight_col != "B":
            self.highlight_col = COLUMNS_IN_ORDER[COLUMN_OFFSETS[self.highlight_col] - 1]
        else:
            self.channel_window_start[channel_index] = (self.channel_window_start[channel_index] - 1) % count

    def handle_right(self):
        channel_index = self._current_channel_index()
        count = self._program_count(channel_index)
        if self.highlight_col != "D":
            self.highlight_col = COLUMNS_IN_ORDER[COLUMN_OFFSETS[self.highlight_col] + 1]
        else:
            self.channel_window_start[channel_index] = (self.channel_window_start[channel_index] + 1) % count

    # -- reads for rendering / playback --------------------------------------

    def visible_channels(self):
        """Returns 4 dicts (top-to-bottom, for rows 2-5): number/callsign +
        up to 3 visible programs (NOW/NEXT/LATER), cycling via modulo."""
        result = []
        for row in range(TOP_ROW, TOP_ROW + VISIBLE_ROWS):
            channel_index = self._visible_channel_index(row)
            channel = self.channels[channel_index]
            count = self._program_count(channel_index)
            start = self.channel_window_start[channel_index]
            programs = [channel["programs"][(start + i) % count] for i in range(3)]
            result.append({
                "number": channel["number"],
                "callsign": channel["callsign"],
                "programs": programs,
            })
        return result

    def highlight_position(self):
        """(col, row) for the renderer's draw_highlight -- row is always 2-4."""
        return self.highlight_col, self.highlight_row

    def current_indices(self):
        """(channel_index, program_index) -- absolute indices into
        self.channels / that channel's programs list, for handing off to
        the playback controller."""
        channel_index = self._current_channel_index()
        count = self._program_count(channel_index)
        start = self.channel_window_start[channel_index]
        offset = COLUMN_OFFSETS[self.highlight_col]
        program_index = (start + offset) % count
        return channel_index, program_index

    def sync_to(self, channel_index, program_index):
        """Re-point the highlight at an absolute (channel_index, program_index)
        -- used when returning from playback, so the guide highlight lands on
        whatever was actually playing (which may have drifted from the guide's
        own cursor via channel/program changes made while in playback mode)."""
        offset_in_window = self.highlight_row - TOP_ROW
        self.channel_scroll_offset = (channel_index - offset_in_window) % self._channel_count()
        self.highlight_col = "B"
        self.channel_window_start[channel_index] = program_index

    def focused_program(self):
        channel_index = self._current_channel_index()
        channel = self.channels[channel_index]
        count = self._program_count(channel_index)
        start = self.channel_window_start[channel_index]
        offset = COLUMN_OFFSETS[self.highlight_col]
        return channel["programs"][(start + offset) % count]
