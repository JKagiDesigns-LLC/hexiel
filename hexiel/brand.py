"""hexiel's logo: angel wings around an H under a halo. The image lives in
docs/logo.svg (and web/logo.svg); this is the terminal rendition."""

from rich.text import Text

LOGO = r"""
                       .-~~~~~-.
  __.._                `-~~~~~-'                _..__
  \    ``--.._          ██   ██          _..--''    /
   \ ``--.._  ``--._    ██   ██    _.--''  _..--'' /
    `-._    ``--._  \   ███████   /  _.--''    _.-'
        `-.__     \  \  ██   ██  /  /     __.-'
             `--.__\  \ ██   ██ /  /__.--'
                    `-'         `-'
"""
LOGO_WIDTH = max(len(line) for line in LOGO.splitlines())
GOLD = "#d9b55a"
WING = ["#cfe6df", "#cfe6df", "#a8cfc4", "#93c2b5", "#7fb3a5", "#6aa294", "#5f9486", "#5f9486"]
HALO_COLS = range(22, 33)


def logo_text(tagline: str = "your codebase's guardian angel") -> Text:
    """The coloured logo: gold halo + H, wings shaded light to sage."""
    out = Text()
    for row, line in enumerate(LOGO.strip("\n").splitlines()):
        for col, ch in enumerate(line):
            gold = ch == "█" or (row < 2 and col in HALO_COLS)
            out.append(ch, style=f"bold {GOLD}" if gold else WING[min(row, len(WING) - 1)])
        out.append("\n")
    out.append(" " * 20 + "hexiel", style=f"bold {GOLD}")
    out.append(f"  · {tagline}", style="#8b919d")
    return out


def small_logo_text() -> Text:
    """One line for narrow terminals."""
    out = Text()
    out.append("~\\\\ ", style="#a8cfc4")
    out.append("H", style=f"bold {GOLD}")
    out.append(" //~  ", style="#a8cfc4")
    out.append("hexiel", style=f"bold {GOLD}")
    out.append(" · your codebase's guardian angel", style="#8b919d")
    return out
