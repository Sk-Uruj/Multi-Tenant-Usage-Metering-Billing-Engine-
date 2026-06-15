"""
STRATA v0.9.1 — billing_engine.py
Three billing dimensions — exact IBM COS model:

  1. Storage charges   — file_size_mb × seconds_in_tier × tier_rate
    2. Request charges   — Class A (converted from USD) / Class B (converted from USD) / FREE
    3. Bandwidth charges — egress MB × (IBM COS USD rate converted to INR)  |  ingress = FREE

Run with:
    python billing_engine.py
"""

import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime

DB_NAME            = "cloud_storage.db"
POLL_INTERVAL_SECS = 10

# Storage rates per MB per second
RATES = {
    "HOT":     0.001000,
    "COOL":    0.000400,
    "COLD":    0.000200,
    "ARCHIVE": 0.000050,
}
TIER_ORDER = ["HOT", "COOL", "COLD", "ARCHIVE"]

# Request rates per single operation
REQUEST_RATES = {
    "A":    0.005  / 1000,   # USD base: 0.005 per 1,000 write ops (converted to INR at startup)
    "B":    0.0004 / 1000,   # USD base: 0.0004 per 1,000 read ops (converted to INR at startup)
    "FREE": 0.0,
}

# IBM COS egress rate (USD base): 0.0087/GB == 0.0087/1024 per MB — converted to INR at startup
# Source: cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-billing
# BANDWIDTH_RATE_PER_MB (USD base) = 0.0087 / 1024   # ~0.0000087 per MB (converted to INR at startup)
# Define the USD-base bandwidth rate per MB before converting to INR
BANDWIDTH_RATE_PER_MB = 0.0087 / 1024
# Ingress is always FREE (matches IBM COS, AWS S3, Azure Blob)

# Currency conversion: convert USD rates to INR for display and billing.
# Override with environment variable USD_TO_INR (e.g. USD_TO_INR=82.0).
USD_TO_INR = float(os.getenv("USD_TO_INR", "82.0"))

for k in list(RATES.keys()):
    RATES[k] = round(RATES[k] * USD_TO_INR, 9)

for k in list(REQUEST_RATES.keys()):
    REQUEST_RATES[k] = REQUEST_RATES[k] * USD_TO_INR

