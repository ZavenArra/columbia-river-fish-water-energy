"""
Axis helpers shared by the chart pages.

Plotly matches tick COUNT across secondary axes so their gridlines line up,
which leaves the extra axes on arbitrary values like 990.209k or 13.4596M.
Choosing a round 1/2/5 x 10^n interval puts them back on readable numbers.
"""

import math


def _nice_dtick(lo, hi, target=5):
    """
    A round tick interval covering [lo, hi].

    Plotly matches tick COUNT across a secondary axis so the gridlines line up,
    which leaves the second axis on arbitrary values like 990.209k. Picking a
    1/2/5 x 10^n interval puts it back on round numbers.
    """
    span = (hi - lo) or abs(hi) or 1.0
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for mult in (1, 2, 5, 10):
        if raw <= mult * mag:
            return mult * mag
    return 10 * mag


def _axis_ticks(frames_cols):
    """(tick0, dtick) spanning every (frame, column) pair given."""
    vals = []
    for frame, col in frames_cols:
        if frame is not None and not frame.empty and col in frame:
            series = frame[col].dropna()
            if len(series):
                vals.extend([float(series.min()), float(series.max())])
    if not vals:
        return None, None
    lo, hi = min(vals), max(vals)
    dtick = _nice_dtick(lo, hi)
    return math.floor(lo / dtick) * dtick, dtick




import calendar

MONTHS = [calendar.month_abbr[m] for m in range(1, 13)]

# A month needs at least this much of its hours reported before its generation
# or flow bar is drawn. Three of Bonneville's 2025 months return header-only
# files from USACE; drawing their single stray hour as a bar would read as
# "this dam nearly stopped", which is the opposite of "we have no data".
MIN_COVERAGE = 0.9

FLOW_LABELS = {"gen_af": "Through turbines", "spill_af": "Spilled",
               "other_af": "Other (locks, ladders)"}


# The bars occupy the bottom of the plot and the temperature line sits above
# them, in the same window. Every bar axis is pinned to [0, top / BAR_BAND] so
# all three share one baseline -- left to itself, plotly pads the primary axis
# below zero (it ran to -186k on Bonneville) and the fish bars float clear of
# the others' baseline.
BAR_BAND = 0.72
TEMP_BAND = (0.80, 0.985)


def _temp_axis_range(t_lo, t_hi, band=TEMP_BAND):
    """
    Range that maps [t_lo, t_hi] into the top slice of the plot.

    Returns (range, tickvals). The axis is mostly empty space underneath, so
    ticks are listed explicitly rather than generated across the whole range.

    Callers pass the GLOBAL temperature bounds, not this chart's own min and
    max, so the band occupies the same pixels with the same scale on every dam
    and year -- a line higher on one chart than another then means warmer
    water, not a rescaled axis. Bounds are rounded out to multiples of five so
    the tick labels are stable too.
    """
    if t_lo is None or t_hi is None:
        return None, []
    t_lo = math.floor(t_lo / 5.0) * 5.0
    t_hi = math.ceil(t_hi / 5.0) * 5.0
    if t_hi - t_lo < 1e-9:
        t_lo, t_hi = t_lo - 5.0, t_hi + 5.0
    f0, f1 = band
    span = (t_hi - t_lo) / (f1 - f0)
    r0 = t_lo - f0 * span
    dtick = _nice_dtick(t_lo, t_hi, target=4)
    first = math.ceil(t_lo / dtick) * dtick
    ticks = []
    v = first
    while v <= t_hi + 1e-9:
        ticks.append(round(v, 6))
        v += dtick
    return [r0, r0 + span], ticks




def shared_zero_ranges(extents, band=BAR_BAND):
    """
    Axis ranges that put zero at the SAME height on every axis.

    `extents` maps an axis name to (largest negative magnitude, largest
    positive). Each axis is scaled independently -- they measure different
    things -- but they must agree on where zero sits, or a bar dipping below
    the line on one axis would sit above it on another and the chart would be
    unreadable. The negative share is taken from whichever axis needs the most
    room below the line; the rest get slack rather than being clipped.

    Bars are confined to the bottom `band` of the plot so the temperature line
    keeps its own space above them, exactly as on the monthly page.
    """
    fracs = [n / (n + p) for n, p in extents.values() if (n + p) > 0]
    f = max(fracs) if fracs else 0.0
    f = min(max(f, 0.0), 0.95)
    out = {}
    for name, (neg, pos) in extents.items():
        if (neg + pos) <= 0:
            out[name] = [-1, 1]
            continue
        span = max(neg / f if f > 0 else 0.0,
                   pos / (1 - f) if f < 1 else 0.0) or (neg + pos)
        lo = -f * span
        hi = lo + span / band          # headroom above for the temperature band
        out[name] = [lo, hi]
    return out


def add_temperature_line(fig, xs, values, *, mode, surface, yaxis="y4",
                         dash="solid", legend_group="temp",
                         legend_title="Max temperature", show_band_legend=True,
                         hover_label="max"):
    """
    Draw a temperature line coloured by thermal-stress band.

    One trace per run of months sharing a band, so the line changes colour as
    the water crosses 68 F and 72 F. Each band contributes at most one legend
    entry, because a status colour must never carry meaning on its own.

    Returns True if anything was drawn.
    """
    import plotly.graph_objects as go

    from viz.theme import TEMP_BANDS, temp_band_color, temperature_segments

    labels = {name: label for _, _, name, label in TEMP_BANDS}
    segments = temperature_segments(xs, values)
    seen = set()
    for band, seg_x, seg_y in segments:
        colour = temp_band_color(band, mode)
        first = show_band_legend and band not in seen
        seen.add(band)
        fig.add_trace(go.Scatter(
            x=seg_x, y=seg_y, name=labels[band], legendgroup=legend_group,
            legendgrouptitle_text=legend_title, showlegend=first,
            mode="lines+markers", yaxis=yaxis,
            line=dict(color=colour, width=2, dash=dash),
            marker=dict(size=8, color=colour,
                        line=dict(width=1, color=surface)),
            hovertemplate=f"%{{x}}<br>{hover_label} %{{y:.1f}} °F<extra></extra>"))
    return bool(segments)
