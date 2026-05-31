"""
Module 1 (v2): Database Initialization — 4-Tier Schema
cloud_storage.db

Tiers: HOT → COOL → COLD → ARCHIVE
"""

import sqlite3
import uuid
import os

DB_NAME = "cloud_storage.db"

DDL_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT    UNIQUE NOT NULL,
    password TEXT    NOT NULL,
    api_key  TEXT    UNIQUE NOT NULL
);
"""

DDL_FILES = """
CREATE TABLE IF NOT EXISTS files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename         TEXT    NOT NULL,
    file_size_mb     REAL    NOT NULL,
    storage_tier     TEXT    NOT NULL DEFAULT 'HOT'
                             CHECK(storage_tier IN ('HOT','COOL','COLD','ARCHIVE')),
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Tracks when each tier was entered so billing can split cost correctly
    -- across every tier a file has lived in
    hot_entered_at      TIMESTAMP,
    cool_entered_at     TIMESTAMP,
    cold_entered_at     TIMESTAMP,
    archive_entered_at  TIMESTAMP
);
"""

DDL_BILLING = """
CREATE TABLE IF NOT EXISTS billing_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_hours_tracked REAL    NOT NULL DEFAULT 0.0,
    amount_owed         REAL    NOT NULL DEFAULT 0.0,
    last_calculated_at  TIMESTAMP NOT NULL
);
"""

DDL_TIER_EVENTS = """
CREATE TABLE IF NOT EXISTS tier_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    from_tier   TEXT,
    to_tier     TEXT    NOT NULL,
    event_type  TEXT    NOT NULL CHECK(event_type IN ('DEMOTE','PROMOTE')),
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SEED_USERS = [
    {"username": "alice_dev",   "password": "alice123",  "api_key": str(uuid.uuid4())},
    {"username": "bob_staging", "password": "bob456",    "api_key": str(uuid.uuid4())},
    {"username": "carol_prod",  "password": "carol789",  "api_key": str(uuid.uuid4())},
]


def init_storage_dirs():
    for tier in ("hot", "cool", "cold", "archive"):
        os.makedirs(f"storage/{tier}", exist_ok=True)
    print("  [OK] Storage dirs: storage/hot  storage/cool  storage/cold  storage/archive")


def migrate_existing_db(conn):
    """
    Non-destructive migration: adds new columns and tables to an
    existing cloud_storage.db without losing any data.
    """
    cursor = conn.cursor()

    # Add new columns to files if they don't exist yet
    new_file_cols = [
        "hot_entered_at      TIMESTAMP",
        "cool_entered_at     TIMESTAMP",
        "cold_entered_at     TIMESTAMP",
        "archive_entered_at  TIMESTAMP",
    ]
    existing = [r[1] for r in cursor.execute("PRAGMA table_info(files)").fetchall()]
    for col_def in new_file_cols:
        col_name = col_def.strip().split()[0]
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE files ADD COLUMN {col_def}")
            print(f"  [MIGRATE] Added column: files.{col_name}")

    # Update storage_tier CHECK constraint — SQLite can't alter constraints,
    # so we just validate at app level and rely on the new table definition
    # for fresh installs. Existing rows with HOT/COLD are still valid.

    # Add COOL to any existing COLD entries that were just 'COLD' before
    # (leave them as-is — they are valid under the new CHECK)

    # Update existing HOT files to have hot_entered_at = created_at
    cursor.execute("""
        UPDATE files
        SET    hot_entered_at = created_at
        WHERE  hot_entered_at IS NULL AND storage_tier = 'HOT'
    """)

    # Create tier_events table (new)
    cursor.execute(DDL_TIER_EVENTS)

    conn.commit()
    print("  [OK] Migration complete.")


def create_schema(conn):
    conn.executescript(f"""
        PRAGMA foreign_keys = ON;
        {DDL_USERS}
        {DDL_FILES}
        {DDL_BILLING}
        {DDL_TIER_EVENTS}
    """)
    conn.commit()
    print("  [OK] Full schema created.")


def seed_users(conn):
    inserted = 0
    for u in SEED_USERS:
        try:
            conn.execute(
                "INSERT INTO users (username, password, api_key) VALUES (?,?,?)",
                (u["username"], u["password"], u["api_key"]),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE users SET password=? WHERE username=? AND (password IS NULL OR password='')",
                (u["password"], u["username"]),
            )
    conn.commit()
    print(f"  [OK] {inserted} user(s) seeded.")


def print_summary(conn):
    rows = conn.execute("SELECT id, username, password, api_key FROM users ORDER BY id").fetchall()
    print()
    print("  ┌─────┬────────────────┬───────────┬──────────────────────────────────────┐")
    print("  │ ID  │ Username       │ Password  │ API Key                              │")
    print("  ├─────┼────────────────┼───────────┼──────────────────────────────────────┤")
    for r in rows:
        print(f"  │ {r[0]:<3} │ {r[1]:<14} │ {r[2]:<9} │ {r[3]} │")
    print("  └─────┴────────────────┴───────────┴──────────────────────────────────────┘")
    print()
    print("  Login at http://127.0.0.1:8000/login")


def main():
    print(f"\n{'='*62}")
    print("  Cloud Storage Engine — 4-Tier Schema Init")
    print(f"{'='*62}\n")

    init_storage_dirs()

    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA journal_mode=WAL;")
    print(f"  [OK] Connected to {DB_NAME}")

    try:
        # Check if DB already has tables (existing install)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        if "files" in tables:
            print("  [INFO] Existing DB detected — running migration...")
            migrate_existing_db(conn)
        else:
            create_schema(conn)

        seed_users(conn)
        print_summary(conn)

    except sqlite3.Error as exc:
        conn.rollback()
        raise SystemExit(f"  [FATAL] {exc}")
    finally:
        conn.close()

    print(f"\n{'='*62}")
    print("  Setup complete. Tiers: HOT → COOL → COLD → ARCHIVE")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
