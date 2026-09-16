"""
Candidate dams to check for data availability.

These are the Columbia/Snake River projects we might want in the dashboard.
Not all of them have all three data types — storage dams have no fish ladders,
and the mid-Columbia PUD dams are not USACE-operated, so they may not appear in
the USACE water-control archive. Which codes actually resolve is determined
empirically by `etl/validate_dams.py`, which writes `config/dam_availability.json`.

Site codes are the ones DART uses; USACE uses the same codes lowercased in its
file names. That correspondence is exactly what the validation script tests --
do not assume it holds for a dam until it appears in dam_availability.json.
"""

# The codes to check, in rough downstream -> upstream / mainstem -> tributary order.
CANDIDATE_DAMS = [
    # Lower Columbia (USACE, all with fish ladders)
    "BON",  # Bonneville
    "TDA",  # The Dalles
    "JDA",  # John Day
    "MCN",  # McNary
    # Lower Snake (USACE, all with fish ladders)
    "IHR",  # Ice Harbor
    "LGS",  # Little Goose
    "LMN",  # Lower Monumental
    "LWG",  # Lower Granite
    # Mid-Columbia (public utility district dams, with fish ladders)
    "PRD",  # Priest Rapids
    "WAN",  # Wanapum
    "RIS",  # Rock Island
    "RRH",  # Rocky Reach
    "WEL",  # Wells
    # Storage / upper-system projects (expected: no fish passage)
    "GCL",  # Grand Coulee
    "CHJ",  # Chief Joseph
    "DWR",  # Dworshak
    "LIB",  # Libby
    "HGH",  # Hungry Horse
    "ALF",  # Albeni Falls
]

# Reference metadata. `expect_passage` records our prior belief only; the
# validation script measures the truth rather than trusting this.
DAM_INFO = {
    "BON": {"name": "Bonneville",        "river": "Columbia",   "operator": "USACE",       "expect_passage": True},
    "TDA": {"name": "The Dalles",        "river": "Columbia",   "operator": "USACE",       "expect_passage": True},
    "JDA": {"name": "John Day",          "river": "Columbia",   "operator": "USACE",       "expect_passage": True},
    "MCN": {"name": "McNary",            "river": "Columbia",   "operator": "USACE",       "expect_passage": True},
    "IHR": {"name": "Ice Harbor",        "river": "Snake",      "operator": "USACE",       "expect_passage": True},
    "LGS": {"name": "Little Goose",      "river": "Snake",      "operator": "USACE",       "expect_passage": True},
    "LMN": {"name": "Lower Monumental",  "river": "Snake",      "operator": "USACE",       "expect_passage": True},
    "LWG": {"name": "Lower Granite",     "river": "Snake",      "operator": "USACE",       "expect_passage": True},
    "PRD": {"name": "Priest Rapids",     "river": "Columbia",   "operator": "Grant PUD",   "expect_passage": True},
    "WAN": {"name": "Wanapum",           "river": "Columbia",   "operator": "Grant PUD",   "expect_passage": True},
    "RIS": {"name": "Rock Island",       "river": "Columbia",   "operator": "Chelan PUD",  "expect_passage": True},
    "RRH": {"name": "Rocky Reach",       "river": "Columbia",   "operator": "Chelan PUD",  "expect_passage": True},
    "WEL": {"name": "Wells",             "river": "Columbia",   "operator": "Douglas PUD", "expect_passage": True},
    "GCL": {"name": "Grand Coulee",      "river": "Columbia",   "operator": "USBR",        "expect_passage": False},
    "CHJ": {"name": "Chief Joseph",      "river": "Columbia",   "operator": "USACE",       "expect_passage": False},
    "DWR": {"name": "Dworshak",          "river": "Clearwater", "operator": "USACE",       "expect_passage": False},
    "LIB": {"name": "Libby",             "river": "Kootenai",   "operator": "USACE",       "expect_passage": False},
    "HGH": {"name": "Hungry Horse",      "river": "S. Fk. Flathead", "operator": "USBR",   "expect_passage": False},
    "ALF": {"name": "Albeni Falls",      "river": "Pend Oreille","operator": "USACE",      "expect_passage": False},
}

# DART species codes for the adult-passage query (`ftype[]`), read off the
# DART adult query form. NOTE: these are short codes, not display names.
DART_SPECIES = {
    "Chinook": "fc",
    "Jack-Chinook": "fcj",
    "Coho": "fk",
    "Jack-Coho": "fkj",
    "Sockeye": "fb",
    "Steelhead": "fs",
    "Steelhead-Wild": "fsw",
    "Bull Trout": "ft",
    "Lamprey (daytime)": "fl",
    "Lamprey Nighttime": "flnt",
    "Lamprey LPS": "flps",
    "Shad": "fa",
    "Chum": "fe",
    "Pink": "fp",
}

# DART river-environment parameter values (`data[]`). These are the submit
# values, which differ from the labels shown on the DART form -- e.g. the form
# displays "Temperature (WQM)" but submits "Temp (WQM)".
DART_RIVER_PARAMS = {
    "Temperature (WQM)": "Temp (WQM)",
    "Temperature (Scroll Case)": "Temp (Scroll Case)",
    "Dissolved Gas": "Dissolved Gas",
    "Dissolved Gas Percent": "Dissolved Gas Percent",
    "Outflow": "Outflow",
    "Inflow": "Inflow",
    "Spill": "Spill",
    "Spill Percent": "Spill Percent",
    "Elevation": "Elevation",
    "Turbidity": "Turbidity",
    "Barometric Pressure": "Barometric Pressure",
}


