"""Profile scanning and SQLite persistence."""

import multiprocessing as mp
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

def clean_text(value: str) -> str:
    cleaned = unescape(value)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = cleaned.replace("\\n", " ").replace("\\t", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_meta_content(html: str, key: str) -> str:
    escaped_key = re.escape(key)
    patterns = [
        rf'<meta[^>]+(?:name|property)\s*=\s*["\']{escaped_key}["\'][^>]+content\s*=\s*["\'](.*?)["\']',
        rf'<meta[^>]+content\s*=\s*["\'](.*?)["\'][^>]+(?:name|property)\s*=\s*["\']{escaped_key}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            value = clean_text(match.group(1))
            if value:
                return value
    return ""


def extract_title(html: str, profile_url: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        value = clean_text(match.group(1))
        if value:
            return value
    for key in ("og:title", "twitter:title"):
        value = extract_meta_content(html, key)
        if value:
            return value
    match = re.search(r'"title"\s*:\s*"([^"]+)"', html, re.IGNORECASE)
    if match:
        value = clean_text(match.group(1))
        if value:
            return value
    return profile_url.rsplit("/", 1)[-1]


def extract_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    desc_node = soup.find(attrs={"data-testid": "UserDescription"})
    if desc_node is not None:
        value = clean_text(desc_node.get_text(" ", strip=True))
        if value:
            return value
    for key in ("description", "og:description", "twitter:description"):
        value = extract_meta_content(html, key)
        if value:
            return value
    match = re.search(r'"description"\s*:\s*"([^"]+)"', html, re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def extract_profile_link(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    link_node = soup.find("a", attrs={"data-testid": "UserUrl"}, href=True)
    if link_node is None:
        return "", ""
    href = str(link_node.get("href", "")).strip()
    label = clean_text(link_node.get_text(" ", strip=True))
    return href, label


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
                "title_hint": title_hint,
                "description_hint": description_hint,
            }
        )
    return candidates


def fetch_profile_html(
    session: requests.Session, profile_url: str, max_chars: int = 1200000
) -> tuple[int, str]:
    response = session.get(
        profile_url,
        timeout=(6, 14),
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    response.raise_for_status()
    html = response.text or ""
    if len(html) > max_chars:
        html = html[:max_chars]
    return response.status_code, html


def ensure_scan_db(db_path: str) -> sqlite3.Connection:
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            following_url TEXT NOT NULL,
            profiles_found INTEGER NOT NULL,
            generated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            profile_url TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            external_url TEXT NOT NULL DEFAULT '',
            external_url_text TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(scan_id) REFERENCES scans(id)
        )
        """
    )
    existing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
    }
    if "external_url" not in existing_columns:
        conn.execute("ALTER TABLE profiles ADD COLUMN external_url TEXT NOT NULL DEFAULT ''")
    if "external_url_text" not in existing_columns:
        conn.execute(
            "ALTER TABLE profiles ADD COLUMN external_url_text TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()
    return conn


def run_scan_process(
    following_url: str,
    source_html: str,
    db_path: str,
    event_queue: "mp.Queue[dict]",
    parallel_requests: int,
) -> None:
    try:
        event_queue.put(
            {"type": "progress", "message": f"Obteniendo followings desde: {following_url}"}
        )
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                )
            }
        )
        html_source = source_html
        if html_source:
            event_queue.put(
                {
                    "type": "progress",
                    "message": "Usando HTML renderizado de la pestaña para detectar perfiles",
                }
            )
        else:
            response = session.get(following_url, timeout=20)
            response.raise_for_status()
            html_source = response.text

        profile_candidates = extract_profile_candidates(following_url, html_source)
        if not profile_candidates:
            event_queue.put(
                {
                    "type": "failed",
                    "error": "No se encontraron perfiles en la pagina. Verifica que el perfil sea visible.",
                }
            )
            return

        profile_count = len(profile_candidates)
        max_workers = max(1, min(12, int(parallel_requests)))
        event_queue.put({"type": "progress", "message": f"Perfiles detectados: {profile_count}"})
        event_queue.put(
            {
                "type": "progress",
                "message": f"Escaneo concurrente: {max_workers} requests en paralelo",
            }
        )
        rows: list[dict[str, str | int]] = []

        def fetch_profile(candidate: dict[str, str]) -> dict[str, str | int]:
            profile_url = candidate["url"]
            title_hint = candidate.get("title_hint", "")
            description_hint = candidate.get("description_hint", "")
            local_session = requests.Session()
            local_session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                    )
                }
            )
            try:
                status_code, profile_html = fetch_profile_html(local_session, profile_url)
                title = extract_title(profile_html, profile_url)
                if title == profile_url.rsplit("/", 1)[-1] and title_hint:
                    title = title_hint
                description = extract_description(profile_html)
                if not description and description_hint:
                    description = description_hint
                external_url, external_url_text = extract_profile_link(profile_html)
                return {
                    "url": profile_url,
                    "status_code": status_code,
                    "title": title,
                    "description": description,
                    "external_url": external_url,
                    "external_url_text": external_url_text,
                }
            except requests.RequestException as exc:
                return {
                    "url": profile_url,
                    "status_code": 0,
                    "title": title_hint or profile_url.rsplit("/", 1)[-1],
                    "description": description_hint or f"ERROR: {exc}",
                    "external_url": "",
                    "external_url_text": "",
                }
            finally:
                local_session.close()

        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(fetch_profile, candidate): candidate["url"]
                for candidate in profile_candidates
            }
            for idx, candidate in enumerate(profile_candidates, start=1):
                event_queue.put(
                    {
                        "type": "progress",
                        "message": f"[{idx}/{profile_count}] Solicitando {candidate['url']}",
                    }
                )
            for future in as_completed(future_map):
                rows.append(future.result())
                completed += 1
                event_queue.put(
                    {
                        "type": "progress",
                        "message": f"[{completed}/{profile_count}] Finalizado {future_map[future]}",
                    }
                )

        conn = ensure_scan_db(db_path)
        generated_at = datetime.now().isoformat()
        cursor = conn.execute(
            "INSERT INTO scans (following_url, profiles_found, generated_at) VALUES (?, ?, ?)",
            (following_url, profile_count, generated_at),
        )
        scan_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO profiles (
                scan_id,
                profile_url,
                status_code,
                title,
                description,
                external_url,
                external_url_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scan_id,
                    str(item["url"]),
                    int(item["status_code"]),
                    str(item["title"]),
                    str(item["description"]),
                    str(item.get("external_url", "")),
                    str(item.get("external_url_text", "")),
                )
                for item in rows
            ],
        )
        conn.commit()
        conn.close()
        event_queue.put(
            {
                "type": "finished",
                "scan_id": scan_id,
                "profiles_found": profile_count,
                "db": db_path,
            }
        )
    except requests.RequestException as exc:
        event_queue.put(
            {"type": "failed", "error": f"Fallo de red durante el escaneo: {exc}"}
        )
    except Exception as exc:
        event_queue.put({"type": "failed", "error": f"Error inesperado en escaneo: {exc}"})
