"""
Module 4 (v2): 4-Tier Billing Engine
Rates per MB per second:
  HOT     $0.001000
  COOL    $0.000400
  COLD    $0.000200
  ARCHIVE $0.000050

Cost is split across each tier a file has actually lived in,
using the tier_entered_at timestamps to calculate time windows.

Run with:
    python billing_engine.py
"""

import sqlite3
import time
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_NAME  = "cloud_storage.db"
POLL_INTERVAL_SECS = 10

RATES = {
    "HOT":     0.001000,
    "COOL":    0.000400,
    "COLD":    0.000200,
    "ARCHIVE": 0.000050,
}

TIER_ORDER = ["HOT", "COOL", "COLD", "ARCHIVE"]

# Request charges (per single operation)
REQUEST_RATES = {
    "A":    0.005  / 1000,   # Class A: writes — $0.005 per 1,000
    "B":    0.0004 / 1000,   # Class B: reads  — $0.0004 per 1,000
    "FREE": 0.0,
}

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
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Cost calculation — split across tiers a file has lived in
# ---------------------------------------------------------------------------

def calc_file_cost(row, now: datetime) -> dict:
    """
    Returns a breakdown dict with cost per tier and total.

    Logic:
      - HOT window   : hot_entered_at  → cool_entered_at (or now if still HOT)
      - COOL window  : cool_entered_at → cold_entered_at (or now if still COOL)
      - COLD window  : cold_entered_at → archive_entered_at (or now if still COLD)
      - ARCHIVE window: archive_entered_at → now

    If an entered_at is NULL, that tier was never reached.
    """
    created    = parse_dt(row["created_at"])
    hot_in     = parse_dt(row["hot_entered_at"])   or created
    cool_in    = parse_dt(row["cool_entered_at"])
    cold_in    = parse_dt(row["cold_entered_at"])
    archive_in = parse_dt(row["archive_entered_at"])
    size_mb    = row["file_size_mb"]
    current    = row["storage_tier"]

    # Build windows: (tier, start, end)
    windows = []

    # HOT window
    hot_end = cool_in or (now if current == "HOT" else None)
    if hot_in and hot_end:
        windows.append(("HOT", hot_in, hot_end))

    # COOL window
    cool_end = cold_in or (now if current == "COOL" else None)
    if cool_in and cool_end:
        windows.append(("COOL", cool_in, cool_end))

    # COLD window
    cold_end = archive_in or (now if current == "COLD" else None)
    if cold_in and cold_end:
        windows.append(("COLD", cold_in, cold_end))

    # ARCHIVE window
    if archive_in and current == "ARCHIVE":
        windows.append(("ARCHIVE", archive_in, now))

    breakdown = {t: 0.0 for t in TIER_ORDER}
    total_secs = 0.0

    for (tier, start, end) in windows:
        secs = max((end - start).total_seconds(), 0)
        cost = size_mb * secs * RATES[tier]
        breakdown[tier] += cost
        total_secs += secs

    total_cost = sum(breakdown.values())
    return {
        "breakdown": breakdown,
        "total_secs": total_secs,
        "total_cost": total_cost,
    }


# ---------------------------------------------------------------------------
# Main billing pass
# ---------------------------------------------------------------------------

