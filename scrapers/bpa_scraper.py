#!/usr/bin/env python3
"""
Bonneville Power Administration (BPA) Data Scraper
====================================================
Downloads two kinds of public BPA data:

1. GENERATION MIX (hydro, thermal/fossil-biomass, wind+solar "VER",
   nuclear), load, and net interchange at 5-minute resolution.
     - Live: rolling 7-day feed, updated ~every 5 minutes.
     - Historical: full-year archives, 2007-present.
   Source: BPA Transmission "VER & Balancing Authority Monitoring" page
   https://transmission.bpa.gov/Business/Operations/Wind/default.aspx

2. STREAMFLOW through the Federal Columbia River Power System dams.
   BPA's "2020 Level Modified Streamflow" dataset: historical flows
   (1929-2018) adjusted to a consistent (2018) level of irrigation
   depletion and with river regulation effects normalized out, at
   daily / semimonthly / monthly / seasonal resolution, plus a
   naturalized (No Regulation-No Irrigation) flow CSV for 1929-2008.
   Source: https://www.bpa.gov/energy-and-services/power/historical-streamflow-data

All of these are direct, fixed-URL file downloads (txt/xlsx/csv/zip)
that BPA publishes for exactly this kind of programmatic use — not HTML
pages that need to be parsed.

Usage examples
--------------
  # Live rolling 7-day generation mix (hydro/thermal/wind/nuclear/load)
  python bpa_scraper.py gen-live --out bpa_live.csv

  # Full year 2023 generation-mix archive
  python bpa_scraper.py gen-history --year 2023 --out bpa_2023.csv

  # Daily streamflow through the dams, 1929-2018 (unzips several files)
  python bpa_scraper.py streamflow --freq daily --dir bpa_streamflow

  # Naturalized (no dams, no irrigation) flow series, 1929-2008
  python bpa_scraper.py streamflow-nrni --out bpa_nrni.csv
"""

import argparse
import io
import os
import zipfile

import pandas as pd
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; bpa-data-fetcher/1.0; "
                  "for research use of BPA's published public data files)"
}

# ---------------------------------------------------------------------------
# 1. Generation / load / interchange data
# ---------------------------------------------------------------------------

# Rolling 7-day feed: Date/Time, Load, VER (wind+solar), Hydro,
# Fossil/Biomass, Interchange, Nuclear — all in MW, 5-minute increments.
LIVE_GEN_URL = "https://transmission.bpa.gov/business/operations/Wind/baltwg3.txt"

# Full-year historical archives. Newer years are .xlsx, older are .xls.
HISTORICAL_GEN_URLS = {
    **{y: f"https://transmission.bpa.gov/Business/operations/Wind/OPITabularReports/WindGenTotalLoadYTD_{y}.xlsx"
       for y in range(2022, 2027)},
    **{y: f"https://transmission.bpa.gov/Business/operations/Wind/OPITabularReports/WindGenTotalLoadYTD_{y}.xls"
       for y in range(2011, 2022)},
    2010: "https://transmission.bpa.gov/Business/operations/Wind/OPITabularReports/TotalWindLoad_5Min_10.xls",
    2009: "https://transmission.bpa.gov/Business/operations/Wind/OPITabularReports/TotalWindLoad_5Min_09.xls",
    2008: "https://transmission.bpa.gov/Business/operations/Wind/OPITabularReports/TotalWindLoad_5Min_08.xls",
    2007: "https://transmission.bpa.gov/Business/operations/Wind/OPITabularReports/TotalWindLoad_5Min_07.xls",
}


def fetch_live_generation() -> pd.DataFrame:
    """
    Rolling last-7-days generation mix at 5-minute intervals.
    Columns: Date/Time, Load, VER, Hydro, Fossil/Biomass, Interchange, Nuclear (MW).
    """
    resp = requests.get(LIVE_GEN_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    lines = [l for l in resp.text.splitlines() if l.strip()]
    header_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("Date/Time"))
    value_cols = lines[header_idx].split()[1:]  # everything after "Date/Time"

    rows = []
    for line in lines[header_idx + 1:]:
        parts = line.split()
        if len(parts) < 2 + len(value_cols):
            continue  # skip stray/footer lines
        date, time_ = parts[0], parts[1]
        values = parts[2:2 + len(value_cols)]
        rows.append([f"{date} {time_}"] + values)

    df = pd.DataFrame(rows, columns=["Date/Time"] + value_cols)
    df["Date/Time"] = pd.to_datetime(df["Date/Time"], errors="coerce")
    for c in value_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_historical_generation(year: int) -> pd.DataFrame:
    """Full-year 5-minute generation-mix archive for `year` (2007-present)."""
    if year not in HISTORICAL_GEN_URLS:
        raise ValueError(
            f"No known historical generation file for {year}. "
            f"Available years: {sorted(HISTORICAL_GEN_URLS)}"
        )
    url = HISTORICAL_GEN_URLS[year]
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return pd.read_excel(io.BytesIO(resp.content))


