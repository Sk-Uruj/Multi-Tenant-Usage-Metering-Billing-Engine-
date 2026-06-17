"""
STRATA v0.9.1 — main.py
Features:
  - Bucket/Namespace System (IBM COS ListBuckets)
  - Bandwidth/Egress Metering (IBM COS 3-dimension billing)
  - Payment Gateway (simulated, INR)
  - All rates in INR via USD_TO_INR conversion at startup

Fixes applied:
  - get_session_user returns JSON 401 for API/fetch calls (not HTML redirect)
  - USD_TO_INR default corrected to 83.5
"""

import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_NAME        = "cloud_storage.db"
SESSION_SECRET = "CHANGE_THIS_IN_PRODUCTION"

TIER_DIRS = {
    "HOT":     "storage/hot",
    "COOL":    "storage/cool",
    "COLD":    "storage/cold",
    "ARCHIVE": "storage/archive",
}

TIER_RATES = {
    "HOT":     0.001000,
    "COOL":    0.000400,
    "COLD":    0.000200,
    "ARCHIVE": 0.000050,
}

TIER_STYLES = {
    "HOT":     {"bg": "#FF2D6B", "color": "#fff"},
    "COOL":    {"bg": "#0057FF", "color": "#fff"},
    "COLD":    {"bg": "#00FFD1", "color": "#000"},
    "ARCHIVE": {"bg": "#888",    "color": "#fff"},
}

REQUEST_RATES = {
    "A":    0.005  / 1000,
    "B":    0.0004 / 1000,
    "FREE": 0.0,
}

# IBM COS egress: $0.0087/GB = $0.0087/1024 per MB
BANDWIDTH_RATE_PER_MB = 0.0087 / 1024

# ---------------------------------------------------------------------------
# Currency conversion — INR
# Set USD_TO_INR env variable to override. Default: 83.5
# ---------------------------------------------------------------------------
USD_TO_INR = float(os.getenv("USD_TO_INR", "83.5"))

for k in list(TIER_RATES.keys()):
    TIER_RATES[k] = round(TIER_RATES[k] * USD_TO_INR, 9)

for k in list(REQUEST_RATES.keys()):
    REQUEST_RATES[k] = REQUEST_RATES[k] * USD_TO_INR

BANDWIDTH_RATE_PER_MB = BANDWIDTH_RATE_PER_MB * USD_TO_INR

# ---------------------------------------------------------------------------
app = FastAPI(title="STRATA — Multi-Tenant Cloud Storage Engine", version="0.9.1")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=False)
templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

