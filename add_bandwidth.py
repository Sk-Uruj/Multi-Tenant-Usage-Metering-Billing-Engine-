"""
add_bandwidth.py
One-time migration — adds bandwidth_logs table to cloud_storage.db.
Run with ALL other processes stopped.
"""

import sqlite3

DB_NAME = "cloud_storage.db"

DDL = """
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


def run():
    print(f"\n{'='*55}")
    print("  STRATA — Bandwidth Metering Migration")
    print(f"{'='*55}\n")

    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(DDL)
        conn.commit()

        # Verify
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        print(f"  Tables now: {tables}")
        print(f"  [OK] bandwidth_logs table ready.")
        print(f"\n  ✓ Migration complete. Restart all processes.\n")
    except Exception as exc:
        conn.rollback()
        print(f"\n  [FATAL] {exc}\n")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
