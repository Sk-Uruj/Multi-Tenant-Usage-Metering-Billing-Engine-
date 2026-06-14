# STRATA — Project Documentation

## Overview

STRATA is a lightweight, multi-tenant cloud-storage simulator designed for local development and demonstration. It models an object storage system with bucket namespaces, a 4-tier storage lifecycle (HOT → COOL → COLD → ARCHIVE), usage-based billing, request logging, and a web UI.

Primary goals:
- Demonstrate tiered storage lifecycle and promotion/demotion behavior.
- Show per-tenant usage billing (size × time × tier-rate) and request-based charges.
- Provide a simple multi-tenant API + UI for uploads, downloads, bucket management.

---

## High-level components

- `main.py` — FastAPI app serving API endpoints and Jinja2 templates (dashboard, invoice, login). Handles session auth, uploads/downloads, bucket management, request logging via middleware, and utilities (key rotation, system reset).
- `init_db.py` — Creates the SQLite schema, seeds developer users, and initializes storage directories. Performs non-destructive migrations for existing DBs.
- `add_buckets.py` — One-time migration to add `buckets` table, add `bucket_id` to `files`, create `default` bucket for users and move files into per-bucket directories.
- `add_request_logs.py` — One-time migration to add `request_logs` table.
- `tiering_engine.py` — Background poller that demotes files from HOT→COOL→COLD→ARCHIVE when they have been idle long enough; moves files on disk and writes `tier_events`.
- `billing_engine.py` — Background poller that computes per-file, per-tier cost windows and aggregates per-tenant invoices; updates `billing_records`.
- `templates/` — Jinja2 templates: `dashboard.html`, `invoice.html`, `login.html` with inline CSS/JS.
- `storage/` — Filesystem storage under `storage/hot|cool|cold|archive/{username}/{bucket}/{filename}`.
- `cloud_storage.db` — SQLite database file (created by `init_db.py`).

---

## Detailed feature list

### Authentication & Sessions
- Session-based login at `/login` backed by Starlette `SessionMiddleware`.
- Developer users are seeded by `init_db.py` (alice_dev, bob_staging, carol_prod).
- Current implementation stores plaintext passwords in the seeded DB (dev convenience) — MUST be changed for production.

### Bucket / Namespace System
- Create bucket: `PUT /buckets/{bucket_name}` — validates name (lowercase, 3–63 chars, alphanum and hyphens), inserts DB row, and creates physical directories in all tiers.
- List buckets: `GET /buckets` — returns list of buckets, file counts, total MB per bucket for the authenticated tenant.
- List bucket objects: `GET /buckets/{bucket_name}` — returns files in a bucket.
- Delete bucket: `DELETE /buckets/{bucket_name}` — only allowed when bucket is empty (enforced by DB query). Removes DB row and physical directories.
- Migration: `add_buckets.py` moves an existing flat per-tenant storage layout into per-bucket directories and creates `default` buckets.

### Object operations
- Upload: `POST /upload` — multipart form with `file` and optional `bucket` (defaults to `default`). Stores file in HOT tier on disk, inserts `files` DB record with `hot_entered_at` set.
- Download: `GET /files/{bucket}/{filename}` — verifies DB record and file existence, performs `promote_to_hot()` which moves file to HOT tier, updates DB and logs a `PROMOTE` event. Returns `FileResponse` with headers `X-Storage-Tier` and `X-Bucket`.
- Delete object: `DELETE /files/{bucket}/{filename}` — deletes from disk if present and removes DB record.
- List all files: `GET /files` — flat listing of all tenant files across buckets.

### Tiering lifecycle
- Tiers: HOT, COOL, COLD, ARCHIVE (configured in `TIER_DIRS`).
- Demotion thresholds are constants in `tiering_engine.py` (seconds). Tiering worker moves files on disk and updates `files` table (`storage_tier` and `*_entered_at`).
- Promotions happen on download: file moved to HOT, `hot_entered_at` updated, and a `PROMOTE` event logged.
- `tier_events` table tracks DEMOTE and PROMOTE events for audit/billing.

