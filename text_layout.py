"""Pixel-measured text fitting for the guide -- truncates/wraps against a
pygame Font's actual rendered width instead of a fixed character count.
(The brief's character-count figures didn't match the mockup's real box
widths at 24px Fjalla One -- confirmed a typo/approximation, so every
truncation decision here is measured against the font directly.)

Two truncation styles, matching the mockup's own actual behavior:
- truncate_hard: cuts wherever the pixel limit lands, mid-word if that's
  where it falls (confirmed against the mockup's own sample text --
  "Arena Of Pleas", "Saturday Nigh", "Dont Believe H" are all mid-word
  cuts). Used for grid-cell program lines and the Main Title artist line.
- truncate_on_space / wrap_on_space: back off to the last full word.
  Used only for the Main Title's song-title lines, per the brief's
  explicit "wrap on a space" / "truncate on a space if longer".
"""


def _longest_prefix_fitting(font, text, max_width):
    if font.size(text)[0] <= max_width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.size(text[:mid])[0] <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def truncate_hard(font, text, max_width):
    return _longest_prefix_fitting(font, text, max_width)


def truncate_on_space(font, text, max_width):
    cut = _longest_prefix_fitting(font, text, max_width)
    if cut == text:
        return cut
    last_space = cut.rfind(" ")
    return cut[:last_space] if last_space > 0 else cut


def wrap_title_first_line(font, text, max_width):
    """Greedy word-wrap of just the FIRST line, returning (line, remainder).
    remainder is None if the whole text fit on one line. Used by the Main
    Title panel's 3-line layout: ARTIST / TITLE (first line) / rest-of-title
    + TRT (only when the title didn't fit on one line)."""
    if font.size(text)[0] <= max_width:
        return text, None
    words = text.split(" ")
    line = ""
    i = 0
    while i < len(words):
        candidate = f"{line} {words[i]}".strip()
        if font.size(candidate)[0] <= max_width:
            line = candidate
            i += 1
        else:
            break
    if not line:
        # A single word wider than the whole line -- hard-truncate it.
        line = truncate_hard(font, words[0], max_width)
        i = 1
    remainder = " ".join(words[i:])
    return line, (remainder or None)


def wrap_on_space(font, text, max_width, max_lines=2):
    """Greedy word-wrap on spaces into at most max_lines, each fitting
    max_width. Any words left over after max_lines are folded onto the
    final line and truncated on a space (matches 'truncate on a space
    if longer')."""
    words = text.split(" ")
    lines = []
    current = ""

    i = 0
    while i < len(words) and len(lines) < max_lines:
        word = words[i]
        candidate = f"{current} {word}".strip()
        if font.size(candidate)[0] <= max_width:
            current = candidate
            i += 1
        elif current:
            lines.append(current)
            current = ""
        else:
            # A single word wider than the whole line -- hard-truncate it.
            lines.append(truncate_hard(font, word, max_width))
            current = ""
            i += 1

    if current and len(lines) < max_lines:
        lines.append(current)

    if i < len(words) and lines:
        remaining = " ".join(words[i:])
        combined = f"{lines[-1]} {remaining}"
        lines[-1] = truncate_on_space(font, combined, max_width)

    while len(lines) < max_lines:
        lines.append("")

    return lines
