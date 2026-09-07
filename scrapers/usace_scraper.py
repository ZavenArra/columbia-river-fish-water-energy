#!/usr/bin/env python3
"""
USACE Northwestern Division Columbia/Snake River Water-Control Data Scraper
=============================================================================
Downloads USACE's public hourly dam-operations data: flow, spill,
hydropower generation (MW), and pool elevations for the federal dams on
the Columbia and Snake rivers.

Source: USACE Northwestern Division, Columbia Basin Water Management
("Columbia River Water Control Data"). Historical monthly CSV archive:
    https://www.nwd-wc.usace.army.mil/dd/nwdp/hist_csv/www/{site}_{YYYYMM}.csv
e.g. https://www.nwd-wc.usace.army.mil/dd/nwdp/hist_csv/www/lwg_202511.csv

Each file is one month of hourly data for one project. Columns vary a bit
by site but generally include some mix of:
  Date, forebay/tailwater elevation & water temperature, % Spill,
  Gen (MW), Gen Flow (kcfs), Spill Flow (kcfs), Total Flow (kcfs),
  FB Elev, TW Elev, Head, Units online, etc.
i.e. this single file gives you BOTH river flow and power generation
for a project in one place.

IMPORTANT CAVEATS (please read before relying on this)
--------------------------------------------------------
- This domain's robots.txt disallows generic automated crawling. The URL
  pattern above was confirmed against a real, currently-served file
  (a November 2025 Lower Granite dataset), but that doesn't override the
  site's stated crawler policy — check nwd-wc.usace.army.mil/robots.txt
  yourself and use this in a way you're comfortable with (low request
  rate, identify yourself in the User-Agent, don't hammer it).
- USACE has been migrating parts of this system to a new domain,
  public.crohms.org. If the URLs below stop resolving, check
  https://www.nwd.usace.army.mil/CRWM/Water-Control-Data/ for the current
  location, or use the interactive query tool (when available) at
  https://public.crohms.org/dd/common/dataquery/www/
- Column names differ per project — inspect df.columns after your first
  pull for any site you haven't used before.
- This is a small government operations site, not a bulk data API like
  DART. Fetch only what you need.

Usage examples
--------------
  # Lower Granite Dam (Snake River), November 2025
  python usace_scraper.py fetch --site lwg --year 2025 --month 11

  # Bonneville Dam, all of 2024 (concatenates 12 monthly files)
  python usace_scraper.py fetch-year --site bon --year 2024 --out bon_2024.csv

  # Several dams, one month, stacked into one file
  python usace_scraper.py fetch --site bon jda mcn ihr lgs lmn lwg \\
      --year 2024 --month 6 --out june_2024.csv
"""

import argparse
import io
import time

import pandas as pd
import requests

BASE_URL = "https://www.nwd-wc.usace.army.mil/dd/nwdp/hist_csv/www"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; usace-data-fetcher/1.0; "
                  "for research use of USACE's published public water-control data)"
}

# Common project codes on the Columbia/Snake system. Not exhaustive — any
# code used on the NWD Columbia River Water Control Data site should work.
KNOWN_SITES = [
    "alf",  # Albeni Falls
    "bon",  # Bonneville
    "chj",  # Chief Joseph
    "dwr",  # Dworshak
    "gcl",  # Grand Coulee
    "hgh",  # Hungry Horse
    "ihr",  # Ice Harbor
    "jda",  # John Day
    "lgs",  # Little Goose
    "lib",  # Libby
    "lmn",  # Lower Monumental
    "lwg",  # Lower Granite
    "mcn",  # McNary
    "prd",  # Priest Rapids
    "ris",  # Rock Island
    "rrh",  # Rocky Reach
    "tda",  # The Dalles
    "wan",  # Wanapum
    "wel",  # Wells
]


def fetch_month(site: str, year: int, month: int, retries=3, delay=2) -> pd.DataFrame:
    """
    Fetch one project's hourly data for one month.
    site: lowercase project code, e.g. "bon", "lwg", "mcn"
    """
    url = f"{BASE_URL}/{site.lower()}_{year:04d}{month:02d}.csv"
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text), skip_blank_lines=True)
            df.columns = [c.strip() for c in df.columns]
            df.insert(0, "site", site.upper())
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def fetch_year(site: str, year: int, delay_between_calls: float = 1.0) -> pd.DataFrame:
    """Fetch and concatenate all 12 months of a year for one project."""
    frames = []
    for month in range(1, 13):
        try:
            frames.append(fetch_month(site, year, month))
        except RuntimeError as e:
            print(f"  Skipping {site.upper()} {year}-{month:02d}: {e}")
        time.sleep(delay_between_calls)
    if not frames:
        raise RuntimeError(f"No data retrieved for {site.upper()} in {year}.")
    return pd.concat(frames, ignore_index=True, sort=False)


def fetch_many(sites, year: int, month: int, delay_between_calls: float = 1.0) -> pd.DataFrame:
    """Fetch the same month across multiple projects and stack the results."""
    frames = []
    for site in sites:
        try:
            frames.append(fetch_month(site, year, month))
        except RuntimeError as e:
            print(f"  Skipping {site.upper()}: {e}")
        time.sleep(delay_between_calls)
    if not frames:
        raise RuntimeError("No data retrieved for any requested site.")
    return pd.concat(frames, ignore_index=True, sort=False)


def main():
    ap = argparse.ArgumentParser(
        description="Scrape USACE Columbia/Snake River dam flow & generation data"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Fetch one or more sites for a single month")
    f.add_argument("--site", nargs="+", required=True, help="Project codes, e.g. bon lwg mcn")
    f.add_argument("--year", type=int, required=True)
    f.add_argument("--month", type=int, required=True)
    f.add_argument("--out", default=None)

    fy = sub.add_parser("fetch-year", help="Fetch a full year for one site")
    fy.add_argument("--site", required=True)
    fy.add_argument("--year", type=int, required=True)
    fy.add_argument("--out", default=None)

    args = ap.parse_args()

    if args.cmd == "fetch":
        df = fetch_many(args.site, args.year, args.month)
        out = args.out or f"usace_{'_'.join(s.lower() for s in args.site)}_{args.year}{args.month:02d}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} rows to {out}")
        print(df.head())

    elif args.cmd == "fetch-year":
        df = fetch_year(args.site, args.year)
        out = args.out or f"usace_{args.site.lower()}_{args.year}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} rows to {out}")
        print(df.head())


if __name__ == "__main__":
    main()
