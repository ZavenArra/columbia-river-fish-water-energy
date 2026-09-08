#!/usr/bin/env python3
"""
Shared schema, CSV parsing and upsert logic for the dam dashboard ETL.

Both etl/backfill.py and etl/refresh.py import from here; neither reimplements
any of it. Fetching, caching and rate limiting live in etl/fetchers.py.

IDEMPOTENCY
-----------
Every table has a composite primary key, and every write goes through
`INSERT ... ON CONFLICT(pk) DO UPDATE SET col = COALESCE(excluded.col, col)`.

COALESCE rather than INSERT OR REPLACE is deliberate. REPLACE deletes the
conflicting row and inserts a new one, so a source that only knows some of the
columns would blank out everything another source had already written to that
row. COALESCE merges: a NULL coming in never overwrites a value already there.

TIME BASE
---------
USACE publishes in local (Pacific) dam time and CWMS returns UTC. Everything
here is normalised to **local Pacific time** so the mid-Columbia dams line up
with the federal ones instead of sitting 7-8 hours off in the same table.

UNITS
-----
`temperature.value_f` is Fahrenheit throughout. DART reports Celsius and is
converted on the way in; USACE already reports Fahrenheit. Flow is kcfs
throughout; CWMS reports cfs and is divided by 1000.
"""

import io
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "data" / "dams.db"
PACIFIC = ZoneInfo("America/Los_Angeles")

# USACE writes "M" (and a few relatives) where a reading is missing.
MISSING_TOKENS = {"m", "", "-", "na", "n/a", "nan", "null", "---"}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS fish_passage (
    date        TEXT    NOT NULL,
    dam_code    TEXT    NOT NULL,
    species     TEXT    NOT NULL,
    count       INTEGER,
    PRIMARY KEY (date, dam_code, species)
);
CREATE INDEX IF NOT EXISTS ix_fish_passage_dam_date
    ON fish_passage (dam_code, date);

CREATE TABLE IF NOT EXISTS temperature (
    date             TEXT    NOT NULL,
    dam_code         TEXT    NOT NULL,
    parameter        TEXT    NOT NULL,
    value_f          REAL,
    -- Which DART site the reading actually came from. Differs from dam_code
    -- when the dam's own site has no sensor (Dworshak -> DWQI tailwater).
    source_site      TEXT,
    from_scroll_case INTEGER DEFAULT 0,
    from_tailwater   INTEGER DEFAULT 0,
    PRIMARY KEY (date, dam_code, parameter)
);
CREATE INDEX IF NOT EXISTS ix_temperature_dam_date
    ON temperature (dam_code, date);

CREATE TABLE IF NOT EXISTS flow_generation (
    date_hour          TEXT NOT NULL,   -- 'YYYY-MM-DD HH:00', local Pacific
    dam_code           TEXT NOT NULL,
    gen_mw             REAL,            -- total plant output
    gen_mw_ph1         REAL,            -- Bonneville reports two powerhouses
    gen_mw_ph2         REAL,
    gen_flow_kcfs      REAL,
    gen_flow_kcfs_ph1  REAL,
    gen_flow_kcfs_ph2  REAL,
    spill_flow_kcfs    REAL,
    total_flow_kcfs    REAL,
    fb_elev            REAL,
    tw_elev            REAL,
    source             TEXT,            -- 'usace_hist_csv' | 'cwms_api'
    PRIMARY KEY (date_hour, dam_code)
);
CREATE INDEX IF NOT EXISTS ix_flow_generation_dam_hour
    ON flow_generation (dam_code, date_hour);

-- Pool elevation is DAILY and lives on its own, not in flow_generation, so it
-- can be joined against whatever period a query aggregates to. DART is the only
-- source covering all 19 dams: CWMS catalogues Elev-Forebay/Elev-Tailwater but
-- serves them empty, and the USACE hourly files only cover federal projects
-- (where fb_elev/tw_elev in flow_generation remain the higher-resolution copy).
-- Readings are in feet and self-consistent down the cascade -- each dam's
-- tailwater elevation matches the next dam downstream's forebay.
CREATE TABLE IF NOT EXISTS elevation_daily (
    date        TEXT NOT NULL,
    dam_code    TEXT NOT NULL,
    location    TEXT NOT NULL,   -- 'forebay' | 'tailwater'
    value_ft    REAL,
    source_site TEXT,            -- DART site the reading came from
    source      TEXT,
    PRIMARY KEY (date, dam_code, location)
);
CREATE INDEX IF NOT EXISTS ix_elevation_daily_dam_date
    ON elevation_daily (dam_code, date);

