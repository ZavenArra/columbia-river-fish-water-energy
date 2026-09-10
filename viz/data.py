"""
Read-only data access for the dashboard. Every reader is cached (ttl=300).

The app never writes: connections are opened with mode=ro so a bug in the UI
cannot corrupt the store. Writes happen only in etl/.
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / "data" / "dams.db"
sys.path.insert(0, str(REPO / "config"))

CACHE_TTL = 300  # seconds

# One hour of flow at 1 kcfs, expressed in acre-feet:
#   1000 cfs * 3600 s / 43,560 cu ft per acre-foot
ACRE_FT_PER_KCFS_HOUR = 1000 * 3600 / 43560


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
# Monthly aggregates (the monthly-profile page)
# ---------------------------------------------------------------------------
# All three measures are genuine monthly TOTALS, so a bar height means "how much
# in this month": fish are a count, generation is energy (MWh), flow is volume
# (acre-feet). MW and kcfs are rates and cannot be summed into a total as-is --
# summing hourly MW yields MWh directly, and hourly kcfs scales to acre-feet.

import calendar  # noqa: E402


def _hours_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1] * 24


@st.cache_data(ttl=CACHE_TTL)
def load_monthly_passage(dam: str, year: int) -> pd.DataFrame:
    """-> month, species, fish (total count)."""
    with _connect() as conn:
        return pd.read_sql_query("""
            SELECT CAST(substr(date, 6, 2) AS INTEGER) AS month,
                   species, SUM(count) AS fish
            FROM fish_passage
            WHERE dam_code = ? AND substr(date, 1, 4) = ? AND count IS NOT NULL
            GROUP BY month, species ORDER BY month, species
        """, conn, params=(dam, str(year)))


@st.cache_data(ttl=CACHE_TTL)
def load_monthly_generation(dam: str, year: int) -> pd.DataFrame:
    """
    -> month, mwh, hours, coverage

    Each row in flow_generation is one hour, so summing gen_mw over a month is
    already megawatt-hours. `coverage` is the fraction of the month's hours that
    actually reported, so a month with a handful of hours can be suppressed
    rather than drawn as a near-zero bar that reads as "barely generated".
    """
    with _connect() as conn:
        df = pd.read_sql_query("""
            SELECT CAST(substr(date_hour, 6, 2) AS INTEGER) AS month,
                   SUM(gen_mw) AS mwh, COUNT(gen_mw) AS hours
            FROM flow_generation
            WHERE dam_code = ? AND substr(date_hour, 1, 4) = ?
              AND gen_mw IS NOT NULL
            GROUP BY month ORDER BY month
        """, conn, params=(dam, str(year)))
    if df.empty:
        return df.assign(coverage=pd.Series(dtype="float"))
    df["coverage"] = df.apply(
        lambda r: r["hours"] / _hours_in_month(year, int(r["month"])), axis=1)
    return df


@st.cache_data(ttl=CACHE_TTL)
def load_monthly_flow(dam: str, year: int) -> pd.DataFrame:
    """
    -> month, total_af, gen_af, spill_af, other_af, hours, coverage, has_split

    Volume in acre-feet. When a dam reports the split, the bar is broken into
    turbine flow, spill, and "other" -- the remainder of the total, which is
    USACE's Misc Flow (fish ladders, navigation lock and so on). Including that
    remainder is what makes the stack add up to the true total rather than
    quietly falling a few percent short.

    The CWMS-sourced dams publish only total outflow, so has_split is False and
    the bar is drawn whole.
    """
    K = ACRE_FT_PER_KCFS_HOUR
    with _connect() as conn:
        df = pd.read_sql_query("""
            SELECT CAST(substr(date_hour, 6, 2) AS INTEGER) AS month,
                   SUM(total_flow_kcfs) AS total_k,
                   SUM(gen_flow_kcfs)   AS gen_k,
                   SUM(spill_flow_kcfs) AS spill_k,
                   COUNT(total_flow_kcfs) AS hours,
                   COUNT(gen_flow_kcfs)   AS gen_hours
            FROM flow_generation
            WHERE dam_code = ? AND substr(date_hour, 1, 4) = ?
              AND total_flow_kcfs IS NOT NULL
            GROUP BY month ORDER BY month
        """, conn, params=(dam, str(year)))
    if df.empty:
        return df.assign(total_af=pd.Series(dtype="float"), has_split=False)

    df["coverage"] = df.apply(
        lambda r: r["hours"] / _hours_in_month(year, int(r["month"])), axis=1)
    df["total_af"] = df["total_k"] * K
    df["gen_af"] = df["gen_k"] * K
    df["spill_af"] = df["spill_k"] * K
    # Only split a month where the components were actually reported alongside
    # the total; otherwise the remainder would absorb the whole bar.
    split = (df["gen_hours"] > 0) & df["gen_af"].notna() & df["spill_af"].notna()
    df["other_af"] = (df["total_af"] - df["gen_af"].fillna(0)
                      - df["spill_af"].fillna(0)).where(split)
    df.loc[~split, ["gen_af", "spill_af"]] = None
    # Negative remainder means the source's own components exceed its total
    # (USACE publishes a few negative Misc Flow hours); clamp so the stack
    # cannot render below the axis.
    df["other_af"] = df["other_af"].clip(lower=0)
    df.attrs["has_split"] = bool(split.any())
    df["has_split"] = split
    return df


@st.cache_data(ttl=CACHE_TTL)
def load_dams_with_flow_and_year(year: int) -> list:
    """Dams having any flow_generation row in a given year."""
    with _connect() as conn:
        return pd.read_sql_query("""
            SELECT DISTINCT dam_code FROM flow_generation
            WHERE substr(date_hour, 1, 4) = ? ORDER BY dam_code
        """, conn, params=(str(year),))["dam_code"].tolist()
