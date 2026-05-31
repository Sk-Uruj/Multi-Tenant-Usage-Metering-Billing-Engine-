"""
Module 2 + 5 (v4): FastAPI — 4-Tier Storage + Request Charge Logging
Every authenticated request is logged to request_logs with its operation class:
  Class A (WRITE) : POST /upload              $0.005  per 1,000
  Class B (READ)  : GET  /files, /files/{f}  $0.0004 per 1,000
  FREE            : DELETE /files/{f}         $0.000

Run with:
    uvicorn main:app --reload
"""

import os
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
    "HOT":     {"bg": "#FF2D6B", "color": "#fff", "label": "● HOT"},
    "COOL":    {"bg": "#0057FF", "color": "#fff", "label": "● COOL"},
    "COLD":    {"bg": "#00FFD1", "color": "#000", "label": "● COLD"},
    "ARCHIVE": {"bg": "#888",    "color": "#fff", "label": "● ARCHIVE"},
}

# Request charge rates
REQUEST_RATES = {
    "A":    0.005  / 1000,   # $0.005 per 1,000  Class A (write)
    "B":    0.0004 / 1000,   # $0.0004 per 1,000 Class B (read)
    "FREE": 0.0,             # free
}

# Endpoint → operation class mapping
OP_CLASS_MAP = {
    ("POST",   "/upload"):       "A",
    ("GET",    "/files"):        "B",
    ("GET",    "/files/"):       "B",   # prefix match for /files/{filename}
    ("DELETE", "/files/"):       "FREE",
}

