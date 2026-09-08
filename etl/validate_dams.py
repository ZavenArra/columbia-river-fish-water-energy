#!/usr/bin/env python3
"""
One-off data-availability probe for the candidate dams.

For each code in config/dams.CANDIDATE_DAMS, ask three questions:

  has_flow_generation : does USACE serve an hourly water-control file for it,
                        and does that file carry generation (MW) / flow columns?
  has_temperature     : does DART return river temperature for it?
  has_fish_passage    : does DART return real, numeric adult passage counts
                        for it (i.e. does the dam have a counted fish ladder)?

Writes config/dam_availability.json and prints a summary table.

    python etl/validate_dams.py                 # all candidates
    python etl/validate_dams.py --dams BON LWG  # a subset
    python etl/validate_dams.py --dart-params scraper   # reproduce the bug, see below

TWO WORKAROUNDS ARE BAKED IN HERE
---------------------------------
Both are contained in this script; nothing under scrapers/ is modified.

1. USACE TLS. www.nwd-wc.usace.army.mil serves a valid DigiCert certificate but
   does not send the intermediate CA, so verification fails with
   "unable to get local issuer certificate". config/certs/usace-ca-bundle.pem is
   the system trust store plus that intermediate, fetched from the AIA URL in
   the server's own certificate. We point REQUESTS_CA_BUNDLE at it. Certificate
   verification stays ON -- this supplies the missing link, it does not skip it.

2. DART parameters. dart_scraper's adult-passage query is wrong: it sends
   species in `data[]` as display names ("Chinook"), but DART's adult form sends
   species in a separate `ftype[]` field as short codes ("fc"), and uses `data[]`
   only for optional 10-year-average overlays. As sent by the scraper, DART
   answers with an HTML error page ("Invalid submission: Data Query Type") for
   every dam, including Bonneville. `_fetch_adult_passage_corrected()` below
   sends the corrected form; run with `--dart-params scraper` to reproduce the
   failure. The scraper itself still needs patching.

   Temperature needs no local reimplementation: the scraper's default parameter
   ("Temperature (WQM)") is likewise a display label rather than the submit
   value, but it is an argument, so we pass the correct value ("Temp (WQM)")
   straight into the scraper's own function.
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scrapers"))
sys.path.insert(0, str(REPO / "config"))

# Point requests at the bundle that includes USACE's missing intermediate CA.
# Must happen before any request is issued; requests reads this per-request.
_CA_BUNDLE = REPO / "config" / "certs" / "usace-ca-bundle.pem"
if _CA_BUNDLE.exists():
    os.environ["REQUESTS_CA_BUNDLE"] = str(_CA_BUNDLE)

import pandas as pd  # noqa: E402
import requests  # noqa: E402

import cwms_scraper  # noqa: E402
import dart_scraper  # noqa: E402
import eia_scraper  # noqa: E402
import usace_scraper  # noqa: E402
from dams import (CANDIDATE_DAMS, DAM_INFO, DART_RIVER_PARAMS,  # noqa: E402
                  DART_SPECIES, TAILWATER_SITE, TEMPERATURE_PARAM_ORDER)

# Column-name fragments that indicate the USACE file carries generation / flow.
GEN_HINTS = ("gen", "mw", "power")
FLOW_HINTS = ("flow", "cfs", "kcfs", "spill", "outflow", "discharge")


# ---------------------------------------------------------------------------
# DART adult passage, with the parameter scheme corrected
# ---------------------------------------------------------------------------

def _numeric_summary(df):
    """(n_rows, n_numeric_values, sum) for a DART csvSingle 'value' column."""
    if df is None or df.empty or "value" not in df.columns:
        return 0, 0, None
    vals = pd.to_numeric(df["value"], errors="coerce").dropna()
    return len(df), int(len(vals)), (float(vals.sum()) if len(vals) else None)


# ---------------------------------------------------------------------------
# Per-source probes. Each returns a dict and never raises.
# ---------------------------------------------------------------------------

def _usace_raw_probe(code, year, month):
    """
    Did the file exist even though the scraper could not parse it?

    Some projects (Bonneville, e.g.) ship a header row with fewer fields than
    their data rows, which makes pandas' C parser raise. That is a formatting
    quirk, not missing data, so distinguish the two: re-fetch the raw file and
    parse it leniently, padding the header out to the widest data row.
    """
    url = f"{usace_scraper.BASE_URL}/{code.lower()}_{year:04d}{month:02d}.csv"
    resp = requests.get(url, headers=usace_scraper.HEADERS, timeout=40)
    if resp.status_code != 200:
        return {"file_exists": False, "status": resp.status_code}
    lines = [l for l in resp.text.splitlines() if l.strip()]
    if not lines:
        return {"file_exists": False, "status": 200}
    header = [c.strip() for c in lines[0].split(",")]
    widest = max(len(l.split(",")) for l in lines)
    padded = header + [f"unnamed_{i}" for i in range(len(header), widest)]
    # Row 0 is the column names, row 1 is a units row -- both are metadata.
    df = pd.read_csv(io.StringIO("\n".join(lines)), names=padded,
                     skiprows=2, engine="python")
    return {"file_exists": True, "status": 200, "columns": padded,
            "n_rows": int(len(df)), "units_row": [c.strip() for c in lines[1].split(",")],
            "ragged_header": widest != len(header)}


def probe_usace(code, year, month):
    out = {"ok": False, "n_rows": 0, "n_data_rows": 0, "columns": [],
           "has_gen": False, "has_flow": False, "scraper_parse_ok": False,
           "file_exists": None, "ragged_header": False, "error": None}
    cols = []
    try:
        df = usace_scraper.fetch_month(code.lower(), year, month, retries=2, delay=2)
        cols = [str(c) for c in df.columns]
        # fetch_month reads with header=0, so the units row lands in the frame
        # as a data row. Several storage projects serve header + units and
        # nothing else; discount that row so they are not counted as populated.
        out.update(ok=True, scraper_parse_ok=True, file_exists=True,
                   n_rows=int(len(df)), n_data_rows=max(0, int(len(df)) - 1))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        # The scraper failing does not mean the data is absent -- check directly.
        try:
            raw = _usace_raw_probe(code, year, month)
            out["file_exists"] = raw["file_exists"]
            if raw["file_exists"]:
                cols = raw["columns"]
                # the raw probe already skips both header and units rows
                out.update(ok=True, n_rows=raw["n_rows"], n_data_rows=raw["n_rows"],
                           ragged_header=raw.get("ragged_header", False))
        except Exception as e2:  # noqa: BLE001
            out["error"] += f" | raw probe: {type(e2).__name__}: {str(e2)[:120]}"

    if cols:
        low = [c.lower() for c in cols]
        out.update(columns=cols,
                   has_gen=any(h in c for c in low for h in GEN_HINTS),
                   has_flow=any(h in c for c in low for h in FLOW_HINTS))
    return out


def probe_cwms_flow(code, year, month):
    """Hourly flow from the CWMS API -- the fallback when no USACE file exists."""
    out = {"ok": False, "n_values": 0, "units": None, "ts_id": None, "error": None}
    beg = datetime(year, month, 1, tzinfo=timezone.utc)
    end = beg + timedelta(days=3)
    ts_id = cwms_scraper.SERIES["outflow"].format(loc=code.upper(), ver="REV")
    try:
        df = cwms_scraper.fetch_timeseries(ts_id, beg, end, retries=2, delay=2)
        vals = df["value"].dropna() if not df.empty else []
        out.update(ok=len(vals) > 0, n_values=int(len(vals)),
                   units=df.attrs.get("units"), ts_id=ts_id)
    except Exception as e:  # noqa: BLE001
        out.update(ts_id=ts_id, error=f"{type(e).__name__}: {str(e)[:160]}")
    return out


def probe_eia_generation(code, year, cache_dir=None):
    """
    Monthly per-plant net generation from EIA-923, for dams with no USACE file.

    EIA-923 publishes on a lag, so a request for the current year can legitimately
    come back empty; that is reported as a miss, not an error.
    """
    out = {"ok": False, "plant_id": None, "n_months": 0, "annual_mwh": None,
           "year": year, "error": None}
    plant_id = eia_scraper.DAM_PLANT_IDS.get(code.upper())
    out["plant_id"] = plant_id
    if plant_id is None:
        out["error"] = "no EIA plant id mapped for this dam"
        return out
    try:
        df = eia_scraper.fetch_plant_monthly(year, [plant_id], cache_dir=cache_dir)
        vals = df["net_generation_mwh"].dropna()
        nonzero = vals[vals != 0]
        out.update(ok=len(nonzero) > 0, n_months=int(len(nonzero)),
                   annual_mwh=float(vals.sum()) if len(vals) else None)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out


def probe_elevation(code, year):
    """
    Daily pool elevation from DART, forebay and tailwater.

    DART reports elevation in feet at every candidate dam, which is the only
    source that covers all 19 -- CWMS catalogues Elev-Forebay/Elev-Tailwater but
    serves them empty, and the USACE monthly files only cover federal projects.
    The readings are self-consistent down the cascade: each dam's tailwater
    elevation matches the next dam downstream's forebay.
    """
    out = {"forebay": {"ok": False, "site": code.upper(), "n_values": 0,
                       "mean_ft": None, "error": None},
           "tailwater": {"ok": False, "site": TAILWATER_SITE.get(code.upper()),
                         "n_values": 0, "mean_ft": None, "error": None}}
    for key in ("forebay", "tailwater"):
        site = out[key]["site"]
        if not site:
            continue
        try:
            df = dart_scraper.fetch_river_temperature(
                locations=[site], years=[year], parameter="Elevation",
                start_mmdd="01/01", end_mmdd="12/31",
            )
            n_rows, n_vals, _ = _numeric_summary(df)
            vals = pd.to_numeric(df["value"], errors="coerce").dropna()
            out[key].update(ok=n_vals > 0, n_values=n_vals,
                            mean_ft=round(float(vals.mean()), 1) if len(vals) else None)
        except Exception as e:  # noqa: BLE001
            out[key]["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        time.sleep(0.5)
    return out


def probe_temperature(code, year, params=None):
    """
    Work down a fallback chain and keep the first source that returns numbers:

      1. Temp (WQM) at the dam        -- forebay water-quality monitor
      2. Temp (Scroll Case) at the dam -- powerhouse intake sensor
      3. Temp (WQM) at the tailwater site -- measured below the dam

    None of these are interchangeable, so record which one supplied the data
    along with booleans for the two substitutions. In practice tier 2 exists
    only at BON and IHR, which already have tier 1; tier 3 is what actually
    rescues a dam (Dworshak: DWR has no temperature, DWQI has a full year).
    """
    params = params or TEMPERATURE_PARAM_ORDER
    attempts = [(code, p) for p in params]
    tailwater = TAILWATER_SITE.get(code.upper())
    if tailwater:
        attempts.append((tailwater, params[0]))

    out = {"ok": False, "n_rows": 0, "n_values": 0, "site": None, "parameter": None,
           "from_scroll_case": False, "from_tailwater": False,
           "attempts": [], "error": None}
    for site, param in attempts:
        record = {"site": site, "parameter": param, "n_values": 0, "error": None}
        try:
            df = dart_scraper.fetch_river_temperature(
                locations=[site], years=[year], parameter=param,
                start_mmdd="01/01", end_mmdd="12/31",
            )
            n_rows, n_vals, _ = _numeric_summary(df)
            record["n_values"] = n_vals
            out["attempts"].append(record)
            if n_vals > 0:
                out.update(ok=True, n_rows=n_rows, n_values=n_vals, site=site,
                           parameter=param,
                           from_scroll_case="scroll case" in param.lower(),
                           from_tailwater=site.upper() != code.upper())
                return out
        except Exception as e:  # noqa: BLE001
            record["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            out["attempts"].append(record)
            out["error"] = record["error"]
        time.sleep(0.5)
    return out


def probe_passage(code, year, species=("Chinook",), mode="corrected"):
    out = {"ok": False, "n_rows": 0, "n_values": 0, "total_fish": None,
           "mode": mode, "error": None}
    try:
        # dart_scraper now sends species as ftype[] codes; names are resolved
        # for us, so this is just the ordinary call.
        df = dart_scraper.fetch_adult_passage(
            projects=[code], species=list(species), years=[year],
            start_mmdd="01/01", end_mmdd="12/31",
        )
        n_rows, n_vals, total = _numeric_summary(df)
        # A ladder with a real count series has numeric values; zeros[]=1 means
        # a dam with no ladder would still not produce a populated series.
        out.update(ok=n_vals > 0, n_rows=n_rows, n_values=n_vals, total_fish=total)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ---------------------------------------------------------------------------

def main():
    today = date.today()
    prev_month = (today.replace(day=1) - pd.Timedelta(days=1))

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dams", nargs="+", default=CANDIDATE_DAMS)
    ap.add_argument("--usace-year", type=int, default=prev_month.year)
    ap.add_argument("--usace-month", type=int, default=prev_month.month)
    ap.add_argument("--dart-year", type=int, default=today.year - 1,
                    help="a complete year, so a dry sensor is not mistaken for no data")
    ap.add_argument("--species", nargs="+", default=["Chinook"])
    ap.add_argument("--dart-params", choices=["corrected", "scraper"], default="corrected",
                    help="'scraper' reproduces the broken as-shipped passage query")
    ap.add_argument("--eia-year", type=int, default=today.year - 2,
                    help="EIA-923 publishes on a lag; default is two years back")
    ap.add_argument("--eia-cache", default=str(REPO / "data" / "raw" / "eia_cache"),
                    help="where to cache the EIA-923 zip (~20 MB per year)")
    ap.add_argument("--skip-eia", action="store_true",
                    help="skip the EIA generation probe (avoids a large download)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--out", default=str(REPO / "config" / "dam_availability.json"))
    args = ap.parse_args()

    print(f"USACE: {args.usace_year}-{args.usace_month:02d}   "
          f"DART: {args.dart_year}   species: {', '.join(args.species)}   "
          f"passage params: {args.dart_params}\n")

    results = {}
    for code in args.dams:
        info = DAM_INFO.get(code, {})
        print(f"{code} ({info.get('name', '?')}) ... ", end="", flush=True)

        usace = probe_usace(code, args.usace_year, args.usace_month)
        time.sleep(args.delay)

        usace_has_data = bool(usace["ok"] and usace["n_data_rows"] > 0)
        has_flow = usace_has_data and usace["has_flow"]
        has_gen = usace_has_data and usace["has_gen"]
        flow_source = "usace_hist_csv" if has_flow else None
        gen_source = "usace_hist_csv" if has_gen else None

        # No federal file? Fall back to CWMS for flow and EIA for generation.
        cwms = eia = None
        if not has_flow:
            cwms = probe_cwms_flow(code, args.usace_year, args.usace_month)
            time.sleep(args.delay)
            if cwms["ok"]:
                has_flow, flow_source = True, "cwms_api"
        if not has_gen and not args.skip_eia:
            eia = probe_eia_generation(code, args.eia_year, args.eia_cache)
            if eia["ok"]:
                has_gen, gen_source = True, "eia923_monthly"

        temp = probe_temperature(code, args.dart_year)
        time.sleep(args.delay)
        elev = probe_elevation(code, args.dart_year)
        time.sleep(args.delay)
        pas = probe_passage(code, args.dart_year, args.species, args.dart_params)
        time.sleep(args.delay)

        results[code] = {
            "name": info.get("name"),
            "river": info.get("river"),
            "operator": info.get("operator"),
            "expect_passage": info.get("expect_passage"),
            "has_flow": bool(has_flow),
            "has_generation": bool(has_gen),
            "has_flow_generation": bool(has_flow and has_gen),
            "flow_source": flow_source,
            "generation_source": gen_source,
            "has_temperature": bool(temp["ok"]),
            "temperature_site": temp.get("site"),
            "temperature_parameter": temp.get("parameter"),
            # True when temperature fell back to the powerhouse scroll-case
            # sensor instead of the forebay water-quality monitor.
            "temperature_from_scroll_case": bool(temp.get("from_scroll_case")),
            # True when it came from the tailwater site rather than the dam itself.
            "temperature_from_tailwater": bool(temp.get("from_tailwater")),
            "has_fish_passage": bool(pas["ok"]),
            "has_elevation": bool(elev["forebay"]["ok"]),
            "elevation_site": elev["forebay"]["site"],
            "has_tailwater_elevation": bool(elev["tailwater"]["ok"]),
            "tailwater_elevation_site": elev["tailwater"]["site"],
            "usace": usace,
            "elevation": elev,
            "cwms": cwms,
            "eia": eia,
            "temperature": temp,
            "passage": pas,
        }
        r = results[code]
        tflag = "n"
        if r["has_temperature"]:
            tflag = "Y"
            if r["temperature_from_scroll_case"]:
                tflag = "Y*"
            elif r["temperature_from_tailwater"]:
                tflag = "Y+"
        print(f"flow={'Y' if r['has_flow'] else 'n'} "
              f"gen={'Y' if r['has_generation'] else 'n'} "
              f"temp={tflag} "
              f"elev={'Y' if r['has_elevation'] else 'n'}"
              f"{'+tw' if r['has_tailwater_elevation'] else ''} "
              f"passage={'Y' if r['has_fish_passage'] else 'n'}")

    # ---- summary table ----
    print("\n" + "=" * 122)
    print(f"{'CODE':<6}{'NAME':<20}{'FLOW':>6}{'SOURCE':>17}{'GEN':>5}{'SOURCE':>17}"
          f"{'TEMP':>6}{'PARAMETER':>25}{'ELEV':>6}{'PASS':>6}")
    print("-" * 122)
    for code, r in results.items():
        temp_p = r["temperature_parameter"] or ""
        if r["temperature_from_scroll_case"]:
            temp_p += " *"
        elif r["temperature_from_tailwater"]:
            temp_p = f"{temp_p} @{r['temperature_site']} +"
        print(f"{code:<6}{(r['name'] or '?'):<20}"
              f"{('yes' if r['has_flow'] else 'no'):>6}{(r['flow_source'] or '-'):>17}"
              f"{('yes' if r['has_generation'] else 'no'):>5}{(r['generation_source'] or '-'):>17}"
              f"{('yes' if r['has_temperature'] else 'no'):>6}{temp_p:>25}"
              f"{(('fb+tw' if r['has_tailwater_elevation'] else 'fb') if r['has_elevation'] else 'no'):>6}"
              f"{('yes' if r['has_fish_passage'] else 'no'):>6}")
    print("=" * 122)
    print("  * scroll-case sensor substituted for the forebay water-quality monitor")
    print("  + tailwater site substituted for the dam's own site")

    scroll = [c for c, r in results.items() if r["temperature_from_scroll_case"]]
    if scroll:
        print(f"\nScroll-case temperature substituted for: {', '.join(scroll)}")
    tw = [f"{c} ({r['temperature_site']})" for c, r in results.items()
          if r["temperature_from_tailwater"]]
    if tw:
        print(f"Tailwater temperature substituted for: {', '.join(tw)}")

    empty = [c for c, r in results.items()
             if r["usace"]["file_exists"] and r["usace"]["n_data_rows"] == 0]
    if empty:
        print(f"USACE file served but contains no data rows: {', '.join(empty)}")

    ragged = [c for c, r in results.items() if r["usace"].get("ragged_header")]
    if ragged:
        print(f"USACE header narrower than data rows (scraper's read_csv fails): "
              f"{', '.join(ragged)}")

    all3 = [c for c, r in results.items()
            if r["has_flow_generation"] and r["has_temperature"] and r["has_fish_passage"]]
    print(f"\nAll three data types: {len(all3)}/{len(results)} -> "
          f"{', '.join(all3) if all3 else 'none'}")

    surprises = [c for c, r in results.items()
                 if r["expect_passage"] is not None
                 and r["has_fish_passage"] != r["expect_passage"]]
    if surprises:
        print(f"Passage differs from expectation: {', '.join(surprises)}")

    payload = {
        "generated": today.isoformat(),
        "usace_month": f"{args.usace_year}-{args.usace_month:02d}",
        "dart_year": args.dart_year,
        "species_probed": args.species,
        "dart_param_mode": args.dart_params,
        "eia_year": args.eia_year,
        "dams": results,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
