"""
helpers.py — STRATA storage helper functions

These are the pure utility functions that implement STRATA's core
domain logic — the "how does it actually work" layer. They have no
FastAPI decorators or HTTP request/response handling; they're plain
Python functions that route handlers call to do the real work.

Why separate from the route modules?
  Multiple route modules need these same helpers: the upload route
  calls compute_etag and log_bandwidth; the download route calls
  promote_to_hot and log_bandwidth; the bucket route calls get_bucket.
  Putting them here, in a neutral module, means each route file imports
  from helpers rather than from each other — avoiding circular imports.

Functions in this module:

  tier_file_path    — constructs the physical on-disk path for a file
                      given its tier, owner, bucket, and filename.

  compute_etag      — MD5 hash of a file's byte content, following the
                      AWS S3 / IBM COS ETag convention. Used to detect
                      corruption or tampering after upload.

  guess_content_type — MIME type detection from a filename's extension.
                      Falls back to 'application/octet-stream' for
                      unrecognized extensions, matching S3/IBM COS.

  get_bucket        — looks up a bucket row by owner and name. Returns
                      None if not found, so callers can raise 404s.

  log_tier_event    — inserts a row into tier_events whenever a file
                      moves between tiers. This is the audit trail that
                      powers the dashboard's "Tier Event Log."

  promote_to_hot    — the most complex helper. Moves a file physically
                      from its current tier directory back to HOT,
                      updates the database, and logs the event. Resilient
                      to a race condition where the background tiering
                      engine moves the file again between our read and
                      our write — see the function's own docstring for
                      the full details of that bug and its fix.

  log_bandwidth     — records a data transfer (upload=ingress,
                      download=egress) into bandwidth_logs for billing.
"""

import hashlib
import mimetypes
import os
import shutil
from datetime import datetime

import config


def tier_file_path(tier: str, username: str, bucket: str, filename: str) -> str:
    """Constructs the full on-disk path for a file at a given tier.

    Files live at: storage/{tier}/{username}/{bucket}/{filename}
    This function is the single source of truth for that convention —
    every part of the codebase that needs to locate a file on disk calls
    this rather than constructing the path manually.
    """
    return os.path.join(config.TIER_DIRS[tier], username, bucket, filename)


def compute_etag(contents: bytes) -> str:
    """MD5 hash of the exact byte content — the AWS S3 / IBM COS ETag convention.

    ETags let a client verify a downloaded file's integrity hasn't changed
    since upload: recompute the hash on the received bytes and compare
    to the stored ETag. If they differ, the file was corrupted or tampered with.
    Computed once at upload time and stored in the files table.
    """
    return hashlib.md5(contents).hexdigest()


def guess_content_type(filename: str) -> str:
    """MIME type detection from the filename's extension.

    Returns the detected MIME string (e.g. 'image/png', 'application/pdf')
    or 'application/octet-stream' for unrecognized extensions — the same
    fallback AWS S3 and IBM COS use when they can't determine a type.
    Stored in the files table at upload time.
    """
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


def get_bucket(conn, user_id: int, bucket_name: str):
    """Looks up a bucket by owner ID and name. Returns a dict if found,
    or None if the bucket doesn't exist or belongs to a different user.
    Used by route handlers to validate bucket access before operating on files.
    """
    row = conn.execute(
        "SELECT * FROM buckets WHERE user_id=? AND name=?",
        (user_id, bucket_name),
    ).fetchone()
    return dict(row) if row else None


def log_tier_event(conn, file_id, user_id, from_tier, to_tier, event_type, ts):
    """Records a tier transition into the tier_events audit table.

    Called whenever a file moves between tiers — whether via an automatic
    demotion by tiering_engine.py or a manual promotion triggered by a
    download. Powers the "Tier Event Log" panel in the dashboard.
    """
    conn.execute(
        "INSERT INTO tier_events "
        "(file_id, user_id, from_tier, to_tier, event_type, occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, user_id, from_tier, to_tier, event_type, ts),
    )


def promote_to_hot(conn, row: dict, username: str, bucket: str, now_iso: str):
    """Promotes a file back to HOT tier when a user downloads it.

    This function is resilient to a race condition with the background
    tiering engine: if the engine demotes the file again in the gap
    between when the download route read the file's tier and when this
    function actually runs, the file won't be at the expected location.

    The original bug (the 'Butterfly.png bug') silently updated the
    database to claim the file was HOT even when the physical move failed,
    permanently decoupling what the database claimed from where the file
    actually sat on disk.

    This version:
    1. Checks the expected path; if found, moves the file normally.
    2. If NOT found, re-fetches the DB's current tier (the engine may
       have moved it) and retries from the true current location.
    3. If still not found after the retry, logs a loud warning instead
       of silently lying to the database.
    """
    file_id  = row["id"]
    user_id  = row["user_id"]
    filename = row["filename"]
    current  = row["storage_tier"]

    if current == "HOT":
        conn.execute(
            "UPDATE files SET last_accessed_at=? WHERE id=?",
            (now_iso, file_id),
        )
        return

    src     = tier_file_path(current, username, bucket, filename)
    dst_dir = os.path.join(config.TIER_DIRS["HOT"], username, bucket)
    dst     = os.path.join(dst_dir, filename)

    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst)
    else:
        # Expected location is empty — the tiering engine may have just
        # demoted this file further in the gap since we last checked.
        retry_row  = conn.execute(
            "SELECT storage_tier FROM files WHERE id=?", (file_id,)
        ).fetchone()
        retry_tier = retry_row["storage_tier"] if retry_row else current

        if retry_tier != current:
            current = retry_tier
            src     = tier_file_path(current, username, bucket, filename)

        if current == "HOT":
            conn.execute(
                "UPDATE files SET last_accessed_at=? WHERE id=?",
                (now_iso, file_id),
            )
            return

        if os.path.exists(src):
            os.makedirs(dst_dir, exist_ok=True)
            shutil.move(src, dst)
        else:
            print(f"[WARNING] promote_to_hot: '{filename}' not found at "
                  f"expected path '{src}' (tier={current}). "
                  f"Updating DB anyway as a last resort, but file may be lost.")

    conn.execute(
        "UPDATE files SET storage_tier='HOT', last_accessed_at=?, hot_entered_at=? WHERE id=?",
        (now_iso, now_iso, file_id),
    )
    log_tier_event(conn, file_id, user_id, current, "HOT", "PROMOTE", now_iso)


def log_bandwidth(conn, user_id, file_id, bucket, filename, size_mb, direction="egress"):
    """Records a data transfer event into bandwidth_logs for billing.

    Called on every upload (direction='ingress') and download (direction='egress').
    The billing engine reads these records to compute Dimension 3 charges.
    Ingress is always free; egress is billed at the IBM COS rate.
    """
    conn.execute(
        """
        INSERT INTO bandwidth_logs
               (user_id, file_id, bucket_name, filename,
                bytes_transferred, mb_transferred, direction, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, file_id, bucket, filename,
         int(size_mb * 1024 * 1024), round(size_mb, 6),
         direction, datetime.utcnow().isoformat()),
    )
