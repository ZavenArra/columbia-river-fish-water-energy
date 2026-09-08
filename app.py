"""
Columbia/Snake River Dam Dashboard.

Reads data/dams.db and nothing else -- every write happens in etl/. The app
opens SQLite read-only (mode=ro) so a bug here can never corrupt the store; the
one exception is the "Refresh recent data" button, which shells out to
etl/refresh.py rather than writing anything itself.

Run with:  streamlit run app.py
"""

import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

REPO = Path(__file__).resolve().parent
DB_PATH = REPO / "data" / "dams.db"
REFRESH_SCRIPT = REPO / "etl" / "refresh.py"
sys.path.insert(0, str(REPO / "config"))

CACHE_TTL = 300  # seconds

# --- Palette ---------------------------------------------------------------
# Categorical slots in fixed order, assigned to YEARS. Validated with the
# dataviz validator in both modes on the adjacent pairlist (line charts):
# light  worst adjacent CVD dE 9.1, normal-vision 22.9
# dark   worst adjacent CVD dE 8.4, normal-vision 19.8
# Light mode WARNs on contrast for aqua/yellow, which obliges relief -- hence
# the "Weekly data" table under every dam.
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


# ---------------------------------------------------------------------------
# Data access -- all cached, all read-only
# ---------------------------------------------------------------------------

def _connect():
    """Read-only connection: the app must never be able to write."""
    import sqlite3
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _iso_frame(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    """
    Add ISO year/week.

    Grouping uses the ISO year, not the calendar year: 31 December can belong to
    week 1 of the following ISO year, and plotting that point at week 1 under the
    old year's colour would wrap a line back across the chart.
    """
    if df.empty:
        return df.assign(iso_year=pd.Series(dtype="int"),
                         iso_week=pd.Series(dtype="int"))
    stamp = pd.to_datetime(df[date_col], errors="coerce")
    iso = stamp.dt.isocalendar()
    return df.assign(iso_year=iso.year.astype("int64"),
                     iso_week=iso.week.astype("int64")).dropna(subset=[date_col])


@st.cache_data(ttl=CACHE_TTL)
def load_dam_names() -> dict:
    """Code -> display name, from config/dams.py."""
    try:
        from dams import DAM_INFO
        return {k: (v.get("name") or k) for k, v in DAM_INFO.items()}
    except Exception:  # noqa: BLE001
        return {}


@st.cache_data(ttl=CACHE_TTL)
def load_availability() -> pd.DataFrame:
    """Which data types each dam actually has, straight from the tables."""
    with _connect() as conn:
        return pd.read_sql_query("""
            SELECT d.dam_code,
                   MAX(d.has_flow_gen)   AS has_flow_gen,
                   MAX(d.has_passage)    AS has_passage,
                   MAX(d.has_temp)       AS has_temp
            FROM (
                SELECT dam_code, 1 AS has_flow_gen, 0 AS has_passage, 0 AS has_temp
                  FROM flow_generation WHERE gen_mw IS NOT NULL
                UNION ALL
                SELECT dam_code, 0, 1, 0 FROM fish_passage
                UNION ALL
                SELECT dam_code, 0, 0, 1 FROM temperature WHERE value_f IS NOT NULL
            ) d
            GROUP BY d.dam_code ORDER BY d.dam_code
        """, conn)


@st.cache_data(ttl=CACHE_TTL)
def load_flow_dams() -> list:
    """Dams with any flow_generation rows -- the population for the selector."""
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT DISTINCT dam_code FROM flow_generation ORDER BY dam_code",
            conn)["dam_code"].tolist()


@st.cache_data(ttl=CACHE_TTL)
def load_years() -> list:
    with _connect() as conn:
        rows = pd.read_sql_query("""
            SELECT substr(date_hour,1,4) AS y FROM flow_generation
            UNION SELECT substr(date,1,4) FROM fish_passage
            UNION SELECT substr(date,1,4) FROM temperature
        """, conn)["y"].dropna().astype(int)
    return sorted(rows.unique().tolist())


@st.cache_data(ttl=CACHE_TTL)
def load_species() -> list:
    with _connect() as conn:
        return pd.read_sql_query(
            "SELECT DISTINCT species FROM fish_passage ORDER BY species",
            conn)["species"].tolist()


@st.cache_data(ttl=CACHE_TTL)
def load_generation(dams: tuple) -> pd.DataFrame:
    """Daily mean MW per dam. Weekly rollup happens after ISO weeks are known."""
    if not dams:
        return pd.DataFrame(columns=["dam_code", "date", "gen_mw"])
    marks = ",".join("?" * len(dams))
    with _connect() as conn:
        df = pd.read_sql_query(f"""
            SELECT dam_code, substr(date_hour,1,10) AS date, AVG(gen_mw) AS gen_mw
            FROM flow_generation
            WHERE dam_code IN ({marks}) AND gen_mw IS NOT NULL
            GROUP BY dam_code, date
        """, conn, params=list(dams))
    return _iso_frame(df, "date")


