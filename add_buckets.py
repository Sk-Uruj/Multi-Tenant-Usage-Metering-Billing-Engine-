"""
add_buckets.py
One-time migration — adds bucket support to existing cloud_storage.db.
Run with ALL other processes stopped.

What it does:
  1. Creates the buckets table
  2. Adds bucket_id column to files table
  3. Creates a 'default' bucket for every existing user
  4. Assigns all existing files to their owner's default bucket
  5. Creates physical storage subdirectories for the default bucket
"""

import sqlite3
import os
import shutil

DB_NAME = "cloud_storage.db"

TIER_DIRS = {
    "HOT":     "storage/hot",
    "COOL":    "storage/cool",
    "COLD":    "storage/cold",
    "ARCHIVE": "storage/archive",
}


def run():
    print(f"\n{'='*55}")
    print("  STRATA — Bucket System Migration")
    print(f"{'='*55}\n")

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF;")

    try:
        cur = conn.cursor()

        # ── 1. Create buckets table ────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS buckets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name       TEXT    NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name)
            );
        """)
        conn.commit()
        print("  [OK] buckets table created")

        # ── 2. Add bucket_id to files if missing ───────────────────────────
        existing_cols = [r[1] for r in cur.execute("PRAGMA table_info(files)").fetchall()]
        if "bucket_id" not in existing_cols:
            cur.execute("ALTER TABLE files ADD COLUMN bucket_id INTEGER REFERENCES buckets(id) ON DELETE SET NULL;")
            conn.commit()
            print("  [OK] Added files.bucket_id column")
        else:
            print("  [SKIP] files.bucket_id already exists")

        # ── 3. Create default bucket for every user ────────────────────────
        users = cur.execute("SELECT id, username FROM users").fetchall()
        for user in users:
            try:
                cur.execute(
                    "INSERT INTO buckets (user_id, name, created_at) VALUES (?, 'default', datetime('now'))",
                    (user["id"],),
                )
                print(f"  [OK] Created 'default' bucket for {user['username']}")
            except sqlite3.IntegrityError:
                print(f"  [SKIP] 'default' bucket already exists for {user['username']}")
        conn.commit()

        # ── 4. Assign existing files to default bucket ─────────────────────
        updated = 0
        files = cur.execute(
            "SELECT id, user_id FROM files WHERE bucket_id IS NULL"
        ).fetchall()

        for f in files:
            bucket = cur.execute(
                "SELECT id FROM buckets WHERE user_id=? AND name='default'",
                (f["user_id"],),
            ).fetchone()
            if bucket:
                cur.execute(
                    "UPDATE files SET bucket_id=? WHERE id=?",
                    (bucket["id"], f["id"]),
                )
                updated += 1

        conn.commit()
        print(f"  [OK] Assigned {updated} existing file(s) to default bucket")

        # ── 5. Create physical subdirectories ──────────────────────────────
        users = cur.execute("SELECT username FROM users").fetchall()
        for user in users:
            username = user["username"]
            for tier_dir in TIER_DIRS.values():
                # Move existing files into the default bucket subdirectory
                old_dir = os.path.join(tier_dir, username)
                new_dir = os.path.join(tier_dir, username, "default")

                if os.path.exists(old_dir) and not os.path.isdir(new_dir):
                    os.makedirs(new_dir, exist_ok=True)
                    # Move all files from old flat dir into default subdir
                    for fname in os.listdir(old_dir):
                        src = os.path.join(old_dir, fname)
                        dst = os.path.join(new_dir, fname)
                        if os.path.isfile(src):
                            shutil.move(src, dst)
                    print(f"  [OK] Moved {username}/{tier_dir.split('/')[1]}/ files → default/")
                elif not os.path.exists(new_dir):
                    os.makedirs(new_dir, exist_ok=True)

        # ── Verify ─────────────────────────────────────────────────────────
        bucket_count = cur.execute("SELECT COUNT(*) FROM buckets").fetchone()[0]
        file_count   = cur.execute("SELECT COUNT(*) FROM files WHERE bucket_id IS NOT NULL").fetchone()[0]
        print(f"\n  ✓ Migration complete.")
        print(f"    Buckets created: {bucket_count}")
        print(f"    Files assigned:  {file_count}")
        print(f"\n  Restart all processes and run: python init_db.py (optional)\n")

    except Exception as exc:
        conn.rollback()
        print(f"\n  [FATAL] {exc}\n")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.close()


if __name__ == "__main__":
    run()
