"""
STRATA v0.9.0 — tiering_engine.py
Bucket-aware tiering engine.
Files now live at: storage/{tier}/{username}/{bucket}/{filename}
"""

import os
import shutil
import sqlite3
import time
from datetime import datetime

DB_NAME  = "cloud_storage.db"

HOT_TO_COOL_SECS     = 120
COOL_TO_COLD_SECS    = 300
COLD_TO_ARCHIVE_SECS = 600
POLL_INTERVAL_SECS   = 15

TIER_DIRS = {
    "HOT":     "storage/hot",
    "COOL":    "storage/cool",
    "COLD":    "storage/cold",
    "ARCHIVE": "storage/archive",
}
DEMOTION_CHAIN = [
    ("HOT",  "COOL",    HOT_TO_COOL_SECS),
    ("COOL", "COLD",    COOL_TO_COLD_SECS),
    ("COLD", "ARCHIVE", COLD_TO_ARCHIVE_SECS),
]


def log(level, msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level.upper():7}] {msg}")


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def manage_storage_tiers():
    conn = get_conn()
    try:
        now     = datetime.utcnow()
        now_iso = now.isoformat()
        total_demoted = 0

        for (from_tier, to_tier, threshold_secs) in DEMOTION_CHAIN:
            rows = conn.execute(
                """
                SELECT f.id, f.filename, f.file_size_mb,
                       f.last_accessed_at, f.user_id,
                       u.username,
                       COALESCE(b.name, 'default') as bucket_name
                FROM   files f
                JOIN   users u ON u.id = f.user_id
                LEFT JOIN buckets b ON b.id = f.bucket_id
                WHERE  f.storage_tier = ?
                """,
                (from_tier,),
            ).fetchall()

            if not rows:
                continue

            log("info", f"Checking {len(rows)} {from_tier} file(s) "
                        f"(threshold: {threshold_secs}s)...")

            for row in rows:
                file_id     = row["id"]
                filename    = row["filename"]
                username    = row["username"]
                user_id     = row["user_id"]
                bucket_name = row["bucket_name"]
                size_mb     = row["file_size_mb"]

                try:
                    last_dt = datetime.fromisoformat(row["last_accessed_at"])
                except (ValueError, TypeError):
                    log("warning", f"  '{filename}' — bad timestamp, skipping.")
                    continue

                elapsed = (now - last_dt).total_seconds()

                if elapsed <= threshold_secs:
                    remaining = int(threshold_secs - elapsed)
                    log("info",
                        f"  '{filename}' ({username}/{bucket_name}) — "
                        f"{int(elapsed)}s idle, {from_tier}→{to_tier} in ~{remaining}s")
                    continue

                # ── Move the file ──────────────────────────────────────────
                src     = os.path.join(TIER_DIRS[from_tier], username, bucket_name, filename)
                dst_dir = os.path.join(TIER_DIRS[to_tier],   username, bucket_name)
                dst     = os.path.join(dst_dir, filename)

                if not os.path.exists(src):
                    log("warning",
                        f"  '{filename}' ({username}/{bucket_name}) — "
                        f"missing from disk, updating DB only.")
                else:
                    try:
                        os.makedirs(dst_dir, exist_ok=True)
                        shutil.move(src, dst)
                    except OSError as exc:
                        log("error", f"  '{filename}' — move failed: {exc}")
                        continue

                # ── Update DB ──────────────────────────────────────────────
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
                conn.execute(
                    """
                    INSERT INTO tier_events
                           (file_id, user_id, from_tier, to_tier, event_type, occurred_at)
                    VALUES (?, ?, ?, ?, 'DEMOTE', ?)
                    """,
                    (file_id, user_id, from_tier, to_tier, now_iso),
                )

                total_demoted += 1
                log("info",
                    f"  [DEMOTED] '{filename}' ({username}/{bucket_name}) | "
                    f"{size_mb:.3f} MB | {int(elapsed)}s idle | "
                    f"{from_tier} → {to_tier}")

        conn.commit()

        if total_demoted:
            log("info", f"Pass complete — {total_demoted} file(s) demoted.")
        else:
            log("info", "Pass complete — no files crossed threshold.")

    except sqlite3.Error as exc:
        conn.rollback()
        log("error", f"Database error: {exc}")
    finally:
        conn.close()


def main():
    print()
    print("=" * 64)
    print("  STRATA v0.9.0 — Bucket-Aware 4-Tier Tiering Engine")
    print(f"  HOT→COOL: {HOT_TO_COOL_SECS}s  |  "
          f"COOL→COLD: {COOL_TO_COLD_SECS}s  |  "
          f"COLD→ARCHIVE: {COLD_TO_ARCHIVE_SECS}s")
    print(f"  Path: storage/{{tier}}/{{username}}/{{bucket}}/{{filename}}")
    print(f"  Poll interval: {POLL_INTERVAL_SECS}s  |  Press Ctrl+C to stop.")
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
