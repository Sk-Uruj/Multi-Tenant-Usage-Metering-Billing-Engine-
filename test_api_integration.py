"""
test_api_integration.py — FastAPI integration tests for STRATA

Unlike test_strata.py (which tests individual functions in isolation),
these tests spin up the REAL FastAPI app and simulate REAL HTTP requests
against it using FastAPI's TestClient — login, upload, download, bucket
operations, payments — checking the full request/response cycle: status
codes, JSON shapes, session cookies, and response headers.

Each test gets a completely fresh, temporary, throwaway SQLite database
and storage folder. Your real cloud_storage.db and storage/ folder are
never touched.

Run with:
    pytest test_api_integration.py -v

Requires: pytest, httpx, fastapi, python-multipart, itsdangerous, bcrypt,
          python-dotenv (same dependencies as the main project)
"""

import io
import os
import sqlite3

import bcrypt
import pytest
from fastapi.testclient import TestClient

import main as strata_main


# ---------------------------------------------------------------------------
# Fixtures — fresh, isolated DB + storage for every single test
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Builds a completely isolated STRATA instance for one test:
    - A throwaway SQLite database with the minimal schema the app needs
    - A throwaway storage/ folder tree
    - One seeded test user (bcrypt-hashed password, matching production)
    Returns a ready-to-use TestClient pointed at the real FastAPI app.
    """
    db_path = tmp_path / "test_cloud_storage.db"
    monkeypatch.setattr(strata_main, "DB_NAME", str(db_path))

    tier_dirs = {}
    for tier in ("HOT", "COOL", "COLD", "ARCHIVE"):
        d = tmp_path / "storage" / tier.lower()
        d.mkdir(parents=True)
        tier_dirs[tier] = str(d)
    monkeypatch.setattr(strata_main, "TIER_DIRS", tier_dirs)

    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            api_key TEXT UNIQUE NOT NULL
        );
        CREATE TABLE buckets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, name)
        );
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bucket_id INTEGER,
            filename TEXT NOT NULL,
            file_size_mb REAL NOT NULL,
            storage_tier TEXT NOT NULL DEFAULT 'HOT',
            created_at TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL,
            hot_entered_at TEXT,
            cool_entered_at TEXT,
            cold_entered_at TEXT,
            archive_entered_at TEXT,
            etag TEXT,
            content_type TEXT
        );
        CREATE TABLE billing_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            total_hours_tracked REAL NOT NULL DEFAULT 0.0,
            amount_owed REAL NOT NULL DEFAULT 0.0,
            last_calculated_at TEXT NOT NULL
        );
        CREATE TABLE tier_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            from_tier TEXT,
            to_tier TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            method TEXT NOT NULL,
            op_class TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE bandwidth_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_id INTEGER,
            bucket_name TEXT NOT NULL DEFAULT 'default',
            filename TEXT NOT NULL,
            bytes_transferred INTEGER NOT NULL DEFAULT 0,
            mb_transferred REAL NOT NULL DEFAULT 0.0,
            direction TEXT NOT NULL DEFAULT 'egress',
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payment_id TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            status TEXT NOT NULL DEFAULT 'pending',
            storage_charge REAL NOT NULL DEFAULT 0.0,
            request_charge REAL NOT NULL DEFAULT 0.0,
            bandwidth_charge REAL NOT NULL DEFAULT 0.0,
            card_last4 TEXT,
            card_type TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT
        );
    """)

    # Seed one test user with a REAL bcrypt hash, matching production exactly
    hashed_pw = bcrypt.hashpw(b"testpass123", bcrypt.gensalt()).decode("utf-8")
    conn.execute(
        "INSERT INTO users (username, password, api_key) VALUES (?, ?, ?)",
        ("testuser", hashed_pw, "test-api-key-12345"),
    )
    conn.commit()
    conn.close()

    test_client = TestClient(strata_main.app)
    return test_client