def calculate_tenant_bills():
    conn = get_conn()
    try:
        now = datetime.utcnow()

        rows = conn.execute(
            """
            SELECT f.id, f.user_id, f.filename, f.file_size_mb,
                   f.storage_tier, f.created_at, f.last_accessed_at,
                   f.hot_entered_at, f.cool_entered_at,
                   f.cold_entered_at, f.archive_entered_at,
                   u.username
            FROM   files f
            JOIN   users u ON u.id = f.user_id
            ORDER  BY f.user_id, f.id
            """
        ).fetchall()

        if not rows:
            log("info", "No file records. Upload files to see billing.")
            return

        # Accumulate per user
        user_totals = defaultdict(lambda: {
            "username":   "",
            "total_cost": 0.0,
            "total_secs": 0.0,
            "files":      [],
            "by_tier":    {t: 0.0 for t in TIER_ORDER},
        })

        for row in rows:
            user_id  = row["user_id"]
            result   = calc_file_cost(row, now)

            user_totals[user_id]["username"]   = row["username"]
            user_totals[user_id]["total_cost"] += result["total_cost"]
            user_totals[user_id]["total_secs"] += result["total_secs"]

            for tier in TIER_ORDER:
                user_totals[user_id]["by_tier"][tier] += result["breakdown"][tier]

            user_totals[user_id]["files"].append({
                "filename":   row["filename"],
                "size_mb":    row["file_size_mb"],
                "tier":       row["storage_tier"],
                "total_secs": result["total_secs"],
                "cost":       result["total_cost"],
                "breakdown":  result["breakdown"],
            })

        # ── Request charges per user ───────────────────────────────────────
        req_rows = conn.execute(
            """
            SELECT user_id, op_class, COUNT(*) as cnt
            FROM   request_logs
            GROUP  BY user_id, op_class
            """
        ).fetchall()

        req_costs = {}
        for r in req_rows:
            uid  = r["user_id"]
            cost = r["cnt"] * REQUEST_RATES.get(r["op_class"], 0)
            req_costs[uid] = req_costs.get(uid, 0.0) + cost

        # Upsert billing_records — storage + request charges combined
        now_iso = now.isoformat()
        for user_id, data in user_totals.items():
            total_hours    = data["total_secs"] / 3600
            storage_cost   = data["total_cost"]
            request_cost   = req_costs.get(user_id, 0.0)
            amount_owed    = storage_cost + request_cost
            existing = conn.execute(
                "SELECT id FROM billing_records WHERE user_id = ?", (user_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE billing_records
                    SET    total_hours_tracked=?, amount_owed=?, last_calculated_at=?
                    WHERE  user_id=?
                    """,
                    (total_hours, amount_owed, now_iso, user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO billing_records
                           (user_id, total_hours_tracked, amount_owed, last_calculated_at)
                    VALUES (?,?,?,?)
                    """,
                    (user_id, total_hours, amount_owed, now_iso),
                )

        conn.commit()
        _print_dashboard(user_totals, now)

    except sqlite3.Error as exc:
        conn.rollback()
        log("error", f"Database error: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------

def _print_dashboard(user_totals, now):
    W   = 72
    div = "─" * W

    print()
    print(f"┌{div}┐")
    print(f"│{'  CLOUD STORAGE — 4-TIER LIVE BILLING':^{W}}│")
    print(f"│{'  ' + now.strftime('%Y-%m-%d %H:%M:%S UTC'):^{W}}│")
    print(f"│  {'Tier Rates:':12} HOT=$0.001  COOL=$0.0004  COLD=$0.0002  ARCHIVE=$0.00005  │")
    print(f"├{div}┤")

    grand_total = 0.0

    for user_id, data in user_totals.items():
        username    = data["username"]
        total_cost  = data["total_cost"]
        total_hours = data["total_secs"] / 3600
        files       = data["files"]
        by_tier     = data["by_tier"]
        grand_total += total_cost

        print(f"│  {'Tenant':<10} {username:<18} {'Files:'} {len(files):<4}{'':>31}│")
        print(f"│  {'Tracked':<10} {total_hours:.5f} hrs{'':>50}│")
        print(f"│  {'Tier costs:':<12} "
              f"HOT=${by_tier['HOT']:.5f}  "
              f"COOL=${by_tier['COOL']:.5f}  "
              f"COLD=${by_tier['COLD']:.5f}  "
              f"ARC=${by_tier['ARCHIVE']:.5f}  │")
        print(f"│  {div[:68]}  │")

        for f in files:
            tier_tag = f"[{f['tier']}]"
            line = (
                f"│    {f['filename'][:20]:<20}  "
                f"{tier_tag:<9}  "
                f"{f['size_mb']:>7.3f} MB  "
                f"{f['total_secs']:>7.1f}s  "
                f"${f['cost']:>10.7f}  │"
            )
            print(line)

        print(f"│  {'Amount Owed':<20} ${total_cost:>12.7f}{'':>35}│")
        print(f"├{div}┤")

    print(f"│  {'GRAND TOTAL':40} ${grand_total:>12.7f}{'':>17}│")
    print(f"└{div}┘")
    print(f"\n  Next update in {POLL_INTERVAL_SECS}s\n")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 72)
    print("  Multi-Tenant Cloud Storage — 4-Tier Billing Engine")
    for t, r in RATES.items():
        print(f"  {t:<8} rate: ${r}/MB/sec")
    print(f"  Poll interval: {POLL_INTERVAL_SECS}s  |  Press Ctrl+C to stop.")
    print("=" * 72)
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
        log("engine", "Billing engine stopped.")
