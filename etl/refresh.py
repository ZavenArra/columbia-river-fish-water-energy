#!/usr/bin/env python3
"""
Incremental refresh: the last ~60 days for every dam, upserted into data/dams.db.

Deliberately does NOT write the raw cache: the current month is still being
appended to, and caching a partial month would make a later backfill skip
fetching the complete one.

Meant to be triggered by hand from the Streamlit app, so it has to finish fast.
It gets there by running the three hosts concurrently -- DART, USACE and CWMS
each have their own worker enforcing their own spacing, so no single server sees
overlapping requests, but the run takes about as long as the slowest host rather
than the sum of all three.

Every fetch is submitted first and parsed as it lands; database writes happen on
the main thread, which keeps the SQLite connection single-threaded. Writes are
the same COALESCE upserts backfill.py uses, so running this over a period the
backfill already covers refreshes those rows instead of duplicating them.

Period handling per source:
  DART   a date range, split at year boundaries (DART queries are per-year)
  USACE  the current and previous month, since its files are monthly
  CWMS   one call per series across the whole window

Elevation is included by default but can be dropped with --skip-elevation. It
costs ~35 extra DART requests (forebay plus tailwater for every dam) and DART is
the slowest host, so skipping it takes a full run from roughly 75s to 45s. Worth
skipping when you only want the fast-moving series; the daily pool level rarely
needs to be as fresh as passage counts or flow.

EIA-923 is deliberately not refreshed: it is monthly, published on a long lag,
and costs a ~20 MB download, none of which suits an interactive refresh. Use
`backfill.py` for generation at the PUD dams, or pass --with-eia.

Usage
-----
  python etl/refresh.py
  python etl/refresh.py --skip-elevation        # ~45s instead of ~75s
  python etl/refresh.py --days 30 --delay 0.5
"""

import argparse
import json
import logging
import sys
from concurrent.futures import as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scrapers"))
sys.path.insert(0, str(REPO / "config"))
sys.path.insert(0, str(REPO / "etl"))

import os  # noqa: E402

_CA = REPO / "config" / "certs" / "usace-ca-bundle.pem"
if _CA.exists():
    os.environ["REQUESTS_CA_BUNDLE"] = str(_CA)

import fetchers  # noqa: E402
import load_db  # noqa: E402
from backfill import CWMS_SERIES, DEFAULT_SPECIES, setup_logging  # noqa: E402
from dams import DART_LABEL_TO_SPECIES  # noqa: E402

log = logging.getLogger("etl.refresh")


def dart_segments(start: date, end: date):
    """
    Split a date range into per-year (year, start_mmdd, end_mmdd) pieces.

    A 60-day window in January spans two years, and a DART query covers one year
    at a time, so the range has to be cut at the year boundary.
    """
    segments = []
    year = start.year
    while year <= end.year:
        seg_start = max(start, date(year, 1, 1))
        seg_end = min(end, date(year, 12, 31))
        if seg_start <= seg_end:
            segments.append((year, seg_start.strftime("%m/%d"),
                             seg_end.strftime("%m/%d")))
        year += 1
    return segments


