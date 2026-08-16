"""Profile scanning helpers and MySQL persistence."""

import json
import re
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

MYSQL_PROFILE_TABLE = "twitter_profiles"
MYSQL_DONATION_TABLE = "donation_sites"
MYSQL_VTUBER_TABLE = "vtubers"
USER_BY_SCREEN_NAME_HAR = "UserByScreenName_Archive [26-05-25 16-59-07].har"
FOLLOWING_API_HAR_PATTERN = "x.com_i_api_graphql_Following_Archive*.har"

KNOWN_VTUBER_AGENCIES = {
    "hololive",
    "holostars",
    "nijisanji",
    "vshojo",
    "phase connect",
    "idol corp",
    "first stage production",
    "specialite",
}


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
    owner_match = re.search(r"x\.com/([^/?#]+)/(following|lists?/?.*)", following_url, re.IGNORECASE)
    owner_handle = owner_match.group(1).lower() if owner_match else ""
    html_text = html.replace("&quot;", '"')

    def is_valid_handle(handle: str) -> bool:
        value = handle.strip().lower()
        if not value or value in blocked or value == owner_handle:
            return False
        return re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) is not None

    soup = BeautifulSoup(html_text, "html.parser")
    search_roots: list[Tag] = []
    for node in soup.find_all(attrs={"aria-label": True}):
        aria = clean_text(str(node.get("aria-label", ""))).lower()
        if any(term in aria for term in ("following", "siguiendo", "members", "miembros", "lista", "list")):
            if isinstance(node, Tag):
                search_roots.append(node)
    if not search_roots:
        search_roots = [soup]

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_handle(raw_handle: str) -> None:
        handle = str(raw_handle).strip().lstrip("/")
        if not is_valid_handle(handle):
            return
        lower = handle.lower()
        if lower in seen:
            return
        seen.add(lower)
        candidates.append({"url": f"https://x.com/{handle}"})

    for root in search_roots:
        cells = list(root.find_all(attrs={"data-testid": "UserCell"}))
        if not cells and root is soup:
            cells = list(soup.find_all(attrs={"data-testid": "UserCell"}))
        for cell in cells:
            for anchor in cell.find_all("a", href=True):
                href = str(anchor["href"]).strip()
                match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})", href)
                if match:
                    add_handle(match.group(1))
    return candidates


def normalize_profile_handle(profile_url: str) -> str:
    value = profile_url.rstrip("/").rsplit("/", 1)[-1].strip()
    return value.lstrip("@")


def compare_profile_sources(sources: dict[str, list[str]]) -> dict[str, object]:
    normalized_sources: dict[str, list[str]] = {}
    handle_sources: dict[str, set[str]] = {}

    for source_name, urls in sources.items():
        handles: list[str] = []
        seen: set[str] = set()
        for url in urls:
            handle = normalize_profile_handle(str(url))
            if not handle:
                continue
            lower = handle.lower()
            if lower in seen:
                continue
            seen.add(lower)
            handles.append(handle)
            handle_sources.setdefault(lower, set()).add(source_name)
        normalized_sources[source_name] = handles

    duplicates: list[dict[str, object]] = []
    for handle_lower, source_names in sorted(handle_sources.items()):
        if len(source_names) < 2:
            continue
        duplicates.append(
            {
                "handle": handle_lower,
                "sources": sorted(source_names),
            }
        )

    unique_by_source: dict[str, list[str]] = {}
    for source_name, handles in normalized_sources.items():
        unique_by_source[source_name] = [
            handle
            for handle in handles
            if len(handle_sources.get(handle.lower(), set())) == 1
        ]

    return {
        "sources": normalized_sources,
        "duplicates": duplicates,
        "unique_by_source": unique_by_source,
        "total_duplicates": len(duplicates),
    }


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


def normalize_string_list(value: object) -> list[str]:
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
        text = str(item).strip()
        lower = text.lower()
        if not text or lower in seen:
            continue
        seen.add(lower)
        normalized.append(text)
    return normalized


