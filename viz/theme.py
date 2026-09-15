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


# ---------------------------------------------------------------------------
# Monthly-profile palette
# ---------------------------------------------------------------------------
# Assigned by role rather than by slot order, because this page identifies four
# different things at once: species, generation, flow components, temperature.
#
# Validated with the dataviz validator. Species are checked on the ALL-PAIRS
# list, not the adjacent one: in a stacked bar any two species become adjacent
# in a month where only those two are present, so every pair has to hold up.
#
#   species light  all-pairs: worst CVD dE 7.2 (protan, warn band), worst
#                  normal-vision dE 17.3 -- clears the 15 floor
#   species dark   adjacent:  all checks pass, no warnings
#                  all-pairs: pink<->red dE 7.8, below the normal floor
#   flow ramp      ordinal, both modes: monotone lightness, single hue,
#                  adjacent dL gaps clear
#
# The CVD warn band is legal only with secondary encoding, which is present:
# every segment carries a surface-coloured border (a gap), the legend is
# grouped per measure, and the table below the chart repeats every value.
SPECIES_COLORS = {
    "light": {"pink": "#ef8fb5", "green": "#008300", "yellow": "#eda100",
              "red": "#e34948", "purple": "#4a3aa7", "brown": "#7a3d10"},
    "dark": {"pink": "#d55181", "green": "#008300", "yellow": "#c98500",
             "red": "#c94a4a", "purple": "#9085e9", "brown": "#a85c20"},
}

# The order species colours are handed out in, matching the requested
# pink, green, yellow, red, purple, brown.
SPECIES_COLOR_ORDER = ["pink", "green", "yellow", "red", "purple", "brown"]

GENERATION_COLOR = {"light": "#eb6834", "dark": "#d95926"}

# Flow is parts of one whole, so its segments are an ordinal ramp of a single
# hue rather than three categorical colours: light blue spill, regular blue
# through-turbines, dark blue the remainder.
FLOW_RAMP = {
    "light": {"spill_af": "#86b6ef", "gen_af": "#2a78d6", "other_af": "#104281"},
    "dark": {"spill_af": "#b7d3f6", "gen_af": "#5598e7", "other_af": "#256abf"},
}

# Temperature is a line, not a bar, and lives in its own band above them, so
# mark type and position already separate it; aqua is the one categorical hue
# the page does not otherwise spend.
TEMPERATURE_COLOR = {"light": "#1baf7a", "dark": "#199e70"}


def species_palette(mode: str, species: list) -> dict:
    """Species -> hex, assigned in fixed order so filtering never repaints."""
    table = SPECIES_COLORS["dark" if mode == "dark" else "light"]
    order = SPECIES_COLOR_ORDER
    return {sp: table[order[i % len(order)]] for i, sp in enumerate(species)}


# ---------------------------------------------------------------------------
# Temperature bands
# ---------------------------------------------------------------------------
# Water temperature is a STATUS, not another series: 68 F is where thermal
# stress on migrating salmon sets in and 72 F is where it gets severe. So the
# line is coloured from the reserved status palette rather than the categorical
# slots -- those hexes are fixed, never themed, and deliberately kept apart from
# the series colours so a status never impersonates a series.
#
# The status steps sit close to same-hue series colours by design (status-serious
# is dE 5.8 from the slot-2 orange the generation bar uses; status-critical is
# 4.8 from the red Shad segment). The documented mitigation is that a status
# colour never carries meaning alone, so every band is named in the legend and
# the thresholds are stated in the caption beneath the chart.
TEMP_BANDS = [
    (None, 68.0, "cool", "Below 68 °F"),
    (68.0, 72.0, "serious", "68–72 °F"),
    (72.0, None, "critical", "72 °F and above"),
]

TEMP_BAND_COLORS = {
    # "cool" keeps the page's existing temperature identity; the other two are
    # the fixed status steps.
    "cool": {"light": "#1baf7a", "dark": "#199e70"},
    "serious": {"light": "#ec835a", "dark": "#ec835a"},
    "critical": {"light": "#d03b3b", "dark": "#d03b3b"},
}


def temp_band(value) -> str:
    """Which band a temperature falls in."""
    for lo, hi, name, _ in TEMP_BANDS:
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return name
    return "cool"


def temp_band_color(name: str, mode: str) -> str:
    return TEMP_BAND_COLORS[name]["dark" if mode == "dark" else "light"]


def temperature_segments(xs, values):
    """
    Split a temperature series into runs that share a band.

    -> [(band_name, xs_run, values_run)]

    Each run after the first repeats the previous point so the line stays
    unbroken, which means the joining segment takes the colour of the band it
    is entering. Crossings are not interpolated: these are monthly maxima, so a
    crossing "somewhere between June and July" would be invented precision.
    """
    pairs = [(x, v) for x, v in zip(xs, values) if v is not None and v == v]
    if not pairs:
        return []
    runs, cur_band, cur = [], temp_band(pairs[0][1]), [pairs[0]]
    for point in pairs[1:]:
        band = temp_band(point[1])
        if band == cur_band:
            cur.append(point)
        else:
            runs.append((cur_band, cur))
            cur_band, cur = band, [cur[-1], point]   # repeat to bridge the gap
    runs.append((cur_band, cur))
    return [(band, [p[0] for p in pts], [p[1] for p in pts]) for band, pts in runs]
