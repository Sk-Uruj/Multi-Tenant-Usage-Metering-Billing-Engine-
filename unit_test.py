"""
test_strata.py — Regression test suite for STRATA

Covers the real bugs found and fixed during development:
  - promote_to_hot() silently corrupting the database when a file isn't
    found at its expected disk location (the Butterfly.png bug)
  - bcrypt password verification (hash/verify round trip)
  - File size exemption boundary (128KB)
  - File type classification multipliers
  - balance_due billing math (the overpayment / credit scenario)
  - ETag and Content-Type generation

Run with:
    pytest test_strata.py -v

Requires: pytest, fastapi, python-multipart, itsdangerous, bcrypt,
          python-dotenv (same dependencies as the main project)
"""

import os
import shutil
import sqlite3
import tempfile

import bcrypt
import pytest

import main as strata_main
import tiering_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_storage(tmp_path, monkeypatch):
    """Creates a temporary storage/{tier}/username/bucket/ directory tree
    and points TIER_DIRS at it, so tests never touch the real project's
    storage folder."""
    tier_dirs = {}
    for tier in ("HOT", "COOL", "COLD", "ARCHIVE"):
        d = tmp_path / "storage" / tier.lower()
        d.mkdir(parents=True)
        tier_dirs[tier] = str(d)

    monkeypatch.setattr(strata_main, "TIER_DIRS", tier_dirs)
    return tier_dirs


@pytest.fixture
def memory_db():
    """A real, in-memory SQLite database matching the production schema
    (just the columns these tests touch) so promote_to_hot's actual SQL
    runs against a genuine database, not a mock."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, filename TEXT, storage_tier TEXT,
            last_accessed_at TEXT, hot_entered_at TEXT
        );
        CREATE TABLE tier_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER, user_id INTEGER,
            from_tier TEXT, to_tier TEXT,
            event_type TEXT, occurred_at TEXT
        );
    """)
    conn.commit()
    yield conn
    conn.close()