# ---------------------------------------------------------------------------
# Where each measurement comes from
# ---------------------------------------------------------------------------
# Determined empirically -- see etl/validate_dams.py and dam_availability.json.
#
# Flow and generation come from different places depending on who runs the dam:
#
#   federal (USACE) projects   -> usace_scraper: one monthly CSV carries both
#                                 flow and generation (MW), hourly.
#   mid-Columbia PUD projects  -> no USACE file exists. Flow comes from
#                                 cwms_scraper (USACE CWMS API, hourly);
#                                 generation from eia_scraper, because CWMS
#                                 lists a Power.Total series for them but
#                                 serves it empty.
#
# Generation for the PUD dams forces a resolution/attribution trade-off:
#   EIA-923 -> exact per-dam, but monthly
#   EIA-930 -> hourly, but per balancing authority, and only Wells maps 1:1
FLOW_SOURCE = {
    **{d: "usace_hist_csv" for d in
       ["BON", "TDA", "JDA", "MCN", "IHR", "LGS", "LMN", "LWG"]},
    **{d: "cwms_api" for d in ["PRD", "WAN", "RIS", "RRH", "WEL"]},
}

GENERATION_SOURCE = {
    **{d: "usace_hist_csv" for d in
       ["BON", "TDA", "JDA", "MCN", "IHR", "LGS", "LMN", "LWG"]},
    **{d: "eia923_monthly" for d in ["PRD", "WAN", "RIS", "RRH", "WEL"]},
}

# Temperature parameters to try, in order. Storage projects often report a
# scroll-case reading when no water-quality monitor is installed, so fall back
# to it rather than recording the dam as having no temperature at all.
TEMPERATURE_PARAM_ORDER = ["Temp (WQM)", "Temp (Scroll Case)"]

# DART tailwater / downstream site codes, for dams whose forebay site has no
# temperature sensor. Dworshak is the case that matters: DWR (forebay) returns
# no temperature at all, but DWQI (tailwater) has a full year of it. Tailwater
# temperature is measured below the dam rather than in the pool, so it is a
# substitute worth flagging, not an equivalent.
TAILWATER_SITE = {
    "BON": "WRNO",  # Warrendale OR, downstream of Bonneville
    "TDA": "TDDO",
    "JDA": "JHAW",
    "MCN": "MCPW",
    "IHR": "IDSW",
    "LGS": "LGSW",
    "LMN": "LMNW",
    "LWG": "LGNW",
    "PRD": "PRXW",
    "WAN": "WANW",
    "RIS": "RIGW",
    "RRH": "RRDW",
    "WEL": "WELW",
    "GCL": "GCGW",
    "CHJ": "CHQW",
    "DWR": "DWQI",
    "HGH": "HGHM",
}

# DART abbreviates species in the `parameter` column of its passage CSV. Map
# those abbreviations back to the names used in SPECIES_CODES / the database.
# Confirmed against a live all-species query at Bonneville.
DART_LABEL_TO_SPECIES = {
    "Chin": "Chinook",
    "JChin": "Jack-Chinook",
    "Coho": "Coho",
    "JCoho": "Jack-Coho",
    "Sock": "Sockeye",
    "Stlhd": "Steelhead",
    "WStlhd": "Steelhead-Wild",
    "BTrout": "Bull Trout",
    "Lmpry": "Lamprey (daytime)",
    "LmpryNight": "Lamprey Nighttime",
    "LmpryLPS": "Lamprey LPS",
    "Shad": "Shad",
    "Chum": "Chum",
    "Pink": "Pink",
}


# ---------------------------------------------------------------------------
# Geographic order, for the dashboard
# ---------------------------------------------------------------------------
# Run-of-river dams with fish ladders, ordered as a fish encounters them:
# up the Columbia mainstem from the ocean, then up the Snake from its
# confluence with the Columbia (which is just below McNary).
#
# river_mile is measured from each river's own mouth, so the Snake numbers
# restart at the confluence rather than continuing the Columbia's.
COLUMBIA_MAINSTEM = ["BON", "TDA", "JDA", "MCN", "PRD", "WAN", "RIS", "RRH", "WEL"]
SNAKE_MAINSTEM = ["IHR", "LMN", "LGS", "LWG"]

# The dam list the monthly page offers. Storage projects (Grand Coulee, Chief
# Joseph, Dworshak, Libby, Hungry Horse, Albeni Falls) are deliberately absent:
# they have no fish ladders, so two of the three measures would always be blank.
MONTHLY_DAM_ORDER = COLUMBIA_MAINSTEM + SNAKE_MAINSTEM

RIVER_MILE = {
    "BON": 146, "TDA": 192, "JDA": 216, "MCN": 292,
    "PRD": 397, "WAN": 415, "RIS": 453, "RRH": 474, "WEL": 516,
    "IHR": 10, "LMN": 41, "LGS": 70, "LWG": 107,
}

# Which river each dam sits on, for the schematic.
RIVER_OF = {**{d: "Columbia" for d in COLUMBIA_MAINSTEM},
            **{d: "Snake" for d in SNAKE_MAINSTEM}}


# The migration corridor a Snake River salmon actually climbs: the four lower
# Columbia dams, then the four lower Snake dams above the confluence. Listed
# downstream to upstream, which is the order a fish meets them.
MIGRATION_CORRIDOR = ["BON", "TDA", "JDA", "MCN", "IHR", "LMN", "LGS", "LWG"]
