#!/usr/bin/env python3
"""
Fetching, raw-file caching and rate limiting for the dam dashboard ETL.

Shared by etl/backfill.py and etl/refresh.py so neither reimplements it.
Parsing and database writes live in etl/load_db.py.

WHY THE RAW CACHE MATTERS
-------------------------
Every successful fetch is written to data/raw/{source}/{dam}_{year}[_{month}].csv
*atomically* -- to a temp file, then os.replace. Because a rename is atomic, a
file that exists is always a complete file. That is what makes "skip anything
already on disk" safe after a Ctrl-C or a crash: there is no such thing as a
half-written cache entry that would be mistaken for a good one.

RETRIES
-------
tenacity owns the retry policy. The scrapers each have their own internal retry
loop (dart_scraper.fetch_csv, usace_scraper.fetch_month and
cwms_scraper.fetch_timeseries all default to retries=3), so they are called here
with retries=1 -- otherwise the two layers multiply and a single dead URL costs
4 x 3 = 12 requests against a small government server.

RATE LIMITING
-------------
Requests are grouped by host (dart / usace / cwms / eia). Each host gets one
worker thread enforcing its own minimum spacing, so different sources run
concurrently while any single host still sees strictly serialised, spaced-out
requests. That is what lets refresh.py finish in ~20s without being rude to
anyone: the concurrency is across hosts, never within one.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw"

log = logging.getLogger("etl.fetchers")

# Which host each logical source talks to. Sources sharing a host share a queue.
SOURCE_HOST = {
    "dart_passage": "dart",
    "dart_temperature": "dart",
    "dart_elevation": "dart",
    "usace": "usace",
    "cwms": "cwms",
    "eia923": "eia",
}

RETRY = dict(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)


# ---------------------------------------------------------------------------
# Raw cache
# ---------------------------------------------------------------------------

def raw_path(source: str, dam_code: str, year: int, month: int = None) -> Path:
    stem = f"{dam_code.upper()}_{year:04d}"
    if month is not None:
        stem += f"_{month:02d}"
    return RAW_DIR / source / f"{stem}.csv"


def raw_exists(source: str, dam_code: str, year: int, month: int = None) -> bool:
    path = raw_path(source, dam_code, year, month)
    return path.exists() and path.stat().st_size > 0


def save_raw(df: pd.DataFrame, source: str, dam_code: str, year: int,
             month: int = None) -> Path:
    """Write the cache entry atomically; see the module docstring."""
    path = raw_path(source, dam_code, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".csv.tmp{os.getpid()}")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path


def load_raw(source: str, dam_code: str, year: int, month: int = None) -> pd.DataFrame:
    return pd.read_csv(raw_path(source, dam_code, year, month))


# ---------------------------------------------------------------------------
# Per-host rate limiting
# ---------------------------------------------------------------------------

class HostScheduler:
    """
    One worker thread per host, each spacing its own requests by `delay`.

    submit() returns a Future. Work for different hosts overlaps; work for the
    same host is serialised and spaced. Use as a context manager.
    """

    def __init__(self, delay: float = 1.0, hosts=None):
        self.delay = delay
        self._pools = {h: ThreadPoolExecutor(max_workers=1,
                                             thread_name_prefix=f"etl-{h}")
                       for h in (hosts or set(SOURCE_HOST.values()))}
        self._last = {h: 0.0 for h in self._pools}
        self._locks = {h: threading.Lock() for h in self._pools}

    def _spaced(self, host, fn, *args, **kwargs):
        with self._locks[host]:
            gap = self.delay - (time.monotonic() - self._last[host])
            if gap > 0:
                time.sleep(gap)
            try:
                return fn(*args, **kwargs)
            finally:
                self._last[host] = time.monotonic()

    def submit(self, source: str, fn, *args, **kwargs):
        host = SOURCE_HOST.get(source, source)
        if host not in self._pools:
            self._pools[host] = ThreadPoolExecutor(max_workers=1)
            self._last[host] = 0.0
            self._locks[host] = threading.Lock()
        return self._pools[host].submit(self._spaced, host, fn, *args, **kwargs)

    def shutdown(self):
        for pool in self._pools.values():
            pool.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()


# ---------------------------------------------------------------------------
# Fetchers -- one per source, each returning a DataFrame
# ---------------------------------------------------------------------------

@retry(**RETRY)
def fetch_dart_passage(dam_code, year, species, start_mmdd="01/01",
                       end_mmdd="12/31") -> pd.DataFrame:
    import dart_scraper
    return dart_scraper.fetch_adult_passage(
        projects=[dam_code], species=list(species), years=[year],
        start_mmdd=start_mmdd, end_mmdd=end_mmdd,
    )


@retry(**RETRY)
def fetch_dart_river(site, year, parameter, start_mmdd="01/01",
                     end_mmdd="12/31") -> pd.DataFrame:
    """
    Any DART River Environment parameter for one site.

    dart_scraper.fetch_river_temperature is the generic river-query function
    despite its name -- `parameter` selects what it returns, so the same call
    serves temperature and elevation.
    """
    import dart_scraper
    return dart_scraper.fetch_river_temperature(
        locations=[site], years=[year], parameter=parameter,
        start_mmdd=start_mmdd, end_mmdd=end_mmdd,
    )


def fetch_dart_temperature(site, year, parameter, start_mmdd="01/01",
                           end_mmdd="12/31") -> pd.DataFrame:
    return fetch_dart_river(site, year, parameter, start_mmdd, end_mmdd)


def fetch_dart_elevation(site, year, start_mmdd="01/01",
                         end_mmdd="12/31") -> pd.DataFrame:
    return fetch_dart_river(site, year, "Elevation", start_mmdd, end_mmdd)


@retry(**RETRY)
def fetch_usace_month(dam_code, year, month) -> pd.DataFrame:
    """
    Return the month's file as a single-column frame of raw lines.

    usace_scraper.fetch_month parses with pd.read_csv, which raises on the six
    projects whose header row is narrower than their data rows. The raw text is
    what we want to cache anyway -- load_db.parse_usace_month does the padding,
    the units row and the 1..24 hour convention -- so fetch the text directly
    with the scraper's own URL, headers and session behaviour.
    """
    import requests
    import usace_scraper
    url = f"{usace_scraper.BASE_URL}/{dam_code.lower()}_{year:04d}{month:02d}.csv"
    resp = requests.get(url, headers=usace_scraper.HEADERS, timeout=60)
    resp.raise_for_status()
    lines = [l for l in resp.text.splitlines() if l.strip()]
    if len(lines) < 3:
        raise ValueError(f"{url} returned header/units only, no data rows")
    return pd.DataFrame({"line": lines})


@retry(**RETRY)
def fetch_cwms_series(dam_code, series, begin, end) -> pd.DataFrame:
    import cwms_scraper
    ts_id = cwms_scraper.SERIES[series].format(loc=dam_code.upper(), ver="REV")
    return cwms_scraper.fetch_timeseries(ts_id, begin, end, retries=1)


@retry(**RETRY)
def fetch_eia_year(year, plant_ids, cache_dir=None) -> pd.DataFrame:
    """
    One EIA-923 zip covers every plant, so this is called once per YEAR and the
    result split per dam -- never once per (dam, year), which would re-download
    ~20 MB five times over.
    """
    import eia_scraper
    return eia_scraper.fetch_plant_monthly(year, plant_ids, cache_dir=cache_dir)


def usace_lines_to_text(df: pd.DataFrame) -> str:
    """Rebuild the raw USACE text from its cached one-line-per-row frame."""
    return "\n".join(str(x) for x in df["line"].tolist())