-- The mid-Columbia PUD dams have no hourly generation published anywhere:
-- USACE serves no file for them and CWMS returns its Power.Total series empty.
-- EIA-923 has them, but only monthly, which cannot honestly be forced into the
-- hourly grain above -- hence a separate table rather than an invented split.
CREATE TABLE IF NOT EXISTS generation_monthly (
    year               INTEGER NOT NULL,
    month              INTEGER NOT NULL,
    dam_code           TEXT    NOT NULL,
    net_generation_mwh REAL,
    plant_id           INTEGER,
    source             TEXT,
    PRIMARY KEY (year, month, dam_code)
);
CREATE INDEX IF NOT EXISTS ix_generation_monthly_dam
    ON generation_monthly (dam_code, year, month);
"""


def connect(db_path=None) -> sqlite3.Connection:
    """Open the database, creating the parent directory if needed."""
    path = Path(db_path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _is_path(source) -> bool:
    """
    Is this a filesystem path, or is it CSV content?

    A bare `Path(str(source)).exists()` raises ENAMETOOLONG once `source` is a
    whole CSV file's text, so rule out anything multi-line or longer than a
    filename before touching the filesystem.
    """
    if isinstance(source, Path):
        return True
    if not isinstance(source, str):
        return False
    if "\n" in source or len(source) > 1024:
        return False
    try:
        return Path(source).exists()
    except OSError:
        return False


def _num(value):
    """Parse a cell to float, treating USACE's missing-data tokens as None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    text = str(value).strip()
    if text.lower() in MISSING_TOKENS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _c_to_f(celsius):
    return None if celsius is None else celsius * 9.0 / 5.0 + 32.0


def _sum_present(*values):
    """Sum the non-None values; None if every one of them is missing."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def read_csv_text(text: str) -> pd.DataFrame:
    """DART's CSV responses start with a blank line; pandas handles that."""
    return pd.read_csv(io.StringIO(text))


# ---------------------------------------------------------------------------
# DART parsers
# ---------------------------------------------------------------------------

def _dart_frame(source) -> pd.DataFrame:
    """
    Normalise a DART csvSingle payload: text, path or DataFrame in, tidy out.

    DART appends a trailing all-empty row, which becomes a NaN row that would
    otherwise turn into a junk record.
    """
    if isinstance(source, pd.DataFrame):
        df = source.copy()
    elif _is_path(source):
        df = pd.read_csv(source)
    else:
        df = read_csv_text(str(source))
    df.columns = [str(c).strip() for c in df.columns]
    # 'mm-dd' is not a valid Python identifier, so itertuples would expose it
    # positionally; rename it up front rather than depending on column order.
    df = df.rename(columns={"mm-dd": "mm_dd"})
    return df.dropna(how="all").dropna(subset=["year", "mm_dd"])


def _dart_date(year, mm_dd) -> str:
    """DART writes '6-1' rather than '06-01'."""
    month, day = str(mm_dd).split("-")[:2]
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def parse_dart_passage(source, dam_code: str, label_map=None) -> list:
    """
    -> [(date, dam_code, species, count)]

    `parameter` holds DART's species abbreviation ('Chin', 'WStlhd', ...).
    Anything not in the map is kept under its raw label rather than dropped,
    so an unrecognised species is visible in the data instead of silently lost.
    """
    label_map = label_map or {}
    df = _dart_frame(source)
    rows = []
    for rec in df.itertuples(index=False):
        raw = str(getattr(rec, "parameter", "") or "").strip()
        if not raw:
            continue
        count = _num(getattr(rec, "value", None))
        rows.append((
            _dart_date(rec.year, rec.mm_dd),
            dam_code.upper(),
            label_map.get(raw, raw),
            None if count is None else int(round(count)),
        ))
    return rows