class RequestLoggerMiddleware(BaseHTTPMiddleware):
    def classify(self, method, path):
        if method == "POST"   and path == "/upload":              return "A"
        if method == "PUT"    and path.startswith("/buckets"):    return "A"
        if method == "GET"    and path.startswith("/buckets"):    return "B"
        if method == "GET"    and path.startswith("/files"):      return "B"
        if method == "DELETE" and path.startswith("/files"):      return "FREE"
        if method == "DELETE" and path.startswith("/buckets"):    return "FREE"
        return None

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        op_class = self.classify(request.method, request.url.path)
        if op_class is None or response.status_code >= 400:
            return response
        user_id = request.session.get("user_id")
        if not user_id:
            return response
        try:
            conn = get_conn()
            conn.execute(
                "INSERT INTO request_logs (user_id,endpoint,method,op_class,occurred_at) VALUES (?,?,?,?,?)",
                (user_id, request.url.path, request.method,
                 op_class, datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return response

app.add_middleware(RequestLoggerMiddleware)

# ---------------------------------------------------------------------------
# Auth dependency — FIX: returns JSON 401 for API/fetch calls
# ---------------------------------------------------------------------------

def get_session_user(request: Request) -> dict:
    user_id = request.session.get("user_id")
    if not user_id:
        # Detect whether this is a browser navigation or a fetch/API call
        accept = request.headers.get("accept", "")
        if "text/html" in accept and "application/json" not in accept:
            # Browser navigation — redirect to login page
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        else:
            # fetch() / API call — return JSON 401, not HTML redirect
            raise HTTPException(status_code=401, detail="Session expired. Please log in.")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, api_key FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session invalid. Please log in.")
    return dict(row)

SessionUser = Annotated[dict, Depends(get_session_user)]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def tier_file_path(tier: str, username: str, bucket: str, filename: str) -> str:
    return os.path.join(TIER_DIRS[tier], username, bucket, filename)


def get_bucket(conn, user_id: int, bucket_name: str):
    row = conn.execute(
        "SELECT * FROM buckets WHERE user_id=? AND name=?",
        (user_id, bucket_name),
    ).fetchone()
    return dict(row) if row else None


def log_tier_event(conn, file_id, user_id, from_tier, to_tier, event_type, ts):
    conn.execute(
        "INSERT INTO tier_events (file_id,user_id,from_tier,to_tier,event_type,occurred_at) VALUES (?,?,?,?,?,?)",
        (file_id, user_id, from_tier, to_tier, event_type, ts),
    )


def promote_to_hot(conn, row: dict, username: str, bucket: str, now_iso: str):
    current  = row["storage_tier"]
    file_id  = row["id"]
    user_id  = row["user_id"]
    filename = row["filename"]

    if current == "HOT":
        conn.execute(
            "UPDATE files SET last_accessed_at=? WHERE id=?",
            (now_iso, file_id),
        )
        return

    src     = tier_file_path(current, username, bucket, filename)
    dst_dir = os.path.join(TIER_DIRS["HOT"], username, bucket)
    dst     = os.path.join(dst_dir, filename)

    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst)

    conn.execute(
        "UPDATE files SET storage_tier='HOT', last_accessed_at=?, hot_entered_at=? WHERE id=?",
        (now_iso, now_iso, file_id),
    )
    log_tier_event(conn, file_id, user_id, current, "HOT", "PROMOTE", now_iso)


def log_bandwidth(conn, user_id, file_id, bucket, filename, size_mb, direction="egress"):
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

# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None}
    )


@app.post("/login", include_in_schema=False)
def login_submit(request: Request,
                 username: str = Form(...),
                 password: str = Form(...)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, api_key FROM users WHERE username=? AND password=?",
            (username, password),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Invalid username or password."},
            status_code=401,
        )
    request.session["user_id"]  = row["id"]
    request.session["username"] = row["username"]
    return RedirectResponse(url="/", status_code=302)


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

# ---------------------------------------------------------------------------
# BUCKET ROUTES
# ---------------------------------------------------------------------------

@app.put("/buckets/{bucket_name}", summary="Create a bucket")
def create_bucket(bucket_name: str, current_user: SessionUser):
    if not re.match(r'^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$', bucket_name):
        raise HTTPException(
            status_code=400,
            detail="Bucket name must be 3-63 chars, lowercase alphanumeric and hyphens only.",
        )
    user_id = current_user["id"]
    conn    = get_conn()
    try:
        conn.execute(
            "INSERT INTO buckets (user_id, name, created_at) VALUES (?,?,?)",
            (user_id, bucket_name, datetime.utcnow().isoformat()),
        )
        conn.commit()
        for tier_dir in TIER_DIRS.values():
            os.makedirs(
                os.path.join(tier_dir, current_user["username"], bucket_name),
                exist_ok=True,
            )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Bucket '{bucket_name}' already exists.")
    finally:
        conn.close()

    return JSONResponse(status_code=201, content={
        "message":    f"Bucket '{bucket_name}' created.",
        "owner":      current_user["username"],
        "bucket":     bucket_name,
        "created_at": datetime.utcnow().isoformat(),
    })


@app.get("/buckets", summary="List all buckets (ListBuckets)")
def list_buckets(current_user: SessionUser):
    user_id = current_user["id"]
    conn    = get_conn()
    try:
        buckets = conn.execute(
            "SELECT id, name, created_at FROM buckets WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        result = []
        for b in buckets:
            stats = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(file_size_mb),0) as total_mb FROM files WHERE bucket_id=? AND user_id=?",
                (b["id"], user_id),
            ).fetchone()
            result.append({
                "name":       b["name"],
                "created_at": b["created_at"],
                "file_count": stats["cnt"],
                "total_mb":   round(stats["total_mb"], 4),
            })
    finally:
        conn.close()
    return {"owner": current_user["username"], "buckets": result, "count": len(result)}


