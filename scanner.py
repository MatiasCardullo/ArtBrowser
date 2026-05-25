"""Profile scanning helpers and MySQL persistence."""

import json
import re
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

MYSQL_PROFILE_TABLE = "twitter_profiles"
MYSQL_DONATION_TABLE = "donation_sites"


def is_ignored_profile_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(str(raw_url).strip())
    except ValueError:
        return True
    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.rstrip("/").lower()
    query = parsed.query.lower()
    return host in {"carrd.co", "www.carrd.co"} and path == "/build" and query == "ref=auto"


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

    spans = name_node.find_all("span")
    handle_tag = f"@{profile_handle.lower()}"
    for span in spans:
        value = clean_text(span.get_text(" ", strip=True))
        if not value:
            continue
        if value.lower() == handle_tag:
            continue
        if PROFILE_HANDLE_TAG_RE.fullmatch(value) is not None:
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
    def _add_if_supported(raw: str, out: list[str], seen_set: set[str]) -> None:
        value = str(raw).strip()
        if not value:
            return
        if is_ignored_profile_url(value):
            return
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "mailto"}:
            return
        if value in seen_set:
            return
        seen_set.add(value)
        out.append(value)

    for root in search_roots:
        for anchor in root.find_all("a", href=True):
            href = unescape(str(anchor.get("href", "")).strip())
            title_attr = unescape(str(anchor.get("title", "")).strip())
            expanded_attr = unescape(str(anchor.get("data-expanded-url", "")).strip())
            full_attr = unescape(str(anchor.get("data-full-url", "")).strip())
            text_value = clean_text(anchor.get_text(" ", strip=True))

            raw_options = [expanded_attr, full_attr, title_attr, href, text_value]
            normalized_options: list[str] = []
            for option in raw_options:
                value = str(option).strip()
                if value:
                    normalized_options.append(value)

            if not normalized_options:
                continue
            # If href is t.co, prefer expanded metadata/text first.
            if is_tco_url(href):
                preferred = [u for u in normalized_options if not is_tco_url(u)]
                if preferred:
                    normalized_options = preferred + [u for u in normalized_options if is_tco_url(u)]

            for resolved in normalized_options:
                _add_if_supported(resolved, candidates, seen)

        # Also parse naked URLs in visible text (some profiles don't render external links as anchors).
        text_blob = clean_text(root.get_text(" ", strip=True))
        for match in re.findall(r"(https?://[^\s<>\"]+)", text_blob):
            cleaned = match.rstrip(").,;!?")
            _add_if_supported(cleaned, candidates, seen)
        for match in re.findall(r"\b(www\.[^\s<>\"]+)", text_blob):
            cleaned = ("https://" + match).rstrip(").,;!?")
            _add_if_supported(cleaned, candidates, seen)
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


def extract_handles_from_description(description: str) -> list[str]:
    found = re.findall(r"(?:^|\s)@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])", description or "")
    seen: set[str] = set()
    handles: list[str] = []
    for handle in found:
        lower = handle.lower()
        if lower in seen:
            continue
        seen.add(lower)
        handles.append(handle)
    return handles


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


def extract_urls_from_html_document(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        value = str(raw).strip()
        if not value:
            return
        if is_ignored_profile_url(value):
            return
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "mailto"}:
            return
        if value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for anchor in soup.find_all("a", href=True):
        href = unescape(str(anchor.get("href", "")).strip())
        if href:
            _add(href)
        title_attr = unescape(str(anchor.get("title", "")).strip())
        if title_attr:
            _add(title_attr)
        expanded_attr = unescape(str(anchor.get("data-expanded-url", "")).strip())
        if expanded_attr:
            _add(expanded_attr)
        full_attr = unescape(str(anchor.get("data-full-url", "")).strip())
        if full_attr:
            _add(full_attr)

    text_blob = clean_text(soup.get_text(" ", strip=True))
    for match in re.findall(r"(https?://[^\s<>\"]+)", text_blob):
        _add(match.rstrip(").,;!?"))
    for match in re.findall(r"\b(www\.[^\s<>\"]+)", text_blob):
        _add(("https://" + match).rstrip(").,;!?"))
    return candidates


