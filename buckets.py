"""
buckets.py — STRATA bucket (namespace) routes

Buckets are STRATA's equivalent of IBM COS buckets or AWS S3 buckets —
named namespaces that group a user's objects together. Every file belongs
to exactly one bucket, and bucket names must be unique per tenant.

Routes in this module:

  PUT    /buckets/{name}    Create a new bucket
  GET    /buckets           List all buckets for the current user (IBM COS ListBuckets)
  GET    /buckets/{name}    List all objects inside a specific bucket
  DELETE /buckets/{name}    Delete a bucket (must be empty first)

Bucket naming rules (matching AWS S3 / IBM COS conventions):
  - 3-63 characters
  - Lowercase letters, numbers, and hyphens only
  - Must start and end with a letter or number (not a hyphen)

Physical storage:
  When a bucket is created, its directory is also created inside every tier
  folder — storage/hot/{username}/{bucket}/, storage/cool/{username}/{bucket}/,
  etc. This means files can move between tiers without needing to create
  directories on the fly during a time-sensitive tier transition.
"""

import os
import re
import shutil
import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import config
from auth import SessionUser
from database import get_conn
from helpers import get_bucket

router = APIRouter()


@router.put("/buckets/{bucket_name}", summary="Create a bucket")
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
        for tier_dir in config.TIER_DIRS.values():
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


@router.get("/buckets", summary="List all buckets (ListBuckets)")
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
                "SELECT COUNT(*) as cnt, COALESCE(SUM(file_size_mb),0) as total_mb "
                "FROM files WHERE bucket_id=? AND user_id=?",
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


@router.get("/buckets/{bucket_name}", summary="List objects in a bucket")
def list_bucket_objects(bucket_name: str, current_user: SessionUser):
    user_id = current_user["id"]
    conn    = get_conn()
    try:
        bucket = get_bucket(conn, user_id, bucket_name)
        if bucket is None:
            raise HTTPException(status_code=404, detail=f"Bucket '{bucket_name}' not found.")
        objects = conn.execute(
            "SELECT id, filename, file_size_mb, storage_tier, created_at, last_accessed_at "
            "FROM files WHERE bucket_id=? AND user_id=? ORDER BY created_at DESC",
            (bucket["id"], user_id),
        ).fetchall()
    finally:
        conn.close()
    return {
        "bucket":  bucket_name,
        "owner":   current_user["username"],
        "objects": [dict(r) for r in objects],
        "count":   len(objects),
    }


@router.delete("/buckets/{bucket_name}", summary="Delete a bucket")
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
        for tier_dir in config.TIER_DIRS.values():
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
        "message": f"Bucket '{bucket_name}' deleted.",
        "owner":   current_user["username"],
    })
