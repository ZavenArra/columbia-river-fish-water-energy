#!/usr/bin/env python3
"""
USACE CWMS Data API Scraper (hourly flow / elevation)
=======================================================
A second route to USACE water-control data, via the CWMS Data API rather
than the monthly CSV archive that usace_scraper.py reads:

    https://cwms-data.usace.army.mil/cwms-data/

Why this exists: the monthly CSV archive (usace_scraper.py) only serves
the federal projects. It has no file at all for the mid-Columbia PUD dams
-- Priest Rapids, Wanapum, Rock Island, Rocky Reach and Wells -- yet those
dams do appear in CWMS under office NWDP, with hourly flow going back to
the 1960s. This module covers that gap, and works for the federal
projects too.

WHAT THIS DOES AND DOES NOT GIVE YOU
------------------------------------
Available and populated:
    {LOC}.Flow-Out.Ave.1Hour.1Hour.CBT-REV     total outflow, cfs
    {LOC}.Flow-Gen.Ave.1Hour.1Hour.CBT-REV     flow through turbines, cfs
    {LOC}.Flow-Spill.Ave.1Hour.1Hour.CBT-REV   spill, cfs
    {LOC}.Elev.Inst.1Hour.0.CBT-REV            forebay elevation, ft

NOT available: generation. {LOC}.Power.Total.1Hour.1Hour.* is listed in
the CWMS catalog, with extents that claim data from the 1960s to today,
but the timeseries endpoint returns zero values for every project tried,
federal and PUD alike (checked BON, LWG, PRD, WEL, both -RAW and -REV).
The catalog entry exists; the data is not served publicly. For megawatts
use usace_scraper.py (federal projects) or eia_scraper.py (any plant).

Series names end in a version: -REV is the reviewed/quality-controlled
series and is the default here; -RAW is the raw telemetry. GCPUD-RAW
series also exist for the Grant PUD projects, fed by the utility.

Usage examples
--------------
  # Hourly outflow at Priest Rapids for June 2025
  python cwms_scraper.py flow --sites PRD --begin 2025-06-01 --end 2025-07-01

  # All five mid-Columbia dams, one file
  python cwms_scraper.py flow --sites PRD WAN RIS RRH WEL \\
      --begin 2025-06-01 --end 2025-07-01 --out midcol_flow.csv

  # What series does a project actually have?
  python cwms_scraper.py catalog --site PRD
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

BASE_URL = "https://cwms-data.usace.army.mil/cwms-data"
OFFICE = "NWDP"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cwms-data-fetcher/1.0; "
                  "for research use of USACE's public CWMS Data API)",
    "Accept": "application/json;version=2",
}

# Short name -> CWMS time-series pattern. {loc} is the project code, {ver}
# the version suffix (REV reviewed / RAW raw telemetry).
SERIES = {
    "outflow": "{loc}.Flow-Out.Ave.1Hour.1Hour.CBT-{ver}",
    "gen_flow": "{loc}.Flow-Gen.Ave.1Hour.1Hour.CBT-{ver}",
    "spill": "{loc}.Flow-Spill.Ave.1Hour.1Hour.CBT-{ver}",
    "elevation": "{loc}.Elev.Inst.1Hour.0.CBT-{ver}",
}

# Listed in the CWMS catalog but served empty -- see module docstring.
UNPOPULATED_SERIES = {"power": "{loc}.Power.Total.1Hour.1Hour.CBT-{ver}"}


def _iso(dt) -> str:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_timeseries(ts_id: str, begin, end, office: str = OFFICE,
                     retries: int = 3, delay: float = 2.0) -> pd.DataFrame:
    """
    Fetch one CWMS time series as a DataFrame of (timestamp, value).

    Returns an empty frame -- not an error -- when the series exists but
    holds no values for the window, which is how CWMS reports the power
    series. Callers should check `len(df)`, not just for an exception.
    """
    params = {"office": office, "name": ts_id, "begin": _iso(begin), "end": _iso(end)}
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(f"{BASE_URL}/timeseries", params=params,
                                headers=HEADERS, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("values") or []
            df = pd.DataFrame(rows, columns=["timestamp", "value", "quality"])
            if not df.empty:
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df.attrs["units"] = payload.get("units")
            df.attrs["ts_id"] = ts_id
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError(f"Failed to fetch {ts_id} after {retries} attempts: {last_err}")


def fetch_flow(sites, begin, end, series="outflow", version="REV",
               delay_between_calls: float = 0.5) -> pd.DataFrame:
    """Tidy hourly flow for one or more projects: site, timestamp, value, units."""
    if series not in SERIES:
        raise ValueError(f"series must be one of {list(SERIES)}")
    frames = []
    for site in sites:
        ts_id = SERIES[series].format(loc=site.upper(), ver=version)
        try:
            df = fetch_timeseries(ts_id, begin, end)
        except RuntimeError as e:
            print(f"  Skipping {site.upper()}: {e}")
            continue
        if df.empty:
            print(f"  {site.upper()}: series exists but returned no values")
            continue
        df.insert(0, "site", site.upper())
        df["series"] = series
        df["units"] = df.attrs.get("units")
        frames.append(df)
        time.sleep(delay_between_calls)
    if not frames:
        raise RuntimeError("No data retrieved for any requested site.")
    return pd.concat(frames, ignore_index=True)


def catalog(site: str, office: str = OFFICE) -> pd.DataFrame:
    """Every time series CWMS lists for a project, with its period of record."""
    resp = requests.get(f"{BASE_URL}/catalog/TIMESERIES",
                        params={"office": office, "like": f"^{site.upper()}\\.",
                                "page-size": 500},
                        headers=HEADERS, timeout=60)
    resp.raise_for_status()
    rows = []
    for e in resp.json().get("entries", []):
        ext = (e.get("extents") or [{}])[0]
        rows.append({"name": e.get("name"), "units": e.get("units"),
                     "interval": e.get("interval"),
                     "earliest": ext.get("earliest-time"),
                     "latest": ext.get("latest-time")})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description="Fetch USACE CWMS hourly flow/elevation data")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("flow", help="Fetch an hourly series for one or more projects")
    f.add_argument("--sites", nargs="+", required=True, help="Project codes, e.g. PRD WAN RIS")
    f.add_argument("--series", default="outflow", choices=list(SERIES))
    f.add_argument("--version", default="REV", choices=["REV", "RAW"])
    f.add_argument("--begin", default=None, help="ISO date; default 30 days ago")
    f.add_argument("--end", default=None, help="ISO date; default now")
    f.add_argument("--out", default=None)

    c = sub.add_parser("catalog", help="List the series a project publishes")
    c.add_argument("--site", required=True)
    c.add_argument("--out", default=None)

    args = ap.parse_args()

    if args.cmd == "flow":
        end = args.end or datetime.now(timezone.utc)
        begin = args.begin or (datetime.now(timezone.utc) - timedelta(days=30))
        df = fetch_flow(args.sites, begin, end, args.series, args.version)
        out = args.out or f"cwms_{args.series}_{'_'.join(s.lower() for s in args.sites)}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} rows to {out}")
        print(df.head())

    elif args.cmd == "catalog":
        df = catalog(args.site)
        out = args.out or f"cwms_catalog_{args.site.lower()}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} series to {out}")
        print(df.to_string())


if __name__ == "__main__":
    main()
