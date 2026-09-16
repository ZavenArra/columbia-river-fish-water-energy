"""
Corridor page: water temperature across the whole migration route at once.

Months on the x axis, the eight dams a Snake River salmon climbs on the y axis
(Bonneville at the top, Lower Granite at the bottom -- the order a fish meets
them), and temperature as colour.

Colour uses the same thermal-stress bands as the other pages: teal below 68 F,
orange from 68 to 72 F, red at 72 F and above. The scale has hard edges at those
thresholds rather than a gradient, so the question the chart answers is "where
and when does the corridor become dangerous", not "what shade is July".

The z range is fixed across every year. It has to be: the band edges are
anchored to absolute temperatures, and rescaling per year would slide 68 F
around until the colours stopped meaning anything.
"""

from datetime import date

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from dams import MIGRATION_CORRIDOR
from viz.axes import MONTHS
from viz.data import (load_dam_names, load_global_temp_range,
                      load_temperature_grid, load_temperature_years)
from viz.theme import INK, temperature_colorscale, theme_mode


def corridor_surface(grid, *, names, zmin, zmax, mode, title):
    """Heatmap of dam x month temperature, banded by thermal stress."""
    if grid is None or grid.empty or not grid.notna().any().any():
        return None
    ink = INK[mode]

    # Reversed so the first dam listed sits at the TOP: the y axis then reads
    # downstream to upstream, the order a fish meets them.
    dams = [d for d in grid.index][::-1]
    z = [[grid.loc[d, m] for m in range(1, 13)] for d in dams]
    labels = [f"{names.get(d, d)} ({d})" for d in dams]
    text = [[("" if pd.isna(v) else f"{v:.0f}") for v in row] for row in z]

    fig = go.Figure(go.Heatmap(
        x=MONTHS, y=labels, z=z,
        zmin=zmin, zmax=zmax,
        colorscale=temperature_colorscale(zmin, zmax, mode),
        # Gaps stay gaps: a dam-month with no reading is not cold water.
        hoverongaps=False,
        xgap=2, ygap=2,               # the 2px surface gap between cells
        text=text, texttemplate="%{text}",
        # Dark labels on every band. White on the orange band measures about
        # 2.6:1, which fails; dark ink is ~8:1 on teal and orange and ~4.5:1 on
        # red. The band colours are mode-invariant, so this holds in dark mode.
        textfont=dict(size=11, color="#0b0b0b"),
        hovertemplate="%{y}<br>%{x}: %{z:.1f} °F<extra></extra>",
        colorbar=dict(
            title=dict(text="Max °F", font=dict(size=12, color=ink["secondary"])),
            tickvals=[zmin, 68, 72, zmax],
            ticktext=[f"{zmin:.0f}", "68", "72", f"{zmax:.0f}"],
            tickfont=dict(size=11, color=ink["secondary"]),
            outlinewidth=0, thickness=14, len=0.85),
    ))
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top",
                   font=dict(size=15, color=ink["primary"])),
        height=460, margin=dict(l=170, r=20, t=58, b=60),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=ink["secondary"]),
        xaxis=dict(title_text="Month", side="bottom", showgrid=False,
                   tickfont=dict(size=11), title_font=dict(size=12),
                   categoryorder="array", categoryarray=MONTHS),
        yaxis=dict(title_text="", showgrid=False, tickfont=dict(size=11),
                   automargin=True),
    )
    return fig


def render():
    """The corridor temperature page."""
    mode = theme_mode()
    names = load_dam_names()
    corridor = tuple(MIGRATION_CORRIDOR)

    st.sidebar.title("Corridor filters")
    years = load_temperature_years(corridor)
    if not years:
        st.title("Corridor temperature")
        st.info("No temperature data loaded yet. Run `etl/backfill.py` first.")
        return

    # Default to the most recent COMPLETE year: the current calendar year holds
    # only the refresh window, which makes a sparse first impression.
    complete = [y for y in years if y < date.today().year]
    default_year = (complete or years)[-1]
    year = st.sidebar.selectbox("Year", options=years,
                                index=years.index(default_year))

    st.title("Corridor temperature")
    st.caption("Monthly maximum water temperature along the route a Snake River "
               "salmon climbs, Bonneville at the top to Lower Granite at the "
               "bottom. Colour marks thermal stress: teal below 68 °F, orange "
               "from 68 to 72 °F, red at 72 °F and above.")

    grid = load_temperature_grid(year, corridor)
    # Fixed across years so a colour means the same temperature on every chart.
    lo, hi = load_global_temp_range()
    zmin, zmax = 5 * (lo // 5), 5 * -(-hi // 5)

    fig = corridor_surface(grid, names=names, zmin=zmin, zmax=zmax, mode=mode,
                           title=f"Columbia–Snake corridor — {year}")
    if fig is None:
        st.warning(f"No corridor dam has temperature data for {year}.")
        return
    st.plotly_chart(fig, width="stretch", key=f"corridor-{year}")

    # Say plainly which dams are missing, rather than leaving blank rows to be
    # read as cold water.
    absent = [d for d in MIGRATION_CORRIDOR
              if d not in grid.index or not grid.loc[d].notna().any()]
    if absent:
        st.info("No temperature loaded for "
                + ", ".join(f"{names.get(d, d)} ({d})" for d in absent)
                + f" in {year}, so those rows are blank. Run "
                "`etl/backfill.py` for them to fill the corridor in.")

    thin = [MONTHS[m - 1] for m in range(1, 13)
            if not grid[m].notna().any()] if not grid.empty else []
    if thin:
        st.caption("No dam reports temperature in "
                   f"{', '.join(thin)} — DART's water-quality monitors are "
                   "seasonal, so winter months are often empty.")

    with st.expander(f"Corridor temperatures — {year}"):
        table = grid.rename(columns={m: MONTHS[m - 1] for m in range(1, 13)})
        table.index = [f"{names.get(d, d)} ({d})" for d in table.index]
        st.dataframe(table.round(1), width="stretch")