def parse_dart_temperature(source, dam_code: str, parameter: str,
                           source_site=None, from_scroll_case=False,
                           from_tailwater=False) -> list:
    """
    -> [(date, dam_code, parameter, value_f, source_site, scroll, tailwater)]

    DART reports Celsius; stored as Fahrenheit. The provenance flags come from
    config/dam_availability.json and travel with every row, so a substituted
    reading is never silently mixed with a forebay one.
    """
    df = _dart_frame(source)
    rows = []
    for rec in df.itertuples(index=False):
        rows.append((
            _dart_date(rec.year, rec.mm_dd),
            dam_code.upper(),
            parameter,
            _c_to_f(_num(getattr(rec, "value", None))),
            (source_site or dam_code).upper(),
            int(bool(from_scroll_case)),
            int(bool(from_tailwater)),
        ))
    return rows


def parse_dart_elevation(source, dam_code: str, location: str,
                         source_site=None) -> list:
    """
    -> [(date, dam_code, location, value_ft, source_site, source)]

    DART reports elevation in feet, so no conversion. `location` is 'forebay'
    or 'tailwater'; for tailwater the reading comes from a different DART site
    than the dam itself, which is why source_site travels with the row.
    """
    df = _dart_frame(source)
    rows = []
    for rec in df.itertuples(index=False):
        rows.append((
            _dart_date(rec.year, rec.mm_dd),
            dam_code.upper(),
            location,
            _num(getattr(rec, "value", None)),
            (source_site or dam_code).upper(),
            "dart",
        ))
    return rows


# ---------------------------------------------------------------------------
# USACE monthly CSV parser
# ---------------------------------------------------------------------------

def _usace_lines(source) -> list:
    if _is_path(source):
        text = Path(source).read_text(errors="replace")
    else:
        text = str(source)
    return [l for l in text.splitlines() if l.strip()]


def _pick(columns, *candidates):
    """First column matching a candidate name, case/space-insensitively."""
    norm = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        hit = norm.get(cand.strip().lower())
        if hit is not None:
            return hit
    return None


# Unit strings whose values are opaque integer codes, not measurements. These
# are the strongest alignment anchor a USACE file gives us: a coded column holds
# something like 10214, never 8.10.
_CODED_UNITS = {"coded"}


def _looks_coded(value: str) -> bool:
    text = str(value).strip()
    return text.isdigit() and len(text) >= 4


def _alignment_gap(header, units, data_rows):
    """
    Find where a USACE data row carries a column its header never names.

    Some months ship more data fields than header fields -- Bonneville's 2026
    files have 26 data columns against a 25-column header. The extra column is
    NOT at the end: it sits mid-row, so everything after it shifts by one.
    Padding the header on the right (the obvious fix) silently reads `Gen PH2`
    out of the neighbouring `Units` column, turning 145 MW into the code 10214
    and inflating Bonneville's generation roughly twentyfold.

    Generic type-checking cannot settle this on its own -- nearly every column
    is numeric, so most candidate positions score alike. Two anchors do settle
    it: `coded` columns hold opaque integers that never look like measurements,
    and the file's own arithmetic, Head == FB Elev - TW Elev, holds only at the
    true alignment. Returns the data index the unnamed column sits at, or None
    to fall back to padding on the right.
    """
    widest = max((len(r) for r in data_rows), default=len(header))
    if widest - len(header) != 1:
        return None  # only the single-gap case is inferable

    i_fb = next((j for j, n in enumerate(header)
                 if n.strip().lower() == "fb elev"), None)
    i_tw = next((j for j, n in enumerate(header)
                 if n.strip().lower() in ("tw elev", "tw elev ph1")), None)
    i_head = next((j for j, n in enumerate(header)
                   if n.strip().lower() == "head"), None)

    def score(gap):
        # header[j] reads data[j] before the gap, data[j+1] after it
        src = [j if j < gap else j + 1 for j in range(len(header))]
        total = 0
        for row in data_rows:
            for j, unit in enumerate(units):
                k = src[j]
                if k >= len(row):
                    continue
                value = str(row[k]).strip()
                coded = _looks_coded(value)
                if unit.strip().lower() in _CODED_UNITS:
                    total += 3 if coded else -4
                elif value.lower() in MISSING_TOKENS:
                    continue
                else:
                    try:
                        float(value)
                        total += 1
                    except ValueError:
                        total -= 3
            # The decisive anchor: the file's own head arithmetic.
            if None not in (i_fb, i_tw, i_head):
                try:
                    fb = float(row[src[i_fb]])
                    tw = float(row[src[i_tw]])
                    head = float(row[src[i_head]])
                    total += 8 if abs(head - (fb - tw)) <= 0.5 else -8
                except (ValueError, IndexError):
                    pass
        return total

    # gap 0 is impossible: the Date cell always occupies data[0].
    return max(range(1, len(header) + 1), key=lambda g: (score(g), -g))


