"""
add_payments.py
One-time migration — adds payments table to cloud_storage.db.
Run with ALL other processes stopped.
"""

import sqlite3

DB_NAME = "cloud_storage.db"

DDL = """
CREATE TABLE IF NOT EXISTS payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_id      TEXT    UNIQUE NOT NULL,
    amount          REAL    NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'INR',
    status          TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','paid','failed')),
    storage_charge  REAL    NOT NULL DEFAULT 0.0,
    request_charge  REAL    NOT NULL DEFAULT 0.0,
    bandwidth_charge REAL   NOT NULL DEFAULT 0.0,
    card_last4      TEXT,
    card_type       TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at         TIMESTAMP
);
"""


def run():
    print(f"\n{'='*55}")
    print("  STRATA — Payment Gateway Migration")
    print(f"{'='*55}\n")

    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(DDL)
        conn.commit()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        print(f"  Tables now: {tables}")
        print(f"  [OK] payments table ready.")
        print(f"\n  ✓ Migration complete. Restart all processes.\n")
    except Exception as exc:
        conn.rollback()
        print(f"\n  [FATAL] {exc}\n")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
