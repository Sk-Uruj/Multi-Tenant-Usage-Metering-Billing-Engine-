"""
auth.py — STRATA authentication

Handles everything related to identity: who is allowed to make a request,
how new users register, and how sessions start and end.

Three concepts live here together because they're tightly coupled —
get_session_user is the dependency every protected route uses, and the
login/signup/logout routes are the only way to create or destroy the
session that get_session_user reads.

FastAPI Router pattern:
  Instead of decorating routes with @app.get/@app.post directly (which
  requires a reference to the global FastAPI app instance), routes here
  are attached to an APIRouter. main.py then does app.include_router(...)
  to wire them in. This is FastAPI's standard way of splitting routes
  across multiple files without circular imports.

Key design decisions explained:

  get_session_user — returns JSON 401 for API/fetch calls (Accept:
  application/json), but HTML redirects to /login for browser navigation
  (Accept: text/html). This fixes the bug where payment history fetch()
  calls were receiving HTML redirects, which broke JSON.parse().

  bcrypt.checkpw() — password is fetched from DB by username only, then
  verified against the stored bcrypt hash here in Python. We never compare
  password=? in SQL because that would only work for plaintext passwords.

  Auto-login after signup — after a successful POST /signup, the user is
  immediately logged in and redirected to the dashboard. No reason to make
  them re-type credentials they just submitted.
"""

import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Annotated

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import config
from database import get_conn

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Session dependency — used by every protected route in every other module
# ---------------------------------------------------------------------------

def get_session_user(request: Request) -> dict:
    """FastAPI dependency: reads the session cookie and returns the current
    user as a dict, or raises an appropriate HTTP error if not logged in.

    Returns JSON 401 for API/fetch calls — these set Accept: application/json
    and would break if they received an HTML redirect instead. Returns a
    307 redirect to /login for browser navigation (Accept: text/html).
    """
    user_id = request.session.get("user_id")
    if not user_id:
        accept = request.headers.get("accept", "")
        if "text/html" in accept and "application/json" not in accept:
            raise HTTPException(status_code=307, headers={"Location": "/login"})
        else:
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


# Shorthand type alias used by every protected route as a parameter type:
#   def my_route(current_user: SessionUser): ...
SessionUser = Annotated[dict, Depends(get_session_user)]


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None}
    )


@router.post("/login", include_in_schema=False)
def login_submit(request: Request,
                 username: str = Form(...),
                 password: str = Form(...)):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, api_key, password FROM users WHERE username=?",
            (username,),
        ).fetchone()
    finally:
        conn.close()

    valid = False
    if row is not None:
        try:
            valid = bcrypt.checkpw(
                password.encode("utf-8"), row["password"].encode("utf-8")
            )
        except (ValueError, AttributeError):
            valid = False

    if not valid:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Invalid username or password."},
            status_code=401,
        )
    request.session["user_id"]  = row["id"]
    request.session["username"] = row["username"]
    return RedirectResponse(url="/", status_code=302)


@router.get("/signup", include_in_schema=False)
def signup_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request, name="signup.html", context={"error": None}
    )


@router.post("/signup", include_in_schema=False)
def signup_submit(request: Request,
                  username: str = Form(...),
                  password: str = Form(...),
                  confirm_password: str = Form(...)):
    username = username.strip()

    if not re.match(r'^[a-zA-Z0-9_]{3,32}$', username):
        return templates.TemplateResponse(
            request=request, name="signup.html",
            context={"error": "Username must be 3-32 characters: letters, numbers, underscores only."},
            status_code=400,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request=request, name="signup.html",
            context={"error": "Password must be at least 8 characters."},
            status_code=400,
        )

    if password != confirm_password:
        return templates.TemplateResponse(
            request=request, name="signup.html",
            context={"error": "Passwords do not match."},
            status_code=400,
        )

    hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    api_key   = str(uuid.uuid4())
    now_iso   = datetime.utcnow().isoformat()

    conn = get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password, api_key) VALUES (?,?,?)",
            (username, hashed_pw, api_key),
        )
        new_user_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO buckets (user_id, name, created_at) VALUES (?, 'default', ?)",
            (new_user_id, now_iso),
        )
        conn.commit()

        for tier_dir in config.TIER_DIRS.values():
            os.makedirs(os.path.join(tier_dir, username, "default"), exist_ok=True)

    except sqlite3.IntegrityError:
        conn.rollback()
        return templates.TemplateResponse(
            request=request, name="signup.html",
            context={"error": f"Username '{username}' is already taken."},
            status_code=409,
        )
    except sqlite3.Error as exc:
        conn.rollback()
        return templates.TemplateResponse(
            request=request, name="signup.html",
            context={"error": f"Could not create account: {exc}"},
            status_code=500,
        )
    finally:
        conn.close()

    request.session["user_id"]  = new_user_id
    request.session["username"] = username
    return RedirectResponse(url="/", status_code=302)


@router.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