def parse_usace_month(source, dam_code: str) -> list:
    """
    Parse one monthly USACE water-control CSV.

    Quirks in these files, all confirmed against live downloads:
      * row 0 is column names, row 1 is a UNITS row, data starts at row 2
      * some months carry MORE data fields than header fields, with the unnamed
        column in the MIDDLE of the row -- see _alignment_gap
      * the year lives in the units row's first cell ("LWG 2025"), not in the
        Date column, which only carries "MM/DD HH"
      * the hour runs 1..24, where 24 means midnight ending the day -- i.e.
        00:00 of the following day
      * "M" means missing
    """
    lines = _usace_lines(source)
    if len(lines) < 3:
        return []

    header = [c.strip() for c in lines[0].split(",")]
    units_row = [c.strip() for c in lines[1].split(",")]
    data_rows = [[c.strip() for c in l.split(",")] for l in lines[2:]]

    # "LWG 2025" -> 2025
    year = None
    for token in units_row[0].split():
        if token.isdigit() and len(token) == 4:
            year = int(token)
    if year is None:
        raise ValueError(f"No year in USACE units row: {units_row[0]!r}")

    widest = max((len(r) for r in data_rows), default=len(header))
    if widest > len(header):
        # Sample rows rather than all ~740: the layout is fixed for the month.
        gap = _alignment_gap(header, units_row, data_rows[:24])
        if gap is None:
            padded = header + [f"unnamed_{i}"
                               for i in range(len(header), widest)]
        else:
            padded = header[:gap] + ["unnamed_gap"] + header[gap:]
            padded += [f"unnamed_{i}" for i in range(len(padded), widest)]
    else:
        padded = list(header)

    index_of = {name: i for i, name in enumerate(padded)}

    def pick(*candidates):
        norm = {n.strip().lower(): i for n, i in index_of.items()}
        for cand in candidates:
            hit = norm.get(cand.strip().lower())
            if hit is not None:
                return hit
        return None

    i_gen1 = pick("Gen", "Gen PH1")
    i_gen2 = pick("Gen PH2")
    i_gflow1 = pick("Gen Flow", "Gen Flow PH1")
    i_gflow2 = pick("Gen Flow PH2")
    i_spill = pick("Spill Flow")
    i_total = pick("Total Flow")
    i_fb = pick("FB Elev")
    i_tw = pick("TW Elev", "TW Elev PH1")  # BON names it PH1

    def cell(row, idx):
        return _num(row[idx]) if idx is not None and idx < len(row) else None

    rows = []
    for row in data_rows:
        stamp = row[0] if row else ""
        if not stamp or "/" not in stamp:
            continue
        try:
            date_part, hour_part = stamp.split()
            month, day = (int(x) for x in date_part.split("/"))
            hour = int(hour_part)
        except (ValueError, IndexError):
            continue

        # Hour 24 is midnight closing the day -> 00:00 of the next day.
        moment = datetime(year, month, day)
        if hour == 24:
            moment += timedelta(days=1)
        else:
            moment += timedelta(hours=hour)

        gen1, gen2 = cell(row, i_gen1), cell(row, i_gen2)
        gf1, gf2 = cell(row, i_gflow1), cell(row, i_gflow2)

        rows.append((
            moment.strftime("%Y-%m-%d %H:00"),
            dam_code.upper(),
            _sum_present(gen1, gen2),      # gen_mw: both powerhouses
            gen1, gen2,
            _sum_present(gf1, gf2),        # gen_flow_kcfs
            gf1, gf2,
            cell(row, i_spill),
            cell(row, i_total),
            cell(row, i_fb),
            cell(row, i_tw),
            "usace_hist_csv",
        ))
    return rows