### Request logging & billing
- `RequestLoggerMiddleware` classifies endpoints into operation classes: A (writes), B (reads), FREE (deletes). It writes `request_logs` entries for request billing.
- Billing engine computes each file’s time spent in each tier using the `*_entered_at` timestamps. Cost = size_mb × seconds × rate_per_tier.
- Billing aggregates per-user storage cost + request cost and upserts `billing_records`.
- `/invoice` renders per-file breakdown and totals (mirrors billing logic).

### Admin & maintenance
- API Key rotation: `POST /me/regenerate-key` generates a new UUID API key and updates the `users` table.
- System reset: `POST /system/reset` deletes `request_logs`, `tier_events`, `billing_records`, `files`, and `buckets` while keeping `users`. Recreates `storage/*` directories. Use with caution.

---

## Database schema (summary)

Defined / created by `init_db.py` (DDL excerpts):

- `users` (id, username UNIQUE, password, api_key UNIQUE)
- `files` (id, user_id REFERENCES users, bucket_id REFERENCES buckets, filename, file_size_mb, storage_tier CHECK('HOT','COOL','COLD','ARCHIVE'), created_at, last_accessed_at, hot_entered_at, cool_entered_at, cold_entered_at, archive_entered_at)
- `buckets` (id, user_id REFERENCES users, name, created_at, UNIQUE(user_id,name)) — added by `add_buckets.py` migration
- `tier_events` (id, file_id REFERENCES files, user_id REFERENCES users, from_tier, to_tier, event_type IN ('DEMOTE','PROMOTE'), occurred_at)
- `request_logs` (id, user_id REFERENCES users, endpoint, method, op_class IN ('A','B','FREE'), occurred_at) — added by migration
- `billing_records` (id, user_id REFERENCES users, total_hours_tracked, amount_owed, last_calculated_at)

Notes:
- Foreign keys use `ON DELETE` behavior in various places. `init_db.py` enables `PRAGMA foreign_keys` and sets journal mode to WAL.

---

## Storage layout

Filesystem under repository:

```
storage/
  hot/{username}/{bucket}/{filename}
  cool/{username}/{bucket}/{filename}
  cold/{username}/{bucket}/{filename}
  archive/{username}/{bucket}/{filename}
```

`init_db.py` creates these directories. `add_buckets.py` migrates older per-tenant flat directories into `.../{bucket}/` subdirectories.

---

## API reference (endpoints)

- POST /login — form fields `username`, `password` → sets session cookie.
- GET /logout — clears session and redirects to login.
- PUT /buckets/{bucket_name} — create a new bucket (auth required). Validates name; returns 201.
- GET /buckets — list buckets and per-bucket stats.
- GET /buckets/{bucket_name} — list objects in a bucket.
- DELETE /buckets/{bucket_name} — delete empty bucket.
- POST /upload — multipart form: `file` and `bucket`. Stores file in HOT and records DB entry.
- GET /files — list all tenant files.
- GET /files/{bucket}/{filename} — download file and promote to HOT.
- DELETE /files/{bucket}/{filename} — delete object.
- GET / — dashboard (template).
- GET /invoice — printable invoice (template).
- GET /me — returns session user JSON.
- POST /me/regenerate-key — rotates API key and returns new key.
- POST /system/reset — wipes data tables but preserves users.

Authentication: session cookie. No token-based or API-key bearer auth implemented for endpoints (key rotation exists but not used for auth flows in `main.py`).

---

## Background workers

- `tiering_engine.py` (polling):
  - Poll interval `POLL_INTERVAL_SECS` (default 15s). Demotion thresholds defined as constants.
  - Finds files in a tier, checks `last_accessed_at`, moves files if idle beyond threshold, updates DB `storage_tier` and `*_entered_at`, logs `tier_events` with event_type `DEMOTE`.

