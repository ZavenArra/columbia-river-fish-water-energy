#!/usr/bin/env python3
"""
EIA Generation Scraper (mid-Columbia PUD dams, and any US plant)
==================================================================
Fills the one gap the USACE sources cannot: megawatts at the mid-Columbia
PUD dams. usace_scraper.py's monthly CSV archive has no file for Priest
Rapids, Wanapum, Rock Island, Rocky Reach or Wells, and CWMS lists a
Power.Total series for them but serves it empty. EIA publishes their
generation, from the utility side of the meter.

Two datasets, with a real resolution/precision trade-off between them:

1. EIA-923, monthly net generation PER PLANT (MWh).
   Exact per-dam attribution, monthly resolution. Annual zip of Excel
   workbooks; 2001-present.
   https://www.eia.gov/electricity/data/eia923/

2. EIA-930, hourly net generation PER BALANCING AUTHORITY (MW).
   Hourly resolution, but a BA is not a dam. Only Douglas PUD maps
   one-to-one (DOPD = Wells). Chelan PUD (CHPD) covers Rocky Reach AND
   Rock Island together; Grant PUD (GCPD) covers Priest Rapids AND
   Wanapum together. Use it for shape, not for per-dam totals.
   https://www.eia.gov/electricity/gridmonitor/

Neither needs an API key -- both are bulk file downloads. (EIA's API v2
would also serve this, but requires registration; these files do not.)

CAVEAT ON COMPARING TO USACE
----------------------------
EIA reports NET generation, after station service. The USACE "Gen" column
is gross output at the plant. They will not agree exactly, and EIA-930 BA
totals also include any non-hydro resources the utility owns. Do not mix
the two into a single series without noting which is which.

Usage examples
--------------
  # Monthly net generation for the five mid-Columbia dams, 2024
  python eia_scraper.py plant-monthly --year 2024 --dams PRD WAN RIS RRH WEL \\
      --out midcol_gen_2024.csv

  # Any plant by EIA plant id
  python eia_scraper.py plant-monthly --year 2024 --plant-ids 3887 3888

  # Hourly BA net generation for the three mid-Columbia utilities
  python eia_scraper.py ba-hourly --bas CHPD GCPD DOPD --year 2025 --half H2
"""

import argparse
import io
import os
import zipfile

import pandas as pd
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; eia-data-fetcher/1.0; "
                  "for research use of EIA's published public data files)"
}

EIA923_CURRENT = "https://www.eia.gov/electricity/data/eia923/xls/f923_{year}.zip"
EIA923_ARCHIVE = "https://www.eia.gov/electricity/data/eia923/archive/xls/f923_{year}.zip"

EIA930_SIX_MONTH = ("https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/"
                    "EIA930_BALANCE_{year}_{months}.csv")
HALVES = {"H1": "Jan_Jun", "H2": "Jul_Dec"}

# Dam code -> EIA plant id, confirmed against the EIA-923 2024 plant frame.
DAM_PLANT_IDS = {
    "PRD": 3887,  # Priest Rapids  (Grant PUD)
    "WAN": 3888,  # Wanapum        (Grant PUD)
    "RIS": 6200,  # Rock Island    (Chelan PUD)
    "RRH": 3883,  # Rocky Reach    (Chelan PUD)
    "WEL": 3886,  # Wells          (Douglas PUD)
}

