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

# AWS S3 Intelligent-Tiering does not move objects smaller than 128KB into
# infrequent-access tiers — the storage/retrieval overhead of tracking a
# tiny object's tier status outweighs any cost savings. Files below this
# size stay in HOT permanently, regardless of how long they've been idle.
MIN_TIERING_SIZE_MB = 128 / 1024  # 128KB expressed in MB ≈ 0.125

# ---------------------------------------------------------------------------
# File type classification — MIME-based tier policy
#
# Real lifecycle policies (AWS S3 Lifecycle, Azure Blob tiering) are often
# written per content-type, not just per-age, because different file types
# have predictably different access patterns:
#   - Logs/text/backups are rarely re-read after creation → tier down FASTER
#   - Media (images/video/audio) gets sporadic re-access  → STANDARD pace
#   - Archives/binaries are write-once, rarely touched     → tier down FASTER
#
# Each class is a multiplier applied to the standard thresholds above.
# < 1.0 = demotes faster than standard (e.g. 0.5 = half the normal wait)
# = 1.0 = standard pace (the default for any unclassified/unknown type)
# > 1.0 = demotes slower than standard (kept HOT/COOL longer)
# ---------------------------------------------------------------------------
TYPE_CLASS_RULES = [
    # (prefix-match against content_type, multiplier, label)
    ("text/",        0.5,  "log/text — demotes faster, rarely re-read"),
    ("application/x-log", 0.5, "log — demotes faster, rarely re-read"),
    ("application/zip",   0.5, "archive — write-once, demotes faster"),
    ("application/x-tar", 0.5, "archive — write-once, demotes faster"),
    ("application/gzip",  0.5, "archive — write-once, demotes faster"),
    ("image/",       1.0,  "media — standard pace"),
    ("video/",       1.5,  "media — demotes slower, re-accessed sporadically"),
    ("audio/",       1.5,  "media — demotes slower, re-accessed sporadically"),
]
DEFAULT_TYPE_MULTIPLIER = 1.0  # unclassified / unknown content-type


def classify_content_type(content_type: str) -> tuple:
    """
    Returns (multiplier, label) for a given content_type string by matching
    the first prefix rule in TYPE_CLASS_RULES. Falls back to the standard
    1.0x multiplier for unrecognized or missing content types.
    """
    if not content_type:
        return (DEFAULT_TYPE_MULTIPLIER, "unclassified — standard pace")
    for prefix, multiplier, label in TYPE_CLASS_RULES:
        if content_type.startswith(prefix):
            return (multiplier, label)
    return (DEFAULT_TYPE_MULTIPLIER, "unclassified — standard pace")


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
                       f.last_accessed_at, f.user_id, f.content_type,
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
                file_id      = row["id"]
                filename     = row["filename"]
                username     = row["username"]
                user_id      = row["user_id"]
                bucket_name  = row["bucket_name"]
                size_mb      = row["file_size_mb"]
                content_type = row["content_type"]

                try:
                    last_dt = datetime.fromisoformat(row["last_accessed_at"])
                except (ValueError, TypeError):
                    log("warning", f"  '{filename}' — bad timestamp, skipping.")
                    continue

                # ── File type classification — adjust threshold by content-type ──
                multiplier, type_label = classify_content_type(content_type)
                effective_threshold = threshold_secs * multiplier

                elapsed = (now - last_dt).total_seconds()

                if elapsed <= effective_threshold:
                    remaining = int(effective_threshold - elapsed)
                    mult_note = "" if multiplier == 1.0 else f" [{multiplier}x: {type_label}]"
                    log("info",
                        f"  '{filename}' ({username}/{bucket_name}) — "
                        f"{int(elapsed)}s idle, {from_tier}→{to_tier} in ~{remaining}s{mult_note}")
                    continue

                # ── Size-based exemption (AWS S3 Intelligent-Tiering rule) ──
                # Files under 128KB never tier down, no matter how long
                # they've been idle. They stay in HOT permanently.
                if size_mb < MIN_TIERING_SIZE_MB:
                    log("info",
                        f"  '{filename}' ({username}/{bucket_name}) — "
                        f"{size_mb*1024:.1f}KB < 128KB minimum, exempt from tiering, staying {from_tier}")
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
                mult_note = "" if multiplier == 1.0 else f" [{multiplier}x: {type_label}]"
                log("info",
                    f"  [DEMOTED] '{filename}' ({username}/{bucket_name}) | "
                    f"{size_mb:.3f} MB | {int(elapsed)}s idle | "
                    f"{from_tier} → {to_tier}{mult_note}")

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
    print("  STRATA v0.9.1 — Bucket-Aware 4-Tier Tiering Engine")
    print(f"  HOT→COOL: {HOT_TO_COOL_SECS}s  |  "
          f"COOL→COLD: {COOL_TO_COLD_SECS}s  |  "
          f"COLD→ARCHIVE: {COLD_TO_ARCHIVE_SECS}s")
    print(f"  Size exemption: files < {MIN_TIERING_SIZE_MB*1024:.0f}KB never tier "
          f"(AWS S3 Intelligent-Tiering rule)")
    print(f"  Type classification: text/log/archive=0.5x faster, "
          f"video/audio=1.5x slower, image/other=1.0x standard")
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