@app.get("/buckets/{bucket_name}", summary="List objects in a bucket")
def list_bucket_objects(bucket_name: str, current_user: SessionUser):
    user_id = current_user["id"]
    conn    = get_conn()
    try:
        bucket = get_bucket(conn, user_id, bucket_name)
        if bucket is None:
            raise HTTPException(status_code=404, detail=f"Bucket '{bucket_name}' not found.")
        objects = conn.execute(
            "SELECT id, filename, file_size_mb, storage_tier, created_at, last_accessed_at FROM files WHERE bucket_id=? AND user_id=? ORDER BY created_at DESC",
            (bucket["id"], user_id),
        ).fetchall()
    finally:
        conn.close()
    return {"bucket": bucket_name, "owner": current_user["username"],
            "objects": [dict(r) for r in objects], "count": len(objects)}


@app.delete("/buckets/{bucket_name}", summary="Delete a bucket")
def delete_bucket(bucket_name: str, current_user: SessionUser):
    user_id = current_user["id"]
    conn    = get_conn()
    try:
        bucket = get_bucket(conn, user_id, bucket_name)
        if bucket is None:
            raise HTTPException(status_code=404, detail=f"Bucket '{bucket_name}' not found.")
        count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE bucket_id=? AND user_id=?",
            (bucket["id"], user_id),
        ).fetchone()[0]
        if count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Bucket '{bucket_name}' is not empty ({count} object(s)). Delete all objects first.",
            )
        conn.execute("DELETE FROM buckets WHERE id=?", (bucket["id"],))
        conn.commit()
        for tier_dir in TIER_DIRS.values():
            path = os.path.join(tier_dir, current_user["username"], bucket_name)
            if os.path.exists(path):
                shutil.rmtree(path)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()
    return JSONResponse(status_code=200, content={
        "message": f"Bucket '{bucket_name}' deleted.", "owner": current_user["username"],
    })

# ---------------------------------------------------------------------------
# UPLOAD
# ---------------------------------------------------------------------------

@app.post("/upload", summary="Upload a file to a bucket")
async def upload_file(
    current_user: SessionUser,
    file:   UploadFile = File(...),
    bucket: str        = Form(default="default"),
):
    username = current_user["username"]
    user_id  = current_user["id"]

    conn = get_conn()
    try:
        bucket_row = get_bucket(conn, user_id, bucket)
        if bucket_row is None:
            if bucket == "default":
                conn.execute(
                    "INSERT INTO buckets (user_id, name, created_at) VALUES (?,?,?)",
                    (user_id, "default", datetime.utcnow().isoformat()),
                )
                conn.commit()
                bucket_row = dict(conn.execute(
                    "SELECT * FROM buckets WHERE user_id=? AND name='default'", (user_id,)
                ).fetchone())
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"Bucket '{bucket}' not found. Create it first with PUT /buckets/{bucket}",
                )

        bucket_id    = bucket_row["id"]
        contents     = await file.read()
        file_size_mb = len(contents) / (1024 * 1024)
        user_dir     = os.path.join(TIER_DIRS["HOT"], username, bucket)
        os.makedirs(user_dir, exist_ok=True)
        file_path = os.path.join(user_dir, file.filename)
        with open(file_path, "wb") as f:
            f.write(contents)

        now = datetime.utcnow().isoformat()
        cursor = conn.execute(
            "INSERT INTO files (user_id, bucket_id, filename, file_size_mb, storage_tier, created_at, last_accessed_at, hot_entered_at) VALUES (?,?,?,?,'HOT',?,?,?)",
            (user_id, bucket_id, file.filename, file_size_mb, now, now, now),
        )
        log_bandwidth(conn, user_id, cursor.lastrowid, bucket, file.filename, file_size_mb, "ingress")
        conn.commit()

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Filesystem error: {exc}")
    finally:
        conn.close()

    return JSONResponse(status_code=201, content={
        "message":      "File uploaded to HOT storage.",
        "record_id":    cursor.lastrowid,
        "owner":        username,
        "bucket":       bucket,
        "filename":     file.filename,
        "file_path":    file_path,
        "file_size_mb": round(file_size_mb, 4),
        "storage_tier": "HOT",
        "uploaded_at":  now,
    })