@st.cache_data(ttl=CACHE_TTL)
def load_passage(dams: tuple, species: tuple) -> pd.DataFrame:
    if not dams or not species:
        return pd.DataFrame(columns=["dam_code", "date", "count"])
    marks, smarks = ",".join("?" * len(dams)), ",".join("?" * len(species))
    with _connect() as conn:
        df = pd.read_sql_query(f"""
            SELECT dam_code, date, SUM(count) AS count
            FROM fish_passage
            WHERE dam_code IN ({marks}) AND species IN ({smarks})
              AND count IS NOT NULL
            GROUP BY dam_code, date
        """, conn, params=list(dams) + list(species))
    return _iso_frame(df, "date")


@st.cache_data(ttl=CACHE_TTL)
def load_temperature(dams: tuple) -> pd.DataFrame:
    if not dams:
        return pd.DataFrame(columns=["dam_code", "date", "value_f"])
    marks = ",".join("?" * len(dams))
    with _connect() as conn:
        df = pd.read_sql_query(f"""
            SELECT dam_code, date, AVG(value_f) AS value_f
            FROM temperature
            WHERE dam_code IN ({marks}) AND value_f IS NOT NULL
            GROUP BY dam_code, date
        """, conn, params=list(dams))
    return _iso_frame(df, "date")


def weekly(df: pd.DataFrame, value_col: str, how: str) -> pd.DataFrame:
    """Roll daily rows up to one point per (dam, ISO year, ISO week)."""
    if df.empty:
        return pd.DataFrame(columns=["dam_code", "iso_year", "iso_week", value_col])
    agg = df.groupby(["dam_code", "iso_year", "iso_week"], as_index=False)[value_col]
    out = agg.mean() if how == "mean" else agg.sum()
    return out.sort_values(["dam_code", "iso_year", "iso_week"])


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _nice_dtick(lo, hi, target=5):
    """
    A round tick interval covering [lo, hi].

    Plotly matches tick COUNT across a secondary axis so the gridlines line up,
    which leaves the second axis on arbitrary values like 990.209k. Picking a
    1/2/5 x 10^n interval puts it back on round numbers.
    """
    import math
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
    import math
    return math.floor(lo / dtick) * dtick, dtick


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


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Columbia/Snake Dam Dashboard", layout="wide")
    mode = theme_mode()

    if not DB_PATH.exists():
        st.title("Columbia/Snake River Dam Dashboard")
        st.error(f"No database at `{DB_PATH}`.")
        st.markdown("Run the backfill first:\n\n"
                    "```bash\npython etl/backfill.py --start-year 2023 "
                    "--end-year 2024\n```")
        return

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
    palette = SERIES_DARK if mode == "dark" else SERIES_LIGHT
    year_colors = {y: palette[i] for i, y in enumerate(all_years[:MAX_YEARS])}

    # --- sidebar ---------------------------------------------------------
    st.sidebar.title("Filters")

    complete = [d for d in flow_dams
                if has(d, "flow_gen") and has(d, "passage") and has(d, "temp")]
    default_dams = (complete or flow_dams)[:4]

    dams = st.sidebar.multiselect(
        "Dams", options=flow_dams, default=default_dams, format_func=label,
        help="Dams with flow/generation data. Defaults to the first four that "
             "have generation, passage and temperature.",
    )

    years = st.sidebar.multiselect("Years", options=all_years, default=all_years)
    if len(years) > MAX_YEARS:
        st.sidebar.warning(
            f"Showing the first {MAX_YEARS} years selected. Past {MAX_YEARS} "
            "series the categorical palette would have to reuse a hue, which is "
            "indistinguishable under colour-vision deficiency."
        )
        years = years[:MAX_YEARS]

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

    st.sidebar.divider()

    # st.rerun() wipes anything written before it, so a success message shown
    # inline would flash and vanish after a 75-second wait. Hand it to the next
    # run through session state instead.
    result = st.session_state.pop("_refresh_result", None)
    if result:
        (st.sidebar.success if result[0] == "ok" else st.sidebar.error)(result[1])

    if st.sidebar.button("Refresh recent data", width='stretch'):
        with st.spinner("Fetching the last 60 days from DART, USACE and CWMS…"):
            proc = subprocess.run(
                [sys.executable, str(REFRESH_SCRIPT), "--days", "60"],
                capture_output=True, text=True, cwd=str(REPO), timeout=900,
            )
        # Clear every cached reader so the new rows are visible immediately.
        st.cache_data.clear()
        if proc.returncode == 0:
            st.session_state["_refresh_result"] = (
                "ok", "Refreshed — the last 60 days are up to date.")
            st.rerun()
        else:
            st.sidebar.error("Refresh failed — the database was not changed.")
            with st.sidebar.expander("Error detail"):
                st.code((proc.stderr or proc.stdout or "")[-2000:])
    st.sidebar.caption("Runs `etl/refresh.py --days 60`, then clears the caches. "
                       "Takes roughly 75 seconds.")

    # --- main ------------------------------------------------------------
    st.title("Columbia/Snake River Dam Dashboard")
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


if __name__ == "__main__":
    main()
