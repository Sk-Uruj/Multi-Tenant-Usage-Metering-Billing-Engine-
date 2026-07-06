"""
config.py — STRATA configuration

Everything in this file is computed ONCE when the module first loads,
and never changes again for the lifetime of the running server. That's
the defining trait of "config": one-time setup values, not per-request
logic.

This module has NO dependency on FastAPI itself — it's pure Python
constants and environment loading. Every other module in the project
imports from here rather than defining its own copy of these values,
which is what keeps TIER_DIRS, TIER_RATES, etc. consistent everywhere
they're used (main.py, helpers.py, the route modules, and even
tiering_engine.py / billing_engine.py independently read the same
USD_TO_INR environment variable for the same reason).
"""

import os
import secrets

from dotenv import load_dotenv
load_dotenv()  # reads .env file in the project root, if present


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_NAME = "cloud_storage.db"


# ---------------------------------------------------------------------------
# Session signing secret
# ---------------------------------------------------------------------------
# SESSION_SECRET is read from the SESSION_SECRET environment variable
# (typically set via a .env file — see .env.example for the format).
# If it's not set anywhere, we generate a random secret for THIS RUN ONLY
# and print a loud warning — every server restart would otherwise
# invalidate all existing sessions, forcing everyone to log in again.
# Always set SESSION_SECRET explicitly outside of local development.
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print(
        "\n  [WARNING] SESSION_SECRET not set in environment or .env file.\n"
        "  Generated a temporary random secret for this run only —\n"
        "  all existing sessions will be invalidated on next restart.\n"
        "  Set SESSION_SECRET in a .env file to fix this. See .env.example.\n"
    )


# ---------------------------------------------------------------------------
# Archive retrieval latency
# ---------------------------------------------------------------------------
# AWS S3 Glacier / Azure Archive tier both have real rehydration delays
# before an archived object becomes downloadable again (hours, in production).
# This simulates that wait on a demo-friendly timescale.
ARCHIVE_RETRIEVAL_DELAY_SECS = 6


# ---------------------------------------------------------------------------
# Storage tier directories
# ---------------------------------------------------------------------------
# Physical on-disk locations for each tier. Files actually live at:
#   {TIER_DIRS[tier]}/{username}/{bucket}/{filename}
TIER_DIRS = {
    "HOT":     "storage/hot",
    "COOL":    "storage/cool",
    "COLD":    "storage/cold",
    "ARCHIVE": "storage/archive",
}


# ---------------------------------------------------------------------------
# Billing rates — USD base values, converted to INR below
# ---------------------------------------------------------------------------

# Storage rates: cost per MB per second, by tier
TIER_RATES = {
    "HOT":     0.001000,
    "COOL":    0.000400,
    "COLD":    0.000200,
    "ARCHIVE": 0.000050,
}

# Visual styling for each tier, used by the dashboard/invoice templates
TIER_STYLES = {
    "HOT":     {"bg": "#FF2D6B", "color": "#fff"},
    "COOL":    {"bg": "#0057FF", "color": "#fff"},
    "COLD":    {"bg": "#00FFD1", "color": "#000"},
    "ARCHIVE": {"bg": "#888",    "color": "#fff"},
}

# Request rates: cost per single API operation, by class
# Class A = write operations (uploads, bucket creation)
# Class B = read operations (listings, downloads)
# FREE    = delete operations
REQUEST_RATES = {
    "A":    0.005  / 1000,
    "B":    0.0004 / 1000,
    "FREE": 0.0,
}

# IBM COS egress rate: $0.0087/GB = $0.0087/1024 per MB. Ingress is free.
BANDWIDTH_RATE_PER_MB = 0.0087 / 1024


# ---------------------------------------------------------------------------
# Currency conversion — INR
# ---------------------------------------------------------------------------
# All rates above are defined in USD internally, then converted to INR
# ONCE at startup using this multiplier. Set the USD_TO_INR environment
# variable to override the default.
USD_TO_INR = float(os.getenv("USD_TO_INR", "83.5"))

for _k in list(TIER_RATES.keys()):
    TIER_RATES[_k] = round(TIER_RATES[_k] * USD_TO_INR, 9)

for _k in list(REQUEST_RATES.keys()):
    REQUEST_RATES[_k] = REQUEST_RATES[_k] * USD_TO_INR

BANDWIDTH_RATE_PER_MB = BANDWIDTH_RATE_PER_MB * USD_TO_INR
