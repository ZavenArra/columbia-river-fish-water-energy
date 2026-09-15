"""
Year-over-year page: how each monthly measure changed against the year before.

Every bar is (this year - previous year) for the same month, so the chart reads
as "what moved". Positive changes stack upward from the zero line and negative
ones stack downward from it, which is Plotly's `barmode="relative"`.

Temperature is NOT differenced -- a line of temperature changes is far harder to
read than two temperature lines. The target year is drawn solid and the previous
year dotted, both on the same globally-pinned band used elsewhere.

A month is only drawn where BOTH years have usable data. A missing month is not
a change of zero, and filling one in would invent a drop the river never made.
"""

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dams import MONTHLY_DAM_ORDER
from viz.axes import (BAR_BAND, FLOW_LABELS, MIN_COVERAGE, MONTHS, _axis_ticks,
                      _temp_axis_range, add_temperature_line,
                      shared_zero_ranges)
from viz.data import (load_comparable_years, load_dam_names,
                      load_global_temp_range,
                      load_monthly_flow, load_monthly_generation,
                      load_monthly_passage, load_monthly_temperature,
                      load_species)
from viz.schematic import river_schematic
from viz.theme import (FLOW_RAMP, GENERATION_COLOR, INK, TEMPERATURE_COLOR,
                       species_palette, theme_mode)

YEAR_KEY = "delta_target_year"


# ---------------------------------------------------------------------------
# Differencing
# ---------------------------------------------------------------------------

def _usable_months(frame):
    if frame is None or frame.empty or "coverage" not in frame:
        return set() if frame is None or frame.empty else set(frame["month"])
    return set(frame.loc[frame["coverage"] >= MIN_COVERAGE, "month"])


def diff_by_month(cur, prev, value_cols, months):
    """
    (current - previous) per month for one or more value columns.

    Restricted to `months`, which the caller has already narrowed to those
    usable in both years.
    """
    empty = pd.DataFrame(columns=["month"] + list(value_cols))
    if not months:
        return empty
    cur = cur[cur["month"].isin(months)] if cur is not None and not cur.empty else None
    prev = prev[prev["month"].isin(months)] if prev is not None and not prev.empty else None
    if cur is None or prev is None or cur.empty or prev.empty:
        return empty
    merged = cur[["month"] + list(value_cols)].merge(
        prev[["month"] + list(value_cols)], on="month", how="inner",
        suffixes=("_cur", "_prev"))
    out = pd.DataFrame({"month": merged["month"]})
    for col in value_cols:
        out[col] = (merged[f"{col}_cur"].fillna(0) - merged[f"{col}_prev"].fillna(0))
    return out


def diff_passage(cur, prev, months, species):
    """
    -> month, species, delta

    Within a month that both years report, a species absent from one year is
    treated as zero fish -- that is what no rows means for a passage count --
    but a month absent from either year is dropped entirely.
    """
    if not months:
        return pd.DataFrame(columns=["month", "species", "delta"])

    def pivot(frame):
        if frame is None or frame.empty:
            return pd.DataFrame()
        f = frame[frame["month"].isin(months)]
        if f.empty:
            return pd.DataFrame()
        return f.pivot_table(index="month", columns="species", values="fish",
                             aggfunc="sum").fillna(0)

    a, b = pivot(cur), pivot(prev)
    if a.empty or b.empty:
        return pd.DataFrame(columns=["month", "species", "delta"])
    shared = sorted(set(a.index) & set(b.index))
    rows = []
    for sp in species:
        ca = a[sp] if sp in a.columns else None
        cb = b[sp] if sp in b.columns else None
        for m in shared:
            va = float(ca.get(m, 0)) if ca is not None else 0.0
            vb = float(cb.get(m, 0)) if cb is not None else 0.0
            if va or vb:
                rows.append({"month": m, "species": sp, "delta": va - vb})
    return pd.DataFrame(rows, columns=["month", "species", "delta"])


