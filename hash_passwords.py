"""
hash_passwords.py
One-time migration — converts existing plain-text passwords in
cloud_storage.db to bcrypt hashes, in place.

Run with ALL other processes stopped (uvicorn, tiering_engine, billing_engine).

Safe to run multiple times — it detects already-hashed passwords
(bcrypt hashes always start with $2b$, $2a$, or $2y$) and skips them,
so re-running this script never double-hashes anyone's password.
"""

import sqlite3
import bcrypt

DB_NAME = "cloud_storage.db"


def is_bcrypt_hash(value: str) -> bool:
    """bcrypt hashes always start with $2a$, $2b$, or $2y$ followed by a cost factor."""
    return isinstance(value, str) and value.startswith(("$2a$", "$2b$", "$2y$"))


def run():
    print(f"\n{'='*55}")
    print("  STRATA — Password Hashing Migration (bcrypt)")
    print(f"{'='*55}\n")

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row

    try:
        users = conn.execute("SELECT id, username, password FROM users").fetchall()

        if not users:
            print("  [INFO] No users found. Nothing to do.")
            return

        hashed_count  = 0
        skipped_count = 0

        for user in users:
            user_id  = user["id"]
            username = user["username"]
            password = user["password"]

            if is_bcrypt_hash(password):
                print(f"  [SKIP] {username} — already hashed")
                skipped_count += 1
                continue

            # Hash the plain-text password
            hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
            hashed_str = hashed.decode("utf-8")

            conn.execute(
                "UPDATE users SET password=? WHERE id=?",
                (hashed_str, user_id),
            )
            print(f"  [OK]   {username} — password hashed")
            hashed_count += 1

        conn.commit()

        print(f"\n  ✓ Migration complete.")
        print(f"    Hashed:  {hashed_count}")
        print(f"    Skipped: {skipped_count} (already hashed)")
        print(f"\n  Login credentials are UNCHANGED — users still log in with")
        print(f"  their original plain-text password (e.g. alice123).")
        print(f"  Only the STORED value in the database is now a bcrypt hash.\n")

    except sqlite3.Error as exc:
        conn.rollback()
        print(f"\n  [FATAL] {exc}\n")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