# ---------------------------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------------------------

@app.get("/files/{bucket}/{filename}", summary="Download a file from a bucket")
def download_file(bucket: str, filename: str, current_user: SessionUser):
    username = current_user["username"]
    user_id  = current_user["id"]

    conn = get_conn()
    try:
        bucket_row = get_bucket(conn, user_id, bucket)
        if bucket_row is None:
            raise HTTPException(status_code=404, detail=f"Bucket '{bucket}' not found.")

        row = conn.execute(
            "SELECT id, filename, storage_tier, user_id, file_size_mb FROM files WHERE user_id=? AND bucket_id=? AND filename=? ORDER BY created_at DESC LIMIT 1",
            (user_id, bucket_row["id"], filename),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail=f"'{filename}' not found in bucket '{bucket}'.")

        row_dict  = dict(row)
        current   = row_dict["storage_tier"]
        file_path = tier_file_path(current, username, bucket, filename)

        if not os.path.exists(file_path):
            raise HTTPException(status_code=410, detail="File missing from disk.")

        now_iso = datetime.utcnow().isoformat()
        promote_to_hot(conn, row_dict, username, bucket, now_iso)
        log_bandwidth(conn, user_id, row_dict["id"], bucket, filename, row_dict["file_size_mb"], "egress")
        conn.commit()

        hot_path = tier_file_path("HOT", username, bucket, filename)

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()

    return FileResponse(
        path=hot_path if os.path.exists(hot_path) else file_path,
        filename=filename,
        media_type="application/octet-stream",
        headers={"X-Storage-Tier": current, "X-Bucket": bucket},
    )

# ---------------------------------------------------------------------------
# DELETE OBJECT
# ---------------------------------------------------------------------------

@app.delete("/files/{bucket}/{filename}", summary="Delete a file from a bucket")
def delete_file(bucket: str, filename: str, current_user: SessionUser):
    username = current_user["username"]
    user_id  = current_user["id"]

    conn = get_conn()
    try:
        bucket_row = get_bucket(conn, user_id, bucket)
        if bucket_row is None:
            raise HTTPException(status_code=404, detail=f"Bucket '{bucket}' not found.")

        row = conn.execute(
            "SELECT id, storage_tier FROM files WHERE user_id=? AND bucket_id=? AND filename=? LIMIT 1",
            (user_id, bucket_row["id"], filename),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail=f"'{filename}' not found.")

        fp = tier_file_path(row["storage_tier"], username, bucket, filename)
        try:
            os.remove(fp)
        except FileNotFoundError:
            pass

        conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
        conn.commit()

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()

    return JSONResponse(status_code=200, content={
        "message": f"'{filename}' deleted from bucket '{bucket}'.",
        "owner":   username, "bucket": bucket,
    })

# ---------------------------------------------------------------------------
# LIST FILES
# ---------------------------------------------------------------------------

