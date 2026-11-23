"""Brand palette, glyph sets, and gradient helpers for the 10xGraph terminal UI.

Everything visual in the CLI resolves through this module so a single palette
change restyles intros, timelines, tables, and completion screens at once.
Glyphs are separated from color because a terminal can support one without the
other: ASCII fallbacks keep the layout identical when Unicode is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from rich.theme import Theme


# 10xGraph brand palette, from the docs site tokens (agentflow-docs/src/styles/global.css).
# A terminal's background is unknown, so each color sits between the docs light-theme and
# dark-theme value and stays legible on both. Primary text uses the terminal's own
# foreground (plain ``bold``), like the logo's ink, so it never vanishes on a light theme.
ACCENT = "#6a93f0"  # docs --accent: #2f5bd3 light / #7ea6ff dark
AMBER = "#de8a55"  # logo entry node, docs --brand-amber: #c8692f / #e9a57c
SUCCESS = "#3fbf98"  # docs --tip: #0f7a5c / #4fd1ae
WARNING = "#e0a447"  # docs --warn: #8a5a00 / #f0b45a
ERROR = "#e5666d"  # docs --danger: #b4232c / #f2868a
MUTED = "#8691a3"  # docs --muted / --faint
LINE = "#4f5a68"  # rules and dividers
PENDING = "#5c6674"  # not-yet-reached steps

# Surfaces painted only inside the opt-in full-screen frame, which is always dark
# (docs --bg / --surface / --surface-2 / --line / --text).
BACKGROUND = "#0b0e13"
SURFACE = "#10151d"
SURFACE_RAISED = "#161c27"
LINE_ON_DARK = "#2d3849"
INK_ON_DARK = "#eceff4"

# Motion tint: the docs accent flowing into the logo's amber entry node. Sampled
# continuously, so the two stops never band.
BRAND_RAMP: tuple[str, ...] = (ACCENT, AMBER)

TENXGRAPH_THEME = Theme(
    {
        "tenxgraph.success": f"bold {SUCCESS}",
        "tenxgraph.error": f"bold {ERROR}",
        "tenxgraph.warning": f"bold {WARNING}",
        "tenxgraph.info": ACCENT,
        "tenxgraph.muted": MUTED,
        "tenxgraph.title": "bold",
        "tenxgraph.brand": f"bold {AMBER}",
        "tenxgraph.command": "bold",
        "tenxgraph.progress": ACCENT,
        "tenxgraph.accent": ACCENT,
        "tenxgraph.pending": PENDING,
        "tenxgraph.rule": LINE,
        "tenxgraph.chip": f"bold {INK_ON_DARK} on {SURFACE_RAISED}",
        # Header band, drawn only by the full-screen frame on its dark surface.
        "tenxgraph.header": f"on {SURFACE}",
        "tenxgraph.elapsed": MUTED,
    }
)


@dataclass(frozen=True)
class Glyphs:
    """Symbol set for one invocation, chosen by terminal encoding support."""

    check: str
    cross: str
    warn: str
    info: str
    bullet: str
    arrow: str
    pending: str
    active: str
    skipped: str
    rule: str
    thin_rule: str
    node: str
    diamond: str
    caret: str
    block: str
    cursor: str
    tree_stem: str
    tree_branch: str
    tree_end: str
    spinner: tuple[str, ...]
    shades: tuple[str, ...]


UNICODE_GLYPHS = Glyphs(
    check="✓",
    cross="✗",
    warn="▲",
    info="•",
    bullet="•",
    arrow="→",
    pending="○",
    active="◆",
    skipped="⊘",
    rule="━",
    thin_rule="─",
    node="◉",
    diamond="◆",
    caret="▸",
    block="█",
    cursor="▌",
    tree_stem="┃",
    tree_branch="┣",
    tree_end="┗",
    spinner=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    shades=(" ", "░", "▒", "▓", "█"),
)

ASCII_GLYPHS = Glyphs(
    check="OK",
    cross="X",
    warn="!",
    info="*",
    bullet="*",
    arrow="->",
    pending="o",
    active="*",
    skipped="-",
    rule="=",
    thin_rule="-",
    node="O",
    diamond="*",
    caret=">",
    block="#",
    cursor="|",
    tree_stem="|",
    tree_branch="+",
    tree_end="\\",
    spinner=("|", "/", "-", "\\"),
    shades=(" ", ".", ":", "+", "#"),
)


def glyphs_for(unicode: bool) -> Glyphs:
    """Return the glyph set matching the terminal's encoding support."""
    return UNICODE_GLYPHS if unicode else ASCII_GLYPHS


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def sample_ramp(position: float, ramp: tuple[str, ...] = BRAND_RAMP) -> str:
    """Sample a continuous color from ``ramp`` at ``position`` in ``[0, 1]``.

    Interpolating between stops rather than snapping to the nearest one is what
    keeps a wide gradient bar from showing visible banding.
    """
    if not ramp:
        return INK_ON_DARK
    if len(ramp) == 1:
        return ramp[0]

    clamped = min(max(position, 0.0), 1.0)
    scaled = clamped * (len(ramp) - 1)
    index = int(scaled)
    if index >= len(ramp) - 1:
        return ramp[-1]

    blend = scaled - index
    start = _hex_to_rgb(ramp[index])
    end = _hex_to_rgb(ramp[index + 1])
    return _rgb_to_hex(
        (
            round(start[0] + (end[0] - start[0]) * blend),
            round(start[1] + (end[1] - start[1]) * blend),
            round(start[2] + (end[2] - start[2]) * blend),
        )
    )


def dim_hex(value: str, factor: float) -> str:
    """Scale a hex color toward black, for pending or background elements."""
    red, green, blue = _hex_to_rgb(value)
    scale = min(max(factor, 0.0), 1.0)
    return _rgb_to_hex((round(red * scale), round(green * scale), round(blue * scale)))


def gradient_text(
    value: str,
    *,
    ramp: tuple[str, ...] = BRAND_RAMP,
    bold: bool = False,
    offset: float = 0.0,
    span: float = 1.0,
) -> Text:
    """Paint ``value`` with a per-character sweep through ``ramp``.

    ``offset`` shifts the sweep so successive frames can animate a shimmer
    without rebuilding the palette.
    """
    text = Text()
    length = max(len(value), 1)
    weight = "bold " if bold else ""
    for index, character in enumerate(value):
        position = (offset + (index / length) * span) % 1.0
        text.append(character, style=f"{weight}{sample_ramp(position, ramp)}")
    return text


def gradient_rule(
    width: int,
    *,
    glyphs: Glyphs,
    thin: bool = False,
    offset: float = 0.0,
) -> Text:
    """Build a full-width rule that frames branded sections.

    Calm by design: a solid line with a short amber lead-in, echoing the logo's entry
    node. ``offset`` is kept for callers that once animated the rule.
    """
    del offset
    character = glyphs.thin_rule if thin else glyphs.rule
    span = max(width, 1)
    lead = min(4, span)
    text = Text(character * lead, style=AMBER)
    text.append(character * (span - lead), style=LINE)
    return text


def wordmark(color: str | None = None) -> Text:
    """The ``10xgraph`` name as the CLI shows it: bold ink, like the logo.

    Without ``color`` it uses the terminal's own foreground; pass ``INK_ON_DARK`` on a
    surface that paints its own dark background.
    """
    return Text("10xgraph", style=f"bold {color}" if color else "bold")