# Convert bandwidth rate to INR
BANDWIDTH_RATE_PER_MB = BANDWIDTH_RATE_PER_MB * USD_TO_INR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(level, msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level.upper():7}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def parse_dt(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def fmt_duration(secs):
    secs = int(max(secs, 0))
    if secs < 60:   return f"{secs}s"
    if secs < 3600: return f"{secs//60}m {secs%60}s"
    return f"{secs//3600}h {(secs%3600)//60}m"


# ---------------------------------------------------------------------------
# Dimension 1 — Storage cost calculation per file
# ---------------------------------------------------------------------------

def calc_storage_cost(row, now) -> dict:
    """
    Splits a file's lifetime across the tiers it has lived in
    and applies the correct rate for each window.

    Returns:
        breakdown  — cost per tier dict
        total_secs — total tracked seconds
        total_cost — sum of all tier costs
    """
    created_dt = parse_dt(row["created_at"])
    hot_in     = parse_dt(row["hot_entered_at"])  or created_dt
    cool_in    = parse_dt(row["cool_entered_at"])
    cold_in    = parse_dt(row["cold_entered_at"])
    archive_in = parse_dt(row["archive_entered_at"])
    size_mb    = row["file_size_mb"]
    current    = row["storage_tier"]

    windows = []
    hot_end  = cool_in or (now if current == "HOT"     else None)
    cool_end = cold_in or (now if current == "COOL"    else None)
    cold_end = archive_in or (now if current == "COLD" else None)

    if hot_in  and hot_end:  windows.append(("HOT",     hot_in,  hot_end))
    if cool_in and cool_end: windows.append(("COOL",    cool_in, cool_end))
    if cold_in and cold_end: windows.append(("COLD",    cold_in, cold_end))
    if archive_in and current == "ARCHIVE":
        windows.append(("ARCHIVE", archive_in, now))

    breakdown  = {t: 0.0 for t in TIER_ORDER}
    total_secs = 0.0

    for (tier, start, end) in windows:
        secs = max((end - start).total_seconds(), 0)
        breakdown[tier] += size_mb * secs * RATES[tier]
        total_secs      += secs

    return {
        "breakdown":  breakdown,
        "total_secs": total_secs,
        "total_cost": sum(breakdown.values()),
    }


# ---------------------------------------------------------------------------
# Main billing pass
# ---------------------------------------------------------------------------

def calculate_tenant_bills() -> None:
    conn = get_conn()
    try:
        now = datetime.utcnow()

        # ── Dimension 1: Storage charges ───────────────────────────────────
        file_rows = conn.execute(
            """
            SELECT f.id, f.user_id, f.filename, f.file_size_mb,
                   f.storage_tier, f.created_at, f.last_accessed_at,
                   f.hot_entered_at, f.cool_entered_at,
                   f.cold_entered_at, f.archive_entered_at,
                   u.username,
                   COALESCE(b.name, 'default') as bucket_name
            FROM   files f
            JOIN   users u ON u.id = f.user_id
            LEFT JOIN buckets b ON b.id = f.bucket_id
            ORDER  BY f.user_id, f.id
            """
        ).fetchall()

        user_storage = defaultdict(lambda: {
            "username":   "",
            "total_cost": 0.0,
            "total_secs": 0.0,
            "by_tier":    {t: 0.0 for t in TIER_ORDER},
            "files":      [],
        })

        for row in file_rows:
            user_id = row["user_id"]
            result  = calc_storage_cost(row, now)

            user_storage[user_id]["username"]   = row["username"]
            user_storage[user_id]["total_cost"] += result["total_cost"]
            user_storage[user_id]["total_secs"] += result["total_secs"]
            for t in TIER_ORDER:
                user_storage[user_id]["by_tier"][t] += result["breakdown"][t]
            user_storage[user_id]["files"].append({
                "filename":   row["filename"],
                "bucket":     row["bucket_name"],
                "size_mb":    row["file_size_mb"],
                "tier":       row["storage_tier"],
                "cost":       result["total_cost"],
            })

        # ── Dimension 2: Request charges ────────────────────────────────────
        req_rows = conn.execute(
            """
            SELECT user_id, op_class, COUNT(*) as cnt
            FROM   request_logs
            GROUP  BY user_id, op_class
            """
        ).fetchall()

        req_costs = defaultdict(float)
        req_counts = defaultdict(lambda: {"A": 0, "B": 0, "FREE": 0})
        for r in req_rows:
            uid = r["user_id"]
            req_costs[uid]          += r["cnt"] * REQUEST_RATES.get(r["op_class"], 0)
            req_counts[uid][r["op_class"]] = r["cnt"]

        # ── Dimension 3: Bandwidth charges ──────────────────────────────────
        # Only egress is billed. Ingress is FREE.
        bw_rows = conn.execute(
            """
            SELECT user_id,
                   direction,
                   COALESCE(SUM(mb_transferred), 0) as total_mb,
                   COUNT(*) as ops
            FROM   bandwidth_logs
            GROUP  BY user_id, direction
            """
        ).fetchall()

        bw_egress_mb   = defaultdict(float)
        bw_ingress_mb  = defaultdict(float)
        bw_egress_ops  = defaultdict(int)
        bw_ingress_ops = defaultdict(int)

        for r in bw_rows:
            uid = r["user_id"]
            if r["direction"] == "egress":
                bw_egress_mb[uid]  = r["total_mb"]
                bw_egress_ops[uid] = r["ops"]
            else:
                bw_ingress_mb[uid]  = r["total_mb"]
                bw_ingress_ops[uid] = r["ops"]

        bw_costs = {
            uid: bw_egress_mb[uid] * BANDWIDTH_RATE_PER_MB
            for uid in set(list(bw_egress_mb.keys()) + list(user_storage.keys()))
        }

        # ── Collect all user IDs across all dimensions ──────────────────────
        all_user_ids = set(
            list(user_storage.keys()) +
            list(req_costs.keys()) +
            list(bw_costs.keys())
        )

        # ── Upsert billing_records ──────────────────────────────────────────
        now_iso = now.isoformat()
        billing_summary = {}

        for user_id in all_user_ids:
            storage_cost   = user_storage[user_id]["total_cost"]
            request_cost   = req_costs.get(user_id, 0.0)
            bandwidth_cost = bw_costs.get(user_id, 0.0)
            amount_owed    = storage_cost + request_cost + bandwidth_cost
            total_hours    = user_storage[user_id]["total_secs"] / 3600

            existing = conn.execute(
                "SELECT id FROM billing_records WHERE user_id=?", (user_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE billing_records
                    SET    total_hours_tracked = ?,
                           amount_owed         = ?,
                           last_calculated_at  = ?
                    WHERE  user_id = ?
                    """,
                    (total_hours, amount_owed, now_iso, user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO billing_records
                           (user_id, total_hours_tracked, amount_owed, last_calculated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, total_hours, amount_owed, now_iso),
                )

            billing_summary[user_id] = {
                "username":      user_storage[user_id]["username"],
                "storage_cost":  storage_cost,
                "request_cost":  request_cost,
                "bandwidth_cost": bandwidth_cost,
                "amount_owed":   amount_owed,
                "total_hours":   total_hours,
                "by_tier":       user_storage[user_id]["by_tier"],
                "files":         user_storage[user_id]["files"],
                "req_counts":    req_counts[user_id],
                "egress_mb":     bw_egress_mb.get(user_id, 0.0),
                "ingress_mb":    bw_ingress_mb.get(user_id, 0.0),
                "egress_ops":    bw_egress_ops.get(user_id, 0),
                "ingress_ops":   bw_ingress_ops.get(user_id, 0),
            }

        conn.commit()
        _print_dashboard(billing_summary, now)

    except sqlite3.Error as exc:
        conn.rollback()
        log("error", f"Database error: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard renderer — terminal billing report
# ---------------------------------------------------------------------------

def _print_dashboard(billing_summary: dict, now: datetime) -> None:
    W   = 78
    div = "─" * W

    print()
    print(f"┌{div}┐")
    print(f"│{'  STRATA — 3-DIMENSION IBM COS BILLING ENGINE':^{W}}│")
    print(f"│{'  ' + now.strftime('%Y-%m-%d %H:%M:%S UTC'):^{W}}│")
    print(f"├{div}┤")
    print(f"│  {'DIMENSION':12} {'WHAT IS CHARGED':32} {'RATE':28}│")
    print(f"│  {'─'*74}  │")
    print(f"│  {'Storage':12} {'size × time × tier rate':32} {'HOT=₹'+str(RATES['HOT'])+' .../MB/s':28}│")
    print(f"│  {'Requests':12} {'Class A writes / Class B reads':32} {'A=₹{:.6f} B=₹{:.6f} per 1K'.format(REQUEST_RATES.get('A',0)*1000, REQUEST_RATES.get('B',0)*1000):28}│")
    print(f"│  {'Bandwidth':12} {'egress only (ingress=free)':32} {'₹{:.4f}/GB (₹{:.7f}/MB)'.format(BANDWIDTH_RATE_PER_MB*1024, BANDWIDTH_RATE_PER_MB):28}│")
    print(f"├{div}┤")

    grand_total = 0.0

    for user_id, data in billing_summary.items():
        if not data["username"]:
            continue

        username       = data["username"]
        storage_cost   = data["storage_cost"]
        request_cost   = data["request_cost"]
        bandwidth_cost = data["bandwidth_cost"]
        amount_owed    = data["amount_owed"]
        grand_total   += amount_owed

        print(f"│  {'TENANT':<10} {username:<16} "
              f"{'Files:'} {len(data['files']):<4} "
              f"{'Hours:'} {data['total_hours']:.4f}{'':>20}│")
        print(f"│  {'─'*74}  │")

        # Dimension 1 — Storage breakdown by tier
        print(f"│  {'[1] STORAGE':<14} "
              f"HOT=₹{data['by_tier']['HOT']:.5f}  "
              f"COOL=₹{data['by_tier']['COOL']:.5f}  "
              f"COLD=₹{data['by_tier']['COLD']:.5f}  "
              f"ARC=₹{data['by_tier']['ARCHIVE']:.5f}  │")
        print(f"│  {'':14} Storage subtotal: ₹{storage_cost:.7f}{'':>38}│")

        # Dimension 2 — Request charges
        rc = data["req_counts"]
        print(f"│  {'[2] REQUESTS':<14} "
              f"A={rc.get('A',0)} writes  "
              f"B={rc.get('B',0)} reads  "
              f"FREE={rc.get('FREE',0)} deletes  "
              f"Subtotal: ₹{request_cost:.7f}  │")

        # Dimension 3 — Bandwidth charges
        print(f"│  {'[3] BANDWIDTH':<14} "
              f"Egress={data['egress_mb']:.4f} MB ({data['egress_ops']} downloads)  "
              f"Ingress={data['ingress_mb']:.4f} MB (FREE)  "
              f"Subtotal: ₹{bandwidth_cost:.7f}  │")

        print(f"│  {'─'*74}  │")
        print(f"│  {'AMOUNT OWED':<14} "
              f"₹{amount_owed:.7f}  "
              f"(storage + requests + bandwidth){'':>19}│")
        print(f"├{div}┤")

    print(f"│  {'GRAND TOTAL — ALL TENANTS':44} ₹{grand_total:.7f}{'':>20}│")
    print(f"└{div}┘")
    print(f"\n  IBM COS 3-dimension billing  |  "
          f"Next recalculation in {POLL_INTERVAL_SECS}s\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("=" * 78)
    print("  STRATA v0.9.1 — IBM COS 3-Dimension Billing Engine")
    print()
    print("  Dimension 1 — Storage:   HOT=₹0.001  COOL=₹0.0004  "
          "COLD=₹0.0002  ARCHIVE=₹0.00005  /MB/s")
    print("  Dimension 2 — Requests:  Class A=₹0.005/1K  "
          "Class B=₹0.0004/1K  DELETE=FREE")
    print("  Dimension 3 — Bandwidth: Egress=₹0.0087/GB  Ingress=FREE")
    print()
    print(f"  Poll interval: {POLL_INTERVAL_SECS}s  |  Press Ctrl+C to stop.")
    print("=" * 78)
    print()

    while True:
        try:
            calculate_tenant_bills()
        except Exception as exc:
            log("error", f"Unexpected error: {exc}")
        time.sleep(POLL_INTERVAL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log("engine", "Billing engine stopped cleanly.")
