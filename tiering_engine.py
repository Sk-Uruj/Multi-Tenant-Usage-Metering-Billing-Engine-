"""
Module 3 (v2): 4-Tier Storage Engine
HOT → COOL → COLD → ARCHIVE  (demotion)
ARCHIVE/COLD/COOL → HOT      (promotion on access)

Tier thresholds (demo — scale up for production):
  HOT   → COOL    after 2 min  inactivity
  COOL  → COLD    after 5 min  inactivity
  COLD  → ARCHIVE after 10 min inactivity
  ARCHIVE files accessed → promoted back to HOT (handled in main.py download)

Run with:
    python tiering_engine.py
"""

# Quick fix for Python 3.13 deprecation warning
from datetime import datetime, timezone
# then replace datetime.utcnow() calls with:
datetime.now(timezone.utc).replace(tzinfo=None)

import os
import shutil
import sqlite3
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_NAME    = "cloud_storage.db"

# Inactivity thresholds per tier (seconds)
HOT_TO_COOL_SECS    =  120   # 2 min
COOL_TO_COLD_SECS   =  300   # 5 min
COLD_TO_ARCHIVE_SECS = 600   # 10 min

POLL_INTERVAL_SECS = 15

# Storage base directories per tier
TIER_DIRS = {
    "HOT":     "storage/hot",
    "COOL":    "storage/cool",
    "COLD":    "storage/cold",
    "ARCHIVE": "storage/archive",
}

# Demotion path
DEMOTION_CHAIN = [
    ("HOT",  "COOL",    HOT_TO_COOL_SECS),
    ("COOL", "COLD",    COOL_TO_COLD_SECS),
    ("COLD", "ARCHIVE", COLD_TO_ARCHIVE_SECS),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(level: str, msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level.upper():7}] {msg}")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def tier_dir(tier: str, username: str) -> str:
    return os.path.join(TIER_DIRS[tier], username)


def file_path(tier: str, username: str, filename: str) -> str:
    return os.path.join(TIER_DIRS[tier], username, filename)


# ---------------------------------------------------------------------------
# Log a tier event to the DB
# ---------------------------------------------------------------------------

def log_tier_event(conn, file_id, user_id, from_tier, to_tier, event_type, ts):
    conn.execute(
        """
        INSERT INTO tier_events (file_id, user_id, from_tier, to_tier, event_type, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (file_id, user_id, from_tier, to_tier, event_type, ts),
    )


# ---------------------------------------------------------------------------
# Core tiering pass
# ---------------------------------------------------------------------------

def manage_storage_tiers():
    conn = get_conn()
    try:
        now    = datetime.utcnow()
        now_iso = now.isoformat()
        total_demoted = 0

        for (from_tier, to_tier, threshold_secs) in DEMOTION_CHAIN:
            rows = conn.execute(
                """
                SELECT f.id, f.filename, f.file_size_mb,
                       f.last_accessed_at, f.user_id,
                       u.username
                FROM   files f
                JOIN   users u ON u.id = f.user_id
                WHERE  f.storage_tier = ?
                """,
                (from_tier,),
            ).fetchall()

            if not rows:
                continue

            log("info", f"Checking {len(rows)} {from_tier} file(s) "
                        f"(threshold: {threshold_secs}s inactivity)...")

            for row in rows:
                file_id  = row["id"]
                filename = row["filename"]
                username = row["username"]
                user_id  = row["user_id"]
                size_mb  = row["file_size_mb"]

                try:
                    last_dt = datetime.fromisoformat(row["last_accessed_at"])
                except (ValueError, TypeError):
                    log("warning", f"  '{filename}' — bad timestamp, skipping.")
                    continue

                elapsed = (now - last_dt).total_seconds()

                if elapsed <= threshold_secs:
                    remaining = int(threshold_secs - elapsed)
                    log("info", f"  '{filename}' ({username}) — "
                                f"{int(elapsed)}s idle, moves to {to_tier} in ~{remaining}s")
                    continue

                # ── Move the file ──────────────────────────────────────────
                src = file_path(from_tier, username, filename)
                dst_dir = tier_dir(to_tier, username)
                dst = os.path.join(dst_dir, filename)

                if not os.path.exists(src):
                    log("warning", f"  '{filename}' ({username}) — "
                                   f"missing from disk at '{src}', updating DB only.")
                else:
                    try:
                        os.makedirs(dst_dir, exist_ok=True)
                        shutil.move(src, dst)
                    except OSError as exc:
                        log("error", f"  '{filename}' — move failed: {exc}")
                        continue

                # ── Update entered_at column for the destination tier ──────
                entered_col = f"{to_tier.lower()}_entered_at"

                conn.execute(
                    f"""
                    UPDATE files
                    SET    storage_tier     = ?,
                           last_accessed_at = ?,
                           {entered_col}    = ?
                    WHERE  id = ?
                    """,
                    (to_tier, now_iso, now_iso, file_id),
                )

                log_tier_event(conn, file_id, user_id,
                               from_tier, to_tier, "DEMOTE", now_iso)

                total_demoted += 1
                log("info",
                    f"  [DEMOTED] '{filename}' ({username}) | "
                    f"{size_mb:.3f} MB | {int(elapsed)}s idle | "
                    f"{from_tier} → {to_tier}")

        conn.commit()

        if total_demoted:
            log("info", f"Pass complete — {total_demoted} file(s) demoted.")
        else:
            log("info", "Pass complete — no files crossed their inactivity threshold.")

    except sqlite3.Error as exc:
        conn.rollback()
        log("error", f"Database error: {exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 64)
    print("  Multi-Tenant Cloud Storage — 4-Tier Engine")
    print(f"  HOT→COOL:    {HOT_TO_COOL_SECS}s  |  "
          f"COOL→COLD: {COOL_TO_COLD_SECS}s  |  "
          f"COLD→ARCHIVE: {COLD_TO_ARCHIVE_SECS}s")
    print(f"  Poll interval: {POLL_INTERVAL_SECS}s")
    print("  Promotion (any tier → HOT) handled in main.py on file access.")
    print("  Press Ctrl+C to stop.")
    print("=" * 64)
    print()

    while True:
        try:
            log("engine", "--- Tiering pass starting ---")
            manage_storage_tiers()
        except Exception as exc:
            log("error", f"Unexpected error: {exc}")

        log("engine", f"Sleeping {POLL_INTERVAL_SECS}s...\n")
        time.sleep(POLL_INTERVAL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log("engine", "Stopped cleanly.")