def infer_urls_from_description(description: str) -> list[str]:
    text = str(description or "")
    if not text:
        return []
    patterns = (
        (r"(?:ko[\s\-]?fi)\s*[:：]\s*@?([A-Za-z0-9._-]{2,64})", "https://ko-fi.com/{handle}"),
        (r"(?:vgen)\s*[:：]\s*@?([A-Za-z0-9._-]{2,64})", "https://vgen.co/{handle}"),
        (r"(?:twitch)\s*[:：]\s*@?([A-Za-z0-9_]{2,32})", "https://twitch.tv/{handle}"),
        (r"(?:kick)\s*[:：]\s*@?([A-Za-z0-9_]{2,32})", "https://kick.com/{handle}"),
    )
    inferred: list[str] = []
    seen: set[str] = set()
    for pattern, template in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            handle = str(match.group(1) or "").strip().strip("/")
            handle = handle.lstrip("@")
            if not handle:
                continue
            url = template.format(handle=handle)
            if url in seen or is_ignored_profile_url(url):
                continue
            seen.add(url)
            inferred.append(url)
    return inferred


def extract_vtuber_metadata(description: str) -> dict[str, str]:
    text = str(description or "")
    values = {
        "papa": "",
        "mama": "",
        "agencia": "",
        "grupo": "",
    }
    if not text:
        return values

    field_patterns = (
        ("mama", r"(?:mama|ママ)\s*[:：]\s*@?([A-Za-z0-9_]{1,15})"),
        ("papa", r"(?:papa|パパ)\s*[:：]\s*@?([A-Za-z0-9_]{1,15})"),
        (
            "agencia",
            r"(?:agency|agencia|affiliation|所属)\s*[:：]\s*(.+?)(?=\s+(?:group|grupo|unit|agency|agencia|affiliation|mama|papa|ママ|パパ|所属)\s*[:：]|[|/\n\r,;]|$)",
        ),
        (
            "grupo",
            r"(?:group|grupo|unit)\s*[:：]\s*(.+?)(?=\s+(?:group|grupo|unit|agency|agencia|affiliation|mama|papa|ママ|パパ|所属)\s*[:：]|[|/\n\r,;]|$)",
        ),
    )
    for field, pattern in field_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            values[field] = clean_text(str(match.group(1))).strip(" .,:;|/")

    lowered = text.lower()
    if not values["agencia"]:
        for agency in sorted(KNOWN_VTUBER_AGENCIES, key=len, reverse=True):
            if re.search(rf"(?<![a-z0-9]){re.escape(agency)}(?![a-z0-9])", lowered):
                values["agencia"] = agency.title()
                break
    return values


