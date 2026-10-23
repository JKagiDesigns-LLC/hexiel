"""Paste sanitizing (terminal escape chaff) and TGP quieting coverage."""

from hexiel.tui import sanitize_paste


def test_plain_paste_passes_through():
    assert sanitize_paste("hello world") == "hello world"
    assert sanitize_paste("l1\r\nl2\r\nl3") == "l1\nl2\nl3"
    assert sanitize_paste("a\tb") == "a   b"


def test_csi_and_osc_are_dropped():
    assert sanitize_paste("ok\x1b[2Kdone") == "okdone"
    assert sanitize_paste("a\x1b]0;title\x07b") == "ab"
    assert sanitize_paste("a\x1b]8;;https://x\x1b\\b") == "ab"
    assert sanitize_paste("\x1b[?2004hkeep") == "keep"


def test_kitty_graphics_ack_fragment_does_not_land_in_the_prompt():
    # what the user saw after pasting an image: the TGP transmit ack surviving sanitize
    raw = "\x1b_Gi=3602480111;OK\x1b\\;52;c"
    assert sanitize_paste(raw) == ""                      # empty paste -> image path
    assert sanitize_paste("text\x1b_Gi=5;OK\x1b\\more") == "textmore"


def test_partial_apc_and_stray_esc_never_reach_the_prompt():
    assert sanitize_paste("\x1b_Gi=99;") == ""            # unterminated APC + chaff
    assert sanitize_paste("\x1b]52;c;A\nplain") == "52;c;A\nplain"  # unterminated OSC degrades safely


def test_real_text_with_escapes_is_not_swallowed():
    ctrl = sanitize_paste("\x1b[31m ERROR \x1b[0m Build failed")
    assert "ERROR" in ctrl and "Build failed" in ctrl
