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
import bcrypt
import hashlib
import mimetypes
import time
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import (
    DB_NAME,
    SESSION_SECRET,
    ARCHIVE_RETRIEVAL_DELAY_SECS,
    TIER_DIRS,
    TIER_RATES,
    TIER_STYLES,
    REQUEST_RATES,
    BANDWIDTH_RATE_PER_MB,
    USD_TO_INR,
)
from database import get_conn
from helpers import (
    tier_file_path,
    compute_etag,
    guess_content_type,
    get_bucket,
    log_tier_event,
    promote_to_hot,
    log_bandwidth,
)
from auth import router as auth_router, SessionUser, get_session_user
from buckets import router as buckets_router
from files import router as files_router

# ---------------------------------------------------------------------------
app = FastAPI(title="STRATA — Multi-Tenant Cloud Storage Engine", version="0.9.1")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=False)
templates = Jinja2Templates(directory="templates")
app.include_router(auth_router)
app.include_router(buckets_router)
app.include_router(files_router)

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
                   f.etag, f.content_type,
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