def usace_months(start: date, end: date):
    """Every (year, month) the window touches -- usually two."""
    months, cursor = [], date(start.year, start.month, 1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        cursor = date(cursor.year + (cursor.month == 12),
                      1 if cursor.month == 12 else cursor.month + 1, 1)
    return months


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--dams", nargs="+", default=None)
    ap.add_argument("--species", nargs="+", default=DEFAULT_SPECIES)
    ap.add_argument("--delay", type=float, default=1.0,
                    help="minimum seconds between requests to the same host")
    ap.add_argument("--db", default=str(load_db.DEFAULT_DB))
    ap.add_argument("--availability", default=str(load_db.DEFAULT_AVAILABILITY))
    ap.add_argument("--skip-elevation", action="store_true",
                    help="skip daily forebay/tailwater elevation. Elevation is "
                         "~35 extra DART requests and DART is the slowest host, "
                         "so this is the difference between a ~75s and a ~45s run")
    ap.add_argument("--with-eia", action="store_true",
                    help="also refresh EIA-923 monthly generation (slow)")
    ap.add_argument("--log", default=str(REPO / "data" / "etl_refresh.log"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(Path(args.log), args.verbose)
    started = datetime.now(timezone.utc)

    end_date = date.today()
    start_date = end_date - timedelta(days=args.days)

    availability = load_db.load_availability(args.availability)
    dams = [d.upper() for d in (args.dams or availability.keys())
            if d.upper() in availability]

    conn = load_db.connect(args.db)
    load_db.init_db(conn)
    before = load_db.table_counts(conn)

    log.info("Refresh %s..%s (%d days) | %d dams | delay %.1fs/host%s",
             start_date, end_date, args.days, len(dams), args.delay,
             " | elevation skipped" if args.skip_elevation else "")

    segments = dart_segments(start_date, end_date)
    months = usace_months(start_date, end_date)
    cwms_begin = datetime.combine(start_date, datetime.min.time(), timezone.utc)
    cwms_end = datetime.now(timezone.utc)

    failures = []
    pending = {}

    # Submit every fetch up front so the hosts work in parallel; ingest below as
    # each result arrives, on this thread, so SQLite stays single-threaded.
    with fetchers.HostScheduler(delay=args.delay) as sched:
        for dam in dams:
            meta = availability[dam]
            sources = load_db.applicable_sources(meta)

            if "dart_passage" in sources:
                for year, s_mmdd, e_mmdd in segments:
                    fut = sched.submit("dart_passage", fetchers.fetch_dart_passage,
                                       dam, year, args.species, s_mmdd, e_mmdd)
                    pending[fut] = (f"{dam} passage {year}", "dart_passage", dam,
                                    year, meta)

            if "dart_temperature" in sources:
                site = meta.get("temperature_site") or dam
                parameter = meta.get("temperature_parameter") or "Temp (WQM)"
                for year, s_mmdd, e_mmdd in segments:
                    fut = sched.submit("dart_temperature",
                                       fetchers.fetch_dart_temperature,
                                       site, year, parameter, s_mmdd, e_mmdd)
                    pending[fut] = (f"{dam} temp {year}", "dart_temperature", dam,
                                    year, meta)

            if "dart_elevation" in sources and not args.skip_elevation:
                for location, site in load_db.elevation_sites(meta, dam):
                    for year, s_mmdd, e_mmdd in segments:
                        fut = sched.submit("dart_elevation",
                                           fetchers.fetch_dart_elevation,
                                           site, year, s_mmdd, e_mmdd)
                        pending[fut] = (f"{dam} elev {location} {year}",
                                        "dart_elevation", dam,
                                        (location, site), meta)

            if "usace" in sources:
                for year, month in months:
                    fut = sched.submit("usace", fetchers.fetch_usace_month,
                                       dam, year, month)
                    pending[fut] = (f"{dam} usace {year}-{month:02d}", "usace",
                                    dam, (year, month), meta)

            if "cwms" in sources:
                for series in CWMS_SERIES:
                    fut = sched.submit("cwms", fetchers.fetch_cwms_series,
                                       dam, series, cwms_begin, cwms_end)
                    pending[fut] = (f"{dam} cwms {series}", "cwms", dam, series, meta)

        log.info("Submitted %d fetches across %d hosts", len(pending),
                 len({fetchers.SOURCE_HOST[p[1]] for p in pending.values()}))

        # CWMS series arrive one at a time but belong to one row per dam-hour;
        # collect them per dam and ingest once every dam's series have landed.
        cwms_frames = {}
        written = 0

        for fut in as_completed(pending):
            label, source, dam, key, meta = pending[fut]
            try:
                df = fut.result()
            except Exception as e:  # noqa: BLE001
                log.error("  %-26s FAILED: %s", label, str(e)[:160])
                failures.append({"label": label, "source": source,
                                 "error": str(e)[:300]})
                continue

            try:
                if source == "dart_passage":
                    rows = load_db.ingest_dart_passage(conn, df, dam,
                                                       DART_LABEL_TO_SPECIES)
                elif source == "dart_temperature":
                    rows = load_db.ingest_dart_temperature(conn, df, dam, meta)
                elif source == "dart_elevation":
                    location, site = key
                    rows = load_db.ingest_dart_elevation(conn, df, dam,
                                                         location, site)
                elif source == "usace":
                    rows = load_db.ingest_usace(
                        conn, fetchers.usace_lines_to_text(df), dam)
                elif source == "cwms":
                    cwms_frames.setdefault(dam, {})[key] = df
                    rows = 0
                else:
                    rows = 0
                written += rows
                log.info("  %-26s %6d rows", label, rows)
            except Exception as e:  # noqa: BLE001
                log.error("  %-26s INGEST FAILED: %s", label, str(e)[:160])
                failures.append({"label": label, "source": source,
                                 "error": str(e)[:300]})

        for dam, frames in cwms_frames.items():
            try:
                rows = load_db.ingest_cwms(conn, frames, dam)
                written += rows
                log.info("  %-26s %6d rows", f"{dam} cwms merged", rows)
            except Exception as e:  # noqa: BLE001
                log.error("  %s cwms merge FAILED: %s", dam, str(e)[:160])
                failures.append({"label": f"{dam} cwms merge", "source": "cwms",
                                 "error": str(e)[:300]})

    if args.with_eia:
        import backfill
        import eia_scraper
        eia_dams = [d for d in dams if d in eia_scraper.DAM_PLANT_IDS]
        if eia_dams:
            with fetchers.HostScheduler(delay=args.delay) as sched:
                try:
                    rows, _ = backfill.do_eia_year(
                        conn, sched, end_date.year - 1, eia_dams,
                        str(REPO / "data" / "raw" / "eia_cache"), True)
                    log.info("  EIA-923 %d: %d rows", end_date.year - 1, rows)
                except Exception as e:  # noqa: BLE001
                    log.error("  EIA-923 FAILED: %s", str(e)[:160])
                    failures.append({"label": "eia923", "source": "eia923",
                                     "error": str(e)[:300]})

    after = load_db.table_counts(conn)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    log.info("=" * 70)
    log.info("Rows after: %s", after)
    log.info("Net new:    %s", {k: after[k] - before[k] for k in after})
    log.info("Upserted %d rows in %.1fs", written, elapsed)

    if failures:
        out = REPO / "data" / "raw" / "_failures_refresh.json"
        out.write_text(json.dumps(failures, indent=2))
        log.warning("%d failures -- see %s", len(failures), out)

    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