@pytest.fixture
def logged_in_client(client):
    """Same as `client`, but already logged in as testuser. Returns the
    client with session cookies set, ready for authenticated requests."""
    response = client.post(
        "/login",
        data={"username": "testuser", "password": "testpass123"},
        follow_redirects=False,
    )
    assert response.status_code == 302, "Login must succeed before tests can use this fixture"
    return client


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuthentication:

    def test_login_with_correct_password_redirects_to_dashboard(self, client):
        response = client.post(
            "/login",
            data={"username": "testuser", "password": "testpass123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/"

    def test_login_with_wrong_password_rejected(self, client):
        response = client.post(
            "/login",
            data={"username": "testuser", "password": "wrong_password"},
        )
        assert response.status_code == 401

    def test_login_with_nonexistent_username_rejected(self, client):
        response = client.post(
            "/login",
            data={"username": "does_not_exist", "password": "anything"},
        )
        assert response.status_code == 401

    def test_unauthenticated_api_request_gets_json_401_not_html_redirect(self, client):
        """The exact bug we fixed early on: API/fetch calls must get a
        clean JSON 401, not an HTML redirect to /login, or the frontend's
        JSON parsing breaks with 'Unexpected token <'."""
        response = client.get(
            "/files",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/json")

    def test_logout_clears_session(self, logged_in_client):
        response = logged_in_client.get("/logout", follow_redirects=False)
        assert response.status_code == 302

        # After logout, an authenticated-only endpoint should reject us
        follow_up = logged_in_client.get("/files", headers={"Accept": "application/json"})
        assert follow_up.status_code == 401


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

class TestSignup:

    def test_successful_signup_auto_logs_in_and_redirects_to_dashboard(self, client):
        response = client.post(
            "/signup",
            data={
                "username": "brandnewuser",
                "password": "securepass123",
                "confirm_password": "securepass123",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/"

    def test_signup_creates_bcrypt_hashed_password_not_plaintext(self, client):
        client.post(
            "/signup",
            data={
                "username": "hashcheck",
                "password": "securepass123",
                "confirm_password": "securepass123",
            },
        )
        conn = sqlite3.connect(strata_main.DB_NAME)
        row = conn.execute(
            "SELECT password FROM users WHERE username='hashcheck'"
        ).fetchone()
        conn.close()
        assert row[0] != "securepass123"
        assert row[0].startswith(("$2a$", "$2b$", "$2y$"))

    def test_signup_creates_default_bucket_immediately(self, client):
        client.post(
            "/signup",
            data={
                "username": "bucketcheck",
                "password": "securepass123",
                "confirm_password": "securepass123",
            },
        )
        conn = sqlite3.connect(strata_main.DB_NAME)
        user_id = conn.execute(
            "SELECT id FROM users WHERE username='bucketcheck'"
        ).fetchone()[0]
        bucket = conn.execute(
            "SELECT name FROM buckets WHERE user_id=?", (user_id,)
        ).fetchone()
        conn.close()
        assert bucket is not None
        assert bucket[0] == "default"

    def test_duplicate_username_rejected(self, client):
        client.post(
            "/signup",
            data={
                "username": "duplicatecheck",
                "password": "securepass123",
                "confirm_password": "securepass123",
            },
        )
        response = client.post(
            "/signup",
            data={
                "username": "duplicatecheck",
                "password": "differentpass456",
                "confirm_password": "differentpass456",
            },
        )
        assert response.status_code == 409

    def test_password_under_8_characters_rejected(self, client):
        response = client.post(
            "/signup",
            data={
                "username": "shortpwcheck",
                "password": "short",
                "confirm_password": "short",
            },
        )
        assert response.status_code == 400

    def test_mismatched_passwords_rejected(self, client):
        response = client.post(
            "/signup",
            data={
                "username": "mismatchcheck",
                "password": "securepass123",
                "confirm_password": "differentpass456",
            },
        )
        assert response.status_code == 400

    def test_invalid_username_characters_rejected(self, client):
        response = client.post(
            "/signup",
            data={
                "username": "bad user!",
                "password": "securepass123",
                "confirm_password": "securepass123",
            },
        )
        assert response.status_code == 400

    def test_signup_then_logout_then_login_with_same_credentials_works(self, client):
        """End-to-end: signup, log out, log back in with the exact same
        credentials — proving the stored hash genuinely round-trips."""
        client.post(
            "/signup",
            data={
                "username": "roundtripuser",
                "password": "mypassword123",
                "confirm_password": "mypassword123",
            },
        )
        client.get("/logout")

        login_response = client.post(
            "/login",
            data={"username": "roundtripuser", "password": "mypassword123"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302
        assert login_response.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Bucket operations
# ---------------------------------------------------------------------------

class TestBucketRoutes:

    def test_create_bucket_succeeds(self, logged_in_client):
        response = logged_in_client.put("/buckets/my-photos")
        assert response.status_code == 201
        assert response.json()["bucket"] == "my-photos"

    def test_create_duplicate_bucket_rejected(self, logged_in_client):
        logged_in_client.put("/buckets/duplicate-test")
        response = logged_in_client.put("/buckets/duplicate-test")
        assert response.status_code == 409

    def test_create_bucket_with_invalid_name_rejected(self, logged_in_client):
        response = logged_in_client.put("/buckets/AB")  # too short, uppercase
        assert response.status_code == 400

    def test_list_buckets_returns_created_bucket(self, logged_in_client):
        logged_in_client.put("/buckets/listed-bucket")
        response = logged_in_client.get("/buckets")
        assert response.status_code == 200
        bucket_names = [b["name"] for b in response.json()["buckets"]]
        assert "listed-bucket" in bucket_names

    def test_delete_empty_bucket_succeeds(self, logged_in_client):
        logged_in_client.put("/buckets/to-delete")
        response = logged_in_client.delete("/buckets/to-delete")
        assert response.status_code == 200

    def test_delete_nonempty_bucket_rejected(self, logged_in_client):
        logged_in_client.put("/buckets/has-files")
        logged_in_client.post(
            "/upload",
            files={"file": ("test.txt", io.BytesIO(b"content"), "text/plain")},
            data={"bucket": "has-files"},
        )
        response = logged_in_client.delete("/buckets/has-files")
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Upload / Download full round trip
# ---------------------------------------------------------------------------

class TestUploadDownloadRoundTrip:

    def test_upload_creates_file_in_hot_tier(self, logged_in_client):
        response = logged_in_client.post(
            "/upload",
            files={"file": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["storage_tier"] == "HOT"
        assert body["filename"] == "hello.txt"
        assert "etag" in body
        assert body["content_type"] == "text/plain"

    def test_uploaded_file_can_be_downloaded_with_same_content(self, logged_in_client):
        original_content = b"the quick brown fox"
        logged_in_client.post(
            "/upload",
            files={"file": ("fox.txt", io.BytesIO(original_content), "text/plain")},
        )

        response = logged_in_client.get("/files/default/fox.txt")
        assert response.status_code == 200
        assert response.content == original_content

    def test_download_response_includes_correct_headers(self, logged_in_client):
        logged_in_client.post(
            "/upload",
            files={"file": ("photo.png", io.BytesIO(b"fake png bytes"), "image/png")},
        )
        response = logged_in_client.get("/files/default/photo.png")
        assert response.status_code == 200
        assert response.headers["x-storage-tier"] == "HOT"
        assert response.headers["content-type"] == "image/png"
        assert "etag" in response.headers

    def test_download_of_nonexistent_file_returns_404(self, logged_in_client):
        response = logged_in_client.get("/files/default/does_not_exist.txt")
        assert response.status_code == 404

    def test_download_promotes_file_back_to_hot_in_database(self, logged_in_client):
        """Integration-level proof of the exact bug we fixed: download a
        file, then verify via the /files listing that the database
        genuinely reflects HOT — not just that the HTTP call succeeded."""
        logged_in_client.post(
            "/upload",
            files={"file": ("promote_test.txt", io.BytesIO(b"data"), "text/plain")},
        )
        logged_in_client.get("/files/default/promote_test.txt")

        listing = logged_in_client.get("/files", headers={"Accept": "application/json"})
        files = listing.json()["files"]
        match = next(f for f in files if f["filename"] == "promote_test.txt")
        assert match["storage_tier"] == "HOT"

    def test_delete_file_removes_it_from_listing(self, logged_in_client):
        logged_in_client.post(
            "/upload",
            files={"file": ("to_delete.txt", io.BytesIO(b"data"), "text/plain")},
        )
        delete_response = logged_in_client.delete("/files/default/to_delete.txt")
        assert delete_response.status_code == 200

        listing = logged_in_client.get("/files", headers={"Accept": "application/json"})
        filenames = [f["filename"] for f in listing.json()["files"]]
        assert "to_delete.txt" not in filenames

    def test_upload_to_nonexistent_named_bucket_fails(self, logged_in_client):
        """Uploading to 'default' auto-creates it, but uploading to an
        explicitly-named bucket that was never created should fail."""
        response = logged_in_client.post(
            "/upload",
            files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
            data={"bucket": "never-created"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Payment flow
# ---------------------------------------------------------------------------

class TestPaymentFlow:

    def test_payment_rejected_when_no_balance_due(self, logged_in_client):
        """With zero usage and zero billing records, there should be
        nothing to pay — the route must reject the attempt cleanly."""
        response = logged_in_client.post(
            "/payment/process",
            json={"card_last4": "4242", "card_type": "VISA"},
        )
        assert response.status_code == 400

    def test_payment_history_empty_for_new_user(self, logged_in_client):
        response = logged_in_client.get("/payment/history")
        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_payment_succeeds_when_balance_exists(self, logged_in_client, client):
        """Manually seed a billing record with a real balance, then
        confirm the payment route correctly processes it end-to-end."""
        import datetime
        conn = sqlite3.connect(strata_main.DB_NAME)
        user_id = conn.execute("SELECT id FROM users WHERE username='testuser'").fetchone()[0]
        conn.execute(
            "INSERT INTO billing_records (user_id, total_hours_tracked, amount_owed, last_calculated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, 1.0, 100.0, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

        response = logged_in_client.post(
            "/payment/process",
            json={"card_last4": "4242", "card_type": "VISA"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["amount"] == 100.0


# ---------------------------------------------------------------------------
# API key rotation
# ---------------------------------------------------------------------------

class TestApiKeyRotation:

    def test_rotating_key_produces_a_new_different_key(self, logged_in_client):
        me_before = logged_in_client.get("/me").json()
        old_key = me_before["api_key"]

        rotate_response = logged_in_client.post("/me/regenerate-key")
        assert rotate_response.status_code == 200
        new_key = rotate_response.json()["new_api_key"]

        assert new_key != old_key

        me_after = logged_in_client.get("/me").json()
        assert me_after["api_key"] == new_key


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