@app.get("/files", summary="List all files across all buckets")
def list_files(current_user: SessionUser):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT f.id, f.filename, f.file_size_mb, f.storage_tier,
                   f.created_at, f.last_accessed_at, b.name as bucket_name
            FROM   files f
            LEFT JOIN buckets b ON b.id = f.bucket_id
            WHERE  f.user_id=? ORDER BY f.created_at DESC
            """,
            (current_user["id"],),
        ).fetchall()
    finally:
        conn.close()
    files = [dict(r) for r in rows]
    return {
        "username":      current_user["username"],
        "total_files":   len(files),
        "total_size_mb": round(sum(f["file_size_mb"] for f in files), 4),
        "files":         files,
    }

# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard(request: Request, current_user: SessionUser):
    user_id = current_user["id"]
    conn    = get_conn()
    try:
        files = conn.execute(
            """
            SELECT f.id, f.filename, f.file_size_mb, f.storage_tier,
                   f.created_at, f.last_accessed_at,
                   f.hot_entered_at, f.cool_entered_at,
                   f.cold_entered_at, f.archive_entered_at,
                   b.name as bucket_name
            FROM   files f
            LEFT JOIN buckets b ON b.id = f.bucket_id
            WHERE  f.user_id=? ORDER BY f.created_at DESC
            """,
            (user_id,),
        ).fetchall()

        buckets = conn.execute(
            """
            SELECT bk.id, bk.name, bk.created_at,
                   COUNT(f.id) as file_count,
                   COALESCE(SUM(f.file_size_mb),0) as total_mb
            FROM   buckets bk
            LEFT JOIN files f ON f.bucket_id = bk.id
            WHERE  bk.user_id=?
            GROUP  BY bk.id ORDER BY bk.created_at
            """,
            (user_id,),
        ).fetchall()

        billing = conn.execute(
            "SELECT * FROM billing_records WHERE user_id=?", (user_id,)
        ).fetchone()

        events = conn.execute(
            """
            SELECT te.from_tier, te.to_tier, te.event_type,
                   te.occurred_at, f.filename, b.name as bucket_name
            FROM   tier_events te
            JOIN   files f ON f.id = te.file_id
            LEFT JOIN buckets b ON b.id = f.bucket_id
            WHERE  te.user_id=?
            ORDER  BY te.occurred_at DESC LIMIT 10
            """,
            (user_id,),
        ).fetchall()

        req_summary = conn.execute(
            "SELECT op_class, COUNT(*) as cnt FROM request_logs WHERE user_id=? GROUP BY op_class",
            (user_id,),
        ).fetchall()

        bw_summary = conn.execute(
            """
            SELECT direction, COALESCE(SUM(mb_transferred),0) as total_mb, COUNT(*) as ops
            FROM   bandwidth_logs WHERE user_id=? GROUP BY direction
            """,
            (user_id,),
        ).fetchall()

        dash_paid = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as total_paid FROM payments WHERE user_id=? AND status='paid'",
            (user_id,),
        ).fetchone()

    finally:
        conn.close()

    files_list    = [dict(r) for r in files]
    events_list   = [dict(r) for r in events]
    req_counts    = {r["op_class"]: r["cnt"] for r in req_summary}
    bw_totals     = {r["direction"]: {"mb": round(r["total_mb"], 4), "ops": r["ops"]} for r in bw_summary}
    counts        = {t: sum(1 for f in files_list if f["storage_tier"] == t) for t in TIER_DIRS}
    req_cost      = req_counts.get("A", 0) * REQUEST_RATES["A"] + req_counts.get("B", 0) * REQUEST_RATES["B"]
    egress_mb     = bw_totals.get("egress", {}).get("mb", 0)
    bw_cost       = round(egress_mb * BANDWIDTH_RATE_PER_MB, 8)
    total_charges = round((billing["amount_owed"] if billing else 0) + req_cost + bw_cost, 6)
    payments_made = round(dash_paid["total_paid"], 6) if dash_paid else 0.0
    balance_due   = round(max(total_charges - payments_made, 0), 6)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user":          current_user,
            "files":         files_list,
            "buckets":       [dict(r) for r in buckets],
            "billing":       dict(billing) if billing else None,
            "events":        events_list,
            "tier_styles":   TIER_STYLES,
            "tier_rates":    TIER_RATES,
            "counts":        counts,
            "now":           datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_mb":      round(sum(f["file_size_mb"] for f in files_list), 4),
            "req_counts":    req_counts,
            "req_cost":      round(req_cost, 8),
            "bw_totals":     bw_totals,
            "bw_cost":       bw_cost,
            "bw_rate_per_mb": BANDWIDTH_RATE_PER_MB,
            "req_rates":     REQUEST_RATES,
            "egress_mb":     egress_mb,
            "total_charges": total_charges,
            "payments_made": payments_made,
            "balance_due":   balance_due,
        },
    )

# ---------------------------------------------------------------------------
# INVOICE
# ---------------------------------------------------------------------------

@app.get("/invoice", include_in_schema=False)
def invoice_page(request: Request, current_user: SessionUser):
    user_id = current_user["id"]
    now     = datetime.utcnow()

    conn = get_conn()
    try:
        files = conn.execute(
            """
            SELECT f.*, b.name as bucket_name
            FROM   files f
            LEFT JOIN buckets b ON b.id = f.bucket_id
            WHERE  f.user_id=? ORDER BY f.created_at DESC
            """,
            (user_id,),
        ).fetchall()

        billing    = conn.execute("SELECT * FROM billing_records WHERE user_id=?", (user_id,)).fetchone()
        first_file = conn.execute("SELECT MIN(created_at) FROM files WHERE user_id=?", (user_id,)).fetchone()[0]

        req_by_class = conn.execute(
            "SELECT op_class, COUNT(*) as cnt FROM request_logs WHERE user_id=? GROUP BY op_class",
            (user_id,),
        ).fetchall()
        recent_reqs = conn.execute(
            "SELECT method, endpoint, op_class, occurred_at FROM request_logs WHERE user_id=? ORDER BY occurred_at DESC LIMIT 20",
            (user_id,),
        ).fetchall()

        bw_by_direction = conn.execute(
            """
            SELECT direction,
                   COALESCE(SUM(mb_transferred),0) as total_mb,
                   COALESCE(SUM(bytes_transferred),0) as total_bytes,
                   COUNT(*) as ops
            FROM   bandwidth_logs WHERE user_id=? GROUP BY direction
            """,
            (user_id,),
        ).fetchall()

        recent_bw = conn.execute(
            """
            SELECT bucket_name, filename, mb_transferred,
                   bytes_transferred, direction, occurred_at
            FROM   bandwidth_logs WHERE user_id=?
            ORDER  BY occurred_at DESC LIMIT 20
            """,
            (user_id,),
        ).fetchall()

        paid_row = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) as total_paid, COUNT(*) as payment_count
            FROM   payments WHERE user_id=? AND status='paid'
            """,
            (user_id,),
        ).fetchone()

    finally:
        conn.close()

    files_list  = [dict(r) for r in files]
    req_counts  = {r["op_class"]: r["cnt"] for r in req_by_class}
    recent_reqs = [dict(r) for r in recent_reqs]
    bw_stats    = {r["direction"]: {"mb": round(r["total_mb"], 4), "bytes": r["total_bytes"], "ops": r["ops"]} for r in bw_by_direction}
    recent_bw   = [dict(r) for r in recent_bw]

    egress_mb     = bw_stats.get("egress",  {}).get("mb", 0)
    ingress_mb    = bw_stats.get("ingress", {}).get("mb", 0)
    egress_ops    = bw_stats.get("egress",  {}).get("ops", 0)
    ingress_ops   = bw_stats.get("ingress", {}).get("ops", 0)
    egress_cost   = egress_mb * BANDWIDTH_RATE_PER_MB
    total_bw_cost = egress_cost

    def parse_dt(val):
        if not val: return None
        try: return datetime.fromisoformat(val)
        except: return None

    def fmt_duration(secs):
        secs = int(max(secs, 0))
        if secs < 60:   return f"{secs}s"
        if secs < 3600: return f"{secs//60}m {secs%60}s"
        return f"{secs//3600}h {(secs%3600)//60}m"

    file_costs    = {}
    grand_by_tier = {t: 0.0 for t in TIER_RATES}
    tier_costs    = {t: 0.0 for t in TIER_RATES}

    for f in files_list:
        size_mb    = f["file_size_mb"]
        created_dt = parse_dt(f["created_at"]) or now
        hot_in     = parse_dt(f["hot_entered_at"])  or created_dt
        cool_in    = parse_dt(f["cool_entered_at"])
        cold_in    = parse_dt(f["cold_entered_at"])
        arc_in     = parse_dt(f["archive_entered_at"])
        current    = f["storage_tier"]

        windows  = []
        hot_end  = cool_in or (now if current == "HOT"     else None)
        cool_end = cold_in or (now if current == "COOL"    else None)
        cold_end = arc_in  or (now if current == "COLD"    else None)
        if hot_in  and hot_end:  windows.append(("HOT",     hot_in,  hot_end))
        if cool_in and cool_end: windows.append(("COOL",    cool_in, cool_end))
        if cold_in and cold_end: windows.append(("COLD",    cold_in, cold_end))
        if arc_in  and current == "ARCHIVE": windows.append(("ARCHIVE", arc_in, now))

        costs = {t: 0.0 for t in TIER_RATES}
        windows_out = []
        total_secs  = 0

        for (tier, start, end) in windows:
            secs = max((end - start).total_seconds(), 0)
            cost = size_mb * secs * TIER_RATES[tier]
            costs[tier] += cost
            total_secs  += secs
            windows_out.append({
                "tier": tier,
                "from_str": start.strftime("%m/%d %H:%M:%S"),
                "to_str":   end.strftime("%m/%d %H:%M:%S"),
                "duration_str": fmt_duration(secs),
                "cost": cost,
            })

        total_cost = sum(costs.values())
        for t in TIER_RATES:
            grand_by_tier[t] += costs[t]
            tier_costs[t]    += costs[t]

        file_costs[f["id"]] = {
            "hot": costs["HOT"], "cool": costs["COOL"],
            "cold": costs["COLD"], "archive": costs["ARCHIVE"],
            "total": total_cost,
            "duration_str": fmt_duration(total_secs),
            "windows": windows_out,
            "bucket": f.get("bucket_name", "default"),
        }

    class_a_count  = req_counts.get("A",    0)
    class_b_count  = req_counts.get("B",    0)
    free_count     = req_counts.get("FREE", 0)
    class_a_cost   = class_a_count * REQUEST_RATES["A"]
    class_b_cost   = class_b_count * REQUEST_RATES["B"]
    total_req_cost = class_a_cost + class_b_cost
    storage_cost   = billing["amount_owed"] if billing else 0.0
    grand_total    = storage_cost + total_req_cost + total_bw_cost
    payments_made  = round(paid_row["total_paid"],  6) if paid_row else 0.0
    payment_count  = paid_row["payment_count"]          if paid_row else 0
    balance_due    = round(max(grand_total - payments_made, 0), 6)
    counts         = {t: sum(1 for f in files_list if f["storage_tier"] == t) for t in TIER_RATES}

    return templates.TemplateResponse(
        request=request,
        name="invoice.html",
        context={
            "user": current_user, "files": files_list,
            "billing": dict(billing) if billing else None,
            "file_costs": file_costs, "tier_costs": tier_costs,
            "grand_by_tier": grand_by_tier, "counts": counts,
            "now": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "now_ts": now.strftime("%Y%m%d%H%M"),
            "period_start": first_file[:16] if first_file else "—",
            "total_mb": round(sum(f["file_size_mb"] for f in files_list), 4),
            "tier_styles": TIER_STYLES, "tier_rates": TIER_RATES,
            "class_a_count": class_a_count, "class_b_count": class_b_count,
            "free_count": free_count, "class_a_cost": class_a_cost,
            "class_b_cost": class_b_cost, "total_req_cost": total_req_cost,
            "egress_mb": egress_mb, "ingress_mb": ingress_mb,
            "egress_ops": egress_ops, "ingress_ops": ingress_ops,
            "egress_cost": egress_cost, "total_bw_cost": total_bw_cost,
            "bw_rate_per_mb": BANDWIDTH_RATE_PER_MB,
            "recent_bw": recent_bw,
            "storage_cost": storage_cost, "grand_total": grand_total,
            "payments_made": payments_made, "payment_count": payment_count,
            "balance_due": balance_due,
            "recent_reqs": recent_reqs, "req_rates": REQUEST_RATES,
        },
    )

