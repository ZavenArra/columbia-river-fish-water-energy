#!/usr/bin/env python3
"""
Historical backfill for the dam dashboard.

Walks every dam in config/dam_availability.json across a range of years, pulls
only the data types that dam actually has, caches every response under
data/raw/, and upserts everything into data/dams.db.

Three properties this is built around:

  RESUMABLE  Any (dam, year, source) whose raw file already exists is not
             re-fetched. Cached files are still loaded into the database, so an
             interrupted run leaves the DB consistent with what is on disk.
             Because raw files are written atomically, a file that exists is
             always complete -- there is no partial-cache failure mode.

  IDEMPOTENT Every write is a COALESCE upsert on a composite primary key, so
             re-running produces the same row count, never duplicates.

  POLITE     One worker per host, each spacing its own requests. These are small
             government servers, so concurrency is across sources only.

A dam/year that fails after tenacity's retries is logged and the run continues;
failures are summarised at the end and written to a JSON file.

Usage
-----
  python etl/backfill.py --start-year 2020 --end-year 2024
  python etl/backfill.py --start-year 2024 --end-year 2024 --dams BON LWG
  python etl/backfill.py --start-year 2024 --end-year 2024 --skip-eia --force
"""

import argparse
import json
import logging
import sys
from concurrent.futures import as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scrapers"))
sys.path.insert(0, str(REPO / "config"))
sys.path.insert(0, str(REPO / "etl"))

# Supply USACE's missing intermediate CA before any request is made.
import os  # noqa: E402

_CA = REPO / "config" / "certs" / "usace-ca-bundle.pem"
if _CA.exists():
    os.environ["REQUESTS_CA_BUNDLE"] = str(_CA)

import fetchers  # noqa: E402
import load_db  # noqa: E402
from dams import DART_LABEL_TO_SPECIES, DART_SPECIES  # noqa: E402

log = logging.getLogger("etl.backfill")

DEFAULT_SPECIES = ["Chinook", "Jack-Chinook", "Coho", "Sockeye", "Steelhead",
                   "Shad"]
CWMS_SERIES = ["outflow", "gen_flow", "spill"]


