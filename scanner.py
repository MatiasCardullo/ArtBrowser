"""Profile scanning helpers and SQLite persistence."""

import json
import re
import sqlite3
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag


def clean_text(value: str) -> str:
    cleaned = unescape(value)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = cleaned.replace("\\n", " ").replace("\\t", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


PROFILE_HANDLE_TAG_RE = re.compile(r"@[A-Za-z0-9_]{1,15}")
PROFILE_AVATAR_TESTID_RE = re.compile(r"^UserAvatar-Container-")
PROFILE_AVATAR_HANDLE_RE = re.compile(r"^UserAvatar-Container-([A-Za-z0-9_]{1,15})$")


def profile_handle_from_url(profile_url: str) -> str:
    value = profile_url.rstrip("/").rsplit("/", 1)[-1].strip()
    return value or profile_url


def find_profile_banner(soup: BeautifulSoup) -> Tag | None:
    user_name = soup.find(attrs={"data-testid": "UserName"})
    if not isinstance(user_name, Tag):
        return None

    candidate: Tag | None = user_name
    best: Tag | None = user_name
    for _ in range(12):
        if candidate is None:
            break
        has_name = candidate.find(attrs={"data-testid": "UserName"}) is not None
        has_description = candidate.find(attrs={"data-testid": "UserDescription"}) is not None
        has_header_items = (
            candidate.find(attrs={"data-testid": "UserProfileHeader_Items"}) is not None
        )
        has_avatar = (
            candidate.find(attrs={"data-testid": PROFILE_AVATAR_TESTID_RE}) is not None
        )
        has_actions = candidate.find(attrs={"data-testid": "userActions"}) is not None
        if has_name and (has_description or has_header_items):
            best = candidate
        if has_name and (has_avatar or has_actions):
            return candidate
        parent = candidate.parent
        if not isinstance(parent, Tag):
            break
        candidate = parent
    return best


def extract_title_from_banner(banner: Tag | None, profile_handle: str) -> str:
    if banner is None:
        return profile_handle
    name_node = banner.find(attrs={"data-testid": "UserName"})
    if not isinstance(name_node, Tag):
        return profile_handle

    seen: set[str] = set()
    for text in name_node.stripped_strings:
        value = clean_text(text)
        if not value or value in seen:
            continue
        seen.add(value)
        if PROFILE_HANDLE_TAG_RE.fullmatch(value) is not None:
            continue
        if len(value) <= 1:
            continue
        return value
    return profile_handle


def extract_description_from_banner(banner: Tag | None) -> str:
    if banner is None:
        return ""
    desc_node = banner.find(attrs={"data-testid": "UserDescription"})
    if not isinstance(desc_node, Tag):
        return ""
    return clean_text(desc_node.get_text(" ", strip=True))


def extract_urls_from_banner(banner: Tag | None) -> list[str]:
    if banner is None:
        return []

    search_roots: list[Tag] = []
    for node in (
        banner.find(attrs={"data-testid": "UserDescription"}),
        banner.find(attrs={"data-testid": "UserProfileHeader_Items"}),
    ):
        if isinstance(node, Tag):
            search_roots.append(node)
    if not search_roots:
        search_roots = [banner]

    candidates: list[str] = []
    seen: set[str] = set()
    for root in search_roots:
        for anchor in root.find_all("a", href=True):
            href = unescape(str(anchor.get("href", "")).strip())
            if not href.startswith(("http://", "https://")):
                continue
            if href in seen:
                continue
            seen.add(href)
            candidates.append(href)
    return candidates


def extract_handle_from_banner(banner: Tag | None) -> str:
    if banner is None:
        return ""
    avatar = banner.find(attrs={"data-testid": PROFILE_AVATAR_TESTID_RE})
    if isinstance(avatar, Tag):
        testid = str(avatar.get("data-testid", "")).strip()
        match = PROFILE_AVATAR_HANDLE_RE.fullmatch(testid)
        if match:
            return match.group(1)

    name_node = banner.find(attrs={"data-testid": "UserName"})
    if isinstance(name_node, Tag):
        for text in name_node.stripped_strings:
            value = clean_text(text)
            match = PROFILE_HANDLE_TAG_RE.fullmatch(value)
            if match:
                return value.lstrip("@")
    return ""


def is_tco_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower().split(":")[0]
    return host == "t.co"


def extract_title(html: str, profile_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    banner = find_profile_banner(soup)
    return extract_title_from_banner(banner, profile_handle_from_url(profile_url))


def extract_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    banner = find_profile_banner(soup)
    return extract_description_from_banner(banner)


def extract_profile_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    banner = find_profile_banner(soup)
    return extract_urls_from_banner(banner)


def extract_profile_candidates(following_url: str, html: str) -> list[dict[str, str]]:
    blocked = {
        "home",
        "explore",
        "notifications",
        "messages",
        "search",
        "settings",
        "compose",
        "i",
        "login",
        "logout",
        "signup",
        "tos",
        "privacy",
        "about",
        "x",
        "share",
        "intent",
    }
    owner_match = re.search(r"x\.com/([^/?#]+)/following", following_url, re.IGNORECASE)
    owner_handle = owner_match.group(1).lower() if owner_match else ""
    html_text = html.replace("&quot;", '"')

    def is_valid_handle(handle: str) -> bool:
        value = handle.strip().lower()
        if not value or value in blocked or value == owner_handle:
            return False
        return re.fullmatch(r"[a-zA-Z0-9_]{1,15}", handle) is not None

    soup = BeautifulSoup(html_text, "html.parser")
    timeline = None
    for node in soup.find_all(attrs={"aria-label": True}):
        aria = str(node.get("aria-label", ""))
        if ("Cronología" in aria or "Timeline" in aria) and (
            "Siguiendo" in aria or "Following" in aria
        ):
            timeline = node
            break
    if timeline is None:
        return []

    ignored_tokens = {
        "seguir",
        "siguiendo",
        "te sigue",
        "follow",
        "following",
        "follows you",
        "cuenta de comentarios",
    }
    ignored_prefixes = (
        "haz clic para dejar de seguir",
        "click to unfollow",
        "unfollow ",
    )

    def extract_following_name(cell: Tag, handle: str) -> str:
        handle_lower = handle.lower()
        handle_tag = f"@{handle_lower}"
        for anchor in cell.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})", href)
            if not match or match.group(1).lower() != handle_lower:
                continue
            unique_texts: list[str] = []
            for text in anchor.stripped_strings:
                value = clean_text(text)
                if value and value not in unique_texts:
                    unique_texts.append(value)
            for value in unique_texts:
                lower = value.lower()
                if any(lower.startswith(prefix) for prefix in ignored_prefixes):
                    continue
                if lower == handle_lower or lower == handle_tag or lower in ignored_tokens:
                    continue
                if value.startswith("@"):
                    continue
                return value
        return ""

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    for cell in timeline.find_all(attrs={"data-testid": "UserCell"}):
        handle: str | None = None
        for anchor in cell.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})", href)
            if not match:
                continue
            handle = match.group(1)
            lower = handle.lower()
            if not is_valid_handle(handle) or lower in seen:
                continue
            seen.add(lower)
            break
        if handle is None:
            continue

        unique_texts: list[str] = []
        for text in cell.stripped_strings:
            value = clean_text(text)
            if value and value not in unique_texts:
                unique_texts.append(value)

        title_hint = ""
        description_hint = ""
        handle_lower = handle.lower()
        handle_tag = f"@{handle_lower}"
        for value in unique_texts:
            lower = value.lower()
            if any(lower.startswith(prefix) for prefix in ignored_prefixes):
                continue
            if lower == handle_lower or lower == handle_tag or lower in ignored_tokens:
                continue
            if not title_hint and len(value) <= 80 and not value.startswith("@"):
                title_hint = value
                continue
            if len(value) > len(description_hint) and len(value) >= 10:
                description_hint = value

        candidates.append(
            {
                "url": f"https://x.com/{handle}",
                "following_name": extract_following_name(cell, handle),
                "title_hint": title_hint,
                "description_hint": description_hint,
            }
        )
    return candidates