def _extent(frame, group_cols, value_col):
    """(largest negative magnitude, largest positive) of the STACKED totals."""
    if frame is None or frame.empty or value_col not in frame:
        return (0.0, 0.0)
    v = frame[[*group_cols, value_col]].copy()
    pos = v[v[value_col] > 0].groupby(group_cols)[value_col].sum()
    neg = v[v[value_col] < 0].groupby(group_cols)[value_col].sum()
    return (abs(float(neg.min())) if len(neg) else 0.0,
            float(pos.max()) if len(pos) else 0.0)


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def delta_chart(fish, gen, flow, temp_cur, temp_prev, *, species_order,
                species_colors, gen_color, flow_ramp, temp_color, mode, title,
                year, prev_year, temp_bounds=None):
    ink = INK[mode]
    fig = go.Figure()
    drew = False

    # Bar groups left to right: generation, flow, fish -- same order as the
    # monthly page, so the two charts read the same way.
    if gen is not None and not gen.empty:
        fig.add_trace(go.Bar(
            x=[MONTHS[m - 1] for m in gen["month"]], y=gen["mwh"],
            name="Generation", legendgroup="gen",
            legendgrouptitle_text="Generation change",
            marker=dict(color=gen_color,
                        pattern=dict(shape=".", bgcolor=gen_color,
                                     fgcolor=ink["surface"], size=5,
                                     solidity=0.32),
                        line=dict(width=0.5, color=ink["surface"])),
            offsetgroup="gen", yaxis="y2",
            hovertemplate="%{x}<br>%{y:+,.0f} MWh<extra></extra>"))
        drew = True

    if flow is not None and not flow.empty:
        cols = [c for c in ("gen_af", "spill_af", "other_af") if c in flow]
        if not cols:
            cols = ["total_af"]
        for col in cols:
            if flow[col].abs().sum() == 0:
                continue
            fig.add_trace(go.Bar(
                x=[MONTHS[m - 1] for m in flow["month"]], y=flow[col],
                name=FLOW_LABELS.get(col, "Total flow"), legendgroup="flow",
                legendgrouptitle_text="Flow change",
                marker=dict(color=flow_ramp.get(col, flow_ramp["gen_af"]),
                            line=dict(width=0.5, color=ink["surface"])),
                offsetgroup="flow", yaxis="y3",
                hovertemplate=f"%{{x}} · {FLOW_LABELS.get(col, 'Total flow')}"
                              "<br>%{y:+,.0f} acre-ft<extra></extra>"))
            drew = True

    if fish is not None and not fish.empty:
        for sp in species_order:
            part = fish[fish["species"] == sp]
            if part.empty:
                continue
            fig.add_trace(go.Bar(
                x=[MONTHS[m - 1] for m in part["month"]], y=part["delta"],
                name=sp, legendgroup="fish",
                legendgrouptitle_text="Fish passage change",
                marker=dict(color=species_colors[sp],
                            line=dict(width=0.5, color=ink["surface"])),
                offsetgroup="fish", yaxis="y",
                hovertemplate=f"%{{x}} · {sp}<br>%{{y:+,.0f}} fish<extra></extra>"))
            drew = True

    # Temperature: both years as levels, not a difference, each coloured by
    # thermal-stress band. The band legend is taken from the target year only;
    # the previous year gets a single style key, since dash carries the year and
    # colour carries the band on both lines.
    temp_range, temp_ticks = _temp_axis_range(*(temp_bounds or (None, None)))
    if temp_prev is not None and not temp_prev.empty:
        if add_temperature_line(
                fig, [MONTHS[m - 1] for m in temp_prev["month"]],
                list(temp_prev["max_f"]), mode=mode, surface=ink["surface"],
                dash="dot", show_band_legend=False,
                hover_label=f"{prev_year} max"):
            drew = True
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines",
                name=f"{prev_year} (dotted)", legendgroup="temp",
                legendgrouptitle_text="Max temperature",
                line=dict(color=ink["secondary"], width=2, dash="dot"),
                hoverinfo="skip"))
    if temp_cur is not None and not temp_cur.empty:
        if add_temperature_line(
                fig, [MONTHS[m - 1] for m in temp_cur["month"]],
                list(temp_cur["max_f"]), mode=mode, surface=ink["surface"],
                hover_label=f"{year} max"):
            drew = True

    if not drew:
        return None

    ranges = shared_zero_ranges({
        "fish": _extent(fish, ["month", "species"], "delta")
                if fish is not None and not fish.empty else (0.0, 0.0),
        "gen": _extent(gen, ["month"], "mwh")
               if gen is not None and not gen.empty else (0.0, 0.0),
        "flow": _extent(
            flow.melt(id_vars="month", var_name="part", value_name="v")
            if flow is not None and not flow.empty else None,
            ["month", "part"], "v") if flow is not None and not flow.empty
            else (0.0, 0.0),
    })

    def ticks(rng):
        _, d = _axis_ticks([(pd.DataFrame({"v": list(rng)}), "v")])
        return d

    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top",
                   font=dict(size=15, color=ink["primary"])),
        # "relative" is what stacks positives up from zero and negatives down.
        barmode="relative", bargap=0.28, bargroupgap=0.06,
        height=620, margin=dict(l=148, r=150, t=58, b=124),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=ink["secondary"]), hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="left",
                    x=0, font=dict(size=11), traceorder="grouped",
                    itemsizing="constant", entrywidthmode="fraction",
                    entrywidth=0.24, tracegroupgap=8,
                    grouptitlefont=dict(size=12)),
        xaxis=dict(domain=[0.055, 0.88], title_text="Month",
                   categoryorder="array", categoryarray=MONTHS,
                   gridcolor="rgba(0,0,0,0)", showline=True,
                   linecolor=ink["grid"], tickfont=dict(size=11),
                   title_font=dict(size=12)),
        yaxis=dict(title=dict(text=f"Fish passage change ({year} − {prev_year})",
                              font=dict(size=12, color=ink["secondary"])),
                   range=ranges["fish"], dtick=ticks(ranges["fish"]),
                   gridcolor=ink["grid"], zeroline=True,
                   zerolinecolor=ink["secondary"], zerolinewidth=1.5,
                   showline=True, linecolor=ink["grid"],
                   tickfont=dict(size=11), tickformat="~s"),
        yaxis2=dict(title=dict(text="Δ MWh",
                               font=dict(size=12, color=gen_color)),
                    range=ranges["gen"], dtick=ticks(ranges["gen"]),
                    overlaying="y", side="right", showgrid=False,
                    zeroline=False, showline=True, linecolor=gen_color,
                    tickfont=dict(size=11, color=gen_color), tickformat="~s"),
        yaxis3=dict(title=dict(text="Δ acre-feet",
                               font=dict(size=12, color=flow_ramp["gen_af"])),
                    range=ranges["flow"], dtick=ticks(ranges["flow"]),
                    overlaying="y", side="right", position=0.945, anchor="free",
                    showgrid=False, zeroline=False, showline=True,
                    linecolor=flow_ramp["gen_af"],
                    tickfont=dict(size=11, color=flow_ramp["gen_af"]),
                    tickformat="~s"),
    )
    if temp_range:
        fig.update_layout(yaxis4=dict(
            title=dict(text="Max temperature (°F)",
                       font=dict(size=12, color=ink["secondary"])),
            range=temp_range, overlaying="y", side="left", anchor="free",
            position=0.0, showgrid=False, zeroline=False, showline=False,
            linecolor=ink["secondary"], tickmode="array", tickvals=temp_ticks,
            ticktext=[f"{v:.0f}" for v in temp_ticks],
            tickfont=dict(size=11, color=ink["secondary"])))
    return fig


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def _step_year(options, delta):
    """Move the target year by one place, clamped to the ends."""
    try:
        i = options.index(st.session_state[YEAR_KEY])
    except (ValueError, KeyError):
        return
    st.session_state[YEAR_KEY] = options[min(max(i + delta, 0), len(options) - 1)]