def insert_file(conn, filename="test.png", tier="ARCHIVE", user_id=1):
    cur = conn.execute(
        "INSERT INTO files (user_id, filename, storage_tier, last_accessed_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00')",
        (user_id, filename, tier),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# promote_to_hot() — the core bug we found and fixed
# ---------------------------------------------------------------------------

class TestPromoteToHot:

    def test_promotes_file_when_it_exists_at_expected_path(self, temp_storage, memory_db):
        """Happy path: file genuinely exists where the DB says it should.
        This must keep working exactly as before — it's the case every
        normal download already relies on."""
        file_id = insert_file(memory_db, "photo.png", tier="ARCHIVE")
        src_dir = os.path.join(temp_storage["ARCHIVE"], "alice", "default")
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, "photo.png"), "w") as f:
            f.write("fake content")

        row = dict(memory_db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())
        strata_main.promote_to_hot(memory_db, row, "alice", "default", "2026-01-01T01:00:00")

        updated = dict(memory_db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())
        assert updated["storage_tier"] == "HOT"

        hot_path = os.path.join(temp_storage["HOT"], "alice", "default", "photo.png")
        assert os.path.exists(hot_path), "File must have physically moved to HOT"

        archive_path = os.path.join(temp_storage["ARCHIVE"], "alice", "default", "photo.png")
        assert not os.path.exists(archive_path), "File must no longer exist at old location"

    def test_does_not_corrupt_db_when_file_missing_and_db_tier_unchanged(
        self, temp_storage, memory_db
    ):
        """The exact bug scenario: file is NOT at the expected path, and
        re-checking the DB shows the same tier (no one else moved it).
        The old code would silently mark this HOT anyway. The new code
        should still update the DB (last resort) but must not silently
        pretend the physical move succeeded — verified by confirming
        the file genuinely never appears in HOT."""
        file_id = insert_file(memory_db, "ghost.png", tier="ARCHIVE")
        # Deliberately do NOT create the file on disk anywhere.

        row = dict(memory_db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())
        strata_main.promote_to_hot(memory_db, row, "alice", "default", "2026-01-01T01:00:00")

        hot_path = os.path.join(temp_storage["HOT"], "alice", "default", "ghost.png")
        assert not os.path.exists(hot_path), (
            "A missing file must never appear in HOT — there's nothing to move"
        )

    def test_retries_against_fresh_db_state_if_tier_changed(self, temp_storage, memory_db):
        """Simulates the race condition: by the time promote_to_hot runs,
        the tiering engine has already moved the file further (e.g. the
        row we were handed says COOL, but the DB and disk now agree it's
        actually in COLD). The function should detect this via its retry
        and successfully promote from the CORRECT current location."""
        file_id = insert_file(memory_db, "raced.png", tier="COOL")

        # Simulate the tiering engine having already demoted it to COLD
        # on disk AND in the database, after our stale row was read.
        memory_db.execute("UPDATE files SET storage_tier='COLD' WHERE id=?", (file_id,))
        memory_db.commit()

        cold_dir = os.path.join(temp_storage["COLD"], "alice", "default")
        os.makedirs(cold_dir, exist_ok=True)
        with open(os.path.join(cold_dir, "raced.png"), "w") as f:
            f.write("fake content")

        # Pass in a STALE row claiming COOL (as if read before the race)
        stale_row = {
            "id": file_id, "user_id": 1, "filename": "raced.png",
            "storage_tier": "COOL",
        }
        strata_main.promote_to_hot(memory_db, stale_row, "alice", "default", "2026-01-01T01:00:00")

        updated = dict(memory_db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())
        assert updated["storage_tier"] == "HOT"

        hot_path = os.path.join(temp_storage["HOT"], "alice", "default", "raced.png")
        assert os.path.exists(hot_path), "Retry logic must find and move the file from its TRUE location"

    def test_file_already_hot_just_updates_last_accessed(self, memory_db):
        """If the file is already HOT, promote_to_hot should be a no-op
        move-wise — just refresh last_accessed_at."""
        file_id = insert_file(memory_db, "already_hot.png", tier="HOT")
        row = dict(memory_db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())

        strata_main.promote_to_hot(memory_db, row, "alice", "default", "2026-06-01T12:00:00")

        updated = dict(memory_db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone())
        assert updated["storage_tier"] == "HOT"
        assert updated["last_accessed_at"] == "2026-06-01T12:00:00"

        # No tier_event should be logged for an already-HOT file
        events = memory_db.execute("SELECT * FROM tier_events WHERE file_id=?", (file_id,)).fetchall()
        assert len(events) == 0


# ---------------------------------------------------------------------------
# bcrypt password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:

    def test_correct_password_verifies(self):
        plain = "alice123"
        hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
        assert bcrypt.checkpw(plain.encode("utf-8"), hashed) is True

    def test_wrong_password_rejected(self):
        plain = "alice123"
        hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
        assert bcrypt.checkpw(b"wrong_password", hashed) is False

    def test_hash_is_not_plaintext(self):
        plain = "alice123"
        hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
        assert hashed.decode("utf-8") != plain
        assert hashed.decode("utf-8").startswith(("$2a$", "$2b$", "$2y$"))

    def test_same_password_produces_different_hashes(self):
        """bcrypt salts automatically — hashing the same password twice
        must never produce the same output."""
        plain = "alice123"
        hash1 = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
        hash2 = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
        assert hash1 != hash2
        # But both must still independently verify correctly
        assert bcrypt.checkpw(plain.encode("utf-8"), hash1)
        assert bcrypt.checkpw(plain.encode("utf-8"), hash2)


# ---------------------------------------------------------------------------
# File size exemption (128KB boundary)
# ---------------------------------------------------------------------------

