"""
Palette and theme tokens shared by every page.

Categorical slots in fixed order. Validated with the dataviz validator in both
modes on the adjacent pairlist (the right list for lines and for stacked bars):

  light  worst adjacent CVD dE 9.1, normal-vision 19.6
  dark   worst adjacent CVD dE 8.4, normal-vision 19.3

Light mode WARNs on contrast for aqua, yellow and magenta, which obliges relief
-- every page therefore carries a table view of the same numbers.

Slots are assigned to whatever entity a page is identifying (years on the weekly
page, species on the monthly page). Assignment is always by fixed order from the
full set of entities, never by current selection, so filtering never repaints
the survivors.
"""

import streamlit as st

SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767"]
MAX_YEARS = len(SERIES_LIGHT)  # never cycle hues past the slot list

INK = {"light": {"primary": "#0b0b0b", "secondary": "#52514e",
                 "grid": "rgba(11,11,11,0.10)", "surface": "#fcfcfb"},
       "dark": {"primary": "#ffffff", "secondary": "#c3c2b7",
                "grid": "rgba(255,255,255,0.12)", "surface": "#1a1a19"}}


def theme_mode() -> str:
    try:
        return "dark" if st.get_option("theme.base") == "dark" else "light"
    except Exception:  # noqa: BLE001
        return "light"


def series_colors(mode: str) -> list:
    return SERIES_DARK if mode == "dark" else SERIES_LIGHT