def render():
    """The year-over-year change page."""
    mode = theme_mode()
    names = load_dam_names()

    def label(code):
        return f"{names.get(code, code)} ({code})"

    st.sidebar.title("Year-over-year filters")

    # Only offer dams and years that can actually be differenced: a year must
    # share at least one substantially-loaded month with its predecessor. Mere
    # presence of rows is not enough -- a refresh can leave a few stray hours in
    # an otherwise empty year, and offering that produces an empty chart.
    pairs = load_comparable_years()
    dam_options = [d for d in MONTHLY_DAM_ORDER if pairs.get(d)]
    if not dam_options:
        st.title("Year-over-year change")
        st.info("No dam has two consecutive years loaded yet, so there is "
                "nothing to compare. Run `etl/backfill.py` for a second year.")
        return

    default_dam = "BON" if "BON" in dam_options else dam_options[0]
    dam = st.sidebar.selectbox("Dam", options=dam_options, format_func=label,
                               index=dam_options.index(default_dam),
                               help="Only dams with two consecutive years "
                                    "loaded can be compared.")

    year_options = pairs[dam]
    if st.session_state.get(YEAR_KEY) not in year_options:
        # Default to the most recent COMPLETE year. The current calendar year
        # is only a few months long, which makes a poor first impression of a
        # year-over-year chart.
        complete = [y for y in year_options if y < date.today().year]
        st.session_state[YEAR_KEY] = (complete or year_options)[-1]

    st.sidebar.selectbox("Target year", options=year_options, key=YEAR_KEY,
                         help="Compared against the year before it.")
    at_first = st.session_state[YEAR_KEY] == year_options[0]
    at_last = st.session_state[YEAR_KEY] == year_options[-1]
    steps = st.sidebar.container(horizontal=True)
    steps.button("Previous", icon=":material/chevron_left:", width="stretch",
                 disabled=at_first, on_click=_step_year,
                 args=(year_options, -1))
    steps.button("Next", icon=":material/chevron_right:", width="stretch",
                 disabled=at_last, on_click=_step_year, args=(year_options, 1))

    year = st.session_state[YEAR_KEY]
    prev_year = year - 1

    all_species = load_species()
    species_colors = species_palette(mode, all_species)
    st.sidebar.markdown("**Species**")
    chosen_species = [sp for sp in all_species
                      if st.sidebar.checkbox(sp, value=True,
                                             key=f"delta_species_{sp}")]
    # No log toggle here: a change can be negative, and a log axis has no way
    # to show that.

    gen_color = GENERATION_COLOR["dark" if mode == "dark" else "light"]
    flow_ramp = FLOW_RAMP["dark" if mode == "dark" else "light"]
    temp_color = TEMPERATURE_COLOR["dark" if mode == "dark" else "light"]

    st.title("Year-over-year change")
    st.caption("Each bar is this year minus the year before, for the same "
               "month. Increases stack up from the zero line, decreases stack "
               "down from it.")

    # --- load both years and difference them ----------------------------
    p_cur, p_prev = (load_monthly_passage(dam, year),
                     load_monthly_passage(dam, prev_year))
    g_cur, g_prev = (load_monthly_generation(dam, year),
                     load_monthly_generation(dam, prev_year))
    f_cur, f_prev = (load_monthly_flow(dam, year),
                     load_monthly_flow(dam, prev_year))
    t_cur, t_prev = (load_monthly_temperature(dam, year),
                     load_monthly_temperature(dam, prev_year))

    gen_months = _usable_months(g_cur) & _usable_months(g_prev)
    flow_months = _usable_months(f_cur) & _usable_months(f_prev)
    fish_months = ((set(p_cur["month"]) if not p_cur.empty else set())
                   & (set(p_prev["month"]) if not p_prev.empty else set()))

    gen_d = diff_by_month(g_cur, g_prev, ["mwh"], gen_months)
    flow_cols = (["gen_af", "spill_af", "other_af"]
                 if (not f_cur.empty and f_cur.get("has_split") is not None
                     and bool(f_cur["has_split"].any()))
                 else ["total_af"])
    flow_d = diff_by_month(f_cur, f_prev, flow_cols, flow_months)
    fish_d = diff_passage(p_cur, p_prev, fish_months, chosen_species)

    species_order = [sp for sp in all_species
                     if not fish_d.empty and sp in set(fish_d["species"])]

    fig = delta_chart(fish_d, gen_d, flow_d, t_cur, t_prev,
                      species_order=species_order, species_colors=species_colors,
                      gen_color=gen_color, flow_ramp=flow_ramp,
                      temp_color=temp_color, mode=mode,
                      title=f"{label(dam)} — {year} compared with {prev_year}",
                      year=year, prev_year=prev_year,
                      temp_bounds=load_global_temp_range())
    if fig is None:
        st.warning(f"No month has usable data in both {year} and {prev_year} "
                   f"for {label(dam)}.")
        return

    st.plotly_chart(fig, width="stretch", key=f"delta-{dam}-{year}")

    st.mermaid_chart(river_schematic(dam, available=dam_options, mode=mode),
                     width="stretch")
    st.caption(f"{label(dam)} highlighted. Dashed dams have no pair of "
               "consecutive years loaded.")

    st.caption("Temperature line colour marks thermal stress for migrating "
               "salmon: teal below 68 °F, orange from 68 to 72 °F, red at "
               "72 °F and above.")
    st.caption("The three bars use three different y-axes, so their heights "
               "are not comparable to each other — but all three share the same "
               "zero line, so whether a bar rises or falls is directly "
               "comparable. Temperature is shown as levels rather than a "
               f"difference: {year} solid, {prev_year} dotted.")

    covered = sorted(gen_months | flow_months | fish_months)
    missing = [m for m in range(1, 13) if m not in covered]
    if missing:
        st.info(f"No bar for {', '.join(MONTHS[m - 1] for m in missing)}: those "
                f"months lack usable data in {year}, {prev_year} or both. A "
                "missing month is left blank rather than counted as no change.")

    with st.expander(f"Change detail — {label(dam)} {year} vs {prev_year}"):
        table = pd.DataFrame({"Month": MONTHS, "month": range(1, 13)})
        if not fish_d.empty:
            wide = fish_d.pivot_table(index="month", columns="species",
                                      values="delta", aggfunc="sum")
            table = table.merge(wide.reset_index(), on="month", how="left")
            table["Fish total Δ"] = table[[c for c in wide.columns]].sum(axis=1)
        if not gen_d.empty:
            table = table.merge(gen_d.rename(columns={"mwh": "Δ MWh"}),
                                on="month", how="left")
        if not flow_d.empty:
            names_map = {"total_af": "Δ flow total (acre-ft)",
                         "gen_af": "Δ through turbines", "spill_af": "Δ spilled",
                         "other_af": "Δ other"}
            table = table.merge(flow_d.rename(columns=names_map), on="month",
                                how="left")
        for frame, col in ((t_cur, f"{year} max °F"), (t_prev, f"{prev_year} max °F")):
            if frame is not None and not frame.empty:
                table = table.merge(
                    frame[["month", "max_f"]].rename(columns={"max_f": col}),
                    on="month", how="left")
        st.dataframe(table.drop(columns=["month"]), width="stretch",
                     hide_index=True)