# ---------------------------------------------------------------------------
# PAYMENT GATEWAY
# ---------------------------------------------------------------------------

def generate_payment_id() -> str:
    import random, string
    ts     = datetime.utcnow().strftime("%Y%m%d")
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"PAY-{ts}-{suffix}"


def get_grand_total(conn, user_id: int) -> dict:
    billing = conn.execute(
        "SELECT amount_owed FROM billing_records WHERE user_id=?", (user_id,)
    ).fetchone()
    storage_cost = billing["amount_owed"] if billing else 0.0

    req_rows = conn.execute(
        "SELECT op_class, COUNT(*) as cnt FROM request_logs WHERE user_id=? GROUP BY op_class",
        (user_id,),
    ).fetchall()
    request_cost = sum(r["cnt"] * REQUEST_RATES.get(r["op_class"], 0) for r in req_rows)

    bw = conn.execute(
        "SELECT COALESCE(SUM(mb_transferred),0) as total_mb FROM bandwidth_logs WHERE user_id=? AND direction='egress'",
        (user_id,),
    ).fetchone()
    bandwidth_cost = (bw["total_mb"] * BANDWIDTH_RATE_PER_MB) if bw else 0.0

    return {
        "storage_cost":   round(storage_cost,   6),
        "request_cost":   round(request_cost,   6),
        "bandwidth_cost": round(bandwidth_cost, 6),
        "grand_total":    round(storage_cost + request_cost + bandwidth_cost, 6),
    }