def normalize_urls(value: object) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            decoded = json.loads(stripped)
            value = decoded
        except json.JSONDecodeError:
            value = [stripped]
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        url = str(item).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def ensure_scan_db(db_path: str) -> sqlite3.Connection:
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    existing_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "profiles" in existing_tables:
        existing_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
        }
        if "scan_id" in existing_columns:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    profile_url TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    urls TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            if "scans" in existing_tables:
                conn.execute(
                    """
                    INSERT INTO profiles_v2 (
                        generated_at,
                        profile_url,
                        status_code,
                        title,
                        description,
                        urls
                    )
                    SELECT
                        COALESCE(scans.generated_at, ''),
                        profiles.profile_url,
                        profiles.status_code,
                        profiles.title,
                        profiles.description,
                        COALESCE(profiles.urls, '[]')
                    FROM profiles
                    LEFT JOIN scans ON scans.id = profiles.scan_id
                    """
                )
            else:
                conn.execute(
                    """
                    INSERT INTO profiles_v2 (
                        generated_at,
                        profile_url,
                        status_code,
                        title,
                        description,
                        urls
                    )
                    SELECT
                        '',
                        profile_url,
                        status_code,
                        title,
                        description,
                        COALESCE(urls, '[]')
                    FROM profiles
                    """
                )
            conn.execute("DROP TABLE profiles")
            conn.execute("ALTER TABLE profiles_v2 RENAME TO profiles")
            existing_tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            existing_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
            }
        if "following_url" in existing_columns:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    profile_url TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    urls TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO profiles_v3 (
                    generated_at,
                    profile_url,
                    status_code,
                    title,
                    description,
                    urls
                )
                SELECT
                    COALESCE(generated_at, ''),
                    profile_url,
                    status_code,
                    title,
                    description,
                    COALESCE(urls, '[]')
                FROM profiles
                """
            )
            conn.execute("DROP TABLE profiles")
            conn.execute("ALTER TABLE profiles_v3 RENAME TO profiles")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            profile_url TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            urls TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    if "scans" in existing_tables:
        conn.execute("DROP TABLE scans")
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
    }
    if "generated_at" not in existing_columns:
        conn.execute("ALTER TABLE profiles ADD COLUMN generated_at TEXT NOT NULL DEFAULT ''")
    if "urls" not in existing_columns:
        conn.execute("ALTER TABLE profiles ADD COLUMN urls TEXT NOT NULL DEFAULT '[]'")
    conn.commit()
    return conn


def get_success_profile_urls(db_path: str, urls: list[str]) -> set[str]:
    normalized = [str(url).strip().lower() for url in urls if str(url).strip()]
    if not normalized:
        return set()
    conn = ensure_scan_db(db_path)
    placeholders = ", ".join("?" for _ in normalized)
    query = (
        "SELECT LOWER(profile_url) "
        "FROM profiles "
        "WHERE status_code = 200 AND LOWER(profile_url) IN ({})".format(placeholders)
    )
    rows = conn.execute(query, normalized).fetchall()
    conn.close()
    return {str(row[0]) for row in rows if row and row[0]}


def build_profile_row_from_html(
    profile_url: str,
    profile_html: str,
    title_hint: str = "",
    description_hint: str = "",
    status_code: int = 200,
) -> dict[str, str | int | list[str]]:
    profile_handle = profile_handle_from_url(profile_url)
    soup = BeautifulSoup(profile_html, "html.parser")
    banner = find_profile_banner(soup)
    title = extract_title_from_banner(banner, profile_handle)
    if title == profile_handle and title_hint:
        title = title_hint
    description = extract_description_from_banner(banner)
    if not description and description_hint:
        description = description_hint
    urls = extract_urls_from_banner(banner)
    return {
        "url": profile_url,
        "status_code": status_code,
        "title": title,
        "description": description,
        "urls": urls,
        "detected_handle": extract_handle_from_banner(banner),
    }


def save_scan_results(
    db_path: str,
    rows: list[dict[str, str | int | list[str]]],
) -> int:
    profile_count = len(rows)
    if profile_count == 0:
        return 0
    conn = ensure_scan_db(db_path)
    generated_at = datetime.now().isoformat()
    conn.executemany(
        """
        INSERT INTO profiles (
            generated_at,
            profile_url,
            status_code,
            title,
            description,
            urls
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                generated_at,
                str(item["url"]),
                int(item["status_code"]),
                str(item["title"]),
                str(item["description"]),
                json.dumps(normalize_urls(item.get("urls", [])), ensure_ascii=False),
            )
            for item in rows
        ],
    )
    conn.commit()
    conn.close()
    return profile_count
