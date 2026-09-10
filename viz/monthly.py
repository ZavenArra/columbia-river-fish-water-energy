"""
Monthly profile page: one dam, one year, three bars per month.

Per month, side by side:
  1. fish passage, stacked by species          (left axis, count)
  2. generation                                (right axis, MWh)
  3. total flow, stacked into its components   (far-right axis, acre-feet)

All three are genuine monthly TOTALS, so a bar's height means "how much in this
month". MW and kcfs are rates, so they are converted: summing hourly MW gives
MWh directly, and hourly kcfs scales to acre-feet.

A NOTE ON THE THREE AXES
------------------------
Three measures on one plot means three y-scales, and their alignment is
arbitrary: a fish bar taller than a flow bar means nothing at all. Only compare
a bar with the SAME-COLOURED bars in other months. The axes are drawn in the
colour of the bars they belong to so it is at least obvious which is which, and
every value is available exactly in the table below the chart.

COLOUR
------
Six species take categorical slots 1-6, generation slot 7, flow slot 8 -- eight
hues, the documented cap. The flow bar needs three segments, so rather than a
ninth hue its parts share slot 8 and are separated by texture instead, which is
the sanctioned way past the cap.
"""

import calendar

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from viz.axes import _axis_ticks
from viz.data import (load_dam_names, load_dams_with_flow_and_year,
                      load_monthly_flow, load_monthly_generation,
                      load_monthly_passage, load_species, load_years)
from viz.theme import INK, series_colors, theme_mode

MONTHS = [calendar.month_abbr[m] for m in range(1, 13)]

# A month needs at least this much of its hours reported before its generation
# or flow bar is drawn. Three of Bonneville's 2025 months return header-only
# files from USACE; drawing their single stray hour as a bar would read as
# "this dam nearly stopped", which is the opposite of "we have no data".
MIN_COVERAGE = 0.9

# Texture separates the flow bar's parts, so they can share one hue.
FLOW_PATTERNS = {"gen_af": "", "spill_af": "/", "other_af": "."}
FLOW_LABELS = {"gen_af": "Through turbines", "spill_af": "Spilled",
               "other_af": "Other (locks, ladders)"}


def _bar_axis_titles(mode):
    ink = INK[mode]
    return ink