def extract_link_page_content(page_url: str, html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    parsed = urlparse(page_url)
    host = parsed.netloc.lower().split(":")[0]
    target = None
    if host == "buymeacoffee.com" or host.endswith(".buymeacoffee.com"):
        target = soup.find("div", class_="tw-feature-box")
    elif host == "ko-fi.com" or host.endswith(".ko-fi.com"):
        target = soup.find("div", class_="profile-page-tile")
    if not isinstance(target, Tag):
        return ""
    return clean_text(target.get_text(" ", strip=True))


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

        candidates.append(
            {
                "url": f"https://x.com/{handle}",
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


def _mysql_driver():
    try:
        import pymysql  # type: ignore[import-not-found]

        return pymysql
    except ImportError as exc:
        raise RuntimeError(
            "PyMySQL is required. Install it with: python -m pip install PyMySQL"
        ) from exc


def _mysql_connection(db_config: dict[str, object], with_database: bool = True):
    driver = _mysql_driver()
    database = str(db_config.get("database", "artbrowser")).strip() or "artbrowser"
    kwargs: dict[str, object] = {
        "host": str(db_config.get("host", "127.0.0.1")).strip() or "127.0.0.1",
        "port": int(db_config.get("port", 3306)),
        "user": str(db_config.get("user", "root")).strip() or "root",
        "password": str(db_config.get("password", "")),
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if with_database:
        kwargs["database"] = database
    return driver.connect(**kwargs)


def _mysql_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Invalid MySQL identifier: {value}")
    return f"`{value}`"


def ensure_scan_db(db_config: dict[str, object]) -> None:
    database = str(db_config.get("database", "artbrowser")).strip() or "artbrowser"
    database_sql = _mysql_identifier(database)
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    donation_table_sql = _mysql_identifier(MYSQL_DONATION_TABLE)
    conn = _mysql_connection(db_config, with_database=False)
    cursor = conn.cursor()
    cursor.execute(
        f"CREATE DATABASE IF NOT EXISTS {database_sql} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    conn.commit()
    cursor.close()
    conn.close()

    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {table_sql} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            generated_at DATETIME(6) NOT NULL,
            profile_url VARCHAR(512) NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            urls JSON NOT NULL,
            artist_category VARCHAR(64) NOT NULL DEFAULT 'general',
            content_rating VARCHAR(16) NOT NULL DEFAULT 'unknown',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_twitter_profiles_profile_url (profile_url(191)),
            INDEX idx_twitter_profiles_category_rating (artist_category, content_rating)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """.format(table_sql=table_sql)
    )
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (MYSQL_PROFILE_TABLE,),
    )
    columns = {str(row[0]) for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT INDEX_NAME
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (MYSQL_PROFILE_TABLE,),
    )
    indexes = {str(row[0]) for row in cursor.fetchall()}
    if "idx_twitter_profiles_status_url" in indexes:
        cursor.execute(
            f"ALTER TABLE {table_sql} DROP INDEX idx_twitter_profiles_status_url"
        )
    if "status_code" in columns:
        cursor.execute(f"ALTER TABLE {table_sql} DROP COLUMN status_code")
    if "link_details" in columns:
        cursor.execute(f"ALTER TABLE {table_sql} DROP COLUMN link_details")
    if "idx_twitter_profiles_profile_url" not in indexes:
        cursor.execute(
            f"CREATE INDEX idx_twitter_profiles_profile_url ON {table_sql} (profile_url(191))"
        )
    if "artist_category" not in columns:
        cursor.execute(
            f"ALTER TABLE {table_sql} ADD COLUMN artist_category VARCHAR(64) NOT NULL DEFAULT 'general' AFTER urls"
        )
    if "content_rating" not in columns:
        cursor.execute(
            f"ALTER TABLE {table_sql} ADD COLUMN content_rating VARCHAR(16) NOT NULL DEFAULT 'unknown' AFTER artist_category"
        )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {donation_table_sql} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            generated_at DATETIME(6) NOT NULL,
            profile_url VARCHAR(512) NOT NULL,
            site_url VARCHAR(512) NOT NULL,
            host VARCHAR(191) NOT NULL,
            content MEDIUMTEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_donation_sites_profile (profile_url(191)),
            INDEX idx_donation_sites_host (host)
        ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
        """
    )
    conn.commit()
    cursor.close()
    conn.close()


def get_success_profile_urls(db_config: dict[str, object], urls: list[str]) -> set[str]:
    normalized = [str(url).strip().lower() for url in urls if str(url).strip()]
    if not normalized:
        return set()
    ensure_scan_db(db_config)
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    placeholders = ", ".join("%s" for _ in normalized)
    query = (
        "SELECT LOWER(profile_url) "
        f"FROM {_mysql_identifier(MYSQL_PROFILE_TABLE)} "
        f"WHERE TRIM(description) <> '' "
        "AND JSON_LENGTH(urls) > 0 "
        f"AND LOWER(profile_url) IN ({placeholders})"
    )
    cursor.execute(query, normalized)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {str(row[0]) for row in rows if row and row[0]}


def get_scan_dashboard_data(db_config: dict[str, object], limit: int = 80) -> dict[str, object]:
    ensure_scan_db(db_config)
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    cursor.execute(f"SELECT COUNT(*) FROM {table_sql}")
    total_profiles = int(cursor.fetchone()[0])
    cursor.execute(
        f"""
        SELECT artist_category, COUNT(*)
        FROM {table_sql}
        GROUP BY artist_category
        ORDER BY COUNT(*) DESC, artist_category ASC
        """
    )
    categories = [(str(row[0] or "general"), int(row[1])) for row in cursor.fetchall()]
    cursor.execute(
        f"""
        SELECT profile_url, title, description, urls, artist_category, content_rating, generated_at
        FROM {table_sql}
        ORDER BY generated_at DESC, id DESC
        LIMIT %s
        """,
        (max(1, int(limit)),),
    )
    rows: list[dict[str, object]] = []
    for row in cursor.fetchall():
        rows.append(
            {
                "profile_url": str(row[0] or ""),
                "title": str(row[1] or ""),
                "description": str(row[2] or ""),
                "urls": normalize_urls(row[3]),
                "artist_category": str(row[4] or "general"),
                "content_rating": str(row[5] or "unknown"),
                "generated_at": str(row[6] or ""),
            }
        )
    cursor.close()
    conn.close()
    return {
        "total_profiles": total_profiles,
        "categories": categories,
        "rows": rows,
    }


def build_profile_row_from_html(
    profile_url: str,
    profile_html: str,
) -> dict[str, str | int | list[str]]:
    profile_handle = profile_handle_from_url(profile_url)
    soup = BeautifulSoup(profile_html, "html.parser")
    banner = find_profile_banner(soup)
    title = extract_title_from_banner(banner, profile_handle)
    description = extract_description_from_banner(banner)
    urls = extract_urls_from_banner(banner)
    return {
        "url": profile_url,
        "title": title,
        "description": description,
        "urls": urls,
        "detected_handle": extract_handle_from_banner(banner),
    }


def save_scan_results(
    db_config: dict[str, object],
    rows: list[dict[str, str | int | list[str]]],
    ensure_db: bool = True,
) -> int:
    profile_count = len(rows)
    if profile_count == 0:
        return 0
    if ensure_db:
        ensure_scan_db(db_config)
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    generated_at = datetime.now()
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    cursor.executemany(
        f"""
        INSERT INTO {table_sql} (
            generated_at,
            profile_url,
            title,
            description,
            urls,
            artist_category,
            content_rating
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                generated_at,
                str(item["url"]),
                str(item["title"]),
                str(item["description"]),
                json.dumps(normalize_urls(item.get("urls", [])), ensure_ascii=False),
                str(item.get("artist_category", "general") or "general"),
                str(item.get("content_rating", "unknown") or "unknown"),
            )
            for item in rows
        ],
    )
    conn.commit()
    donation_rows: list[tuple[object, str, str, str, str]] = []
    for item in rows:
        profile_url = str(item["url"])
        raw_sites = item.get("donation_sites", [])
        if not isinstance(raw_sites, list):
            continue
        for site in raw_sites:
            if not isinstance(site, dict):
                continue
            site_url = str(site.get("url", "")).strip()
            content = str(site.get("content", "")).strip()
            if not site_url or not content:
                continue
            parsed = urlparse(site_url)
            host = parsed.netloc.lower().split(":")[0]
            donation_rows.append((generated_at, profile_url, site_url, host, content))
    if donation_rows:
        cursor.executemany(
            f"""
            INSERT INTO {_mysql_identifier(MYSQL_DONATION_TABLE)} (
                generated_at,
                profile_url,
                site_url,
                host,
                content
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            donation_rows,
        )
        conn.commit()
    cursor.close()
    conn.close()
    return profile_count