- `billing_engine.py` (polling):
  - Poll interval `POLL_INTERVAL_SECS` (default 10s).
  - Reads `files` and `users`, computes windows of time spent in each tier using entered timestamps, calculates cost using tier rates, aggregates per-user totals, computes request costs from `request_logs`, and upserts `billing_records`.

Both run as independent processes (simple infinite loops with sleeps) intended to be started separately.

---

## Run / dev instructions

1. Create DB schema, seed users, and storage dirs:

```powershell
python init_db.py
```

2. Start API (development):

```powershell
uvicorn main:app --reload
```

3. Start tiering engine (in separate terminal):

```powershell
python tiering_engine.py
```

4. Start billing engine (in separate terminal):

```powershell
python billing_engine.py
```

5. (Optional) Run migrations when upgrading:
- `python add_buckets.py` — adds `buckets` and moves files into default buckets.
- `python add_request_logs.py` — adds the `request_logs` table.

Templates are reachable via the browser at `http://127.0.0.1:8000/` after login.

Dev credentials (seeded):
- `alice_dev` / `alice123`
- `bob_staging` / `bob456`
- `carol_prod` / `carol789`

---

## Exact tech stack

- Language: Python 3.x
- Web framework: FastAPI (Starlette middleware)
- ASGI server: Uvicorn (recommended)
- Templates: Jinja2
- Database: SQLite (`sqlite3` stdlib)
- Storage: Local filesystem under `storage/`
- Background workers: Plain Python scripts with poll loops (`tiering_engine.py`, `billing_engine.py`)
- Libraries: `fastapi`, `uvicorn`, `jinja2`, plus Python stdlib modules (os, sqlite3, shutil, uuid, datetime)

---

## Security considerations (urgent)

- Passwords should be stored using a secure hash (bcrypt or argon2). Replace plaintext storage and alter login flow to verify hashes.
- `SESSION_SECRET` in `main.py` is hardcoded. Move to environment variables and set `https_only=True` behind TLS.
- No rate limiting, CSRF protection for forms, or input sanitization for filenames — add these for production.
- SQLite + local filesystem are not robust for concurrent, multi-instance production deployments. Migrate to a server DB (Postgres) and object storage (S3/COS) before production.
- Sanitize filenames to avoid traversal; enforce size/type limits on uploads.

---

## Limitations

- No automated tests included.
- Billing uses floating-point math and per-second granularity — acceptable for demo but not financial ledger-grade.
- Worker design uses polling loops; consider job runners or task queues for production.
- API-key authentication not enforced on endpoints; current UI uses session cookies only.

---

## Recommendations (prioritized)

1. Hash user passwords and remove dev credentials from UI.
2. Move secrets (session secret) to environment variables; enable HTTPS.
3. Add `requirements.txt` and a `README.md` with quick start.
4. Migrate DB to Postgres and adopt schema migrations (Alembic).
5. Add tests for critical logic (tiering transitions and billing calculations).
6. Sanitize and validate uploaded filenames and sizes.
7. Implement token-based API auth if programmatic access is needed.
8. Replace polling workers with scheduled tasks or task queue and add health/liveness endpoints.

---

## File map

- `main.py` — API + UI
- `init_db.py` — DB init + seed
- `add_buckets.py` — buckets migration
- `add_request_logs.py` — request logs migration
- `tiering_engine.py` — tiering worker
- `billing_engine.py` — billing worker
- `templates/dashboard.html`, `templates/invoice.html`, `templates/login.html`
- `storage/` — physical object store

---

## Next steps I can do for you (pick any):
- Generate `README.md` and `requirements.txt` with explicit run commands.
- Implement password hashing & update login flow.
- Add minimal API-key authentication for programmatic endpoints.
- Create unit tests for billing and tiering logic.

---

*Document generated on: 2026-06-01*