# ---------------------------------------------------------------------------
# 2. Historical streamflow through the FCRPS dams
# ---------------------------------------------------------------------------

STREAMFLOW_ZIPS = {
    "daily": "https://www.bpa.gov/-/media/Aep/power/historical-streamflow-reports/historic-streamflow-all-daily-data.zip",
    "monthly": "https://www.bpa.gov/-/media/Aep/power/historical-streamflow-reports/historic-streamflow-all-monthly-data.zip",
    "semimonthly": "https://www.bpa.gov/-/media/Aep/power/historical-streamflow-reports/historic-streamflow-all-semimonthly-data.zip",
    "seasonal": "https://www.bpa.gov/-/media/Aep/power/historical-streamflow-reports/historic-streamflow-all-seasonal-data.zip",
}

STREAMFLOW_NRNI_CSV = (
    "https://www.bpa.gov/-/media/Aep/power/historical-streamflow-reports/"
    "historic-streamflow-nrni-flows-1929-2008-corrected-04-2017.csv"
)


def download_streamflow_zip(freq: str, extract_dir: str) -> str:
    """
    Download and unzip BPA's modified-streamflow dataset: flow through the
    Federal Columbia River Power System dams, historical 1929-2018,
    adjusted to a consistent 2018 level of irrigation depletion.
    freq: one of "daily", "monthly", "semimonthly", "seasonal"
    Returns the directory the files were extracted to.
    """
    if freq not in STREAMFLOW_ZIPS:
        raise ValueError(f"freq must be one of {list(STREAMFLOW_ZIPS)}")
    resp = requests.get(STREAMFLOW_ZIPS[freq], headers=HEADERS, timeout=120)
    resp.raise_for_status()
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def fetch_streamflow_nrni() -> pd.DataFrame:
    """No Regulation-No Irrigation naturalized flows, 1929-2008, as a single CSV."""
    resp = requests.get(STREAMFLOW_NRNI_CSV, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Scrape BPA streamflow and power-generation data")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g_live = sub.add_parser("gen-live", help="Fetch rolling 7-day generation mix (5-min)")
    g_live.add_argument("--out", default="bpa_generation_live.csv")

    g_hist = sub.add_parser("gen-history", help="Fetch a full year of historical generation data")
    g_hist.add_argument("--year", type=int, required=True)
    g_hist.add_argument("--out", default=None)

    s_zip = sub.add_parser("streamflow", help="Download BPA's historical streamflow dataset (dam flows)")
    s_zip.add_argument("--freq", choices=list(STREAMFLOW_ZIPS), default="daily")
    s_zip.add_argument("--dir", default="bpa_streamflow")

    s_nrni = sub.add_parser("streamflow-nrni", help="Fetch the 1929-2008 naturalized-flow CSV")
    s_nrni.add_argument("--out", default="bpa_streamflow_nrni.csv")

    args = ap.parse_args()

    if args.cmd == "gen-live":
        df = fetch_live_generation()
        df.to_csv(args.out, index=False)
        print(f"Saved {len(df)} rows to {args.out}")
        print(df.head())

    elif args.cmd == "gen-history":
        df = fetch_historical_generation(args.year)
        out = args.out or f"bpa_generation_{args.year}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} rows to {out}")
        print(df.head())

    elif args.cmd == "streamflow":
        path = download_streamflow_zip(args.freq, args.dir)
        print(f"Extracted {args.freq} streamflow files to {path}/")
        for f in sorted(os.listdir(path)):
            print(" -", f)

    elif args.cmd == "streamflow-nrni":
        df = fetch_streamflow_nrni()
        df.to_csv(args.out, index=False)
        print(f"Saved {len(df)} rows to {args.out}")
        print(df.head())


if __name__ == "__main__":
    main()
