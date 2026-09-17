from navigation import NavigationState


def make_channels(counts):
    """counts: list of program-counts, one per channel, numbered 1..N."""
    channels = []
    for i, count in enumerate(counts, start=1):
        channels.append({
            "number": i,
            "callsign": f"CH{i}",
            "programs": [{"artist": f"Ch{i}", "title": f"Prog{p}"} for p in range(count)],
        })
    return channels


def describe(nav):
    col, row = nav.highlight_position()
    fp = nav.focused_program()
    return f"row={row} col={col} scroll={nav.channel_scroll_offset} -> {fp['artist']}/{fp['title']}"


def test_vertical_scroll_pins_at_row4():
    nav = NavigationState(make_channels([5] * 8))
    assert nav.highlight_row == 2 and nav.channel_scroll_offset == 0
    nav.handle_down()
    assert nav.highlight_row == 3
    nav.handle_down()
    assert nav.highlight_row == 4
    nav.handle_down()  # should scroll, not go to row 5
    assert nav.highlight_row == 4, "highlight must stay pinned at row 4"
    assert nav.channel_scroll_offset == 1, "channel list should have scrolled by 1"
    print("test_vertical_scroll_pins_at_row4 PASS:", describe(nav))


def test_vertical_scroll_wraps_circularly():
    nav = NavigationState(make_channels([5] * 4))  # exactly 4 channels
    nav.handle_up()  # at top already, row2 -> should wrap scroll backward
    assert nav.channel_scroll_offset == 3 % 4  # wraps to last channel as top-of-window
    print("test_vertical_scroll_wraps_circularly PASS: scroll_offset=", nav.channel_scroll_offset)


def test_horizontal_pins_at_columnD_then_scrolls():
    nav = NavigationState(make_channels([10] * 8))
    nav.handle_down(); nav.handle_down()  # row 4
    assert nav.highlight_row == 4
    before = nav.focused_program()["title"]
    nav.handle_right()
    assert nav.highlight_col == "C"
    nav.handle_right()
    assert nav.highlight_col == "D"
    nav.handle_right()  # pinned at D -- should scroll the channel's window instead
    assert nav.highlight_col == "D", "highlight must stay pinned at column D"
    after = nav.focused_program()["title"]
    assert before != after, "focused program should have advanced by one"
    print(f"test_horizontal_pins_at_columnD_then_scrolls PASS: {before} -> {after}")


def test_short_channel_cycles_instead_of_blank():
    nav = NavigationState(make_channels([2, 5, 5, 5]))  # first channel only has 2 programs
    visible = nav.visible_channels()
    titles = [p["title"] for p in visible[0]["programs"]]
    assert titles == ["Prog0", "Prog1", "Prog0"], f"expected cycling, got {titles}"
    print("test_short_channel_cycles_instead_of_blank PASS:", titles)


def test_left_right_wraps_within_channel():
    nav = NavigationState(make_channels([3]))
    # highlight starts at col B (offset 0); move right to D (offset 2)
    nav.handle_right(); nav.handle_right()
    assert nav.highlight_col == "D"
    nav.handle_right()  # pinned -> window_start advances 0->1
    assert nav.channel_window_start[0] == 1
    nav.handle_right()  # 1->2
    nav.handle_right()  # 2->0 (wraps, since count=3)
    assert nav.channel_window_start[0] == 0
    print("test_left_right_wraps_within_channel PASS")


def test_full_user_example_walkthrough():
    """Mirrors the user's own literal description: B2 -down-> B3 -down-> B4
    -down-> (scroll, stay B4) ... then from B4: -right-> C4 -right-> D4
    -right-> (scroll, stay D4)."""
    nav = NavigationState(make_channels([6] * 10))
    assert nav.highlight_position() == ("B", 2)
    nav.handle_down()
    assert nav.highlight_position() == ("B", 3)
    nav.handle_down()
    assert nav.highlight_position() == ("B", 4)
    scroll_before = nav.channel_scroll_offset
    nav.handle_down()
    assert nav.highlight_position() == ("B", 4)
    assert nav.channel_scroll_offset == scroll_before + 1
    nav.handle_right()
    assert nav.highlight_position() == ("C", 4)
    nav.handle_right()
    assert nav.highlight_position() == ("D", 4)
    channel_index = nav._current_channel_index()
    window_before = nav.channel_window_start[channel_index]
    nav.handle_right()
    assert nav.highlight_position() == ("D", 4)
    assert nav.channel_window_start[channel_index] == window_before + 1
    print("test_full_user_example_walkthrough PASS")


if __name__ == "__main__":
    test_vertical_scroll_pins_at_row4()
    test_vertical_scroll_wraps_circularly()
    test_horizontal_pins_at_columnD_then_scrolls()
    test_short_channel_cycles_instead_of_blank()
    test_left_right_wraps_within_channel()
    test_full_user_example_walkthrough()
    print("\nALL PASS")