def load_user_by_screen_name_template(base_dir: Path) -> dict[str, object]:
    path = base_dir / USER_BY_SCREEN_NAME_HAR
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("log", {}).get("entries", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"No HAR entries found in {path.name}")
    request = entries[0].get("request", {})
    url = str(request.get("url", ""))
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    headers: dict[str, str] = {}
    for item in request.get("headers", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        value = str(item.get("value", "")).strip()
        if name in {
            "authorization",
            "x-twitter-client-language",
            "x-twitter-active-user",
            "x-twitter-auth-type",
            "x-client-transaction-id",
        } and value:
            headers[name] = value
    return {
        "path": parsed.path,
        "features": params.get("features", ["{}"])[0],
        "field_toggles": params.get("fieldToggles", ["{}"])[0],
        "headers": headers,
    }


def load_following_api_template(base_dir: Path) -> dict[str, object]:
    """Load Following GraphQL API template from HAR files."""
    import glob
    
    # Find the most recent Following HAR file
    har_files = sorted(glob.glob(str(base_dir / "x.com_i_api_graphql_Following_Archive*.har")), reverse=True)
    if not har_files:
        raise ValueError(f"No Following HAR files found in {base_dir}")
    
    path = Path(har_files[0])
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("log", {}).get("entries", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"No HAR entries found in {path.name}")
    
    request = entries[0].get("request", {})
    url = str(request.get("url", ""))
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    
    # Extract the API path and operation ID from URL
    path_str = str(parsed.path)
    # Path format: /i/api/graphql/{operation_id}/Following
    
    headers: dict[str, str] = {}
    for item in request.get("headers", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        value = str(item.get("value", "")).strip()
        if name in {
            "authorization",
            "x-twitter-client-language",
            "x-twitter-active-user",
            "x-twitter-auth-type",
            "x-client-transaction-id",
        } and value:
            headers[name] = value
    
    return {
        "path": path_str,
        "features": params.get("features", ["{}"])[0],
        "field_toggles": params.get("fieldToggles", ["{}"])[0],
        "headers": headers,
    }


def extract_profiles_from_following_response(response_data: object) -> list[dict[str, str]]:
    """Extract profile handles and details from Following GraphQL response."""
    if not isinstance(response_data, dict):
        return []
    
    profiles: list[dict[str, str]] = []
    seen: set[str] = set()
    
    try:
        data = response_data.get("data", {})
        if not isinstance(data, dict):
            return []
        
        user_obj = data.get("user", {})
        if not isinstance(user_obj, dict):
            return []
        
        result_obj = user_obj.get("result", {})
        if not isinstance(result_obj, dict):
            return []
        
        timeline_obj = result_obj.get("timeline", {})
        if not isinstance(timeline_obj, dict):
            return []
        
        timeline_timeline = timeline_obj.get("timeline", {})
        if not isinstance(timeline_timeline, dict):
            return []
        
        instructions = timeline_timeline.get("instructions", [])
        if not isinstance(instructions, list):
            return []
        
        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            
            entries = instruction.get("entries", [])
            if not isinstance(entries, list):
                continue
            
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                
                content = entry.get("content", {})
                if not isinstance(content, dict):
                    continue
                
                item_content = content.get("itemContent", {})
                if not isinstance(item_content, dict):
                    continue
                
                user_results = item_content.get("user_results", {})
                if not isinstance(user_results, dict):
                    continue
                
                user_result = user_results.get("result", {})
                if not isinstance(user_result, dict):
                    continue
                
                core = user_result.get("core", {})
                if not isinstance(core, dict):
                    continue
                
                screen_name = str(core.get("screen_name", "")).strip()
                # Validate screen_name: must be 1-15 alphanumeric + underscore, not empty
                if not screen_name:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", screen_name):
                    # Invalid handle format, skip
                    continue
                if screen_name.lower() in seen:
                    continue
                
                seen.add(screen_name.lower())
                
                # Extract profile information from the response
                legacy = user_result.get("legacy", {})
                if not isinstance(legacy, dict):
                    legacy = {}
                
                profile_bio = user_result.get("profile_bio", {})
                if not isinstance(profile_bio, dict):
                    profile_bio = {}
                
                name = str(core.get("name", "")).strip()
                description = clean_text(
                    str(profile_bio.get("description", "") or legacy.get("description", "") or "")
                )
                
                # Extract URLs
                urls: list[str] = []
                seen_urls: set[str] = set()
                
                def add_url(raw: object) -> None:
                    value = str(raw or "").strip()
                    if not value or is_ignored_profile_url(value):
                        return
                    if value in seen_urls:
                        return
                    seen_urls.add(value)
                    urls.append(value)
                
                entities = legacy.get("entities", {})
                if isinstance(entities, dict):
                    for section_name in ("url", "description"):
                        section = entities.get(section_name, {})
                        if not isinstance(section, dict):
                            continue
                        for item in section.get("urls", []):
                            if not isinstance(item, dict):
                                continue
                            add_url(item.get("expanded_url") or item.get("url"))
                
                # Extract URLs from description text
                for match in re.findall(r"(https?://[^\s<>\"]+)", description):
                    text_url = match.rstrip(").,;!?")
                    if not is_tco_url(text_url):
                        add_url(text_url)
                
                for inferred_url in infer_urls_from_description(description):
                    add_url(inferred_url)
                
                # Final validation: ensure we have a valid screen_name
                if not screen_name or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", screen_name):
                    continue
                
                profiles.append({
                    "url": f"https://x.com/{screen_name}",
                    "title": name or screen_name,
                    "description": description,
                    "urls": urls,
                    "detected_handle": screen_name,
                    "followers_count": legacy.get("followers_count", 0),
                    "following_count": legacy.get("friends_count", 0),
                    "tweet_count": legacy.get("statuses_count", 0),
                    "vtuber_metadata": extract_vtuber_metadata(description),
                })
    
    except Exception as e:
        print(f"Error parsing Following response: {e}")
    
    return profiles


def fetch_following_profiles_from_api(
    template: dict[str, object],
    user_id: str,
    count: int = 100,
    cursor: str | None = None,
) -> tuple[list[dict[str, str]], str | None]:
    """
    Fetch following profiles directly from Twitter GraphQL API.
    
    Args:
        template: API template from load_following_api_template()
        user_id: Twitter user ID to get followings for
        count: Number of profiles per request (default 100, max 100)
        cursor: Pagination cursor for subsequent requests
    
    Returns:
        Tuple of (profiles_list, next_cursor)
    """
    try:
        from urllib.request import Request, urlopen
        from urllib.parse import urlencode
    except ImportError as e:
        raise RuntimeError(f"Failed to import urllib: {e}") from e
    
    try:
        path = str(template.get("path", ""))
        features = str(template.get("features", "{}"))
        field_toggles = str(template.get("field_toggles", "{}"))
        headers_dict = dict(template.get("headers", {}))
        
        # Build variables JSON for GraphQL
        variables = {
            "userId": str(user_id).strip(),
            "count": min(int(count), 100),
            "includePromotedContent": False,
            "withGrokTranslatedBio": True,
        }
        if cursor:
            variables["cursor"] = str(cursor).strip()
        
        # Build full URL
        url = f"https://x.com{path}"
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": features,
            "fieldToggles": field_toggles,
        }
        full_url = url + "?" + urlencode(params)
        
        # Create request with headers
        req = Request(full_url)
        headers_dict.update({
            "content-type": "application/json",
            "accept": "*/*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        for header_name, header_value in headers_dict.items():
            req.add_header(header_name, str(header_value))
        
        # Make request
        response = urlopen(req, timeout=10)
        response_data = response.read().decode('utf-8')
        data = json.loads(response_data)
        
        profiles = extract_profiles_from_following_response(data)
        
        # Extract next cursor for pagination
        next_cursor = None
        try:
            instructions = (
                data.get("data", {})
                .get("user", {})
                .get("result", {})
                .get("timeline", {})
                .get("timeline", {})
                .get("instructions", [])
            )
            for instruction in instructions:
                if instruction.get("type") == "TimelineCursor" and instruction.get("direction") == "Bottom":
                    next_cursor = instruction.get("value", "")
                    break
        except Exception:
            pass
        
        return profiles, next_cursor
    
    except Exception as e:
        print(f"Error fetching Following profiles: {e}")
        return [], None


def extract_user_id_from_profile_response(profile_response: object) -> str:
    """Extract user ID from UserByScreenName API response."""
    if not isinstance(profile_response, dict):
        return ""
    try:
        user_result = (
            profile_response.get("data", {})
            .get("user", {})
            .get("result", {})
        )
        if isinstance(user_result, dict):
            user_id = user_result.get("id", "")
            if user_id:
                return str(user_id)
    except Exception:
        pass
    return ""


def validate_user_by_screen_name_response(payload: object) -> tuple[bool, str]:
    """
    Validate UserByScreenName API response.
    
    Returns:
        Tuple of (is_valid, error_message)
        - If valid: (True, "")
        - If invalid: (False, error_reason)
    """
    if not isinstance(payload, dict):
        return False, "Response is not a dict"
    
    try:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return False, "No data field"
        
        user_obj = data.get("user", {})
        if not isinstance(user_obj, dict):
            return False, "No user field"
        
        result_user = user_obj.get("result", {})
        if not isinstance(result_user, dict):
            return False, "No result field"
        
        typename = result_user.get("__typename", "")
        
        if typename == "User":
            return True, ""
        elif typename == "UserUnavailable":
            reason = result_user.get("reason", "")
            if reason == "Deactivated":
                return False, "Cuenta desactivada"
            elif reason == "Suspended":
                return False, "Cuenta suspendida"
            else:
                return False, f"Cuenta no disponible ({reason})"
        elif typename == "UserNotFound":
            return False, "Cuenta no existe"
        else:
            return False, f"Tipo inválido: {typename}"
    
    except Exception as e:
        return False, f"Error parsing response: {e}"


def build_profile_row_from_api_json(
    profile_url: str,
    payload: object,
) -> dict[str, str | int | list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("UserByScreenName response is not an object")
    
    # Validate response
    is_valid, error_msg = validate_user_by_screen_name_response(payload)
    if not is_valid:
        raise ValueError(error_msg)
    
    result = (
        payload.get("data", {})
        if isinstance(payload.get("data"), dict)
        else {}
    )
    user = result.get("user", {}) if isinstance(result, dict) else {}
    result_user = user.get("result", {}) if isinstance(user, dict) else {}

    core = result_user.get("core", {})
    legacy = result_user.get("legacy", {})
    profile_bio = result_user.get("profile_bio", {})
    if not isinstance(core, dict):
        core = {}
    if not isinstance(legacy, dict):
        legacy = {}
    if not isinstance(profile_bio, dict):
        profile_bio = {}

    title = clean_text(str(core.get("name", "") or profile_handle_from_url(profile_url)))
    description = clean_text(
        str(profile_bio.get("description", "") or legacy.get("description", "") or "")
    )
    urls: list[str] = []
    seen: set[str] = set()

    def add_url(raw: object) -> None:
        value = str(raw or "").strip()
        if not value or is_ignored_profile_url(value):
            return
        if value in seen:
            return
        seen.add(value)
        urls.append(value)

    entities = legacy.get("entities", {})
    if isinstance(entities, dict):
        for section_name in ("url", "description"):
            section = entities.get(section_name, {})
            if not isinstance(section, dict):
                continue
            for item in section.get("urls", []):
                if not isinstance(item, dict):
                    continue
                add_url(item.get("expanded_url") or item.get("url"))

    for match in re.findall(r"(https?://[^\s<>\"]+)", description):
        text_url = match.rstrip(").,;!?")
        if not is_tco_url(text_url):
            add_url(text_url)
    for inferred_url in infer_urls_from_description(description):
        add_url(inferred_url)

    detected_handle = str(core.get("screen_name", "") or "").strip()
    return {
        "url": profile_url,
        "title": title,
        "description": description,
        "urls": urls,
        "detected_handle": detected_handle,
    }


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
    vtuber_table_sql = _mysql_identifier(MYSQL_VTUBER_TABLE)
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
            artist_categories JSON NOT NULL,
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
    if "artist_categories" not in columns:
        cursor.execute(
            f"ALTER TABLE {table_sql} ADD COLUMN artist_categories JSON NULL AFTER artist_category"
        )
        cursor.execute(
            f"UPDATE {table_sql} SET artist_categories = JSON_ARRAY(artist_category)"
        )
    else:
        cursor.execute(
            f"UPDATE {table_sql} SET artist_categories = JSON_ARRAY(artist_category) WHERE artist_categories IS NULL"
        )
    if "content_rating" not in columns:
        cursor.execute(
            f"ALTER TABLE {table_sql} ADD COLUMN content_rating VARCHAR(16) NOT NULL DEFAULT 'unknown' AFTER artist_categories"
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
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {vtuber_table_sql} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            perfil VARCHAR(512) NOT NULL,
            name TEXT NOT NULL,
            papa TEXT NOT NULL,
            mama TEXT NOT NULL,
            agencia TEXT NOT NULL,
            grupo TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_vtubers_perfil (perfil(191))
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
        f"AND LOWER(profile_url) IN ({placeholders})"
    )
    cursor.execute(query, normalized)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {str(row[0]) for row in rows if row and row[0]}


def get_saved_profile_urls(db_config: dict[str, object], limit: int | None = None) -> list[str]:
    ensure_scan_db(db_config)
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    query = f"SELECT profile_url FROM {table_sql} ORDER BY generated_at DESC, id DESC"
    params: tuple[object, ...] = ()
    if limit is not None:
        query += " LIMIT %s"
        params = (max(1, int(limit)),)
    cursor.execute(query, params)
    urls: list[str] = []
    seen: set[str] = set()
    for row in cursor.fetchall():
        url = str(row[0] or "").strip()
        lower = url.lower()
        if not url or lower in seen:
            continue
        seen.add(lower)
        urls.append(url)
    cursor.close()
    conn.close()
    return urls


def get_scan_dashboard_data(db_config: dict[str, object], limit: int = 80) -> dict[str, object]:
    ensure_scan_db(db_config)
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    cursor.execute(f"SELECT COUNT(*) FROM {table_sql}")
    total_profiles = int(cursor.fetchone()[0])
    cursor.execute(f"SELECT artist_categories FROM {table_sql}")
    category_counter: dict[str, int] = {}
    for row in cursor.fetchall():
        values = normalize_string_list(row[0] if row else [])
        if not values:
            values = ["general"]
        for value in values:
            key = str(value).strip().lower() or "general"
            category_counter[key] = category_counter.get(key, 0) + 1
    categories = sorted(category_counter.items(), key=lambda item: (-item[1], item[0]))
    cursor.execute(
        f"""
        SELECT profile_url, title, description, urls, artist_category, artist_categories, content_rating, generated_at
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
                "artist_categories": normalize_string_list(row[5]),
                "content_rating": str(row[6] or "unknown"),
                "generated_at": str(row[7] or ""),
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
    for inferred_url in infer_urls_from_description(description):
        if inferred_url not in urls and not is_ignored_profile_url(inferred_url):
            urls.append(inferred_url)
    return {
        "url": profile_url,
        "title": title,
        "description": description,
        "urls": urls,
        "detected_handle": extract_handle_from_banner(banner),
    }


def save_following_profiles_directly(
    db_config: dict[str, object],
    profiles: list[dict[str, str | int | list]],
    ensure_db: bool = True,
) -> int:
    """
    Save profiles extracted directly from Following API to database.
    
    These profiles already have all the data (title, description, urls)
    extracted from the API response. No additional UserByScreenName calls needed!
    
    Args:
        db_config: Database configuration
        profiles: List of profile dicts from extract_profiles_from_following_response()
        ensure_db: Whether to ensure database exists first
    
    Returns:
        Number of profiles saved
    """
    # Deduplicate by URL
    unique_rows: dict[str, dict[str, str | int | list]] = {}
    for item in profiles:
        key = str(item.get("url", "")).strip().lower()
        if key:
            unique_rows[key] = item
    
    profiles = list(unique_rows.values())
    profile_count = len(profiles)
    
    if profile_count == 0:
        return 0
    
    if ensure_db:
        ensure_scan_db(db_config)
    
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    generated_at = datetime.now()
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    vtuber_table_sql = _mysql_identifier(MYSQL_VTUBER_TABLE)
    
    # Delete existing profiles for these URLs
    profile_urls = [str(item.get("url", "")).strip().lower() for item in profiles if str(item.get("url", "")).strip()]
    if profile_urls:
        placeholders = ", ".join("%s" for _ in profile_urls)
        cursor.execute(
            f"DELETE FROM {table_sql} WHERE LOWER(profile_url) IN ({placeholders})",
            profile_urls,
        )
        cursor.execute(
            f"""
            DELETE FROM {_mysql_identifier(MYSQL_DONATION_TABLE)}
            WHERE LOWER(profile_url) IN ({placeholders})
            """,
            profile_urls,
        )
    
    # Insert profiles
    cursor.executemany(
        f"""
        INSERT INTO {table_sql} (
            generated_at,
            profile_url,
            title,
            description,
            urls,
            artist_category,
            artist_categories,
            content_rating
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                generated_at,
                str(item.get("url", "")),
                str(item.get("title", "")),
                str(item.get("description", "")),
                json.dumps(normalize_urls(item.get("urls", [])), ensure_ascii=False),
                str(item.get("artist_category", "general") or "general"),
                json.dumps(
                    normalize_string_list(item.get("artist_categories", [])) or ["general"],
                    ensure_ascii=False,
                ),
                str(item.get("content_rating", "unknown") or "unknown"),
            )
            for item in profiles
        ],
    )
    conn.commit()
    
    # Handle vtuber profiles
    vtuber_rows: list[tuple[str, str, str, str, str, str]] = []
    for item in profiles:
        profile_url = str(item.get("url", ""))
        categories = normalize_string_list(item.get("artist_categories", []))
        if not categories:
            categories = [str(item.get("artist_category", "general") or "general")]
        
        if "vtuber" in {value.lower() for value in categories}:
            metadata = item.get("vtuber_metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            vtuber_rows.append(
                (
                    profile_url,
                    str(item.get("title", "")),
                    str(metadata.get("papa", "") or ""),
                    str(metadata.get("mama", "") or ""),
                    str(metadata.get("agencia", "") or ""),
                    str(metadata.get("grupo", "") or ""),
                )
            )
    
    if vtuber_rows:
        cursor.executemany(
            f"""
            INSERT INTO {vtuber_table_sql} (
                perfil,
                name,
                papa,
                mama,
                agencia,
                grupo
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                papa = VALUES(papa),
                mama = VALUES(mama),
                agencia = VALUES(agencia),
                grupo = VALUES(grupo)
            """,
            vtuber_rows,
        )
        conn.commit()
    
    cursor.close()
    conn.close()
    return profile_count


def save_scan_results(
    db_config: dict[str, object],
    rows: list[dict[str, str | int | list[str]]],
    ensure_db: bool = True,
) -> int:
    unique_rows: dict[str, dict[str, str | int | list[str]]] = {}
    for item in rows:
        key = str(item.get("url", "")).strip().lower()
        if key:
            unique_rows[key] = item
    rows = list(unique_rows.values())
    profile_count = len(rows)
    if profile_count == 0:
        return 0
    if ensure_db:
        ensure_scan_db(db_config)
    conn = _mysql_connection(db_config, with_database=True)
    cursor = conn.cursor()
    generated_at = datetime.now()
    table_sql = _mysql_identifier(MYSQL_PROFILE_TABLE)
    vtuber_table_sql = _mysql_identifier(MYSQL_VTUBER_TABLE)
    profile_urls = [str(item["url"]).strip().lower() for item in rows if str(item["url"]).strip()]
    if profile_urls:
        placeholders = ", ".join("%s" for _ in profile_urls)
        cursor.execute(
            f"DELETE FROM {table_sql} WHERE LOWER(profile_url) IN ({placeholders})",
            profile_urls,
        )
        cursor.execute(
            f"""
            DELETE FROM {_mysql_identifier(MYSQL_DONATION_TABLE)}
            WHERE LOWER(profile_url) IN ({placeholders})
            """,
            profile_urls,
        )
    cursor.executemany(
        f"""
        INSERT INTO {table_sql} (
            generated_at,
            profile_url,
            title,
            description,
            urls,
            artist_category,
            artist_categories,
            content_rating
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                generated_at,
                str(item["url"]),
                str(item["title"]),
                str(item["description"]),
                json.dumps(normalize_urls(item.get("urls", [])), ensure_ascii=False),
                str(item.get("artist_category", "general") or "general"),
                json.dumps(
                    normalize_string_list(item.get("artist_categories", [])) or ["general"],
                    ensure_ascii=False,
                ),
                str(item.get("content_rating", "unknown") or "unknown"),
            )
            for item in rows
        ],
    )
    conn.commit()
    donation_rows: list[tuple[object, str, str, str, str]] = []
    vtuber_rows: list[tuple[str, str, str, str, str, str]] = []
    for item in rows:
        profile_url = str(item["url"])
        categories = normalize_string_list(item.get("artist_categories", []))
        if not categories:
            categories = [str(item.get("artist_category", "general") or "general")]
        if "vtuber" in {value.lower() for value in categories}:
            metadata = item.get("vtuber_metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            vtuber_rows.append(
                (
                    profile_url,
                    str(item.get("title", "")),
                    str(metadata.get("papa", "") or ""),
                    str(metadata.get("mama", "") or ""),
                    str(metadata.get("agencia", "") or ""),
                    str(metadata.get("grupo", "") or ""),
                )
            )
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
    if vtuber_rows:
        cursor.executemany(
            f"""
            INSERT INTO {vtuber_table_sql} (
                perfil,
                name,
                papa,
                mama,
                agencia,
                grupo
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                papa = VALUES(papa),
                mama = VALUES(mama),
                agencia = VALUES(agencia),
                grupo = VALUES(grupo)
            """,
            vtuber_rows,
        )
        conn.commit()
    cursor.close()
    conn.close()
    return profile_count
