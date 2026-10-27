"""Module to locate and safely synchronize cookies and session data from system Firefox profiles."""

from __future__ import annotations

import configparser
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path


def find_system_firefox_profile() -> Path | None:
    """Locate the active default Firefox profile on the current operating system."""
    app_data = os.environ.get("APPDATA")
    if not app_data:
        # Check Linux/macOS fallback
        home = Path.home()
        candidates = [
            home / ".mozilla" / "firefox",
            home / "Library" / "Application Support" / "Firefox" / "Profiles",
        ]
        for base in candidates:
            if base.is_dir():
                for p in base.glob("*.default-release"):
                    return p
                for p in base.glob("*.default"):
                    return p
        return None

    firefox_dir = Path(app_data) / "Mozilla" / "Firefox"
    profiles_ini = firefox_dir / "profiles.ini"

    # Try parsing profiles.ini to get the exact Default profile
    if profiles_ini.is_file():
        try:
            config = configparser.ConfigParser()
            config.read(profiles_ini, encoding="utf-8")
            for section in config.sections():
                if section.startswith("Install") or section.startswith("Profile"):
                    if config.has_option(section, "Default"):
                        rel_path = config.get(section, "Default")
                        profile_path = firefox_dir / rel_path
                        if profile_path.is_dir():
                            return profile_path
        except Exception:
            pass

    # Fallback to scanning Profiles directory directly
    profiles_dir = firefox_dir / "Profiles"
    if profiles_dir.is_dir():
        for p in profiles_dir.glob("*.default-release"):
            return p
        for p in profiles_dir.glob("*.default"):
            return p

    return None


def _safe_copy_sqlite(source_file: Path, dest_file: Path) -> bool:
    """Safely copy an SQLite database even if locked by an active Firefox process."""
    try:
        # First try normal copy
        shutil.copy2(source_file, dest_file)
        return True
    except (PermissionError, OSError):
        pass

    # If file is locked, try reading via SQLite backup API or temp file
    try:
        uri = f"file:{source_file.resolve().as_posix()}?mode=ro&immutable=1"
        src_conn = sqlite3.connect(uri, uri=True)
        dst_conn = sqlite3.connect(str(dest_file.resolve()))
        with dst_conn:
            src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        return True
    except Exception:
        return False


def sync_system_firefox_profile(
    target_dir: Path | str = "./.browser_profile/firefox",
    source_profile: Path | str | None = None,
) -> bool:
    """Copy cookies, logins, and web storage from system Firefox into Playwright profile."""
    src = Path(source_profile) if source_profile else find_system_firefox_profile()
    if not src or not src.is_dir():
        print(f"[ProfileSync] No system Firefox profile found.")
        return False

    dest = Path(target_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    print(f"[ProfileSync] Syncing from system Firefox profile: {src.name} -> {dest.name}")
    synced_items = 0

    # 1. Cookies database
    cookies_src = src / "cookies.sqlite"
    if cookies_src.is_file():
        if _safe_copy_sqlite(cookies_src, dest / "cookies.sqlite"):
            synced_items += 1
            # Copy WAL file if exists
            wal = src / "cookies.sqlite-wal"
            if wal.is_file():
                try:
                    shutil.copy2(wal, dest / "cookies.sqlite-wal")
                except Exception:
                    pass

    # 2. Saved logins & encryption keys
    for auth_file in ("key4.db", "logins.json", "cert9.db"):
        s_file = src / auth_file
        if s_file.is_file():
            try:
                shutil.copy2(s_file, dest / auth_file)
                synced_items += 1
            except Exception:
                pass

    # 3. Web Storage (LocalStorage & IndexedDB tokens)
    storage_src = src / "storage" / "default"
    storage_dst = dest / "storage" / "default"
    if storage_src.is_dir():
        try:
            storage_dst.mkdir(parents=True, exist_ok=True)
            for item in storage_src.iterdir():
                d_item = storage_dst / item.name
                if item.is_dir() and not d_item.exists():
                    shutil.copytree(item, d_item, ignore_dangling_symlinks=True)
                elif item.is_file():
                    shutil.copy2(item, d_item)
            synced_items += 1
        except Exception:
            pass

    clean_stale_firefox_lock(dest)
    print(f"[ProfileSync] Synchronized {synced_items} session components into {dest}.")
    return synced_items > 0


def clean_stale_firefox_lock(profile_dir: Path | str) -> bool:
    """Removes stale parent.lock / lock files in profile dir if not held by an active process."""
    p_dir = Path(profile_dir).resolve()
    cleaned = False
    for fname in ("parent.lock", "lock", ".parentlock"):
        lfile = p_dir / fname
        if lfile.exists():
            try:
                lfile.unlink(missing_ok=True)
                cleaned = True
            except OSError:
                pass
    return cleaned


if __name__ == "__main__":
    success = sync_system_firefox_profile()
    if success:
        print("Firefox profile synchronized successfully.")
    else:
        print("Failed to synchronize Firefox profile.")
