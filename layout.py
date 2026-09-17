"""Pixel geometry for the TV DINNER guide, derived from
TV_DINNER_MOCKUP_COORDINATES.csv's LayerKind.NORMAL box bounds.

Note: the PSD's own layer names for the header row (B1/C1/D1) were
mislabeled "A2"/"A3"/"A4" in the export -- confirmed by x-position
(they sit at x=131/311/491, matching columns B/C/D, not A) and by
height (46px, matching the header row, not the 76px schedule rows).
Rebuilt cleanly from the underlying x/y/width/height pattern instead
of trusting the label strings.
"""

CANVAS_SIZE = (720, 480)
FONT_PATH_GUIDE = "FjallaOne-Regular.ttf"
GUIDE_FONT_SIZE = 24

HIGHLIGHT_COLOR = (255, 255, 0)
HIGHLIGHT_BORDER_WIDTH = 4
HIGHLIGHT_INSET = 4  # matches YELLOW HIGHLIGHT 4px.png's ~4px inset from the cell edge

# x, width per column
COLUMNS = {
    "A": (-4, 143),
    "B": (131, 188),
    "C": (311, 188),
    "D": (491, 233),
}

# y, height per row (row 1 = header, rows 2-5 = schedule; row 5 deliberately
# bleeds a few px past the 480px canvas -- matches the user's note that the
# bottom row may be partly obscured by a CRT's bezel)
ROWS = {
    1: (166, 46),
    2: (204, 76),
    3: (272, 76),
    4: (340, 76),
    5: (408, 76),
}

VISIBLE_CHANNEL_ROWS = (2, 3, 4, 5)
VISIBLE_PROGRAM_COLS = ("B", "C", "D")
HIGHLIGHTABLE_ROWS = (2, 3, 4)  # confirmed by user: highlight never reaches row 5

# Column A's cell rect deliberately bleeds 4px off the left edge of the
# canvas (x=-4). NTSC-safe-title margin for cheap/overscanning CRTs means
# the text itself needs to sit further right than the cell's own center --
# this nudges just the drawn text, not the cell geometry.
COLUMN_A_TEXT_OFFSET_X = 16


def cell_rect(col, row):
    x, w = COLUMNS[col]
    y, h = ROWS[row]
    return (x, y, w, h)


# Top info panel (above the grid) -- approximate zones tuned against the mockup,
# not literal CSV layer bounds (those were per-sample-content text boxes, not a
# generic content-agnostic anchor).
CLOCK_RECT = cell_rect("A", 1)
# x, y, w, h -- 3 centered lines drawn inside this (ARTIST / TITLE / TRT).
# Width trimmed ~4 characters' worth from the original 305px so the zone
# no longer butts flush against THUMBNAIL_BOX (485) with zero gap.
TITLE_ZONE = (180, 44, 250, 118)
THUMBNAIL_BOX = (485, 40, 180, 120)
# Actual image size, centered inside THUMBNAIL_BOX. Base 4:3 (160x120)
# stretched an extra 110% on width only, to compensate for the source's
# non-square pixels -- height unchanged.
THUMBNAIL_SIZE = (176, 120)

CELL_TEXT_PADDING_X = 14
CELL_TEXT_PADDING_Y = 10
CHANNEL_TEXT_PADDING_Y = 6
