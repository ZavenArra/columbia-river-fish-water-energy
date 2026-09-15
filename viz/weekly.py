"""
Weekly overview page: each dam's measures against ISO week, one line per year.

Moved verbatim out of the original single-page app.py; behaviour is unchanged.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from viz.axes import _axis_ticks
from viz.data import (load_availability, load_dam_names, load_flow_dams,
                      load_generation, load_passage, load_species,
                      load_temperature, load_years, weekly)
from viz.theme import INK, MAX_YEARS, series_colors, theme_mode


def dual_axis_chart(left, right, *, left_label, right_label, left_col, right_col,
                    year_colors, years, title, mode):
    """
    Two measures against ISO week, one line per year per measure.

    Colour carries the YEAR and line style carries the MEASURE (solid = left,
    dashed = right), so the two encodings stay independent and neither relies on
    colour alone. Year colours are assigned from the full set of years in the
    database, so changing the year filter never repaints the survivors.
    """
    ink = INK[mode]
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    plotted = False

    for year in years:
        colour = year_colors.get(year)
        lslice = left[left["iso_year"] == year] if not left.empty else left
        rslice = right[right["iso_year"] == year] if not right.empty else right

        if not lslice.empty:
            fig.add_trace(go.Scatter(
                x=lslice["iso_week"], y=lslice[left_col],
                name=f"{year} · {left_label}", legendgroup=str(year),
                # A one- or two-point series draws no visible line, so it would
                # sit in the legend pointing at nothing. Add markers instead.
                mode="lines" if len(lslice) > 2 else "lines+markers",
                marker=dict(size=8, color=colour),
                line=dict(color=colour, width=2),
                hovertemplate=f"week %{{x}}<br>{left_label} %{{y:,.1f}}<extra>"
                              f"{year}</extra>",
            ), secondary_y=False)
            plotted = True

        if not rslice.empty:
            fig.add_trace(go.Scatter(
                x=rslice["iso_week"], y=rslice[right_col],
                name=f"{year} · {right_label}", legendgroup=str(year),
                mode="lines" if len(rslice) > 2 else "lines+markers",
                marker=dict(size=8, color=colour, symbol="diamond"),
                line=dict(color=colour, width=2, dash="dot"),
                hovertemplate=f"week %{{x}}<br>{right_label} %{{y:,.0f}}<extra>"
                              f"{year}</extra>",
            ), secondary_y=True)
            plotted = True

    if not plotted:
        return None

    # Legend sits BELOW the plot: a horizontal legend above it collides with the
    # title once there are two entries per year. Margins are generous enough for
    # tick labels and axis titles -- the first render clipped both.
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top",
                   font=dict(size=15, color=ink["primary"])),
        hovermode="x unified",
        height=470,
        margin=dict(l=72, r=78, t=54, b=104),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=ink["secondary"]),
        legend=dict(orientation="h", yanchor="top", y=-0.22,
                    xanchor="left", x=0, font=dict(size=11),
                    itemsizing="constant", traceorder="grouped",
                    groupclick="toggleitem"),
    )
    fig.update_xaxes(title_text="ISO week of year", range=[1, 53], dtick=10,
                     gridcolor=ink["grid"], zeroline=False, showline=True,
                     linecolor=ink["grid"], tickfont=dict(size=11),
                     title_font=dict(size=12))
    # Only the left axis carries gridlines; two grids for two scales would imply
    # a shared one that does not exist.
    ltick0, ldtick = _axis_ticks([(left, left_col)])
    rtick0, rdtick = _axis_ticks([(right, right_col)])
    fig.update_yaxes(title_text=left_label, secondary_y=False, tickformat="~s",
                     tick0=ltick0, dtick=ldtick,
                     gridcolor=ink["grid"], zeroline=False, showline=True,
                     linecolor=ink["grid"], tickfont=dict(size=11),
                     title_font=dict(size=12))
    fig.update_yaxes(title_text=right_label, secondary_y=True, tickformat="~s",
                     tick0=rtick0, dtick=rdtick,
                     showgrid=False, zeroline=False, showline=True,
                     linecolor=ink["grid"], tickfont=dict(size=11),
                     title_font=dict(size=12))
    return fig




def render():
    """The weekly overview page."""
    mode = theme_mode()
    names = load_dam_names()
    availability = load_availability().set_index("dam_code")
    flow_dams = load_flow_dams()
    all_years = load_years()
    all_species = load_species()

    def label(code):
        return f"{names.get(code, code)} ({code})"

    def has(code, field):
        return bool(availability["has_" + field].get(code, 0))

    # Colour follows the year as an entity: assign slots from every year in the
    # database, so filtering years never repaints the ones that remain.
    palette = series_colors(mode)
    year_colors = {y: palette[i] for i, y in enumerate(all_years[:MAX_YEARS])}

    # --- sidebar ---------------------------------------------------------
    st.sidebar.title("Weekly filters")

    complete = [d for d in flow_dams
                if has(d, "flow_gen") and has(d, "passage") and has(d, "temp")]
    default_dams = (complete or flow_dams)[:4]

    dams = st.sidebar.multiselect(
        "Dams", options=flow_dams, default=default_dams, format_func=label,
        help="Dams with flow/generation data. Defaults to the first four that "
             "have generation, passage and temperature.",
    )

    # Default to the most recent years, and keep the most recent when the cap
    # bites. Defaulting to everything and then truncating from the FRONT meant
    # that once a deep backfill landed, the page silently showed the OLDEST
    # eight years -- which for most dams is no data at all.
    years = st.sidebar.multiselect("Years", options=all_years,
                                   default=all_years[-MAX_YEARS:])
    if len(years) > MAX_YEARS:
        st.sidebar.warning(
            f"Showing the {MAX_YEARS} most recent years selected. Past "
            f"{MAX_YEARS} series the categorical palette would have to reuse a "
            "hue, which is indistinguishable under colour-vision deficiency."
        )
        years = years[-MAX_YEARS:]

    passage_dams = [d for d in dams if has(d, "passage")]
    if passage_dams:
        species = st.sidebar.multiselect(
            "Species", options=all_species, default=all_species,
            help="Applies to the "
                 f"{len(passage_dams)} selected dam(s) with passage data.",
        )
    else:
        species = []
        if dams:
            st.sidebar.caption("No selected dam has fish passage data, so the "
                               "species filter is hidden.")

    st.caption("Weekly fish passage against the river conditions and dam "
               "operations at the same time. Colour identifies the year; solid "
               "and dotted lines identify the two measures.")

    if not dams:
        st.info("Choose at least one dam in the sidebar.")
        return
    if not years:
        st.info("Choose at least one year in the sidebar.")
        return

    gen = weekly(load_generation(tuple(dams)), "gen_mw", "mean")
    pas = weekly(load_passage(tuple(dams), tuple(species)), "count", "sum")
    tmp = weekly(load_temperature(tuple(dams)), "value_f", "mean")

    for code in dams:
        st.subheader(label(code))
        g = gen[gen["dam_code"] == code] if not gen.empty else gen
        p = pas[pas["dam_code"] == code] if not pas.empty else pas
        t = tmp[tmp["dam_code"] == code] if not tmp.empty else tmp

        missing = []
        if g.empty:
            missing.append("generation")
        if p.empty:
            missing.append("fish passage")
        if t.empty:
            missing.append("temperature")

        specs = [
            ("Generation vs. fish passage", g, p, "Mean MW", "Fish/week",
             "gen_mw", "count"),
            ("Water temperature vs. fish passage", t, p, "Mean °F", "Fish/week",
             "value_f", "count"),
            ("Generation vs. water temperature", g, t, "Mean MW", "Mean °F",
             "gen_mw", "value_f"),
        ]

        drawn = 0
        for title, left, right, llab, rlab, lcol, rcol in specs:
            # Both measures are required. Drawing "Generation vs. temperature"
            # with only temperature present would be a chart whose title
            # promises a comparison the data cannot make.
            if left.empty or right.empty:
                continue
            fig = dual_axis_chart(left, right, left_label=llab, right_label=rlab,
                                  left_col=lcol, right_col=rcol,
                                  year_colors=year_colors, years=years,
                                  title=title, mode=mode)
            if fig is None:
                continue
            st.plotly_chart(fig, width='stretch',
                            key=f"{code}-{title}")
            drawn += 1

        if missing:
            st.info(f"**{label(code)}** has no {' or '.join(missing)} data for "
                    "the current filters, so charts needing it are omitted.")
        if drawn == 0:
            st.warning(f"Nothing to plot for {label(code)} with these filters.")

        # Table view: the light-mode palette WARNs on contrast for two slots,
        # which obliges an alternative reading of the same numbers.
        with st.expander(f"Weekly data — {label(code)}"):
            table = None
            for frame, col in ((g, "gen_mw"), (t, "value_f"), (p, "count")):
                if frame.empty:
                    continue
                piece = frame[["iso_year", "iso_week", col]]
                table = piece if table is None else table.merge(
                    piece, on=["iso_year", "iso_week"], how="outer")
            if table is None:
                st.caption("No data.")
            else:
                table = table[table["iso_year"].isin(years)]
                st.dataframe(
                    table.rename(columns={
                        "iso_year": "ISO year", "iso_week": "ISO week",
                        "gen_mw": "Mean MW", "value_f": "Mean °F",
                        "count": "Fish/week"}).sort_values(
                        ["ISO year", "ISO week"]),
                    width='stretch', hide_index=True)
        st.divider()
