"""
database.py — STRATA database connection

A single, small module whose only job is opening a SQLite connection
with the project's standard settings. Every other module that needs
the database imports get_conn() from here.

Why a separate file just for one function?
  Once main.py is split into multiple route modules (auth.py, buckets.py,
  files.py, etc.), all of them need database connections. If get_conn()
  lived in main.py, each route module would have to import from main —
  which creates a circular import (main imports the route modules, the
  route modules import from main). Putting get_conn() in its own neutral
  file breaks that circle cleanly: database.py imports only from config,
  and everything else imports from database.py without any cycle forming.

PRAGMA foreign_keys = ON:
  SQLite doesn't enforce foreign key constraints by default — it has to
  be enabled explicitly per-connection. This line means that if you
  accidentally try to insert a file record referencing a user_id that
  doesn't exist, SQLite will reject it with an error rather than silently
  allowing orphaned data.

conn.row_factory = sqlite3.Row:
  Without this, SQLite returns rows as plain tuples: row[0], row[1], etc.
  With Row as the factory, rows behave like dictionaries: row["filename"],
  row["storage_tier"] — much more readable and less error-prone, since
  you're not depending on column order.
"""

import sqlite3

import config


def get_conn() -> sqlite3.Connection:
    """Open and return a configured SQLite connection.
    The caller is responsible for closing the connection when done —
    always use try/finally or a context manager to guarantee this,
    since SQLite file locks are held until the connection is closed.
    """
    conn = sqlite3.connect(config.DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