@app.post("/payment/process", include_in_schema=False)
async def process_payment(request: Request, current_user: SessionUser):
    user_id    = current_user["id"]
    body       = await request.json()
    card_last4 = body.get("card_last4", "****")
    card_type  = body.get("card_type",  "VISA")

    conn = get_conn()
    try:
        totals = get_grand_total(conn, user_id)

        paid_row     = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as total_paid FROM payments WHERE user_id=? AND status='paid'",
            (user_id,),
        ).fetchone()
        already_paid = paid_row["total_paid"] if paid_row else 0.0
        balance_due  = round(max(totals["grand_total"] - already_paid, 0), 6)

        if balance_due <= 0:
            raise HTTPException(
                status_code=400,
                detail="No outstanding balance. Your account is fully settled."
            )

        payment_id = generate_payment_id()
        now_iso    = datetime.utcnow().isoformat()
        total      = totals["grand_total"] or 1
        ratio      = balance_due / total
        s_charge   = round(totals["storage_cost"]   * ratio, 6)
        r_charge   = round(totals["request_cost"]   * ratio, 6)
        b_charge   = round(totals["bandwidth_cost"] * ratio, 6)

        conn.execute(
            """
            INSERT INTO payments
                   (user_id, payment_id, amount, currency, status,
                    storage_charge, request_charge, bandwidth_charge,
                    card_last4, card_type, created_at, paid_at)
            VALUES (?, ?, ?, 'INR', 'paid', ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, payment_id, balance_due,
             s_charge, r_charge, b_charge,
             card_last4, card_type, now_iso, now_iso),
        )
        conn.commit()

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()

    return JSONResponse(status_code=200, content={
        "success":    True,
        "payment_id": payment_id,
        "amount":     balance_due,
        "currency":   "INR",
        "paid_at":    now_iso,
        "breakdown":  {"storage": s_charge, "requests": r_charge, "bandwidth": b_charge},
    })


@app.get("/payment/history", include_in_schema=False)
def payment_history(current_user: SessionUser):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT payment_id, amount, currency, status,
                   storage_charge, request_charge, bandwidth_charge,
                   card_last4, card_type, created_at, paid_at
            FROM   payments WHERE user_id=? ORDER BY created_at DESC
            """,
            (current_user["id"],),
        ).fetchall()
    finally:
        conn.close()
    return {"username": current_user["username"],
            "payments": [dict(r) for r in rows], "count": len(rows)}


