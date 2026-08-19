"""Application config and persistence helpers."""

import json
import os
import shutil
from pathlib import Path
from typing import Iterable, Protocol

from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWidgets import QApplication
from web_common.session import collect_tabs

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
PROFILE_DIR = BASE_DIR / "profile_data"
OLD_PROFILE_DIR = BASE_DIR / "profile_storage"
SCAN_DB_TABLE = "twitter_profiles"
SCAN_LOG_FILE = BASE_DIR / "scan_debug.log"


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = raw.strip().strip('"').strip("'")


load_dotenv_file(BASE_DIR / ".env")

DEFAULT_URL = "https://www.google.com"
POPUP_URL = "about:blank"
DEFAULT_FOLLOWING_SCAN_URL = "https://x.com/my_profile/following"
SCAN_MAX_PARALLEL_REQUESTS = 4
SCAN_FOLLOWING_MAX_SCROLL_ROUNDS = 60
SCAN_FOLLOWING_MAX_PROFILES = 800
SCAN_SKIP_ALREADY_OK = True
MYSQL_HOST = os.getenv("ARTBROWSER_MYSQL_HOST", os.getenv("MYSQL_HOST", "127.0.0.1"))
MYSQL_PORT = int(os.getenv("ARTBROWSER_MYSQL_PORT", os.getenv("MYSQL_PORT", "3306")))
MYSQL_DATABASE = os.getenv("ARTBROWSER_MYSQL_DATABASE", os.getenv("MYSQL_DATABASE", "artbrowser"))
MYSQL_USER = os.getenv("ARTBROWSER_MYSQL_USER", os.getenv("MYSQL_USER", "root"))
MYSQL_PASSWORD = os.getenv("ARTBROWSER_MYSQL_PASSWORD", os.getenv("MYSQL_PASSWORD", ""))


class HasTabUrls(Protocol):
    def tab_urls(self) -> list[str]:
        ...

    def tab_session_entries(self) -> list[dict[str, str]]:
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
    settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
    settings.setAttribute(
        QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True
    )
    settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
    return profile


def load_session() -> list[list[dict[str, str]]]:
    if not SESSION_FILE.exists():
        return [[{"url": DEFAULT_URL}]]
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        windows = raw.get("windows")
        if isinstance(windows, list):
            normalized: list[list[dict[str, str]]] = []
            for window in windows:
                if isinstance(window, list):
                    entries = []
                    for item in window:
                        if isinstance(item, str) and item:
                            entries.append({"url": item})
                        elif isinstance(item, dict) and isinstance(item.get("url"), str):
                            entries.append(
                                {
                                    key: value
                                    for key, value in item.items()
                                    if key in {"url", "title", "favicon"}
                                    and isinstance(value, str)
                                    and value
                                }
                            )
                    if entries:
                        normalized.append(entries)
            if normalized:
                return normalized
    except (json.JSONDecodeError, OSError):
        pass
    return [[{"url": DEFAULT_URL}]]


def save_session(windows: Iterable[HasTabUrls] | None) -> None:
    if windows is None:
        data = []
    else:
        data = []
        for window in windows:
            entries = (
                window.tab_session_entries()
                if hasattr(window, "tab_session_entries")
                else [{"url": url} for url in window.tab_urls()]
            )
            if entries:
                data.append(entries)
    with SESSION_FILE.open("w", encoding="utf-8") as handle:
        json.dump({"windows": data}, handle, indent=2)


def default_settings() -> dict[str, str | bool | int | float]:
    return {
        "following_scan_url": DEFAULT_FOLLOWING_SCAN_URL,
        "scan_target_kind": "followings",
        "scan_shallow_mode": True,
        "scan_compare_urls": "",
        "scan_parallel_requests": SCAN_MAX_PARALLEL_REQUESTS,
        "scan_following_max_scroll_rounds": SCAN_FOLLOWING_MAX_SCROLL_ROUNDS,
        "scan_following_max_profiles": SCAN_FOLLOWING_MAX_PROFILES,
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
            apply_mysql_env_overrides(merged)
            return merged
    except (json.JSONDecodeError, OSError):
        pass
    return default_settings()


def apply_mysql_env_overrides(settings: dict[str, str | bool | int | float]) -> None:
    env_to_setting = (
        (("ARTBROWSER_MYSQL_HOST", "MYSQL_HOST"), "mysql_host"),
        (("ARTBROWSER_MYSQL_PORT", "MYSQL_PORT"), "mysql_port"),
        (("ARTBROWSER_MYSQL_DATABASE", "MYSQL_DATABASE"), "mysql_database"),
        (("ARTBROWSER_MYSQL_USER", "MYSQL_USER"), "mysql_user"),
        (("ARTBROWSER_MYSQL_PASSWORD", "MYSQL_PASSWORD"), "mysql_password"),
    )
    for env_keys, setting_key in env_to_setting:
        env_value = next((os.environ[key] for key in env_keys if key in os.environ), None)
        if env_value is None:
            continue
        if setting_key == "mysql_port":
            try:
                settings[setting_key] = int(env_value)
            except ValueError:
                continue
        else:
            settings[setting_key] = env_value


def save_settings(settings: dict[str, str | bool | int | float]) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
