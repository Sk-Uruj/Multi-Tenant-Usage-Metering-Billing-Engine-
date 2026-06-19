"""
STRATA v0.9.1 — init_db.py
Complete schema init — creates ALL tables in one go.
No need to run separate migration scripts on a fresh install.

Tables:
  users, buckets, files, billing_records,
  tier_events, request_logs, bandwidth_logs, payments
"""

import sqlite3
import uuid
import os
import bcrypt

DB_NAME = "cloud_storage.db"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT    UNIQUE NOT NULL,
    password TEXT    NOT NULL,
    api_key  TEXT    UNIQUE NOT NULL
);
"""

DDL_BUCKETS = """
CREATE TABLE IF NOT EXISTS buckets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, name)
);
"""

DDL_FILES = """
CREATE TABLE IF NOT EXISTS files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket_id           INTEGER REFERENCES buckets(id) ON DELETE SET NULL,
    filename            TEXT    NOT NULL,
    file_size_mb        REAL    NOT NULL,
    storage_tier        TEXT    NOT NULL DEFAULT 'HOT'
                                CHECK(storage_tier IN ('HOT','COOL','COLD','ARCHIVE')),
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    hot_entered_at      TIMESTAMP,
    cool_entered_at     TIMESTAMP,
    cold_entered_at     TIMESTAMP,
    archive_entered_at  TIMESTAMP,
    etag                TEXT,    -- MD5 hash of file content (IBM COS / S3 convention)
    content_type        TEXT     -- MIME type, e.g. 'image/png', 'application/pdf'
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

DDL_REQUEST_LOGS = """
CREATE TABLE IF NOT EXISTS request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    TEXT    NOT NULL,
    method      TEXT    NOT NULL,
    op_class    TEXT    NOT NULL CHECK(op_class IN ('A','B','FREE')),
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

DDL_BANDWIDTH_LOGS = """
CREATE TABLE IF NOT EXISTS bandwidth_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id           INTEGER REFERENCES files(id) ON DELETE SET NULL,
    bucket_name       TEXT    NOT NULL DEFAULT 'default',
    filename          TEXT    NOT NULL,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    mb_transferred    REAL    NOT NULL DEFAULT 0.0,
    direction         TEXT    NOT NULL DEFAULT 'egress'
                              CHECK(direction IN ('egress','ingress')),
    occurred_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

DDL_PAYMENTS = """
CREATE TABLE IF NOT EXISTS payments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_id       TEXT    UNIQUE NOT NULL,
    amount           REAL    NOT NULL,
    currency         TEXT    NOT NULL DEFAULT 'INR',
    status           TEXT    NOT NULL DEFAULT 'pending'
                             CHECK(status IN ('pending','paid','failed')),
    storage_charge   REAL    NOT NULL DEFAULT 0.0,
    request_charge   REAL    NOT NULL DEFAULT 0.0,
    bandwidth_charge REAL    NOT NULL DEFAULT 0.0,
    card_last4       TEXT,
    card_type        TEXT,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at          TIMESTAMP
);
"""