# ---------------------------------------------------------------------------
# CWMS parser
# ---------------------------------------------------------------------------

def parse_cwms_flow(frames_by_series: dict, dam_code: str) -> list:
    """
    Merge the CWMS series for one dam into flow_generation rows.

    frames_by_series maps 'outflow' / 'gen_flow' / 'spill' / 'elevation' to the
    DataFrames cwms_scraper returns. CWMS gives cfs and UTC; rows come out in
    kcfs on local Pacific time, matching the USACE files.

    gen_mw is left NULL: CWMS lists a Power.Total series for these projects but
    serves it empty, so there is no hourly generation to record.
    """
    field_for = {"outflow": "total_flow_kcfs", "gen_flow": "gen_flow_kcfs",
                 "spill": "spill_flow_kcfs", "elevation": "fb_elev"}
    merged = {}
    for series, df in (frames_by_series or {}).items():
        field = field_for.get(series)
        if field is None or df is None or len(df) == 0:
            continue
        if _is_path(df):
            df = pd.read_csv(df)
        for rec in df.to_dict("records"):
            stamp = pd.to_datetime(rec.get("timestamp"), utc=True, errors="coerce")
            value = _num(rec.get("value"))
            if pd.isna(stamp):
                continue
            key = stamp.tz_convert(PACIFIC).strftime("%Y-%m-%d %H:00")
            # Elevation is already feet; the flow series are cfs.
            merged.setdefault(key, {})[field] = (
                value if field == "fb_elev" or value is None else value / 1000.0
            )

    rows = []
    for key in sorted(merged):
        vals = merged[key]
        rows.append((
            key, dam_code.upper(),
            None, None, None,                       # gen_mw, ph1, ph2
            vals.get("gen_flow_kcfs"), None, None,
            vals.get("spill_flow_kcfs"),
            vals.get("total_flow_kcfs"),
            vals.get("fb_elev"), None,
            "cwms_api",
        ))
    return rows


def parse_eia_monthly(df, dam_code: str = None) -> list:
    """-> [(year, month, dam_code, net_generation_mwh, plant_id, source)]"""
    if _is_path(df):
        df = pd.read_csv(df)
    rows = []
    for rec in df.to_dict("records"):
        dam = rec.get("dam") or dam_code
        if not dam or (isinstance(dam, float) and pd.isna(dam)):
            continue
        rows.append((
            int(rec["year"]), int(rec["month"]), str(dam).upper(),
            _num(rec.get("net_generation_mwh")),
            int(rec["plant_id"]) if rec.get("plant_id") is not None
            and not pd.isna(rec.get("plant_id")) else None,
            rec.get("source") or "EIA-923",
        ))
    return rows


# ---------------------------------------------------------------------------
# Upserts
# ---------------------------------------------------------------------------

def _upsert(conn, table, columns, key_columns, rows) -> int:
    """
    Generic COALESCE-merge upsert. See the module docstring for why this is not
    INSERT OR REPLACE.
    """
    if not rows:
        return 0
    updatable = [c for c in columns if c not in key_columns]
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))}) "
        f"ON CONFLICT({', '.join(key_columns)}) DO UPDATE SET "
        + ", ".join(f"{c} = COALESCE(excluded.{c}, {table}.{c})" for c in updatable)
    )
    with conn:
        conn.executemany(sql, rows)
    return len(rows)


def upsert_fish_passage(conn, rows) -> int:
    return _upsert(conn, "fish_passage",
                   ["date", "dam_code", "species", "count"],
                   ["date", "dam_code", "species"], rows)


def upsert_temperature(conn, rows) -> int:
    return _upsert(conn, "temperature",
                   ["date", "dam_code", "parameter", "value_f", "source_site",
                    "from_scroll_case", "from_tailwater"],
                   ["date", "dam_code", "parameter"], rows)