app = FastAPI(title="Multi-Tenant Cloud Storage Engine", version="0.8.0")
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
    """
    Intercepts every response. If the request was authenticated (session
    has user_id) and the endpoint is a billable storage operation,
    logs a row to request_logs.
    """
    LOGGABLE_PREFIXES = ["/upload", "/files"]

    def classify(self, method: str, path: str) -> str | None:
        if method == "POST"   and path == "/upload":
            return "A"
        if method == "GET"    and (path == "/files" or path.startswith("/files/")):
            return "B"
        if method == "DELETE" and path.startswith("/files/"):
            return "FREE"
        return None   # not a billable endpoint

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Only log successful storage operations
        op_class = self.classify(request.method, request.url.path)
        if op_class is None:
            return response
        if response.status_code >= 400:
            return response

        user_id = request.session.get("user_id")
        if not user_id:
            return response

        try:
            conn = get_conn()
            conn.execute(
                """
                INSERT INTO request_logs
                       (user_id, endpoint, method, op_class, occurred_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, request.url.path, request.method,
                 op_class, datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass  # never let logging break the response

        return response

app.add_middleware(RequestLoggerMiddleware)

# ---------------------------------------------------------------------------
# Session auth dependency
# ---------------------------------------------------------------------------

def get_session_user(request: Request) -> dict:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, api_key FROM users WHERE id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        request.session.clear()
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return dict(row)

SessionUser = Annotated[dict, Depends(get_session_user)]

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def tier_file_path(tier: str, username: str, filename: str) -> str:
    return os.path.join(TIER_DIRS[tier], username, filename)


def log_tier_event(conn, file_id, user_id, from_tier, to_tier, event_type, ts):
    conn.execute(
        """
        INSERT INTO tier_events
               (file_id, user_id, from_tier, to_tier, event_type, occurred_at)
        VALUES (?,?,?,?,?,?)
        """,
        (file_id, user_id, from_tier, to_tier, event_type, ts),
    )


def promote_to_hot(conn, row: dict, username: str, now_iso: str):
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

    src     = tier_file_path(current, username, filename)
    dst_dir = os.path.join(TIER_DIRS["HOT"], username)
    dst     = os.path.join(dst_dir, filename)

    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst)

    conn.execute(
        """
        UPDATE files
        SET    storage_tier     = 'HOT',
               last_accessed_at = ?,
               hot_entered_at   = ?
        WHERE  id = ?
        """,
        (now_iso, now_iso, file_id),
    )
    log_tier_event(conn, file_id, user_id, current, "HOT", "PROMOTE", now_iso)

# ---------------------------------------------------------------------------
# GET /login  POST /login  GET /logout
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
# GET /  — scoped dashboard
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard(request: Request, current_user: SessionUser):
    user_id = current_user["id"]
    conn = get_conn()
    try:
        files = conn.execute(
            """
            SELECT id, filename, file_size_mb, storage_tier,
                   created_at, last_accessed_at,
                   hot_entered_at, cool_entered_at,
                   cold_entered_at, archive_entered_at
            FROM   files WHERE user_id=? ORDER BY created_at DESC
            """, (user_id,),
        ).fetchall()

        billing = conn.execute(
            "SELECT * FROM billing_records WHERE user_id=?", (user_id,)
        ).fetchone()

        events = conn.execute(
            """
            SELECT te.from_tier, te.to_tier, te.event_type,
                   te.occurred_at, f.filename
            FROM   tier_events te
            JOIN   files f ON f.id = te.file_id
            WHERE  te.user_id=?
            ORDER  BY te.occurred_at DESC LIMIT 10
            """, (user_id,),
        ).fetchall()

        # Request summary for dashboard
        req_summary = conn.execute(
            """
            SELECT op_class, COUNT(*) as cnt
            FROM   request_logs
            WHERE  user_id=?
            GROUP  BY op_class
            """, (user_id,),
        ).fetchall()

    finally:
        conn.close()

    files_list   = [dict(r) for r in files]
    events_list  = [dict(r) for r in events]
    req_counts   = {r["op_class"]: r["cnt"] for r in req_summary}
    counts       = {t: sum(1 for f in files_list if f["storage_tier"] == t)
                    for t in TIER_DIRS}

    # Quick request cost calc for dashboard
    req_cost = (
        req_counts.get("A",    0) * REQUEST_RATES["A"] +
        req_counts.get("B",    0) * REQUEST_RATES["B"]
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user":        current_user,
            "files":       files_list,
            "billing":     dict(billing) if billing else None,
            "events":      events_list,
            "tier_styles": TIER_STYLES,
            "tier_rates":  TIER_RATES,
            "counts":      counts,
            "now":         datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_mb":    round(sum(f["file_size_mb"] for f in files_list), 4),
            "req_counts":  req_counts,
            "req_cost":    round(req_cost, 8),
        },
    )

# ---------------------------------------------------------------------------
# GET /invoice  — full invoice page
# ---------------------------------------------------------------------------

@app.get("/invoice", include_in_schema=False)
def invoice_page(request: Request, current_user: SessionUser):
    user_id = current_user["id"]
    now     = datetime.utcnow()

    conn = get_conn()
    try:
        files = conn.execute(
            """
            SELECT id, filename, file_size_mb, storage_tier,
                   created_at, last_accessed_at,
                   hot_entered_at, cool_entered_at,
                   cold_entered_at, archive_entered_at
            FROM   files WHERE user_id=? ORDER BY created_at DESC
            """, (user_id,),
        ).fetchall()

        billing = conn.execute(
            "SELECT * FROM billing_records WHERE user_id=?", (user_id,)
        ).fetchone()

        first_file = conn.execute(
            "SELECT MIN(created_at) FROM files WHERE user_id=?", (user_id,)
        ).fetchone()[0]

        # Full request log breakdown
        req_by_class = conn.execute(
            """
            SELECT op_class, COUNT(*) as cnt
            FROM   request_logs WHERE user_id=?
            GROUP  BY op_class
            """, (user_id,),
        ).fetchall()

        # Recent requests for the log table
        recent_reqs = conn.execute(
            """
            SELECT method, endpoint, op_class, occurred_at
            FROM   request_logs
            WHERE  user_id=?
            ORDER  BY occurred_at DESC
            LIMIT  20
            """, (user_id,),
        ).fetchall()

    finally:
        conn.close()

    files_list  = [dict(r) for r in files]
    req_counts  = {r["op_class"]: r["cnt"] for r in req_by_class}
    recent_reqs = [dict(r) for r in recent_reqs]

    def parse_dt(val):
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return None

    def fmt_duration(secs):
        secs = int(max(secs, 0))
        if secs < 60:   return f"{secs}s"
        if secs < 3600: return f"{secs//60}m {secs%60}s"
        return f"{secs//3600}h {(secs%3600)//60}m"

    # Per-file storage cost
    file_costs    = {}
    grand_by_tier = {t: 0.0 for t in TIER_RATES}
    tier_costs    = {t: 0.0 for t in TIER_RATES}

    for f in files_list:
        size_mb    = f["file_size_mb"]
        created_dt = parse_dt(f["created_at"]) or now
        hot_in     = parse_dt(f["hot_entered_at"])     or created_dt
        cool_in    = parse_dt(f["cool_entered_at"])
        cold_in    = parse_dt(f["cold_entered_at"])
        arc_in     = parse_dt(f["archive_entered_at"])
        current    = f["storage_tier"]

        windows = []
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
                "tier":         tier,
                "from_str":     start.strftime("%m/%d %H:%M:%S"),
                "to_str":       end.strftime("%m/%d %H:%M:%S"),
                "duration_str": fmt_duration(secs),
                "cost":         cost,
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
        }

    # Request charges
    class_a_count = req_counts.get("A",    0)
    class_b_count = req_counts.get("B",    0)
    free_count    = req_counts.get("FREE", 0)
    class_a_cost  = class_a_count * REQUEST_RATES["A"]
    class_b_cost  = class_b_count * REQUEST_RATES["B"]
    total_req_cost = class_a_cost + class_b_cost

    storage_cost  = billing["amount_owed"] if billing else 0.0
    grand_total   = storage_cost + total_req_cost

    counts = {t: sum(1 for f in files_list if f["storage_tier"] == t)
              for t in TIER_RATES}

    return templates.TemplateResponse(
        request=request,
        name="invoice.html",
        context={
            "user":           current_user,
            "files":          files_list,
            "billing":        dict(billing) if billing else None,
            "file_costs":     file_costs,
            "tier_costs":     tier_costs,
            "grand_by_tier":  grand_by_tier,
            "counts":         counts,
            "now":            now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "now_ts":         now.strftime("%Y%m%d%H%M"),
            "period_start":   first_file[:16] if first_file else "—",
            "total_mb":       round(sum(f["file_size_mb"] for f in files_list), 4),
            "tier_styles":    TIER_STYLES,
            "tier_rates":     TIER_RATES,
            # Request charge data
            "class_a_count":  class_a_count,
            "class_b_count":  class_b_count,
            "free_count":     free_count,
            "class_a_cost":   class_a_cost,
            "class_b_cost":   class_b_cost,
            "total_req_cost": total_req_cost,
            "storage_cost":   storage_cost,
            "grand_total":    grand_total,
            "recent_reqs":    recent_reqs,
            "req_rates":      REQUEST_RATES,
        },
    )

# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@app.post("/upload", include_in_schema=False)
async def upload_file(current_user: SessionUser, file: UploadFile = File(...)):
    username = current_user["username"]
    user_id  = current_user["id"]
    conn = get_conn()
    try:
        user_dir     = os.path.join(TIER_DIRS["HOT"], username)
        os.makedirs(user_dir, exist_ok=True)
        file_path    = os.path.join(user_dir, file.filename)
        contents     = await file.read()
        file_size_mb = len(contents) / (1024 * 1024)
        with open(file_path, "wb") as f:
            f.write(contents)
        now    = datetime.utcnow().isoformat()
        cursor = conn.execute(
            """
            INSERT INTO files
                   (user_id, filename, file_size_mb, storage_tier,
                    created_at, last_accessed_at, hot_entered_at)
            VALUES (?,?,?,'HOT',?,?,?)
            """,
            (user_id, file.filename, file_size_mb, now, now, now),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Filesystem error: {exc}")
    finally:
        conn.close()
    return JSONResponse(status_code=201, content={
        "message": "File uploaded to HOT storage.",
        "record_id": cursor.lastrowid,
        "owner": username,
        "file_path": file_path,
        "file_size_mb": round(file_size_mb, 4),
        "storage_tier": "HOT",
        "uploaded_at": now,
    })

# ---------------------------------------------------------------------------
# GET /files  GET /files/{filename}  DELETE /files/{filename}
# ---------------------------------------------------------------------------

@app.get("/files", include_in_schema=False)
def list_files(current_user: SessionUser):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM files WHERE user_id=? ORDER BY created_at DESC",
            (current_user["id"],),
        ).fetchall()
    finally:
        conn.close()
    files = [dict(r) for r in rows]
    return {"username": current_user["username"],
            "total_files": len(files),
            "total_size_mb": round(sum(f["file_size_mb"] for f in files), 4),
            "files": files}


@app.get("/files/{filename}", include_in_schema=False)
def download_file(filename: str, current_user: SessionUser):
    username = current_user["username"]
    user_id  = current_user["id"]
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT id, filename, storage_tier, user_id
            FROM   files WHERE user_id=? AND filename=?
            ORDER  BY created_at DESC LIMIT 1
            """, (user_id, filename),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"'{filename}' not found.")
        row_dict  = dict(row)
        current   = row_dict["storage_tier"]
        file_path = tier_file_path(current, username, filename)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=410, detail="File missing from disk.")
        now_iso = datetime.utcnow().isoformat()
        promote_to_hot(conn, row_dict, username, now_iso)
        conn.commit()
        hot_path = tier_file_path("HOT", username, filename)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()
    return FileResponse(
        path=hot_path if os.path.exists(hot_path) else file_path,
        filename=filename, media_type="application/octet-stream",
    )