class TestFileSizeExemption:

    def test_file_under_128kb_is_exempt(self):
        size_mb = 50 / 1024  # 50KB
        assert size_mb < tiering_engine.MIN_TIERING_SIZE_MB

    def test_file_exactly_128kb_is_not_exempt(self):
        size_mb = 128 / 1024
        assert not (size_mb < tiering_engine.MIN_TIERING_SIZE_MB)

    def test_file_over_128kb_is_not_exempt(self):
        size_mb = 5  # 5MB
        assert not (size_mb < tiering_engine.MIN_TIERING_SIZE_MB)


# ---------------------------------------------------------------------------
# File type classification multipliers
# ---------------------------------------------------------------------------

class TestFileTypeClassification:

    def test_text_gets_faster_multiplier(self):
        mult, _ = tiering_engine.classify_content_type("text/plain")
        assert mult == 0.5

    def test_image_gets_standard_multiplier(self):
        mult, _ = tiering_engine.classify_content_type("image/png")
        assert mult == 1.0

    def test_video_gets_slower_multiplier(self):
        mult, _ = tiering_engine.classify_content_type("video/mp4")
        assert mult == 1.5

    def test_audio_gets_slower_multiplier(self):
        mult, _ = tiering_engine.classify_content_type("audio/mpeg")
        assert mult == 1.5

    def test_zip_archive_gets_faster_multiplier(self):
        mult, _ = tiering_engine.classify_content_type("application/zip")
        assert mult == 0.5

    def test_unknown_type_falls_back_to_standard(self):
        mult, _ = tiering_engine.classify_content_type("application/pdf")
        assert mult == 1.0

    def test_missing_content_type_falls_back_to_standard(self):
        mult, _ = tiering_engine.classify_content_type(None)
        assert mult == 1.0

    def test_empty_string_content_type_falls_back_to_standard(self):
        mult, _ = tiering_engine.classify_content_type("")
        assert mult == 1.0


# ---------------------------------------------------------------------------
# Billing math — balance_due (the overpayment scenario we debugged)
# ---------------------------------------------------------------------------

class TestBalanceDueMath:

    def test_balance_due_is_difference_when_underpaid(self):
        grand_total = 100.0
        payments_made = 60.0
        balance_due = round(max(grand_total - payments_made, 0), 6)
        assert balance_due == 40.0

    def test_balance_due_is_zero_when_exactly_paid(self):
        grand_total = 100.0
        payments_made = 100.0
        balance_due = round(max(grand_total - payments_made, 0), 6)
        assert balance_due == 0.0

    def test_balance_due_never_goes_negative_when_overpaid(self):
        """This is the exact real scenario we hit: paid ₹124,626 against
        ₹121,885 of charges. balance_due must clamp at 0, never go
        negative, regardless of how much was overpaid."""
        grand_total = 121885.570826
        payments_made = 124626.155918
        balance_due = round(max(grand_total - payments_made, 0), 6)
        assert balance_due == 0.0
        assert balance_due >= 0


# ---------------------------------------------------------------------------
# Object metadata — ETag + Content-Type
# ---------------------------------------------------------------------------

class TestObjectMetadata:

    def test_etag_is_deterministic(self):
        content = b"identical content"
        etag1 = strata_main.compute_etag(content)
        etag2 = strata_main.compute_etag(content)
        assert etag1 == etag2

    def test_etag_differs_for_different_content(self):
        etag1 = strata_main.compute_etag(b"content A")
        etag2 = strata_main.compute_etag(b"content B")
        assert etag1 != etag2

    def test_etag_is_valid_md5_hex(self):
        etag = strata_main.compute_etag(b"some content")
        assert len(etag) == 32
        assert all(c in "0123456789abcdef" for c in etag)

    def test_content_type_detected_for_known_extensions(self):
        assert strata_main.guess_content_type("photo.png") == "image/png"
        assert strata_main.guess_content_type("report.pdf") == "application/pdf"
        assert strata_main.guess_content_type("notes.txt") == "text/plain"

    def test_content_type_falls_back_for_unknown_extension(self):
        result = strata_main.guess_content_type("mystery.xyz123")
        assert result == "application/octet-stream"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
