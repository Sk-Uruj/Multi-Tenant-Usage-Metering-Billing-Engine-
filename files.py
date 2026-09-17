"""
files.py — STRATA file (object) routes

This module is the front door of STRATA's actual storage engine —
the routes that create, retrieve, and remove individual objects
within buckets.

Routes in this module:

  POST   /upload                    Upload a file to a bucket → HOT tier
  GET    /files/{bucket}/{filename} Download a file (promotes to HOT on access)
  DELETE /files/{bucket}/{filename} Delete a file from a bucket
  GET    /files                     List all files across all buckets

Key behaviors explained:

  Upload → always lands in HOT:
    Every new file starts in HOT tier regardless of size or type.
    ETag (MD5 hash) and Content-Type (MIME detection) are computed once
    at upload time and stored — following the IBM COS / AWS S3 convention
    of returning these on every object GET.

  Download → promotes the file back to HOT:
    Accessing a file in any tier moves it back to HOT, resetting its
    idle clock. This is the "read-triggered promotion" that mirrors how
    S3 Intelligent-Tiering reactivates objects when they're accessed.

    The download route uses TWO database connections deliberately:
      1. First connection: read the file's metadata, then CLOSE.
      2. Sleep for archive delay (if applicable) — no open connection.
      3. Second connection: re-fetch the CURRENT tier (in case the
         tiering engine moved the file during the sleep), promote, commit.
    This avoids the SQLite "database is locked" race condition where
    holding a connection open during the archive sleep caused the tiering
    and billing engines (which write every 10-15s) to collide with it.

  Auto-create default bucket on first upload:
    If a user uploads to 'default' and that bucket doesn't exist yet,
    it's created automatically. Named non-default buckets must be
    created explicitly first with PUT /buckets/{name}.
"""

import os
import sqlite3
import time
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import config
from auth import SessionUser
from database import get_conn
from helpers import (
    compute_etag,
    guess_content_type,
    get_bucket,
    log_bandwidth,
    promote_to_hot,
    tier_file_path,
)

router = APIRouter()


@router.post("/upload", summary="Upload a file to a bucket")
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
        user_dir     = os.path.join(config.TIER_DIRS["HOT"], username, bucket)
        os.makedirs(user_dir, exist_ok=True)
        file_path = os.path.join(user_dir, file.filename)
        with open(file_path, "wb") as f:
            f.write(contents)

        etag         = compute_etag(contents)
        content_type = guess_content_type(file.filename)

        now = datetime.utcnow().isoformat()
        cursor = conn.execute(
            """
            INSERT INTO files
                   (user_id, bucket_id, filename, file_size_mb, storage_tier,
                    created_at, last_accessed_at, hot_entered_at, etag, content_type)
            VALUES (?,?,?,?,'HOT',?,?,?,?,?)
            """,
            (user_id, bucket_id, file.filename, file_size_mb, now, now, now, etag, content_type),
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
        "etag":         etag,
        "content_type": content_type,
    })


@router.get("/files/{bucket}/{filename}", summary="Download a file from a bucket")
def download_file(bucket: str, filename: str, current_user: SessionUser):
    username = current_user["username"]
    user_id  = current_user["id"]

    conn = get_conn()
    try:
        bucket_row = get_bucket(conn, user_id, bucket)
        if bucket_row is None:
            raise HTTPException(status_code=404, detail=f"Bucket '{bucket}' not found.")

        row = conn.execute(
            """
            SELECT id, filename, storage_tier, user_id, file_size_mb, etag, content_type
            FROM   files
            WHERE  user_id=? AND bucket_id=? AND filename=?
            ORDER  BY created_at DESC LIMIT 1
            """,
            (user_id, bucket_row["id"], filename),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail=f"'{filename}' not found in bucket '{bucket}'.")

        row_dict     = dict(row)
        current      = row_dict["storage_tier"]
        file_path    = tier_file_path(current, username, bucket, filename)
        etag         = row_dict.get("etag") or ""
        content_type = row_dict.get("content_type") or "application/octet-stream"

        if not os.path.exists(file_path):
            raise HTTPException(status_code=410, detail="File missing from disk.")

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    finally:
        conn.close()

    if current == "ARCHIVE":
        time.sleep(config.ARCHIVE_RETRIEVAL_DELAY_SECS)

    conn = get_conn()
    try:
        fresh_row = conn.execute(
            "SELECT id, user_id, filename, storage_tier FROM files WHERE id=?",
            (row_dict["id"],),
        ).fetchone()

        if fresh_row is None:
            raise HTTPException(status_code=404, detail="File was deleted during retrieval.")

        fresh_row_dict = dict(fresh_row)
        now_iso = datetime.utcnow().isoformat()
        promote_to_hot(conn, fresh_row_dict, username, bucket, now_iso)
        log_bandwidth(conn, user_id, row_dict["id"], bucket, filename, row_dict["file_size_mb"], "egress")
        conn.commit()

        hot_path = tier_file_path("HOT", username, bucket, filename)

    except HTTPException:
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"DB error during promotion: {exc}")
    finally:
        conn.close()

    return FileResponse(
        path=hot_path if os.path.exists(hot_path) else file_path,
        filename=filename,
        media_type=content_type,
        headers={
            "X-Storage-Tier": current,
            "X-Bucket": bucket,
            "ETag": f'"{etag}"' if etag else "",
            "X-Retrieval-Delay-Secs": str(config.ARCHIVE_RETRIEVAL_DELAY_SECS) if current == "ARCHIVE" else "0",
        },
    )


@router.delete("/files/{bucket}/{filename}", summary="Delete a file from a bucket")
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
        "owner":   username,
        "bucket":  bucket,
    })


@router.get("/files", summary="List all files across all buckets")
def list_files(current_user: SessionUser):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT f.id, f.filename, f.file_size_mb, f.storage_tier,
                   f.created_at, f.last_accessed_at, f.etag, f.content_type,
                   b.name as bucket_name
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
