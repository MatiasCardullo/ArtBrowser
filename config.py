"""Application config and persistence helpers."""

import json
import os
import shutil
from pathlib import Path
from typing import Iterable, Protocol

from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWidgets import QApplication

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
PROFILE_DIR = BASE_DIR / "profile_data"
OLD_PROFILE_DIR = BASE_DIR / "profile_storage"
SCAN_DB_TABLE = "twitter_profiles"

DEFAULT_URL = "https://www.google.com"
POPUP_URL = "about:blank"
DEFAULT_FOLLOWING_SCAN_URL = "https://x.com/my_profile/following"
SCAN_MAX_PARALLEL_REQUESTS = 4
SCAN_FOLLOWING_MAX_SCROLL_ROUNDS = 60
SCAN_FOLLOWING_MAX_PROFILES = 800
SCAN_RESOLVE_TCO = True
SCAN_SKIP_ALREADY_OK = True
MYSQL_HOST = os.getenv("ARTBROWSER_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("ARTBROWSER_MYSQL_PORT", "3306"))
MYSQL_DATABASE = os.getenv("ARTBROWSER_MYSQL_DATABASE", "artbrowser")
MYSQL_USER = os.getenv("ARTBROWSER_MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("ARTBROWSER_MYSQL_PASSWORD", "")


class HasTabUrls(Protocol):
    def tab_urls(self) -> list[str]:
        ...


def ensure_profile_storage() -> None:
    if OLD_PROFILE_DIR.exists() and not PROFILE_DIR.exists():
        shutil.move(str(OLD_PROFILE_DIR), str(PROFILE_DIR))
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / "cache").mkdir(exist_ok=True)


def configure_profile(app: QApplication) -> QWebEngineProfile:
    profile = QWebEngineProfile("ArtBrowser", app)
    profile.setPersistentStoragePath(str(PROFILE_DIR))
    profile.setCachePath(str(PROFILE_DIR / "cache"))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
    settings.setAttribute(
        QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True
    )
    settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
    return profile


def load_session() -> list[list[str]]:
    if not SESSION_FILE.exists():
        return [[DEFAULT_URL]]
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        windows = raw.get("windows")
        if isinstance(windows, list):
            normalized: list[list[str]] = []
            for window in windows:
                if isinstance(window, list):
                    normalized.append(
                        [str(url) for url in window if isinstance(url, str) and url]
                    )
            if normalized:
                return normalized
    except (json.JSONDecodeError, OSError):
        pass
    return [[DEFAULT_URL]]


def save_session(windows: Iterable[HasTabUrls] | None) -> None:
    if windows is None:
        data = []
    else:
        data = [window.tab_urls() for window in windows if window.tab_urls()]
    with SESSION_FILE.open("w", encoding="utf-8") as handle:
        json.dump({"windows": data}, handle, indent=2)


def default_settings() -> dict[str, str | bool | int | float]:
    return {
        "following_scan_url": DEFAULT_FOLLOWING_SCAN_URL,
        "scan_parallel_requests": SCAN_MAX_PARALLEL_REQUESTS,
        "scan_following_max_scroll_rounds": SCAN_FOLLOWING_MAX_SCROLL_ROUNDS,
        "scan_following_max_profiles": SCAN_FOLLOWING_MAX_PROFILES,
        "scan_resolve_tco": SCAN_RESOLVE_TCO,
        "scan_skip_already_ok": SCAN_SKIP_ALREADY_OK,
        "mysql_host": MYSQL_HOST,
        "mysql_port": MYSQL_PORT,
        "mysql_database": MYSQL_DATABASE,
        "mysql_user": MYSQL_USER,
        "mysql_password": MYSQL_PASSWORD,
    }


def load_settings() -> dict[str, str | bool | int | float]:
    if not SETTINGS_FILE.exists():
        return default_settings()
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            merged = default_settings()
            merged.update(raw)
            return merged
    except (json.JSONDecodeError, OSError):
        pass
    return default_settings()


def save_settings(settings: dict[str, str | bool | int | float]) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