SEED_USERS = [
    {"username": "alice_dev",   "password": "alice123",  "api_key": str(uuid.uuid4())},
    {"username": "bob_staging", "password": "bob456",    "api_key": str(uuid.uuid4())},
    {"username": "carol_prod",  "password": "carol789",  "api_key": str(uuid.uuid4())},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def init_storage_dirs():
    for tier in ("hot", "cool", "cold", "archive"):
        os.makedirs(f"storage/{tier}", exist_ok=True)
    print("  [OK] Storage dirs: hot / cool / cold / archive")


def create_schema(conn):
    conn.executescript(f"""
        PRAGMA foreign_keys = ON;
        {DDL_USERS}
        {DDL_BUCKETS}
        {DDL_FILES}
        {DDL_BILLING}
        {DDL_TIER_EVENTS}
        {DDL_REQUEST_LOGS}
        {DDL_BANDWIDTH_LOGS}
        {DDL_PAYMENTS}
    """)
    conn.commit()
    print("  [OK] Full schema created (8 tables)")


def migrate_existing_db(conn):
    """
    Non-destructive migration for existing installs.
    Adds missing tables and columns without losing any data.
    """
    cur = conn.cursor()

    existing_tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    # Add missing tables
    if "buckets" not in existing_tables:
        cur.execute(DDL_BUCKETS)
        print("  [MIGRATE] Created buckets table")

    if "request_logs" not in existing_tables:
        cur.execute(DDL_REQUEST_LOGS)
        print("  [MIGRATE] Created request_logs table")

    if "bandwidth_logs" not in existing_tables:
        cur.execute(DDL_BANDWIDTH_LOGS)
        print("  [MIGRATE] Created bandwidth_logs table")

    if "payments" not in existing_tables:
        cur.execute(DDL_PAYMENTS)
        print("  [MIGRATE] Created payments table")

    # Add missing columns to files
    existing_cols = [r[1] for r in cur.execute("PRAGMA table_info(files)").fetchall()]
    new_cols = [
        ("bucket_id",          "INTEGER REFERENCES buckets(id) ON DELETE SET NULL"),
        ("hot_entered_at",     "TIMESTAMP"),
        ("cool_entered_at",    "TIMESTAMP"),
        ("cold_entered_at",    "TIMESTAMP"),
        ("archive_entered_at", "TIMESTAMP"),
        ("etag",               "TEXT"),
        ("content_type",       "TEXT"),
    ]
    for col_name, col_def in new_cols:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE files ADD COLUMN {col_name} {col_def}")
            print(f"  [MIGRATE] Added files.{col_name}")

    # Backfill hot_entered_at for existing HOT files
    cur.execute("""
        UPDATE files SET hot_entered_at = created_at
        WHERE  hot_entered_at IS NULL AND storage_tier = 'HOT'
    """)

    conn.commit()
    print("  [OK] Migration complete")


def seed_users(conn):
    inserted = 0
    for u in SEED_USERS:
        try:
            # Hash the plain-text seed password with bcrypt before storing.
            # Login still uses the plain-text password shown in SEED_USERS
            # (e.g. alice123) — only the stored value is hashed.
            hashed = bcrypt.hashpw(
                u["password"].encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            conn.execute(
                "INSERT INTO users (username, password, api_key) VALUES (?,?,?)",
                (u["username"], hashed, u["api_key"]),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass   # user already exists — skip
    conn.commit()
    if inserted:
        print(f"  [OK] {inserted} user(s) seeded (passwords hashed with bcrypt)")
    else:
        print("  [OK] Users already exist — skipped seeding")


def seed_default_buckets(conn):
    """Give every user a 'default' bucket if they don't already have one."""
    users   = conn.execute("SELECT id FROM users").fetchall()
    created = 0
    for user in users:
        try:
            conn.execute(
                "INSERT INTO buckets (user_id, name) VALUES (?,?)",
                (user["id"], "default"),
            )
            created += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    if created:
        print(f"  [OK] Created {created} default bucket(s)")


def print_summary(conn):
    rows = conn.execute(
        "SELECT id, username, api_key FROM users ORDER BY id"
    ).fetchall()
    # Map username -> plain-text password from SEED_USERS for display purposes.
    # The DB itself only stores the bcrypt hash, never the plain text.
    seed_pw = {u["username"]: u["password"] for u in SEED_USERS}

    print()
    print("  ┌─────┬────────────────┬───────────┬──────────────────────────────────────┐")
    print("  │ ID  │ Username       │ Password  │ API Key                              │")
    print("  ├─────┼────────────────┼───────────┼──────────────────────────────────────┤")
    for r in rows:
        pw_display = seed_pw.get(r["username"], "(custom)")
        print(f"  │ {r['id']:<3} │ {r['username']:<14} │ {pw_display:<9} │ {r['api_key']} │")
    print("  └─────┴────────────────┴───────────┴──────────────────────────────────────┘")
    print()
    print("  Note: passwords are stored as bcrypt hashes in the database.")
    print("  The table above shows the original plain-text password for login purposes only.")
    print()
    print("  Login at http://127.0.0.1:8000/login")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*62}")
    print("  STRATA v0.9.1 — Complete Database Init")
    print(f"{'='*62}\n")

    init_storage_dirs()

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    print(f"  [OK] Connected to {DB_NAME}")

    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        if "users" in tables:
            print("  [INFO] Existing DB detected — running migration...")
            migrate_existing_db(conn)
        else:
            create_schema(conn)

        seed_users(conn)
        seed_default_buckets(conn)
        print_summary(conn)

    except sqlite3.Error as exc:
        conn.rollback()
        raise SystemExit(f"  [FATAL] {exc}")
    finally:
        conn.close()

    print(f"\n{'='*62}")
    print("  Setup complete.")
    print(f"  Tables: users, buckets, files, billing_records,")
    print(f"          tier_events, request_logs, bandwidth_logs, payments")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