@app.delete("/files/{filename}", include_in_schema=False)
def delete_file(filename: str, current_user: SessionUser):
    username = current_user["username"]
    user_id  = current_user["id"]
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, storage_tier FROM files WHERE user_id=? AND filename=? LIMIT 1",
            (user_id, filename),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"'{filename}' not found.")
        fp = tier_file_path(row["storage_tier"], username, filename)
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
    return JSONResponse(status_code=200,
                        content={"message": f"'{filename}' deleted.", "owner": username})

# ---------------------------------------------------------------------------
# GET /me  POST /me/regenerate-key  POST /system/reset
# ---------------------------------------------------------------------------

@app.get("/me", include_in_schema=False)
def whoami(current_user: SessionUser):
    return current_user


@app.post("/me/regenerate-key", include_in_schema=False)
def regenerate_api_key(request: Request, current_user: SessionUser):
    new_key = str(uuid.uuid4())
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET api_key=? WHERE id=?",
                     (new_key, current_user["id"]))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()
    return JSONResponse(status_code=200, content={
        "message": "API key rotated. Old key is now invalid.",
        "new_api_key": new_key,
        "username": current_user["username"],
        "rotated_at": datetime.utcnow().isoformat(),
    })


@app.post("/system/reset", include_in_schema=False)
def system_reset(current_user: SessionUser):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM request_logs")
        conn.execute("DELETE FROM tier_events")
        conn.execute("DELETE FROM billing_records")
        conn.execute("DELETE FROM files")
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
    return JSONResponse(status_code=200,
                        content={"message": "System reset. Users preserved."})