def setup_logging(log_path: Path, verbose=False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# Per-source work units. Each returns (rows_written, was_fetched).
# ---------------------------------------------------------------------------

def do_dart_passage(conn, sched, dam, year, species, force):
    src = "dart_passage"
    if not force and fetchers.raw_exists(src, dam, year):
        return load_db.ingest_dart_passage(
            conn, fetchers.load_raw(src, dam, year), dam,
            DART_LABEL_TO_SPECIES), False
    df = sched.submit(src, fetchers.fetch_dart_passage, dam, year, species).result()
    fetchers.save_raw(df, src, dam, year)
    return load_db.ingest_dart_passage(conn, df, dam, DART_LABEL_TO_SPECIES), True


def do_dart_temperature(conn, sched, dam, year, meta, force):
    src = "dart_temperature"
    if not force and fetchers.raw_exists(src, dam, year):
        return load_db.ingest_dart_temperature(
            conn, fetchers.load_raw(src, dam, year), dam, meta), False
    site = meta.get("temperature_site") or dam
    parameter = meta.get("temperature_parameter") or "Temp (WQM)"
    df = sched.submit(src, fetchers.fetch_dart_temperature,
                      site, year, parameter).result()
    fetchers.save_raw(df, src, dam, year)
    return load_db.ingest_dart_temperature(conn, df, dam, meta), True


def do_dart_elevation(conn, sched, dam, year, meta, force):
    """
    Forebay and, where DART has one, tailwater elevation for one dam-year.

    Cached per location, so a dam with only a forebay site is not retried for a
    tailwater site it does not have.
    """
    src = "dart_elevation"
    total, fetched = 0, False
    for location, site in load_db.elevation_sites(meta, dam):
        cache_dam = f"{dam}-{location}"
        if not force and fetchers.raw_exists(src, cache_dam, year):
            total += load_db.ingest_dart_elevation(
                conn, fetchers.load_raw(src, cache_dam, year), dam, location, site)
            continue
        df = sched.submit(src, fetchers.fetch_dart_elevation, site, year).result()
        fetchers.save_raw(df, src, cache_dam, year)
        fetched = True
        total += load_db.ingest_dart_elevation(conn, df, dam, location, site)
    return total, fetched


def do_usace_month(conn, sched, dam, year, month, force):
    src = "usace"
    if not force and fetchers.raw_exists(src, dam, year, month):
        df = fetchers.load_raw(src, dam, year, month)
        return load_db.ingest_usace(conn, fetchers.usace_lines_to_text(df), dam), False
    df = sched.submit(src, fetchers.fetch_usace_month, dam, year, month).result()
    fetchers.save_raw(df, src, dam, year, month)
    return load_db.ingest_usace(conn, fetchers.usace_lines_to_text(df), dam), True


def do_cwms_year(conn, sched, dam, begin, end, year, force):
    """All four CWMS series for one dam/period, merged into one set of rows."""
    src = "cwms"
    frames = {}
    fetched = False
    for series in CWMS_SERIES:
        cache_dam = f"{dam}-{series}"
        if not force and fetchers.raw_exists(src, cache_dam, year):
            frames[series] = fetchers.load_raw(src, cache_dam, year)
            continue
        try:
            df = sched.submit(src, fetchers.fetch_cwms_series,
                              dam, series, begin, end).result()
        except Exception as e:  # noqa: BLE001
            log.warning("  %s %s %s: %s", dam, series, year, str(e)[:120])
            continue
        fetched = True
        if len(df):
            fetchers.save_raw(df, src, cache_dam, year)
            frames[series] = df
    return load_db.ingest_cwms(conn, frames, dam), fetched


def do_eia_year(conn, sched, year, dams, cache_dir, force):
    """
    One download per year covering every PUD dam, then split per dam.

    Fetching this per (dam, year) would pull the same ~20 MB zip five times.
    """
    import eia_scraper
    src = "eia923"
    pending = [d for d in dams
               if force or not fetchers.raw_exists(src, d, year)]
    if not pending:
        for dam in dams:
            load_db.ingest_eia(conn, fetchers.load_raw(src, dam, year), dam)
        return 0, False

    plant_ids = [eia_scraper.DAM_PLANT_IDS[d] for d in dams
                 if d in eia_scraper.DAM_PLANT_IDS]
    df = sched.submit(src, fetchers.fetch_eia_year, year, plant_ids,
                      cache_dir).result()
    written = 0
    for dam in dams:
        sub = df[df["dam"] == dam]
        if len(sub):
            fetchers.save_raw(sub, src, dam, year)
            written += load_db.ingest_eia(conn, sub, dam)
    return written, True


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-year", type=int, required=True)
    ap.add_argument("--end-year", type=int, required=True)
    ap.add_argument("--dams", nargs="+", default=None)
    ap.add_argument("--species", nargs="+", default=DEFAULT_SPECIES)
    ap.add_argument("--delay", type=float, default=1.5,
                    help="minimum seconds between requests to the same host")
    ap.add_argument("--db", default=str(load_db.DEFAULT_DB))
    ap.add_argument("--availability", default=str(load_db.DEFAULT_AVAILABILITY))
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even when a raw file already exists")
    ap.add_argument("--skip-eia", action="store_true",
                    help="skip EIA generation (avoids a ~20 MB download per year)")
    ap.add_argument("--log", default=str(REPO / "data" / "etl_backfill.log"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(Path(args.log), args.verbose)
    started = datetime.now(timezone.utc)

    availability = load_db.load_availability(args.availability)
    dams = [d.upper() for d in (args.dams or availability.keys())
            if d.upper() in availability]
    years = list(range(args.start_year, args.end_year + 1))

    conn = load_db.connect(args.db)
    load_db.init_db(conn)
    before = load_db.table_counts(conn)

    log.info("Backfill %s..%s | %d dams | delay %.1fs/host | db %s",
             args.start_year, args.end_year, len(dams), args.delay, args.db)
    log.info("Rows before: %s", before)

    failures = []
    fetched_count = skipped_count = 0
    eia_dams = [d for d in dams
                if "eia923" in load_db.applicable_sources(availability[d])]

    with fetchers.HostScheduler(delay=args.delay) as sched:
        for year in years:
            log.info("--- %d ---", year)

            if eia_dams and not args.skip_eia:
                try:
                    rows, fetched = do_eia_year(conn, sched, year, eia_dams,
                                                str(REPO / "data" / "raw" / "eia_cache"),
                                                args.force)
                    log.info("  EIA-923 %d: %d rows across %d dams%s",
                             year, rows, len(eia_dams),
                             "" if fetched else " (cached)")
                except Exception as e:  # noqa: BLE001
                    log.error("  EIA-923 %d FAILED: %s", year, str(e)[:200])
                    failures.append({"dam": "*", "year": year,
                                     "source": "eia923", "error": str(e)[:300]})

            for dam in dams:
                meta = availability[dam]
                sources = load_db.applicable_sources(meta)
                jobs = []

                if "dart_passage" in sources:
                    jobs.append(("dart_passage", lambda d=dam, y=year: do_dart_passage(
                        conn, sched, d, y, args.species, args.force)))
                if "dart_temperature" in sources:
                    jobs.append(("dart_temperature", lambda d=dam, y=year, m=meta:
                                 do_dart_temperature(conn, sched, d, y, m, args.force)))
                if "dart_elevation" in sources:
                    jobs.append(("dart_elevation", lambda d=dam, y=year, m=meta:
                                 do_dart_elevation(conn, sched, d, y, m, args.force)))
                if "usace" in sources:
                    for month in range(1, 13):
                        jobs.append((f"usace {month:02d}",
                                     lambda d=dam, y=year, mo=month: do_usace_month(
                                         conn, sched, d, y, mo, args.force)))
                if "cwms" in sources:
                    begin = datetime(year, 1, 1, tzinfo=timezone.utc)
                    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                    jobs.append(("cwms", lambda d=dam, b=begin, e=end, y=year:
                                 do_cwms_year(conn, sched, d, b, e, y, args.force)))

                for label, job in jobs:
                    try:
                        rows, fetched = job()
                        fetched_count += int(fetched)
                        skipped_count += int(not fetched)
                        log.info("  %-4s %d %-16s %6d rows%s", dam, year, label,
                                 rows, "" if fetched else "  (cached)")
                    except Exception as e:  # noqa: BLE001
                        log.error("  %-4s %d %-16s FAILED: %s", dam, year, label,
                                  str(e)[:200])
                        failures.append({"dam": dam, "year": year,
                                         "source": label, "error": str(e)[:300]})

    after = load_db.table_counts(conn)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    log.info("=" * 70)
    log.info("Rows after:  %s", after)
    log.info("Added: %s", {k: after[k] - before[k] for k in after})
    log.info("Fetched %d units, skipped %d cached, %.1fs elapsed",
             fetched_count, skipped_count, elapsed)

    if failures:
        out = REPO / "data" / "raw" / "_failures_backfill.json"
        out.write_text(json.dumps(failures, indent=2))
        log.warning("%d failures after retries -- see %s", len(failures), out)
        for f in failures[:20]:
            log.warning("  %s %s %s: %s", f["dam"], f["year"], f["source"],
                        f["error"][:120])
    else:
        log.info("No failures.")

    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