# Dam code -> balancing authority. Note the many-to-one mapping.
DAM_BALANCING_AUTHORITY = {
    "PRD": "GCPD", "WAN": "GCPD",   # Grant PUD   -- two dams, one BA
    "RIS": "CHPD", "RRH": "CHPD",   # Chelan PUD  -- two dams, one BA
    "WEL": "DOPD",                  # Douglas PUD -- one dam, one BA
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


# ---------------------------------------------------------------------------
# EIA-923: monthly net generation per plant
# ---------------------------------------------------------------------------

def download_eia923(year: int, cache_dir: str = None) -> bytes:
    """
    Fetch the EIA-923 annual zip (~20 MB), caching it if cache_dir is given.

    The most recent year or two live under xls/; older years move to
    archive/xls/. Try both rather than guessing which applies.
    """
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cached = os.path.join(cache_dir, f"f923_{year}.zip")
        if os.path.exists(cached) and os.path.getsize(cached) > 1_000_000:
            with open(cached, "rb") as fh:
                return fh.read()

    last_err = None
    for tmpl in (EIA923_ARCHIVE, EIA923_CURRENT):
        url = tmpl.format(year=year)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=300)
            resp.raise_for_status()
            # A miss returns the HTML landing page rather than a 404.
            if not resp.content.startswith(b"PK"):
                last_err = f"{url} did not return a zip"
                continue
            if cache_dir:
                with open(os.path.join(cache_dir, f"f923_{year}.zip"), "wb") as fh:
                    fh.write(resp.content)
            return resp.content
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Could not download EIA-923 for {year}: {last_err}")


def fetch_plant_monthly(year: int, plant_ids=None, cache_dir: str = None) -> pd.DataFrame:
    """
    Monthly net generation (MWh) per plant, tidied to one row per plant-month.

    plant_ids : iterable[int], optional -- restrict to these EIA plant ids.
    """
    blob = download_eia923(year, cache_dir)
    zf = zipfile.ZipFile(io.BytesIO(blob))
    members = [n for n in zf.namelist() if "Schedules_2_3_4_5" in n and n.endswith((".xlsx", ".xls"))]
    if not members:
        raise RuntimeError(f"No Schedule 2/3/4/5 workbook in the {year} zip: {zf.namelist()}")

    raw = pd.read_excel(io.BytesIO(zf.read(members[0])),
                        sheet_name="Page 1 Generation and Fuel Data", skiprows=5)
    raw.columns = [str(c).strip() for c in raw.columns]

    id_col = next(c for c in raw.columns if c.replace(" ", "").lower() in ("plantid", "plantid."))
    name_col = next(c for c in raw.columns if "Plant Name" in c)
    net_cols = {m: next((c for c in raw.columns
                         if c.lower().replace("\n", " ").strip() == f"netgen {m.lower()}"), None)
                for m in MONTHS}
    missing = [m for m, c in net_cols.items() if c is None]
    if missing:
        raise RuntimeError(f"Could not locate Netgen columns for: {missing}")

    if plant_ids is not None:
        raw = raw[raw[id_col].isin(list(plant_ids))]

    # EIA writes missing values as ".", which makes those columns object dtype;
    # a groupby(...).sum(numeric_only=True) would silently drop them. Coerce first.
    for col in net_cols.values():
        raw[col] = pd.to_numeric(raw[col], errors="coerce")

    # One plant reports several fuel/prime-mover rows; sum them per plant.
    grouped = raw.groupby([id_col, name_col], dropna=False)[list(net_cols.values())].sum()

    rows = []
    for (pid, pname), series in grouped.iterrows():
        for month_num, month in enumerate(MONTHS, start=1):
            rows.append({
                "plant_id": int(pid) if pd.notna(pid) else None,
                "plant_name": pname,
                "year": year,
                "month": month_num,
                "net_generation_mwh": float(series[net_cols[month]]),
                "source": "EIA-923",
            })
    out = pd.DataFrame(rows)

    inverse = {v: k for k, v in DAM_PLANT_IDS.items()}
    out.insert(0, "dam", out["plant_id"].map(inverse))
    return out


# ---------------------------------------------------------------------------
# EIA-930: hourly net generation per balancing authority
# ---------------------------------------------------------------------------

