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
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from viz.axes import _axis_ticks, _nice_dtick
from viz.data import (load_dam_names, load_dams_with_flow_and_year,
                      load_global_fish_max, load_global_temp_range,
                      load_monthly_flow,
                      load_monthly_generation, load_monthly_passage,
                      load_monthly_temperature, load_species, load_years)
from dams import MONTHLY_DAM_ORDER
from viz.theme import (FLOW_RAMP, GENERATION_COLOR, INK, TEMPERATURE_COLOR,
                       species_palette, theme_mode)

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


def monthly_chart(passage, generation, flow, temperature, *, species_order,
                  species_colors, gen_color, flow_ramp, temp_color, mode, title,
                  fish_max=None, fish_log=False, temp_bounds=None):
    """Grouped bars per month, with the temperature line in a band above them."""
    ink = INK[mode]
    fig = go.Figure()
    drew = False

    # --- bar 1: generation ------------------------------------------------
    # Trace order sets the left-to-right order of the bar groups, so these
    # blocks run generation, flow, fish -- fish last puts it on the right.
    if not generation.empty:
        fig.add_trace(go.Bar(
            x=[MONTHS[m - 1] for m in generation["month"]],
            y=generation["mwh"], name="Generation",
            legendgroup="gen", legendgrouptitle_text="Generation",
            # Stippled, not solid. Generation orange sits between the red and
            # yellow species in hue and cannot clear the colour-separation
            # floor against both, so the fill carries a second cue and the bar
            # is not mistaken for the Shad segment beside it. bgcolor must be
            # set explicitly -- plotly does not fall back to marker.color, and
            # a pattern with only fgcolor renders white-on-white.
            marker=dict(color=gen_color,
                        pattern=dict(shape=".", bgcolor=gen_color,
                                     fgcolor=ink["surface"], size=5,
                                     solidity=0.32),
                        line=dict(width=0.5, color=ink["surface"])),
            offsetgroup="gen", yaxis="y2",
            hovertemplate="%{x}<br>%{y:,.0f} MWh<extra></extra>",
        ))
        drew = True

    # --- bar 2: flow, stacked into its parts when the dam reports them ----
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
                    marker=dict(color=flow_ramp[col],
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
                marker=dict(color=flow_ramp["gen_af"],
                            line=dict(width=0.5, color=ink["surface"])),
                offsetgroup="flow", yaxis="y3",
                hovertemplate="%{x}<br>%{y:,.0f} acre-ft<extra></extra>",
            ))
            drew = True

    # --- bar 3: fish, stacked by species ---------------------------------
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

    # --- temperature line, in its own band above the bars ----------------
    # The band is pinned to the global bounds, so it never rescales per chart.
    temp_range, temp_ticks = _temp_axis_range(*(temp_bounds or (None, None)))
    if temperature is not None and not temperature.empty:
        fig.add_trace(go.Scatter(
            x=[MONTHS[m - 1] for m in temperature["month"]],
            y=temperature["max_f"], name="Max temperature",
            legendgroup="temp", legendgrouptitle_text="Temperature",
            mode="lines+markers", yaxis="y4",
            line=dict(color=temp_color, width=2),
            marker=dict(size=8, color=temp_color,
                        line=dict(width=1, color=ink["surface"])),
            hovertemplate="%{x}<br>max %{y:.1f} °F<extra></extra>",
        ))
        drew = True

    if not drew:
        return None

    # Pin every bar axis to [0, top / BAR_BAND]: identical zero for all three,
    # and headroom left over for the temperature band.
    def bar_range(top):
        return [0, (top / BAR_BAND) if top else 1]

    # The fish ceiling is global, not per-chart: the same maximum on every dam
    # and year is what lets bar heights be compared between charts at all.
    fish_top = fish_max or ((passage.groupby("month")["fish"].sum().max()
                             if not passage.empty else 0) or 0)
    gen_top = (generation["mwh"].max() if not generation.empty else 0) or 0
    flow_top = (flow["total_af"].max() if not flow.empty else 0) or 0
    _, fish_dtick = _axis_ticks([(pd.DataFrame({"v": [0, fish_top]}), "v")])
    _, gen_dtick = _axis_ticks([(pd.DataFrame({"v": [0, gen_top]}), "v")])
    _, flow_dtick = _axis_ticks([(pd.DataFrame({"v": [0, flow_top]}), "v")])

    # A log fish axis needs its ceiling scaled in EXPONENT space, not value
    # space: log compresses the top so hard that almost any bar would otherwise
    # reach the temperature band. Putting log10(global max) at BAR_BAND keeps
    # the tallest bar exactly where the linear axis puts it, and the ceiling
    # stays a fixed number so it is still identical across charts.
    if fish_log:
        decades = math.log10(max(fish_top, 10.0))
        fish_axis = dict(type="log", range=[0, decades / BAR_BAND],
                         tickmode="array",
                         tickvals=[10 ** k
                                   for k in range(0, int(math.ceil(decades)) + 1)])
    else:
        fish_axis = dict(range=bar_range(fish_top), tick0=0, dtick=fish_dtick)

    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top",
                   font=dict(size=15, color=ink["primary"])),
        barmode="stack",   # traces stack within an offsetgroup, group across
        bargap=0.28, bargroupgap=0.06,
        height=580, margin=dict(l=132, r=128, t=58, b=124),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=ink["secondary"]),
        hovermode="closest",
        # Grouped legend entries otherwise bunch against the left edge.
        # Giving each entry a fixed fraction of the width spreads the four
        # groups evenly across the area under the chart.
        legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="left",
                    x=0, font=dict(size=11), traceorder="grouped",
                    itemsizing="constant", entrywidthmode="fraction",
                    entrywidth=0.24, tracegroupgap=8,
                    grouptitlefont=dict(size=12)),
        xaxis=dict(domain=[0.06, 0.86], title_text="Month",
                   # Plotly orders categories by first appearance, so a dam
                   # whose fish data starts in April would put Apr..Nov before
                   # Jan..Mar. Pin the calendar order explicitly.
                   categoryorder="array", categoryarray=MONTHS,
                   gridcolor="rgba(0,0,0,0)", showline=True,
                   linecolor=ink["grid"], tickfont=dict(size=11),
                   title_font=dict(size=12)),
        yaxis=dict(title=dict(text="Fish passage (count)"
                                   + (" — log scale" if fish_log else ""),
                              font=dict(size=12, color=ink["secondary"])),
                   gridcolor=ink["grid"], zeroline=False, showline=True,
                   linecolor=ink["grid"], tickfont=dict(size=11),
                   tickformat="~s", **fish_axis),
        yaxis2=dict(title=dict(text="Generation (MWh)",
                               font=dict(size=12, color=gen_color)),
                    range=bar_range(gen_top), tick0=0, dtick=gen_dtick,
                    overlaying="y", side="right", showgrid=False,
                    zeroline=False, showline=True, linecolor=gen_color,
                    tickfont=dict(size=11, color=gen_color), tickformat="~s"),
        yaxis3=dict(title=dict(text="Flow (acre-feet)",
                               font=dict(size=12, color=flow_ramp["gen_af"])),
                    range=bar_range(flow_top), tick0=0, dtick=flow_dtick,
                    overlaying="y", side="right", position=0.925, anchor="free",
                    showgrid=False, zeroline=False, showline=True,
                    linecolor=flow_ramp["gen_af"],
                    tickfont=dict(size=11, color=flow_ramp["gen_af"]),
                    tickformat="~s"),
    )

    # Drawn whenever bounds exist, even if this dam-year has no readings, so
    # the axis does not appear and disappear as the selection changes.
    if temp_range:
        fig.update_layout(yaxis4=dict(
            title=dict(text="Max temperature (°F)",
                       font=dict(size=12, color=temp_color)),
            range=temp_range, overlaying="y", side="left", anchor="free",
            position=0.0, showgrid=False, zeroline=False,
            # No axis line: it would run the full plot height and imply the
            # temperature scale extends down through the bars, when the line
            # only occupies the band at the top. Ticks alone carry the scale.
            showline=False,
            linecolor=temp_color, tickmode="array", tickvals=temp_ticks,
            ticktext=[f"{v:.0f}" for v in temp_ticks],
            tickfont=dict(size=11, color=temp_color)))
    return fig


