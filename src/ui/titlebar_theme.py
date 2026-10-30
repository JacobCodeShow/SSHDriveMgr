"""
titlebar_theme.py – Colour palettes for the custom window titlebar.

Provides two frozen dataclasses (DARK_PALETTE, LIGHT_PALETTE) and a
get_palette() helper so every titlebar widget reads colours from a single
source of truth.  No Qt imports – pure data module.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TitlebarPalette:
    """All colours required to paint the custom titlebar and its buttons."""
    bg: str           # Titlebar background
    bg_hover: str     # Non-close button hover fill
    bg_pressed: str   # Non-close button pressed fill
    close_hover: str  # Close button hover fill (red)
    close_pressed: str  # Close button pressed fill
    text: str         # Window-title text colour
    text_dim: str     # Version-pill text colour
    icon: str         # Default button-icon colour
    icon_hover: str   # Button-icon colour on hover (applied via CSS)
    border: str       # 1-px bottom border of the bar


# ── palettes ───────────────────────────────────────────────────────────────

DARK_PALETTE = TitlebarPalette(
    bg="#0d0d12",
    bg_hover="#182030",
    bg_pressed="#111822",
    close_hover="#c42b1c",
    close_pressed="#9e1b0e",
    text="#c8d6e5",
    text_dim="#8fa4b8",
    icon="#4a6070",
    icon_hover="#c8d6e5",
    border="#1a1a2e",
)

LIGHT_PALETTE = TitlebarPalette(
    bg="#f0f2f5",
    bg_hover="#e2e6ec",
    bg_pressed="#d4d8e2",
    close_hover="#c42b1c",
    close_pressed="#9e1b0e",
    text="#1a2332",
    text_dim="#5a6a7a",
    icon="#8090a0",
    icon_hover="#1a2332",
    border="#d4d8df",
)


def _blend(first: str, second: str, weight: float) -> str:
    channels = [round(int(first[i:i + 2], 16) * (1 - weight)
                      + int(second[i:i + 2], 16) * weight) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(channels)


def get_palette(theme: str) -> TitlebarPalette:
    """Resolve builtin/user themes through the Qt-free semantic tokens."""
    from src.theme_manager import get_theme_manager

    colors = get_theme_manager().get_colors(theme)
    base = DARK_PALETTE if colors["is_dark"] else LIGHT_PALETTE
    return TitlebarPalette(
        bg=colors["background"],
        bg_hover=_blend(colors["background"], colors["text"], .10),
        bg_pressed=_blend(colors["background"], colors["text"], .18),
        close_hover=base.close_hover,
        close_pressed=base.close_pressed,
        text=colors["text"],
        text_dim=colors["muted"],
        icon=colors["muted"],
        icon_hover=colors["text"],
        border=colors["border"],
    )
