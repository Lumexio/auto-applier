"""Tests for Firefox system profile discovery and cookie/session synchronization."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from import_profile import (
    _safe_copy_sqlite,
    clean_stale_firefox_lock,
    find_system_firefox_profile,
    sync_system_firefox_profile,
)


def test_clean_stale_firefox_lock() -> None:
    """Test purging stale Firefox parent.lock and Unix lock files."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        p = Path(tmp_dir)
        (p / "parent.lock").touch()
        (p / "lock").touch()
        (p / ".parentlock").touch()
        assert (p / "parent.lock").is_file()

        cleaned = clean_stale_firefox_lock(p)
        assert cleaned is True
        assert not (p / "parent.lock").exists()
        assert not (p / "lock").exists()
        assert not (p / ".parentlock").exists()


def test_find_system_firefox_profile_runs() -> None:
    """Test that profile discovery completes without unhandled exceptions."""
    profile = find_system_firefox_profile()
    if profile is not None:
        assert profile.is_dir(), f"Discovered profile path does not exist: {profile}"



def test_safe_copy_sqlite_and_integrity() -> None:
    """Test copying an active SQLite database safely without corruption."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        src_db = tmp_path / "source.sqlite"
        dst_db = tmp_path / "dest.sqlite"

        # Create source sqlite database with data
        conn = sqlite3.connect(str(src_db))
        conn.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT)")
        conn.execute("INSERT INTO moz_cookies (host, name, value) VALUES ('.myworkday.com', 'session_id', 'secret123')")
        conn.commit()

        # Copy using _safe_copy_sqlite while open
        success = _safe_copy_sqlite(src_db, dst_db)
        conn.close()

        assert success, "Safe copy of SQLite database failed"
        assert dst_db.is_file(), "Destination database file was not created"

        # Verify integrity of copied database
        dst_conn = sqlite3.connect(str(dst_db))
        row = dst_conn.execute("SELECT host, name, value FROM moz_cookies").fetchone()
        dst_conn.close()

        assert row == ('.myworkday.com', 'session_id', 'secret123'), f"Unexpected data in copied DB: {row}"


def test_sync_system_firefox_profile_mocked() -> None:
    """Test synchronizing cookies, credentials, and web storage from a simulated Firefox profile."""
    with tempfile.TemporaryDirectory() as tmp_src, tempfile.TemporaryDirectory() as tmp_dst:
        src_path = Path(tmp_src)
        dst_path = Path(tmp_dst)

        # Mock source Firefox profile files
        cookies_db = src_path / "cookies.sqlite"
        c_conn = sqlite3.connect(str(cookies_db))
        c_conn.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, name TEXT)")
        c_conn.execute("INSERT INTO moz_cookies (name) VALUES ('auth_tok')")
        c_conn.commit()
        c_conn.close()

        (src_path / "key4.db").write_text("dummy-key4", encoding="utf-8")
        (src_path / "logins.json").write_text("{\"logins\": []}", encoding="utf-8")

        # Mock web storage
        storage_dir = src_path / "storage" / "default" / "https+++workday.com"
        storage_dir.mkdir(parents=True)
        (storage_dir / "data.sqlite").write_text("storage-content", encoding="utf-8")

        # Run sync
        result = sync_system_firefox_profile(target_dir=dst_path, source_profile=src_path)
        assert result is True, "Sync returned False for valid mock profile"

        # Verify synchronized files
        assert (dst_path / "cookies.sqlite").is_file(), "cookies.sqlite was not copied"
        assert (dst_path / "key4.db").is_file(), "key4.db was not copied"
        assert (dst_path / "logins.json").is_file(), "logins.json was not copied"
        assert (dst_path / "storage" / "default" / "https+++workday.com" / "data.sqlite").is_file(), "Storage was not copied"


def test_find_system_firefox_profile_runs() -> None:
    """Test that profile discovery completes without unhandled exceptions."""
    profile = find_system_firefox_profile()
    if profile is not None:
        assert profile.is_dir(), f"Discovered profile path does not exist: {profile}"