def render():
    """The monthly profile page."""
    mode = theme_mode()
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

    # Offered in geographic order -- up the Columbia from the ocean, then up
    # the Snake -- rather than alphabetically, so the list reads as a journey
    # a fish makes. Storage dams are excluded: no ladder means no passage data.
    with_data = set(load_dams_with_flow_and_year(year) or [])
    dam_options = [d for d in MONTHLY_DAM_ORDER if d in with_data]
    if not dam_options:
        st.title("Monthly profile")
        st.info(f"No mainstem dam has flow or generation data for {year}.")
        return
    default_dam = "BON" if "BON" in dam_options else dam_options[0]
    dam = st.sidebar.selectbox(
        "Dam", options=dam_options, format_func=label,
        index=dam_options.index(default_dam),
        help="Columbia mainstem from the ocean upstream, then the Snake.")

    # Colour follows the species as an entity: assigned in fixed order from the
    # full species list, so unchecking one never repaints the rest.
    all_species = load_species()
    species_colors = species_palette(mode, all_species)

    st.sidebar.markdown("**Species**")
    chosen_species = [sp for sp in all_species
                      if st.sidebar.checkbox(sp, value=True, key=f"species_{sp}")]
    if not chosen_species:
        st.sidebar.caption("No species selected — the fish bar is hidden.")
    fish_log = st.sidebar.checkbox(
        "Log scale for fish axis", value=False,
        help="Shows the smaller species counts. The axis maximum stays the "
             "same as on the linear scale, so charts remain comparable.")
    gen_color = GENERATION_COLOR["dark" if mode == "dark" else "light"]
    flow_ramp = FLOW_RAMP["dark" if mode == "dark" else "light"]
    temp_color = TEMPERATURE_COLOR["dark" if mode == "dark" else "light"]

    passage = load_monthly_passage(dam, year)
    if not passage.empty:
        passage = passage[passage["species"].isin(chosen_species)]
    generation = load_monthly_generation(dam, year)
    flow = load_monthly_flow(dam, year)
    temperature = load_monthly_temperature(dam, year)

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

    # Stack order follows the full species list, not the selection, so a
    # species keeps its position in the stack regardless of what else is on.
    species_order = [sp for sp in all_species
                     if not passage.empty and sp in set(passage["species"])]

    fig = monthly_chart(passage, generation, flow, temperature,
                        species_order=species_order,
                        species_colors=species_colors, gen_color=gen_color,
                        flow_ramp=flow_ramp, temp_color=temp_color, mode=mode,
                        title=f"{label(dam)} — {year}",
                        fish_max=load_global_fish_max(), fish_log=fish_log,
                        temp_bounds=load_global_temp_range())
    if fig is None:
        st.warning(f"No monthly data for {label(dam)} in {year}.")
        return

    st.plotly_chart(fig, width="stretch", key=f"monthly-{dam}-{year}")
    if fish_log:
        st.caption(":material/warning: On a log scale the fish bar's stacked "
                   "segments are no longer proportional to their counts — a "
                   "segment's height is the gap between two logarithms, not the "
                   "value. Read species totals from the table, and use the log "
                   "scale to see which small species are present at all.")
    st.caption("The three bars use three different y-axes, so their heights are "
               "not comparable to each other — only compare a bar with the same "
               "bar in other months. All three share one baseline at zero. The "
               "temperature line has its own scale in the band above the bars. "
               "Exact values are in the table below.")

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
        if temperature is not None and not temperature.empty:
            table = table.merge(temperature[["month", "max_f"]].rename(
                columns={"max_f": "Max temp (°F)"}), on="month", how="left")
        if not flow.empty:
            cols = {"total_af": "Flow total (acre-ft)",
                    "gen_af": "Through turbines", "spill_af": "Spilled",
                    "other_af": "Other"}
            keep = ["month"] + [c for c in cols if c in flow]
            table = table.merge(flow[keep].rename(columns=cols), on="month",
                                how="left")
        st.dataframe(table.drop(columns=["month"]), width="stretch",
                     hide_index=True)