def monthly_chart(passage, generation, flow, *, species_order, species_colors,
                  gen_color, flow_color, mode, title):
    """Grouped bars per month; each group member stacked where it has parts."""
    ink = INK[mode]
    fig = go.Figure()
    drew = False

    # --- bar 1: fish, stacked by species ---------------------------------
    if not passage.empty:
        pivot = passage.pivot_table(index="month", columns="species",
                                    values="fish", aggfunc="sum").fillna(0)
        for sp in species_order:
            if sp not in pivot.columns:
                continue
            fig.add_trace(go.Bar(
                x=[MONTHS[m - 1] for m in pivot.index], y=pivot[sp],
                name=sp, legendgroup="fish", legendgrouptitle_text="Fish passage",
                marker=dict(color=species_colors[sp],
                            line=dict(width=0.5, color=ink["surface"])),
                offsetgroup="fish", yaxis="y",
                hovertemplate=f"%{{x}} · {sp}<br>%{{y:,.0f}} fish<extra></extra>",
            ))
            drew = True

    # --- bar 2: generation ------------------------------------------------
    if not generation.empty:
        fig.add_trace(go.Bar(
            x=[MONTHS[m - 1] for m in generation["month"]],
            y=generation["mwh"], name="Generation",
            legendgroup="gen", legendgrouptitle_text="Generation",
            marker=dict(color=gen_color,
                        line=dict(width=0.5, color=ink["surface"])),
            offsetgroup="gen", yaxis="y2",
            hovertemplate="%{x}<br>%{y:,.0f} MWh<extra></extra>",
        ))
        drew = True

    # --- bar 3: flow, stacked into its parts when the dam reports them ----
    if not flow.empty:
        split = bool(flow.get("has_split", pd.Series([False])).any())
        if split:
            for col in ("gen_af", "spill_af", "other_af"):
                if col not in flow or flow[col].notna().sum() == 0:
                    continue
                fig.add_trace(go.Bar(
                    x=[MONTHS[m - 1] for m in flow["month"]], y=flow[col],
                    name=FLOW_LABELS[col], legendgroup="flow",
                    legendgrouptitle_text="Flow",
                    marker=dict(color=flow_color,
                                # bgcolor must be set explicitly: plotly does
                                # not fall back to marker.color, so a pattern
                                # with only fgcolor renders white-on-white and
                                # the segment disappears entirely.
                                pattern=dict(shape=FLOW_PATTERNS[col],
                                             bgcolor=flow_color,
                                             fgcolor=ink["surface"], size=6,
                                             solidity=0.35),
                                line=dict(width=0.5, color=ink["surface"])),
                    offsetgroup="flow", yaxis="y3",
                    hovertemplate=f"%{{x}} · {FLOW_LABELS[col]}"
                                  "<br>%{y:,.0f} acre-ft<extra></extra>",
                ))
                drew = True
        else:
            fig.add_trace(go.Bar(
                x=[MONTHS[m - 1] for m in flow["month"]], y=flow["total_af"],
                name="Total flow", legendgroup="flow",
                legendgrouptitle_text="Flow",
                marker=dict(color=flow_color,
                            line=dict(width=0.5, color=ink["surface"])),
                offsetgroup="flow", yaxis="y3",
                hovertemplate="%{x}<br>%{y:,.0f} acre-ft<extra></extra>",
            ))
            drew = True

    if not drew:
        return None

    # Stacked totals set each axis's ceiling, so tick intervals come from the
    # summed height, not the tallest single segment.
    fish_top = (passage.groupby("month")["fish"].sum().max()
                if not passage.empty else 0) or 0
    gen_top = (generation["mwh"].max() if not generation.empty else 0) or 0
    flow_top = (flow["total_af"].max() if not flow.empty else 0) or 0
    _, fish_dtick = _axis_ticks([(pd.DataFrame({"v": [0, fish_top]}), "v")])
    _, gen_dtick = _axis_ticks([(pd.DataFrame({"v": [0, gen_top]}), "v")])
    _, flow_dtick = _axis_ticks([(pd.DataFrame({"v": [0, flow_top]}), "v")])

    # Three y-axes: the plot area is squeezed left to make room for the two
    # right-hand scales, and each axis wears the colour of its own bars.
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top",
                   font=dict(size=15, color=ink["primary"])),
        barmode="stack",   # traces stack within an offsetgroup, group across
        bargap=0.28, bargroupgap=0.06,
        height=560, margin=dict(l=76, r=128, t=58, b=124),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=ink["secondary"]),
        hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.16, xanchor="left",
                    x=0, font=dict(size=11), traceorder="grouped",
                    itemsizing="constant"),
        xaxis=dict(domain=[0.0, 0.86], title_text="Month",
                   # Plotly orders categories by first appearance, so a dam
                   # whose fish data starts in April would put Apr..Nov before
                   # Jan..Mar. Pin the calendar order explicitly.
                   categoryorder="array", categoryarray=MONTHS,
                   gridcolor="rgba(0,0,0,0)", showline=True,
                   linecolor=ink["grid"], tickfont=dict(size=11),
                   title_font=dict(size=12)),
        yaxis=dict(title=dict(text="Fish passage (count)",
                              font=dict(size=12, color=ink["secondary"])),
                   gridcolor=ink["grid"], zeroline=False, showline=True,
                   linecolor=ink["grid"], tickfont=dict(size=11),
                   rangemode="tozero", tickformat="~s",
                   tick0=0, dtick=fish_dtick),
        yaxis2=dict(title=dict(text="Generation (MWh)",
                               font=dict(size=12, color=gen_color)),
                    overlaying="y", side="right", showgrid=False,
                    zeroline=False, showline=True, linecolor=gen_color,
                    tickfont=dict(size=11, color=gen_color),
                    rangemode="tozero", tickformat="~s",
                    tick0=0, dtick=gen_dtick),
        yaxis3=dict(title=dict(text="Flow (acre-feet)",
                               font=dict(size=12, color=flow_color)),
                    overlaying="y", side="right", position=0.925, anchor="free",
                    showgrid=False, zeroline=False, showline=True,
                    linecolor=flow_color,
                    tickfont=dict(size=11, color=flow_color),
                    rangemode="tozero", tickformat="~s",
                    tick0=0, dtick=flow_dtick),
    )
    return fig


