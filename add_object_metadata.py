"""
add_object_metadata.py
One-time migration — adds etag and content_type columns to the files
table in cloud_storage.db, then backfills metadata for any files that
already exist on disk.

Run with ALL other processes stopped (uvicorn, tiering_engine, billing_engine).

Safe to run multiple times — it detects existing columns and already-
backfilled rows, so re-running this script is a no-op for anything
already done.
"""

import sqlite3
import os
import hashlib
import mimetypes

DB_NAME = "cloud_storage.db"

TIER_DIRS = {
    "HOT":     "storage/hot",
    "COOL":    "storage/cool",
    "COLD":    "storage/cold",
    "ARCHIVE": "storage/archive",
}


def compute_etag(file_path: str) -> str:
    """MD5 hash of the file's exact byte content — the AWS S3 / IBM COS ETag convention."""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def guess_content_type(filename: str) -> str:
    """MIME type detection from filename, falling back to a generic binary type."""
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


def run():
    print(f"\n{'='*55}")
    print("  STRATA — Object Metadata Migration (ETag + Content-Type)")
    print(f"{'='*55}\n")

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    try:
        # ── 1. Add columns if missing ───────────────────────────────────────
        existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()]

        if "etag" not in existing_cols:
            conn.execute("ALTER TABLE files ADD COLUMN etag TEXT")
            print("  [OK] Added files.etag column")
        else:
            print("  [SKIP] files.etag already exists")

        if "content_type" not in existing_cols:
            conn.execute("ALTER TABLE files ADD COLUMN content_type TEXT")
            print("  [OK] Added files.content_type column")
        else:
            print("  [SKIP] files.content_type already exists")

        conn.commit()

        # ── 2. Backfill metadata for existing files ─────────────────────────
        files = conn.execute(
            """
            SELECT f.id, f.filename, f.storage_tier, f.etag, f.content_type,
                   u.username, COALESCE(b.name, 'default') as bucket_name
            FROM   files f
            JOIN   users u ON u.id = f.user_id
            LEFT JOIN buckets b ON b.id = f.bucket_id
            """
        ).fetchall()

        backfilled = 0
        skipped    = 0
        missing    = 0

        for f in files:
            if f["etag"] and f["content_type"]:
                skipped += 1
                continue

            file_path = os.path.join(
                TIER_DIRS[f["storage_tier"]], f["username"], f["bucket_name"], f["filename"]
            )

            if not os.path.exists(file_path):
                print(f"  [WARN] '{f['filename']}' ({f['username']}/{f['bucket_name']}) "
                      f"— missing from disk, skipping backfill")
                missing += 1
                continue

            etag         = compute_etag(file_path)
            content_type = guess_content_type(f["filename"])

            conn.execute(
                "UPDATE files SET etag=?, content_type=? WHERE id=?",
                (etag, content_type, f["id"]),
            )
            print(f"  [OK] '{f['filename']}' — {content_type} — etag {etag[:12]}...")
            backfilled += 1

        conn.commit()

        print(f"\n  ✓ Migration complete.")
        print(f"    Backfilled: {backfilled}")
        print(f"    Skipped (already had metadata): {skipped}")
        if missing:
            print(f"    Missing from disk (could not hash): {missing}")
        print()

    except sqlite3.Error as exc:
        conn.rollback()
        print(f"\n  [FATAL] {exc}\n")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