@app.get("/payment/receipt/{payment_id}", include_in_schema=False)
def payment_receipt(payment_id: str, request: Request, current_user: SessionUser):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM payments WHERE payment_id=? AND user_id=?",
            (payment_id, current_user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Payment not found.")
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="receipt.html",
        context={
            "user":    current_user,
            "payment": dict(row),
            "now":     datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )

# ---------------------------------------------------------------------------
# ME + KEY ROTATION + RESET
# ---------------------------------------------------------------------------

@app.get("/me", include_in_schema=False)
def whoami(current_user: SessionUser):
    return current_user


@app.post("/me/regenerate-key", include_in_schema=False)
def regenerate_api_key(request: Request, current_user: SessionUser):
    new_key = str(uuid.uuid4())
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET api_key=? WHERE id=?", (new_key, current_user["id"]))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()
    return JSONResponse(status_code=200, content={
        "message":     "API key rotated.",
        "new_api_key": new_key,
        "username":    current_user["username"],
        "rotated_at":  datetime.utcnow().isoformat(),
    })


@app.post("/system/reset", include_in_schema=False)
def system_reset(current_user: SessionUser):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM request_logs")
        conn.execute("DELETE FROM tier_events")
        conn.execute("DELETE FROM billing_records")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM buckets")
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()
    for base in TIER_DIRS.values():
        if os.path.exists(base):
            shutil.rmtree(base)
        os.makedirs(base, exist_ok=True)
    return JSONResponse(status_code=200, content={"message": "System reset. Users preserved."})