def render():
    """The monthly profile page."""
    mode = theme_mode()
    palette = series_colors(mode)
    names = load_dam_names()
    all_years = load_years()

    def label(code):
        return f"{names.get(code, code)} ({code})"

    st.sidebar.title("Monthly filters")
    default_year = 2025 if 2025 in all_years else (all_years[-1] if all_years else None)
    if not all_years:
        st.title("Monthly profile")
        st.info("No data loaded yet. Run `etl/backfill.py` first.")
        return

    year = st.sidebar.selectbox(
        "Year", options=all_years,
        index=all_years.index(default_year) if default_year in all_years else 0)

    dam_options = load_dams_with_flow_and_year(year) or []
    if not dam_options:
        st.title("Monthly profile")
        st.info(f"No dam has flow or generation data for {year}.")
        return
    default_dam = "BON" if "BON" in dam_options else dam_options[0]
    dam = st.sidebar.selectbox(
        "Dam", options=dam_options, format_func=label,
        index=dam_options.index(default_dam))

    # Colour follows the species as an entity: assign slots from the full
    # species list so filtering never repaints the ones that remain.
    all_species = load_species()
    species_colors = {sp: palette[i % len(palette)]
                      for i, sp in enumerate(all_species)}
    gen_color = palette[6]
    flow_color = palette[7]

    passage = load_monthly_passage(dam, year)
    generation = load_monthly_generation(dam, year)
    flow = load_monthly_flow(dam, year)

    # Only report gaps up to the last month that has any data at all, so a
    # year still in progress is not described as "missing" its future months.
    seen = [df["month"].max() for df in (passage, generation, flow) if not df.empty]
    last_month = int(max(seen)) if seen else 0

    # Suppress months that barely reported rather than drawing a stub bar. A
    # month with no rows whatsoever never appears in the frame to be filtered,
    # so gaps are worked out against the calendar, not against what is present.
    if not generation.empty:
        generation = generation[generation["coverage"] >= MIN_COVERAGE]
    if not flow.empty:
        flow = flow[flow["coverage"] >= MIN_COVERAGE]
    have_gen = set(generation["month"]) if not generation.empty else set()
    have_flow = set(flow["month"]) if not flow.empty else set()
    missing = sorted((set(range(1, last_month + 1)) - have_gen)
                     | (set(range(1, last_month + 1)) - have_flow))

    st.title("Monthly profile")
    st.caption("Fish passage, generation and river flow as monthly totals for a "
               "single dam and year. Each month shows three bars: fish (stacked "
               "by species), generation, and total flow.")

    species_order = [sp for sp in all_species
                     if not passage.empty and sp in set(passage["species"])]

    fig = monthly_chart(passage, generation, flow,
                        species_order=species_order,
                        species_colors=species_colors, gen_color=gen_color,
                        flow_color=flow_color, mode=mode,
                        title=f"{label(dam)} — {year}")
    if fig is None:
        st.warning(f"No monthly data for {label(dam)} in {year}.")
        return

    st.plotly_chart(fig, width="stretch", key=f"monthly-{dam}-{year}")
    st.caption("The three bars use three different y-axes, so their heights are "
               "not comparable to each other — only compare a bar with the same "
               "bar in other months. Exact values are in the table below.")

    if missing:
        st.info(f"No generation or flow bar for "
                f"{', '.join(MONTHS[m - 1] for m in missing)}: USACE serves those "
                "months without data rows, so they are left blank rather than "
                "drawn as near-zero.")

    # Table view: three light-mode slots sit below 3:1 contrast on the surface,
    # so the same numbers must be readable another way.
    with st.expander(f"Monthly data — {label(dam)} {year}"):
        table = pd.DataFrame({"Month": MONTHS, "month": range(1, 13)})
        if not passage.empty:
            wide = passage.pivot_table(index="month", columns="species",
                                       values="fish", aggfunc="sum")
            table = table.merge(wide.reset_index(), on="month", how="left")
            table["Fish total"] = table[[c for c in wide.columns]].sum(axis=1)
        if not generation.empty:
            table = table.merge(generation[["month", "mwh"]].rename(
                columns={"mwh": "Generation (MWh)"}), on="month", how="left")
        if not flow.empty:
            cols = {"total_af": "Flow total (acre-ft)",
                    "gen_af": "Through turbines", "spill_af": "Spilled",
                    "other_af": "Other"}
            keep = ["month"] + [c for c in cols if c in flow]
            table = table.merge(flow[keep].rename(columns=cols), on="month",
                                how="left")
        st.dataframe(table.drop(columns=["month"]), width="stretch",
                     hide_index=True)
