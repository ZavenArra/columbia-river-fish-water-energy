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