def upsert_flow_generation(conn, rows) -> int:
    return _upsert(conn, "flow_generation",
                   ["date_hour", "dam_code", "gen_mw", "gen_mw_ph1", "gen_mw_ph2",
                    "gen_flow_kcfs", "gen_flow_kcfs_ph1", "gen_flow_kcfs_ph2",
                    "spill_flow_kcfs", "total_flow_kcfs", "fb_elev", "tw_elev",
                    "source"],
                   ["date_hour", "dam_code"], rows)


def upsert_elevation_daily(conn, rows) -> int:
    return _upsert(conn, "elevation_daily",
                   ["date", "dam_code", "location", "value_ft", "source_site",
                    "source"],
                   ["date", "dam_code", "location"], rows)


def upsert_generation_monthly(conn, rows) -> int:
    return _upsert(conn, "generation_monthly",
                   ["year", "month", "dam_code", "net_generation_mwh",
                    "plant_id", "source"],
                   ["year", "month", "dam_code"], rows)


def table_counts(conn) -> dict:
    out = {}
    for table in ("fish_passage", "temperature", "elevation_daily",
                  "flow_generation", "generation_monthly"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out


# ---------------------------------------------------------------------------
# Availability-driven ingest
# ---------------------------------------------------------------------------
# These wrap parse_* + upsert_* into one call each, so backfill.py and
# refresh.py only decide *what period* to fetch -- never how to parse or write.

import json  # noqa: E402  (kept next to the code that uses it)

DEFAULT_AVAILABILITY = REPO / "config" / "dam_availability.json"


def load_availability(path=None) -> dict:
    """The per-dam source map produced by etl/validate_dams.py."""
    payload = json.loads(Path(path or DEFAULT_AVAILABILITY).read_text())
    return payload.get("dams", payload)


def applicable_sources(meta: dict) -> set:
    """
    Which logical sources a dam actually has, so nothing is fetched pointlessly.

    Driven entirely by dam_availability.json: a dam with no fish ladder is never
    asked for passage, and a dam with no USACE file goes to CWMS/EIA instead.
    """
    sources = set()
    if meta.get("has_fish_passage"):
        sources.add("dart_passage")
    if meta.get("has_temperature"):
        sources.add("dart_temperature")
    if meta.get("has_elevation") or meta.get("has_tailwater_elevation"):
        sources.add("dart_elevation")
    if meta.get("flow_source") == "usace_hist_csv":
        sources.add("usace")
    elif meta.get("flow_source") == "cwms_api":
        sources.add("cwms")
    if meta.get("generation_source") == "eia923_monthly":
        sources.add("eia923")
    return sources


def ingest_dart_passage(conn, frame, dam_code, label_map=None) -> int:
    return upsert_fish_passage(
        conn, parse_dart_passage(frame, dam_code, label_map))


def ingest_dart_temperature(conn, frame, dam_code, meta) -> int:
    return upsert_temperature(conn, parse_dart_temperature(
        frame, dam_code,
        parameter=meta.get("temperature_parameter") or "Temp (WQM)",
        source_site=meta.get("temperature_site") or dam_code,
        from_scroll_case=meta.get("temperature_from_scroll_case", False),
        from_tailwater=meta.get("temperature_from_tailwater", False),
    ))


def elevation_sites(meta: dict, dam_code: str) -> list:
    """[(location, dart_site)] for whichever elevations this dam actually has."""
    sites = []
    if meta.get("has_elevation"):
        sites.append(("forebay", meta.get("elevation_site") or dam_code))
    if meta.get("has_tailwater_elevation") and meta.get("tailwater_elevation_site"):
        sites.append(("tailwater", meta["tailwater_elevation_site"]))
    return sites


def ingest_dart_elevation(conn, frame, dam_code, location, source_site) -> int:
    return upsert_elevation_daily(conn, parse_dart_elevation(
        frame, dam_code, location, source_site))


def ingest_usace(conn, text, dam_code) -> int:
    return upsert_flow_generation(conn, parse_usace_month(text, dam_code))


def ingest_cwms(conn, frames_by_series, dam_code) -> int:
    return upsert_flow_generation(conn, parse_cwms_flow(frames_by_series, dam_code))


def ingest_eia(conn, frame, dam_code=None) -> int:
    return upsert_generation_monthly(conn, parse_eia_monthly(frame, dam_code))