def fetch_ba_hourly(bas, year: int, half: str = "H1", cache_dir: str = None) -> pd.DataFrame:
    """
    Hourly net generation (MW) for the given balancing authorities.

    The six-month file is ~50 MB covering every US BA, so it is streamed and
    filtered to the requested BAs rather than loaded whole.
    """
    if half not in HALVES:
        raise ValueError(f"half must be one of {list(HALVES)}")
    wanted = {b.upper() for b in bas}
    url = EIA930_SIX_MONTH.format(year=year, months=HALVES[half])

    cached = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cached = os.path.join(cache_dir, f"EIA930_BALANCE_{year}_{HALVES[half]}.csv")

    if cached and os.path.exists(cached):
        chunks = pd.read_csv(cached, chunksize=200_000, low_memory=False)
    else:
        resp = requests.get(url, headers=HEADERS, timeout=600)
        resp.raise_for_status()
        if cached:
            with open(cached, "wb") as fh:
                fh.write(resp.content)
        chunks = pd.read_csv(io.BytesIO(resp.content), chunksize=200_000, low_memory=False)

    keep = []
    for chunk in chunks:
        chunk.columns = [str(c).strip() for c in chunk.columns]
        ba_col = next(c for c in chunk.columns if "Balancing Authority" in c)
        keep.append(chunk[chunk[ba_col].isin(wanted)])
    df = pd.concat(keep, ignore_index=True) if keep else pd.DataFrame()
    if df.empty:
        raise RuntimeError(f"No rows for {sorted(wanted)} in {url}")

    ba_col = next(c for c in df.columns if "Balancing Authority" in c)
    gen_col = next(c for c in df.columns if "Net Generation" in c)
    time_col = next(c for c in df.columns if "UTC Time" in c)
    out = pd.DataFrame({
        "balancing_authority": df[ba_col],
        "timestamp_utc": pd.to_datetime(df[time_col], errors="coerce", format="mixed"),
        "net_generation_mw": pd.to_numeric(df[gen_col], errors="coerce"),
        "source": "EIA-930",
    })
    inverse = {}
    for dam, ba in DAM_BALANCING_AUTHORITY.items():
        inverse.setdefault(ba, []).append(dam)
    out["dams_in_ba"] = out["balancing_authority"].map(
        lambda b: "+".join(sorted(inverse.get(b, []))) or None)
    return out.sort_values(["balancing_authority", "timestamp_utc"]).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description="Fetch EIA generation data for hydro plants")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("plant-monthly", help="EIA-923 monthly net generation per plant")
    pm.add_argument("--year", type=int, required=True)
    pm.add_argument("--dams", nargs="+", default=None,
                    help=f"dam codes, any of {sorted(DAM_PLANT_IDS)}")
    pm.add_argument("--plant-ids", nargs="+", type=int, default=None)
    pm.add_argument("--cache-dir", default=None)
    pm.add_argument("--out", default=None)

    bh = sub.add_parser("ba-hourly", help="EIA-930 hourly net generation per balancing authority")
    bh.add_argument("--bas", nargs="+", default=["CHPD", "GCPD", "DOPD"])
    bh.add_argument("--year", type=int, required=True)
    bh.add_argument("--half", default="H1", choices=list(HALVES))
    bh.add_argument("--cache-dir", default=None)
    bh.add_argument("--out", default=None)

    args = ap.parse_args()

    if args.cmd == "plant-monthly":
        ids = args.plant_ids
        if args.dams:
            ids = (ids or []) + [DAM_PLANT_IDS[d.upper()] for d in args.dams]
        df = fetch_plant_monthly(args.year, ids, args.cache_dir)
        out = args.out or f"eia923_plant_monthly_{args.year}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} rows to {out}")
        print(df.head(12))

    elif args.cmd == "ba-hourly":
        df = fetch_ba_hourly(args.bas, args.year, args.half, args.cache_dir)
        out = args.out or f"eia930_ba_hourly_{args.year}_{args.half}.csv"
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} rows to {out}")
        print(df.head())


if __name__ == "__main__":
    main()
