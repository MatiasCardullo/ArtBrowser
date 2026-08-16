"""Main window implementation."""

import json
import re
import time
from datetime import datetime
from html import escape
from collections.abc import Callable
from urllib.parse import urlencode

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QAction
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtWidgets import (
    QLineEdit, QMainWindow, QTabWidget, QToolBar, QVBoxLayout, QWidget
)

from config import (
    BASE_DIR, DEFAULT_URL, SCAN_DB_TABLE, SCAN_LOG_FILE,
    SCAN_FOLLOWING_MAX_PROFILES,
    SCAN_FOLLOWING_MAX_SCROLL_ROUNDS,
    SCAN_SKIP_ALREADY_OK,
    save_session, save_settings
)
from scanner import (
    build_profile_row_from_html,
    build_profile_row_from_api_json,
    compare_profile_sources,
    ensure_scan_db,
    extract_handles_from_description,
    extract_link_page_content,
    extract_profile_candidates,
    extract_vtuber_metadata,
    extract_urls_from_html_document,
    get_scan_dashboard_data,
    get_success_profile_urls,
    is_ignored_profile_url,
    load_user_by_screen_name_template,
    load_following_api_template,
    fetch_following_profiles_from_api,
    extract_user_id_from_profile_response,
    extract_profiles_from_following_response,
    get_saved_profile_urls,
    normalize_profile_handle,
    validate_user_by_screen_name_response,
    save_following_profiles_directly,
    save_scan_results,
)
from widgets import ScanTab, SettingsTab, WebEngineView


class BrowserWindow(QMainWindow):
    open_windows = []
    profile: QWebEngineProfile | None = None
    app_settings: dict[str, str | bool | int | float] = {}
    profile_cookies: dict[str, str] = {}
    cookie_capture_connected = False
    LINK_VISIBLE_DWELL_MS = 8000
    LINK_STABILIZE_MS = 1200
    LINK_LOAD_WATCHDOG_MS = 45000
    LINK_EXPANSION_HOSTS = {"carrd.co", "crd.co", "linktr.ee", "potofu.me"}
    LINK_NO_OPEN_HOSTS = {"discord.gg"}
    LINK_ABOUT_HOSTS = {"kick.com", "patreon.com", "twitch.tv"}
    LINK_CONTENT_HOSTS = {"buymeacoffee.com", "ko-fi.com"}
    RELATED_PROFILE_BLOCKLIST = {
        "youtube",
        "youtubegaming",
        "twitch",
        "twitchsupport",
        "x",
        "twitter",
        "xsupport",
    }

    def __init__(self, initial_urls: list[str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("ArtBrowser")
        self.resize(1200, 800)
        self.scan_tab: ScanTab | None = None
        self.scan_settings_tab: SettingsTab | None = None
        self.scan_running = False
        self.scan_following_url = ""
        self.scan_pending_candidates: list[dict[str, str]] = []
        self.scan_results: list[dict[str, str | int | list[str]]] = []
        self.scan_saved_count = 0
        self.scan_active_workers = 4
        self.scan_max_profiles = 800
        self.scan_known_profile_urls: set[str] = set()
        self.scan_total_candidates = 0
        self.scan_worker_states: list[dict[str, object] | None] = [None, None, None, None]
        self.scan_url_resolve_cache: dict[str, str] = {}
        self.scan_opened_link_urls: set[str] = set()
        self.scan_api_template: dict[str, object] | None = None
        self.scan_api_view: WebEngineView | None = None
        self.scan_api_ready = False
        self.scan_api_queue: list[tuple[int, str]] = []
        self.scan_superficial_state: dict[str, object] | None = None

        BrowserWindow.open_windows.append(self)
        self._ensure_cookie_capture()
        self._create_toolbar()
        self._create_tabs()

        urls = initial_urls or [DEFAULT_URL]
        for url in urls:
            self.add_tab(url)

    def _ensure_cookie_capture(self) -> None:
        if BrowserWindow.profile is None or BrowserWindow.cookie_capture_connected:
            return
        store = BrowserWindow.profile.cookieStore()
        store.cookieAdded.connect(self._on_profile_cookie_added)
        store.loadAllCookies()
        BrowserWindow.cookie_capture_connected = True

    def _on_profile_cookie_added(self, cookie) -> None:
        try:
            name = bytes(cookie.name()).decode("utf-8", errors="ignore")
            value = bytes(cookie.value()).decode("utf-8", errors="ignore")
            domain = str(cookie.domain()).lower()
        except Exception:
            return
        if not name or not value:
            return
        if domain and "x.com" not in domain and "twitter.com" not in domain:
            return
        BrowserWindow.profile_cookies[name] = value

    def _create_web_view(self) -> WebEngineView:
        return WebEngineView(self.add_tab, BrowserWindow.profile)

    def _create_toolbar(self) -> None:
        toolbar = QToolBar("Barra de navegación")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        back_action = QAction("←", self)
        back_action.setToolTip("Atrás")
        back_action.triggered.connect(self._go_back)
        toolbar.addAction(back_action)

        forward_action = QAction("→", self)
        forward_action.setToolTip("Adelante")
        forward_action.triggered.connect(self._go_forward)
        toolbar.addAction(forward_action)

        reload_action = QAction("⟳", self)
        reload_action.setToolTip("Recargar")
        reload_action.triggered.connect(self._reload_current)
        toolbar.addAction(reload_action)

        toolbar.addSeparator()
        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("https://www.example.com")
        self.url_bar.returnPressed.connect(self._load_url_from_bar)
        toolbar.addWidget(self.url_bar)

        save_page_action = QAction("💾", self)
        save_page_action.setToolTip("Guardar página")
        save_page_action.triggered.connect(self._save_current_page)
        toolbar.addAction(save_page_action)

        settings_action = QAction("⚙", self)
        settings_action.setToolTip("Configuración")
        settings_action.triggered.connect(self.open_settings_tab)
        toolbar.addAction(settings_action)

    def _create_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.tabBarClicked.connect(self._on_tab_bar_clicked)
        self.tabs.tabBar().tabMoved.connect(self._on_tab_moved)
        self._setup_plus_tab()
        container = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        container.setLayout(layout)
        self.setCentralWidget(container)

    def _setup_plus_tab(self) -> None:
        self.plus_widget = QWidget()
        index = self.tabs.addTab(self.plus_widget, "+")
        bar = self.tabs.tabBar()
        bar.setTabButton(index, bar.ButtonPosition.RightSide, None)
        bar.setTabButton(index, bar.ButtonPosition.LeftSide, None)

    def _on_tab_bar_clicked(self, index: int) -> None:
        if self.tabs.widget(index) is self.plus_widget:
            self.add_tab(DEFAULT_URL)

    def _on_tab_moved(self, from_index: int, to_index: int) -> None:
        plus_index = self.tabs.indexOf(self.plus_widget)
        last = self.tabs.count() - 1
        if plus_index != last:
            self.tabs.tabBar().moveTab(plus_index, last)

    def _on_tab_changed(self, index: int) -> None:
        _ = index
        view = self.current_view()
        self.url_bar.setText(view.url().toString() if view else "artbrowser://settings")

    def _on_tab_close_requested(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.plus_widget:
            return
        if self.tabs.count() <= 2:
            self.close()
            return
        self.tabs.removeTab(index)

    def _load_url_from_bar(self) -> None:
        url = QUrl(self.url_bar.text())
        if not url.scheme():
            url.setScheme("https")
        view = self.current_view()
        if view:
            view.setUrl(url)

    def current_view(self) -> WebEngineView | None:
        widget = self.tabs.currentWidget()
        if isinstance(widget, WebEngineView):
            return widget
        if isinstance(widget, ScanTab):
            return widget.web_view
        return None

    def add_tab(self, url: str | QUrl | None = None) -> WebEngineView:
        qurl = url if isinstance(url, QUrl) else QUrl(url or DEFAULT_URL)
        if not qurl.scheme():
            qurl.setScheme("https")
        view = self._create_web_view()
        view.setUrl(qurl)
        view.urlChanged.connect(self._update_url_bar)
        view.titleChanged.connect(
            lambda title: self.tabs.setTabText(self.tabs.indexOf(view), title)
        )
        insert_at = self.tabs.indexOf(self.plus_widget)
        index = self.tabs.insertTab(insert_at, view, "Nueva pestaña")
        self.tabs.setCurrentIndex(index)
        view.iconChanged.connect(lambda icon: self.tabs.setTabIcon(index, icon))
        return view

    def tab_urls(self) -> list[str]:
        urls: list[str] = []
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, WebEngineView):
                urls.append(widget.url().toString())
        return urls

    def _update_url_bar(self, qurl: QUrl) -> None:
        view = self.current_view()
        if view and view.url() == qurl:
            self.url_bar.setText(qurl.toString())

    def _open_new_window(self) -> None:
        new_window = BrowserWindow()
        new_window.show()

    def _go_back(self) -> None:
        view = self.current_view()
        if view:
            view.back()

    def _go_forward(self) -> None:
        view = self.current_view()
        if view:
            view.forward()

    def _reload_current(self) -> None:
        view = self.current_view()
        if view:
            view.reload()

    def open_settings_tab(self) -> None:
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, SettingsTab):
                self.tabs.setCurrentIndex(i)
                return

        settings_widget = SettingsTab(
            BrowserWindow.app_settings,
            self._start_scan_from_settings,
            self._save_scan_settings,
            self._compare_lists_from_settings,
            self._compare_lists_against_sql_from_settings,
        )
        self.scan_settings_tab = settings_widget
        index = self.tabs.addTab(settings_widget, "Configuracion")
        self.tabs.setCurrentIndex(index)

    def _save_scan_settings(self, values: dict[str, str | bool | int | float]) -> None:
        self._apply_scan_settings(values)
        save_settings(BrowserWindow.app_settings)

    def _apply_scan_settings(self, values: dict[str, str | bool | int | float]) -> None:
        url_text = str(values.get("following_scan_url", "")).strip()
        qurl = QUrl(url_text or DEFAULT_URL)
        if not qurl.scheme():
            qurl.setScheme("https")
        BrowserWindow.app_settings["following_scan_url"] = qurl.toString()
        for key in (
            "scan_parallel_requests",
            "scan_following_max_scroll_rounds",
            "scan_following_max_profiles",
            "scan_skip_already_ok",
            "scan_target_kind",
            "scan_shallow_mode",
            "scan_compare_urls",
        ):
            if key in values:
                BrowserWindow.app_settings[key] = values[key]

    def _start_scan_from_settings(self, values: dict[str, str | bool | int | float]) -> None:
        if self.scan_settings_tab is None:
            return
        self._apply_scan_settings(values)
        save_settings(BrowserWindow.app_settings)
        target_kind = str(values.get("scan_target_kind", "followings")).strip().lower()
        shallow_mode = self._scan_setting_bool("scan_shallow_mode", True)
        source_url = str(values.get("following_scan_url", DEFAULT_URL)).strip()
        if shallow_mode:
            self.start_superficial_scan(
                self.scan_settings_tab,
                source_url=source_url,
                target_kind=target_kind,
            )
            return
        if target_kind == "list":
            self._on_scan_failed("El modo profundo solo esta implementado para followings")
            return
        self.start_following_scan(self.scan_settings_tab)

    def _compare_lists_from_settings(self, values: dict[str, str | bool | int | float]) -> None:
        if self.scan_settings_tab is None:
            return
        self._apply_scan_settings(values)
        save_settings(BrowserWindow.app_settings)
        urls = self._normalize_compare_input_urls(values)
        if len(urls) < 2:
            self.scan_settings_tab.status_label.setText("Agrega al menos 2 URLs de listas para comparar")
            return
        self.start_list_comparison(self.scan_settings_tab, urls)

    def _compare_lists_against_sql_from_settings(
        self, values: dict[str, str | bool | int | float]
    ) -> None:
        if self.scan_settings_tab is None:
            return
        self._apply_scan_settings(values)
        save_settings(BrowserWindow.app_settings)
        urls = self._normalize_compare_input_urls(values)
        if not urls:
            self.scan_settings_tab.status_label.setText("Agrega al menos 1 URL para comparar con SQL")
            return
        self.start_sql_comparison(self.scan_settings_tab, urls)

    def _normalize_compare_input_urls(
        self, values: dict[str, str | bool | int | float]
    ) -> list[str]:
        raw_urls = str(values.get("scan_compare_urls", "")).splitlines()
        urls: list[str] = []
        seen: set[str] = set()
        for raw in raw_urls:
            url = str(raw).strip()
            if not url:
                continue
            qurl = QUrl(url)
            if not qurl.scheme():
                qurl.setScheme("https")
            normalized = qurl.toString().strip()
            lower = normalized.lower()
            if lower in seen:
                continue
            seen.add(lower)
            urls.append(normalized)
        return urls

    def _extract_user_handle_from_url(self, url: str) -> str | None:
        """Extract user handle from x.com/{handle}/following URL."""
        try:
            match = re.search(r"x\.com/([A-Za-z0-9_]{1,15})/following", url, re.IGNORECASE)
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    def _fetch_profiles_from_graphql_api(self, following_url: str, max_profiles: int) -> list[dict[str, str]]:
        """
        Try fetching profiles from GraphQL Following API when HTML parsing fails.
        Returns list of profile dicts, or empty list on error.
        """
        try:
            # Extract user handle
            user_handle = self._extract_user_handle_from_url(following_url)
            if not user_handle:
                self._enqueue_scan_log(f"No se pudo extraer handle de la URL: {following_url}")
                return []
            
            self._enqueue_scan_log(f"Intentando API GraphQL para usuario: {user_handle}")
            
            # Get user ID from UserByScreenName API
            user_id = self._get_user_id_from_handle(user_handle)
            if not user_id:
                self._enqueue_scan_log(f"No se pudo obtener user ID para: {user_handle}")
                return []
            
            self._enqueue_scan_log(f"User ID obtenido: {user_id[:16]}...")
            
            # Fetch profiles from Following API
            profiles, _ = fetch_following_profiles_from_api(
                self.scan_following_api_template,
                user_id,
                count=min(100, max_profiles),
                cursor=None,
            )
            
            if not profiles:
                self._enqueue_scan_log("No se obtuvieron perfiles del API GraphQL")
                return []
            
            # Save profiles directly to SQL (they already have all the data!)
            self._enqueue_scan_log(f"Guardando {len(profiles)} perfiles directamente en SQL...")
            try:
                saved_count = save_following_profiles_directly(
                    self._scan_db_config(),
                    profiles,
                    ensure_db=True,
                )
                self._enqueue_scan_log(f"✓ {saved_count} perfiles guardados en SQL")
                self.scan_saved_count = saved_count
            except Exception as exc:
                self._enqueue_scan_log(f"Error guardando en SQL: {exc}")
            
            self._enqueue_scan_log(f"✓ API GraphQL: {len(profiles)} perfiles obtenidos y guardados")
            return profiles
        
        except Exception as e:
            self._enqueue_scan_log(f"Error en API GraphQL: {e}")
            return []

    def _get_user_id_from_handle(self, user_handle: str) -> str:
        """Get user ID from Twitter handle using UserByScreenName API (synchronously)."""
        try:
            from urllib.request import Request, urlopen
            from urllib.parse import urlencode
            import json
            
            path = str(self.scan_api_template.get("path", ""))
            features = str(self.scan_api_template.get("features", "{}"))
            field_toggles = str(self.scan_api_template.get("field_toggles", "{}"))
            headers_dict = dict(self.scan_api_template.get("headers", {}))
            
            variables = {"screen_name": user_handle}
            url = f"https://x.com{path}"
            params = {
                "variables": json.dumps(variables, separators=(",", ":")),
                "features": features,
                "fieldToggles": field_toggles,
            }
            full_url = url + "?" + urlencode(params)
            
            req = Request(full_url)
            headers_dict.update({
                "content-type": "application/json",
                "accept": "*/*",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            })
            
            for header_name, header_value in headers_dict.items():
                req.add_header(header_name, str(header_value))
            
            response = urlopen(req, timeout=10)
            response_data = response.read().decode('utf-8')
            data = json.loads(response_data)
            
            user_id = extract_user_id_from_profile_response(data)
            return user_id if user_id else ""
        
        except Exception as e:
            self._enqueue_scan_log(f"Error obteniendo user ID: {e}")
            return ""

    def start_superficial_scan(
        self,
        settings_tab: SettingsTab,
        source_url: str,
        target_kind: str,
    ) -> None:
        if self.scan_running:
            return

        qurl = QUrl(str(source_url).strip())
        if not qurl.scheme():
            qurl.setScheme("https")
        scan_url = qurl.toString().strip()
        if not scan_url:
            self._on_scan_failed("URL vacia")
            return

        settings_tab.set_scan_running(True)
        self.statusBar().showMessage("Escaneo superficial iniciado", 3000)
        scan_tab = self._open_scan_tab()
        scan_tab.set_running(True)
        scan_tab.clear_workers()
        scan_tab.set_active_workers(0)

        self.scan_running = True
        self.scan_following_url = scan_url
        self.scan_pending_candidates = []
        self.scan_results = []
        self.scan_saved_count = 0
        self.scan_total_candidates = 0
        self.scan_worker_states = [None, None, None, None]
        self.scan_url_resolve_cache = {}
        self.scan_opened_link_urls = set()
        self.scan_api_ready = False
        self.scan_api_queue = []
        self.scan_superficial_state = {
            "sources": [
                {
                    "url": scan_url,
                    "kind": target_kind,
                    "label": scan_url,
                }
            ],
            "index": 0,
            "compare_mode": False,
            "save_results": True,
            "max_rounds": self._scan_setting_int(
                "scan_following_max_scroll_rounds", SCAN_FOLLOWING_MAX_SCROLL_ROUNDS, minimum=1
            ),
            "max_profiles": self._scan_setting_int(
                "scan_following_max_profiles", SCAN_FOLLOWING_MAX_PROFILES, minimum=1
            ),
            "current_candidates": [],
            "current_seen": set(),
            "current_round": 1,
            "current_source": None,
            "compare_sources": {},
        }
        try:
            ensure_scan_db(self._scan_db_config())
        except Exception as exc:
            self._on_scan_failed(f"Error inicializando base de datos: {exc}")
            return

        web_view = scan_tab.web_view
        try:
            web_view.loadFinished.disconnect(self._on_superficial_load_finished)
        except Exception:
            pass
        web_view.loadFinished.connect(self._on_superficial_load_finished)
        self._enqueue_scan_log(f"Cargando fuente superficial: {scan_url}")
        self._superficial_load_current_source()

    def start_list_comparison(
        self,
        settings_tab: SettingsTab,
        source_urls: list[str],
    ) -> None:
        if self.scan_running:
            return
        normalized_sources: list[dict[str, str]] = []
        seen: set[str] = set()
        for idx, raw_url in enumerate(source_urls, 1):
            qurl = QUrl(str(raw_url).strip())
            if not qurl.scheme():
                qurl.setScheme("https")
            url = qurl.toString().strip()
            if not url:
                continue
            lower = url.lower()
            if lower in seen:
                continue
            seen.add(lower)
            normalized_sources.append(
                {
                    "url": url,
                    "kind": "list",
                    "label": f"Lista {idx}",
                }
            )
        if len(normalized_sources) < 2:
            settings_tab.status_label.setText("Agrega al menos 2 URLs de listas para comparar")
            return

        settings_tab.set_scan_running(True)
        self.statusBar().showMessage("Comparacion de listas iniciada", 3000)
        scan_tab = self._open_scan_tab()
        scan_tab.set_running(True)
        scan_tab.clear_workers()
        scan_tab.set_active_workers(0)

        self.scan_running = True
        self.scan_following_url = normalized_sources[0]["url"]
        self.scan_pending_candidates = []
        self.scan_results = []
        self.scan_saved_count = 0
        self.scan_total_candidates = 0
        self.scan_worker_states = [None, None, None, None]
        self.scan_url_resolve_cache = {}
        self.scan_opened_link_urls = set()
        self.scan_api_ready = False
        self.scan_api_queue = []
        self.scan_superficial_state = {
            "sources": normalized_sources,
            "index": 0,
            "compare_mode": True,
            "save_results": False,
            "max_rounds": self._scan_setting_int(
                "scan_following_max_scroll_rounds", SCAN_FOLLOWING_MAX_SCROLL_ROUNDS, minimum=1
            ),
            "max_profiles": self._scan_setting_int(
                "scan_following_max_profiles", SCAN_FOLLOWING_MAX_PROFILES, minimum=1
            ),
            "current_candidates": [],
            "current_seen": set(),
            "current_round": 1,
            "current_source": None,
            "compare_sources": {},
        }

        web_view = scan_tab.web_view
        try:
            web_view.loadFinished.disconnect(self._on_superficial_load_finished)
        except Exception:
            pass
        web_view.loadFinished.connect(self._on_superficial_load_finished)
        self._enqueue_scan_log("Comparando listas superficiales...")
        self._superficial_load_current_source()

    def start_sql_comparison(
        self,
        settings_tab: SettingsTab,
        source_urls: list[str],
    ) -> None:
        if self.scan_running:
            return
        if not source_urls:
            settings_tab.status_label.setText("Agrega al menos 1 URL para comparar con SQL")
            return

        settings_tab.set_scan_running(True)
        self.statusBar().showMessage("Comparacion con SQL iniciada", 3000)
        scan_tab = self._open_scan_tab()
        scan_tab.set_running(True)
        scan_tab.clear_workers()
        scan_tab.set_active_workers(0)

        try:
            sql_urls = get_saved_profile_urls(self._scan_db_config())
        except Exception as exc:
            settings_tab.set_scan_running(False)
            self._on_scan_failed(f"Error leyendo SQL: {exc}")
            return

        self.scan_running = True
        report = compare_profile_sources(
            {
                "SQL": sql_urls,
                "Entrada": source_urls,
            }
        )
        self.scan_superficial_state = None
        self.scan_results = []
        self.scan_saved_count = 0
        self.scan_total_candidates = 0
        self._on_scan_finished(
            {
                "profiles_found": len(source_urls),
                "db": "comparacion con SQL",
                "report_html": self._build_compare_report_html(report),
            }
        )

    def _superficial_state(self) -> dict[str, object] | None:
        state = self.scan_superficial_state
        if isinstance(state, dict):
            return state
        return None

    def _superficial_load_current_source(self) -> None:
        state = self._superficial_state()
        if not self.scan_running or self.scan_tab is None or state is None:
            return
        sources = state.get("sources")
        if not isinstance(sources, list):
            self._finish_superficial_run()
            return
        index = int(state.get("index", 0))
        if index >= len(sources):
            self._finish_superficial_run()
            return
        source = sources[index]
        if not isinstance(source, dict):
            state["index"] = index + 1
            self._superficial_load_current_source()
            return
        state["current_source"] = source
        state["current_candidates"] = []
        state["current_seen"] = set()
        state["current_round"] = 1
        source_url = str(source.get("url", "")).strip()
        source_label = str(source.get("label", source_url)).strip()
        self._enqueue_scan_log(f"Fuente {index + 1}/{len(sources)}: {source_label}")
        self.scan_tab.web_view.setUrl(QUrl(source_url))

    def _click_list_members_tab(self, on_done: Callable[[bool], None] | None = None) -> None:
        if self.scan_tab is None:
            if on_done is not None:
                on_done(False)
            return
        script = """
        (() => {
            const membersLinks = Array.from(document.querySelectorAll('a[href*="/members"]'));
            for (const link of membersLinks) {
                const href = (link.getAttribute('href') || '').toLowerCase();
                const text = ((link.innerText || link.textContent || '') + ' ' + (link.getAttribute('aria-label') || '')).toLowerCase();
                if (href.includes('/members') || text.includes('members') || text.includes('miembros')) {
                    if (typeof link.click === 'function') {
                        link.click();
                        return true;
                    }
                }
            }
            const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const node of buttons) {
                const text = ((node.innerText || node.textContent || '') + ' ' + (node.getAttribute('aria-label') || '')).trim().toLowerCase();
                if (!text) continue;
                if (text.includes('members') || text.includes('miembros')) {
                    if (typeof node.click === 'function') {
                        node.click();
                        return true;
                    }
                }
            }
            return false;
        })()
        """
        self.scan_tab.web_view.page().runJavaScript(
            script,
            lambda data: on_done(bool(data)) if on_done is not None else None,
        )

    def _after_list_members_click(self, clicked: bool) -> None:
        if not clicked:
            self._enqueue_scan_log("No se encontro el enlace de miembros en la lista")
            self._superficial_finish_current_source()
            return
        QTimer.singleShot(900, self._superficial_wait_for_members_view)

    def _scroll_members_modal(self) -> None:
        if self.scan_tab is None:
            return
        script = """
        (() => {
            const roots = Array.from(document.querySelectorAll('[aria-label*="Miembros de la lista"], [aria-label*="Members of the list"], [aria-labelledby*="modal-header"], [role="dialog"]'));
            const candidates = [];
            for (const root of roots) {
                candidates.push(root);
                candidates.push(...Array.from(root.querySelectorAll('*')));
            }
            const scrollables = candidates.filter((node) => {
                if (!node || !node.scrollHeight || !node.clientHeight) return false;
                const style = window.getComputedStyle(node);
                const overflowY = style.overflowY || style.overflow;
                return node.scrollHeight > node.clientHeight + 20 && overflowY !== 'visible';
            });
            const target = scrollables[0] || roots[0] || document.scrollingElement || document.documentElement;
            if (target && typeof target.scrollBy === 'function') {
                target.scrollBy(0, Math.max(600, target.clientHeight * 0.9));
                return true;
            }
            if (target && typeof target.scrollTop === 'number') {
                target.scrollTop += Math.max(600, target.clientHeight * 0.9);
                return true;
            }
            window.scrollBy(0, Math.max(600, window.innerHeight * 0.9));
            return true;
        })()
        """
        self.scan_tab.web_view.page().runJavaScript(script)

    def _on_superficial_load_finished(self, ok: bool) -> None:
        state = self._superficial_state()
        if not self.scan_running or self.scan_tab is None or state is None:
            return
        source = state.get("current_source")
        if not isinstance(source, dict):
            return
        if not ok:
            self._enqueue_scan_log(
                f"Fuente superficial fallida: {str(source.get('url', ''))}"
            )
            self._superficial_finish_current_source()
            return
        kind = str(source.get("kind", "followings")).strip().lower()
        page_url = self.scan_tab.web_view.url().toString().lower()
        if kind == "list" and "/members" not in page_url:
            self._click_list_members_tab(self._after_list_members_click)
            return
        if kind == "list":
            QTimer.singleShot(500, self._superficial_wait_for_members_view)
            return
        QTimer.singleShot(700, lambda: self._superficial_collect_round(1))

    def _superficial_wait_for_members_view(self) -> None:
        if self.scan_tab is None:
            return
        current_url = self.scan_tab.web_view.url().toString().lower()
        state = self._superficial_state()
        if not self.scan_running or state is None:
            return
        source = state.get("current_source")
        if not isinstance(source, dict):
            return
        kind = str(source.get("kind", "followings")).strip().lower()
        if kind == "list" and "/members" not in current_url:
            QTimer.singleShot(600, self._superficial_wait_for_members_view)
            return
        QTimer.singleShot(500, lambda: self._superficial_collect_round(1))

    def _superficial_collect_round(self, round_idx: int) -> None:
        state = self._superficial_state()
        if not self.scan_running or self.scan_tab is None or state is None:
            return
        source = state.get("current_source")
        if not isinstance(source, dict):
            return
        max_rounds = int(state.get("max_rounds", 1))
        max_profiles = int(state.get("max_profiles", 1))
        current_candidates = state.get("current_candidates")
        if not isinstance(current_candidates, list):
            current_candidates = []
            state["current_candidates"] = current_candidates
        if len(current_candidates) >= max_profiles or round_idx > max_rounds:
            self._superficial_finish_current_source()
            return
        self.scan_tab.web_view.page().toHtml(
            lambda html: self._on_superficial_html(round_idx, html or "")
        )

    def _on_superficial_html(self, round_idx: int, html: str) -> None:
        state = self._superficial_state()
        if not self.scan_running or self.scan_tab is None or state is None:
            return
        source = state.get("current_source")
        if not isinstance(source, dict):
            return
        source_url = str(source.get("url", "")).strip()
        current_candidates = state.get("current_candidates")
        current_seen = state.get("current_seen")
        if not isinstance(current_candidates, list) or not isinstance(current_seen, set):
            return
        candidates = extract_profile_candidates(source_url, html)
        new_in_round = 0
        max_profiles = int(state.get("max_profiles", 1))
        for candidate in candidates:
            profile_url = str(candidate.get("url", "")).strip()
            if not profile_url:
                continue
            lower = profile_url.lower()
            if lower in current_seen:
                continue
            current_seen.add(lower)
            current_candidates.append(candidate)
            new_in_round += 1
            if len(current_candidates) >= max_profiles:
                break
        source_label = str(source.get("label", source_url)).strip()
        self._enqueue_scan_log(
            f"{source_label} ronda {round_idx}: +{new_in_round} perfiles, total={len(current_candidates)}"
        )
        if len(current_candidates) >= max_profiles or round_idx >= int(state.get("max_rounds", 1)):
            self._superficial_finish_current_source()
            return
        self.scan_tab.web_view.page().runJavaScript(
            "window.__artbrowser_scroll_members_modal = true;"
        )
        self._scroll_members_modal()
        QTimer.singleShot(900, lambda: self._superficial_collect_round(round_idx + 1))

    def _superficial_finish_current_source(self) -> None:
        state = self._superficial_state()
        if not self.scan_running or self.scan_tab is None or state is None:
            return
        source = state.get("current_source")
        if not isinstance(source, dict):
            return
        source_url = str(source.get("url", "")).strip()
        source_label = str(source.get("label", source_url)).strip()
        current_candidates = state.get("current_candidates")
        if not isinstance(current_candidates, list):
            current_candidates = []
        rows: list[dict[str, str | int | list[str]]] = []
        handles: list[str] = []
        seen_handles: set[str] = set()
        for candidate in current_candidates:
            if not isinstance(candidate, dict):
                continue
            profile_url = str(candidate.get("url", "")).strip()
            if not profile_url:
                continue
            handle = normalize_profile_handle(profile_url)
            if not handle:
                continue
            lower = handle.lower()
            if lower in seen_handles:
                continue
            seen_handles.add(lower)
            handles.append(handle)
            rows.append(
                {
                    "url": profile_url,
                    "title": handle,
                    "description": "",
                    "urls": [],
                }
            )
        compare_sources = state.get("compare_sources")
        if not isinstance(compare_sources, dict):
            compare_sources = {}
            state["compare_sources"] = compare_sources
        compare_sources[source_label or source_url] = handles
        if bool(state.get("save_results", True)) and rows:
            try:
                saved = save_scan_results(
                    db_config=self._scan_db_config(),
                    rows=rows,
                    ensure_db=False,
                )
                self.scan_saved_count += saved
            except Exception as exc:
                self._on_scan_failed(f"Error guardando escaneo superficial: {exc}")
                return
        self.scan_results.extend(rows)
        self._enqueue_scan_log(
            f"{source_label}: {len(handles)} perfiles detectados"
        )
        state["index"] = int(state.get("index", 0)) + 1
        state["current_source"] = None
        state["current_candidates"] = []
        state["current_seen"] = set()
        state["current_round"] = 1
        self._superficial_load_current_source()

    def _finish_superficial_run(self) -> None:
        state = self._superficial_state()
        compare_mode = bool(state.get("compare_mode", False)) if state else False
        compare_sources = {}
        if state and isinstance(state.get("compare_sources"), dict):
            compare_sources = dict(state["compare_sources"])
        try:
            if self.scan_tab is not None:
                self.scan_tab.web_view.loadFinished.disconnect(self._on_superficial_load_finished)
        except Exception:
            pass
        if compare_mode:
            report = compare_profile_sources(compare_sources)
            total_handles = sum(len(handles) for handles in compare_sources.values())
            self._on_scan_finished(
                {
                    "profiles_found": total_handles,
                    "db": "comparacion local",
                    "report_html": self._build_compare_report_html(report),
                }
            )
        else:
            self._on_scan_finished(
                {
                    "profiles_found": self.scan_saved_count,
                    "db": self._scan_db_label(),
                }
            )
        self.scan_superficial_state = None

    def start_following_scan(self, settings_tab: SettingsTab) -> None:
        if self.scan_running:
            return

        url_text = str(BrowserWindow.app_settings.get("following_scan_url", DEFAULT_URL)).strip()
        qurl = QUrl(url_text.strip())
        if not qurl.scheme():
            qurl.setScheme("https")
        scan_url = qurl.toString()
        settings_tab.set_scan_running(True)
        self.statusBar().showMessage("Escaneo iniciado", 3000)

        scan_tab = self._open_scan_tab()
        scan_tab.set_running(True)
        scan_tab.clear_workers()
        self.scan_active_workers = self._scan_parallel_workers()
        scan_tab.set_active_workers(self.scan_active_workers)
        self.scan_running = True
        self.scan_following_url = scan_url
        self.scan_pending_candidates = []
        self.scan_results = []
        self.scan_saved_count = 0
        self.scan_total_candidates = 0
        self.scan_worker_states = [None, None, None, None]
        self.scan_url_resolve_cache = {}
        self.scan_opened_link_urls = set()
        self.scan_api_ready = False
        self.scan_api_queue = []
        try:
            ensure_scan_db(self._scan_db_config())
            self.scan_api_template = load_user_by_screen_name_template(BASE_DIR)
            self.scan_following_api_template = load_following_api_template(BASE_DIR)
        except Exception as exc:
            self._on_scan_failed(f"Error inicializando escaneo: {exc}")
            return
        self._ensure_scan_api_view()
        self._enqueue_scan_log(f"Cargando pagina de followings: {scan_url}")
        web_view = scan_tab.web_view
        web_view.setUrl(QUrl(scan_url))
        self.url_bar.setText(scan_url)

        max_scroll_rounds = self._scan_setting_int(
            "scan_following_max_scroll_rounds", SCAN_FOLLOWING_MAX_SCROLL_ROUNDS, minimum=1
        )
        max_profiles = self._scan_setting_int(
            "scan_following_max_profiles", SCAN_FOLLOWING_MAX_PROFILES, minimum=1
        )
        self.scan_max_profiles = max_profiles
        self._enqueue_scan_log(
            "Recolector followings: "
            f"max_rounds={max_scroll_rounds}, max_profiles={max_profiles}"
        )

        collected_candidates: list[dict[str, str]] = []
        collected_urls: set[str] = set()

        def finalize_candidate_collection(reason: str) -> None:
            if not self.scan_running:
                return
            # Freeze followings page once candidates are collected; workers no longer need it.
            web_view.stop()
            self._show_scan_dashboard("Recolectando perfiles desde workers...")
            if not collected_candidates:
                self._on_scan_failed(
                    "No se encontraron perfiles en la pagina. Verifica que el perfil sea visible."
                )
                return
            self.scan_pending_candidates = collected_candidates[:max_profiles]
            if self._scan_setting_bool("scan_skip_already_ok", SCAN_SKIP_ALREADY_OK):
                candidate_urls = [
                    str(item.get("url", "")).strip()
                    for item in self.scan_pending_candidates
                    if str(item.get("url", "")).strip()
                ]
                try:
                    existing_urls = get_success_profile_urls(self._scan_db_config(), candidate_urls)
                except Exception as exc:
                    self._on_scan_failed(f"Error consultando MySQL: {exc}")
                    return
                if existing_urls:
                    before_count = len(self.scan_pending_candidates)
                    self.scan_pending_candidates = [
                        item
                        for item in self.scan_pending_candidates
                        if str(item.get("url", "")).strip().lower() not in existing_urls
                    ]
                    skipped = before_count - len(self.scan_pending_candidates)
                    self._enqueue_scan_log(
                        f"Saltados por cache MySQL con descripcion: {skipped}"
                    )
            self.scan_total_candidates = len(self.scan_pending_candidates)
            self.scan_known_profile_urls = {
                str(item.get("url", "")).strip().lower()
                for item in self.scan_pending_candidates
                if str(item.get("url", "")).strip()
            }
            self._enqueue_scan_log(
                f"Recoleccion finalizada ({reason}). Perfiles detectados: {self.scan_total_candidates}"
            )
            if self.scan_total_candidates == 0:
                self._on_scan_finished(
                    {
                        "profiles_found": self.scan_saved_count,
                        "db": self._scan_db_label(),
                    }
                )
                return
            self._enqueue_scan_log(
                f"Iniciando {self.scan_active_workers} QWebEngine para abrir perfiles en paralelo"
            )
            self._schedule_profile_workers()

        def collect_candidates_round(round_idx: int) -> None:
            if not self.scan_running:
                return
            web_view.page().toHtml(lambda html: on_following_html(round_idx, html or ""))

        def on_following_html(round_idx: int, html: str) -> None:
            if not self.scan_running:
                return
            candidates = extract_profile_candidates(scan_url, html)
            new_in_round = 0
            for candidate in candidates:
                profile_url = str(candidate.get("url", "")).strip()
                if not profile_url or profile_url in collected_urls:
                    continue
                collected_urls.add(profile_url)
                collected_candidates.append(candidate)
                new_in_round += 1
                if len(collected_candidates) >= max_profiles:
                    break

            self._enqueue_scan_log(
                f"Ronda {round_idx}: +{new_in_round} perfiles, total={len(collected_candidates)}"
            )

            if len(collected_candidates) >= max_profiles:
                finalize_candidate_collection("tope max_profiles")
                return
            if round_idx >= max_scroll_rounds:
                finalize_candidate_collection("tope max_scroll_rounds")
                return

            web_view.page().runJavaScript(
                "window.scrollBy(0, Math.max(600, window.innerHeight * 0.9));"
            )
            QTimer.singleShot(900, lambda: collect_candidates_round(round_idx + 1))

        def on_load_finished(ok: bool) -> None:
            try:
                web_view.loadFinished.disconnect(on_load_finished)
            except Exception:
                pass
            if not self.scan_running:
                return
            
            # PRIORITY: Use Following API GraphQL (single call, no ratelimit per-profile issues)
            # Extract handle from URL, get user_id, fetch all profiles at once
            self._enqueue_scan_log("Pagina cargada. Obteniendo perfiles desde API GraphQL Following...")
            
            user_handle = self._extract_user_handle_from_url(scan_url)
            if not user_handle:
                self._enqueue_scan_log(f"Error: No se pudo extraer handle de {scan_url}")
                if ok:
                    # Fallback to HTML parsing if we loaded the page
                    collect_candidates_round(1)
                else:
                    self._on_scan_failed("No se pudo cargar la página ni extraer handle")
                return
            
            # Get user ID (single UserByScreenName call, safe)
            self._enqueue_scan_log(f"Obteniendo user ID para @{user_handle}...")
            user_id = self._get_user_id_from_handle(user_handle)
            if not user_id:
                self._enqueue_scan_log(f"Error: No se pudo obtener user ID para @{user_handle}")
                if ok:
                    # Fallback to HTML parsing
                    collect_candidates_round(1)
                else:
                    self._on_scan_failed("No se pudo obtener user ID")
                return
            
            # Fetch all profiles from Following API GraphQL (single call, complete data)
            self._enqueue_scan_log(f"Obteniendo lista de seguidos desde Following API...")
            try:
                profiles, _ = fetch_following_profiles_from_api(
                    self.scan_following_api_template,
                    user_id,
                    count=max_profiles,
                    cursor=None,
                )
                
                if not profiles:
                    self._enqueue_scan_log("Following API sin resultados. Intentando HTML parsing...")
                    if ok:
                        collect_candidates_round(1)
                    else:
                        self._on_scan_failed("No se obtuvieron perfiles del API")
                    return
                
                self._enqueue_scan_log(f"✓ Following API: {len(profiles)} perfiles obtenidos")
                
                # Save profiles directly to SQL (ya tienen title, description, URLs, counts)
                self._enqueue_scan_log(f"Guardando perfiles directamente en SQL...")
                try:
                    saved_count = save_following_profiles_directly(
                        self._scan_db_config(),
                        profiles,
                        ensure_db=True,
                    )
                    self.scan_saved_count = saved_count
                    self._enqueue_scan_log(f"✓ {saved_count} perfiles guardados en SQL")
                except Exception as exc:
                    self._enqueue_scan_log(f"Error guardando en SQL: {exc}")
                    return
                
                # API-sourced profiles are already complete, no workers needed!
                web_view.stop()
                self._on_scan_finished(
                    {
                        "profiles_found": self.scan_saved_count,
                        "db": self._scan_db_label(),
                    }
                )
                
            except Exception as exc:
                self._enqueue_scan_log(f"Error en Following API: {exc}")
                if ok:
                    # Fallback to HTML parsing
                    collect_candidates_round(1)
                else:
                    self._on_scan_failed(f"Error en Following API: {exc}")

        web_view.loadFinished.connect(on_load_finished)

    def _scan_setting_int(self, key: str, default: int, minimum: int = 1) -> int:
        raw = BrowserWindow.app_settings.get(key, default)
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return max(minimum, value)

    def _scan_setting_float(
        self,
        key: str,
        default: float,
        minimum: float = 1.0,
        maximum: float | None = None,
    ) -> float:
        raw = BrowserWindow.app_settings.get(key, default)
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _scan_setting_bool(self, key: str, default: bool) -> bool:
        raw = BrowserWindow.app_settings.get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    def _open_scan_tab(self) -> ScanTab:
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, ScanTab):
                self.tabs.setCurrentIndex(i)
                self.scan_tab = widget
                return widget
        web_view = self._create_web_view()
        worker_views = [self._create_web_view() for _ in range(4)]
        self.scan_tab = ScanTab(web_view, worker_views)
        self.scan_tab.web_view.urlChanged.connect(self._update_url_bar)
        for idx, worker_view in enumerate(self.scan_tab.worker_views):
            worker_view.loadFinished.connect(
                lambda ok, worker_idx=idx: self._on_worker_load_finished(worker_idx, ok)
            )
        index = self.tabs.addTab(self.scan_tab, "Escaneo")
        self.tabs.setCurrentIndex(index)
        return self.scan_tab

    def _ensure_scan_api_view(self) -> None:
        if self.scan_api_view is not None:
            self.scan_api_view.stop()
            self.scan_api_view.setUrl(QUrl("https://x.com/home"))
            return
        self.scan_api_view = self._create_web_view()
        self.scan_api_view.loadFinished.connect(self._on_scan_api_view_loaded)
        self.scan_api_view.setUrl(QUrl("https://x.com/home"))

    def _on_scan_api_view_loaded(self, ok: bool) -> None:
        self.scan_api_ready = bool(ok)
        if not ok:
            self._enqueue_scan_log("UserByScreenName: no se pudo inicializar origen x.com")
            pending = list(self.scan_api_queue)
            self.scan_api_queue = []
            for worker_idx, profile_url in pending:
                self._skip_worker_profile(
                    worker_idx, profile_url, "origen x.com no disponible"
                )
            return
        pending = list(self.scan_api_queue)
        self.scan_api_queue = []
        for worker_idx, profile_url in pending:
            self._run_profile_api_fetch(worker_idx, profile_url)

    def _scan_parallel_workers(self) -> int:
        value = self._scan_setting_int("scan_parallel_requests", 4, minimum=2)
        return 4 if value >= 4 else 2

    def _schedule_profile_workers(self) -> None:
        if self.scan_tab is None or not self.scan_running:
            return
        for idx, worker_view in enumerate(self.scan_tab.worker_views[:self.scan_active_workers]):
            state = self.scan_worker_states[idx]
            is_busy = bool(state and state.get("busy"))
            if is_busy and isinstance(state, dict):
                # Hard guard: never reassign a worker until explicit finalization path clears busy.
                continue
            if is_busy or not self.scan_pending_candidates:
                continue
            candidate = self.scan_pending_candidates.pop(0)
            self.scan_worker_states[idx] = {
                "busy": True,
                "candidate": candidate,
                "phase": "loading_profile_api",
            }
            url = str(candidate.get("url", ""))
            self._enqueue_scan_log(
                f"[{len(self.scan_results) + 1}/{self.scan_total_candidates}] Worker {idx + 1}: API {url}"
            )
            self._fetch_profile_api_for_worker(idx, url)

        if (
            self.scan_running
            and self.scan_total_candidates > 0
            and not self.scan_pending_candidates
            and all(
                not (state and state.get("busy"))
                for state in self.scan_worker_states[:self.scan_active_workers]
            )
        ):
            self._finalize_scan_results()

    def _on_worker_load_finished(self, worker_idx: int, ok: bool) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not state or not state.get("busy"):
            return
        if str(state.get("phase", "loading_profile")) != "loading_profile":
            return
        candidate = state.get("candidate")
        if not isinstance(candidate, dict):
            return
        if not ok:
            profile_url = str(candidate.get("url", ""))
            self._skip_worker_profile(worker_idx, profile_url, "fallo de carga")
            return

        self._collect_worker_data(worker_idx, retries_left=10)

    def _fetch_profile_api_for_worker(self, worker_idx: int, profile_url: str) -> None:
        if not self.scan_running or self.scan_api_template is None:
            return
        screen_name = profile_url.rstrip("/").rsplit("/", 1)[-1].strip()
        if not screen_name:
            self._skip_worker_profile(worker_idx, profile_url, "handle vacio")
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict):
            return
        state["api_profile_url"] = profile_url
        if not self.scan_api_ready or self.scan_api_view is None:
            self.scan_api_queue.append((worker_idx, profile_url))
            return
        self._run_profile_api_fetch(worker_idx, profile_url)

    def _run_profile_api_fetch(self, worker_idx: int, profile_url: str) -> None:
        if not self.scan_running or self.scan_api_template is None or self.scan_api_view is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict):
            return
        state["phase"] = "fetching_profile_api"
        fetch_token = f"{worker_idx}-{int(time.time() * 1000)}"
        state["api_fetch_token"] = fetch_token
        state["api_poll_left"] = 60
        variables = {
            "screen_name": profile_url.rstrip("/").rsplit("/", 1)[-1].strip(),
            "withGrokTranslatedBio": True,
        }
        query = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": str(self.scan_api_template.get("features", "{}")),
            "fieldToggles": str(self.scan_api_template.get("field_toggles", "{}")),
        }
        api_path = str(self.scan_api_template.get("path", "")).strip()
        api_url = f"https://x.com{api_path}?{urlencode(query)}"
        fetch_headers = {
            "accept": "*/*",
            "content-type": "application/json",
        }
        if BrowserWindow.profile_cookies.get("ct0", ""):
            fetch_headers["x-csrf-token"] = BrowserWindow.profile_cookies["ct0"]
        headers = self.scan_api_template.get("headers", {})
        if isinstance(headers, dict):
            for name, value in headers.items():
                if value:
                    fetch_headers[str(name).lower()] = str(value)
        script = f"""
        (() => {{
            window.__artbrowser_api_results = window.__artbrowser_api_results || {{}};
            const token = {json.dumps(fetch_token)};
            const headers = {json.dumps(fetch_headers)};
            if (!headers['x-csrf-token']) {{
                const match = document.cookie.match(/(?:^|; )ct0=([^;]+)/);
                if (match) headers['x-csrf-token'] = decodeURIComponent(match[1]);
            }}
            window.__artbrowser_api_results[token] = {{ done: false, ok: false, status: 0, text: '' }};
            fetch({json.dumps(api_url)}, {{
                credentials: 'include',
                headers
            }})
                .then((response) =>
                    response.text().then((text) => {{
                        window.__artbrowser_api_results[token] = {{
                            done: true,
                            ok: response.ok,
                            status: response.status,
                            text
                        }};
                    }})
                )
                .catch((error) => {{
                    window.__artbrowser_api_results[token] = {{
                        done: true,
                        ok: false,
                        status: 0,
                        text: String(error && error.message || error)
                    }};
                }});
            return true;
        }})()
        """
        self.scan_api_view.page().runJavaScript(
            script,
            lambda _data, idx=worker_idx, url=profile_url: self._poll_profile_api_result(idx, url),
        )

    def _poll_profile_api_result(self, worker_idx: int, profile_url: str) -> None:
        if not self.scan_running or self.scan_api_view is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict) or state.get("phase") != "fetching_profile_api":
            return
        token = str(state.get("api_fetch_token", "")).strip()
        retries_left = int(state.get("api_poll_left", 0))
        if not token:
            self._skip_worker_profile(worker_idx, profile_url, "UserByScreenName token vacio")
            return
        if retries_left <= 0:
            self._skip_worker_profile(worker_idx, profile_url, "UserByScreenName timeout")
            return
        state["api_poll_left"] = retries_left - 1
        script = f"""
        (() => {{
            const store = window.__artbrowser_api_results || {{}};
            return store[{json.dumps(token)}] || null;
        }})()
        """
        self.scan_api_view.page().runJavaScript(
            script,
            lambda data, idx=worker_idx, url=profile_url: self._on_profile_api_result_polled(idx, url, data),
        )

    def _on_profile_api_result_polled(
        self, worker_idx: int, profile_url: str, data: object
    ) -> None:
        if not self.scan_running:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict) or state.get("phase") != "fetching_profile_api":
            return
        if not isinstance(data, dict) or not bool(data.get("done", False)):
            QTimer.singleShot(120, lambda: self._poll_profile_api_result(worker_idx, profile_url))
            return
        status = 0
        raw = ""
        ok = False
        try:
            status = int(data.get("status", 0))
        except (TypeError, ValueError):
            status = 0
        raw = str(data.get("text", ""))
        ok = bool(data.get("ok", False))
        if not ok or status != 200:
            excerpt = re.sub(r"\s+", " ", raw).strip()[:180]
            self._enqueue_scan_log(
                f"Worker {worker_idx + 1}: UserByScreenName fallo HTTP {status or 0}"
            )
            if excerpt:
                self._enqueue_scan_log(
                    f"Worker {worker_idx + 1}: detalle API: {excerpt}"
                )
            self._skip_worker_profile(worker_idx, profile_url, "UserByScreenName fallo")
            return
        try:
            payload = json.loads(raw)
            row = build_profile_row_from_api_json(profile_url, payload)
        except Exception as exc:
            error_reason = str(exc).strip()
            self._enqueue_scan_log(
                f"Worker {worker_idx + 1}: UserByScreenName invalido: {error_reason}"
            )
            self._skip_worker_profile(worker_idx, profile_url, f"UserByScreenName ({error_reason})")
            return

        expected_handle = profile_url.rstrip("/").rsplit("/", 1)[-1].lower()
        detected_handle = str(row.get("detected_handle", "")).strip().lower()
        if detected_handle and detected_handle != expected_handle:
            self._skip_worker_profile(worker_idx, profile_url, "handle API no coincide")
            return
        if not str(row.get("description", "")).strip():
            self._skip_worker_profile(worker_idx, profile_url, "descripcion no cargada")
            return
        candidate = state.get("candidate")
        if isinstance(candidate, dict):
            group_hint = str(candidate.get("group_hint", "")).strip()
            agency_hint = str(candidate.get("agency_hint", "")).strip()
            if group_hint:
                row["group_hint"] = group_hint
            if agency_hint:
                row["agency_hint"] = agency_hint
        row.pop("detected_handle", None)
        self._start_resolving_row_urls_with_worker(worker_idx, row)

    def _collect_worker_data(self, worker_idx: int, retries_left: int) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not state or not state.get("busy"):
            return
        view = self.scan_tab.worker_views[worker_idx]
        script = """
        (() => {
            const userNameNode = document.querySelector('[data-testid="UserName"]');
            const descNode = document.querySelector('[data-testid="UserDescription"]');
            const headerItemsNode = document.querySelector('[data-testid="UserProfileHeader_Items"]');
            const ready = Boolean(userNameNode && descNode);
            return {
                ready,
                pageUrl: location.href || '',
                hasUserName: Boolean(userNameNode),
                hasDescription: Boolean(descNode),
                hasHeaderItems: Boolean(headerItemsNode),
            };
        })()
        """
        view.page().runJavaScript(
            script,
            lambda data: self._on_worker_presence_ready(worker_idx, data, retries_left),
        )

    def _on_worker_presence_ready(self, worker_idx: int, data: object, retries_left: int) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not state:
            return
        candidate = state.get("candidate")
        if not isinstance(candidate, dict):
            return

        page_url = ""
        ready = False
        if isinstance(data, dict):
            page_url = str(data.get("pageUrl", "")).lower()
            ready = bool(data.get("ready", False))
        if "/i/flow/login" in page_url:
            ready = True
        if not ready and retries_left > 0:
            self.scan_tab.worker_views[worker_idx].page().runJavaScript(
                "window.scrollBy(0, Math.max(300, window.innerHeight * 0.5));"
            )
            QTimer.singleShot(
                550,
                lambda: self._collect_worker_data(worker_idx, retries_left - 1),
            )
            return
        self.scan_tab.worker_views[worker_idx].page().toHtml(
            lambda html: self._on_worker_html_ready(worker_idx, html, page_url, retries_left)
        )

    def _on_worker_html_ready(
        self, worker_idx: int, html: str, page_url: str, retries_left: int
    ) -> None:
        if not self.scan_running:
            return
        state = self.scan_worker_states[worker_idx]
        if not state:
            return
        candidate = state.get("candidate")
        if not isinstance(candidate, dict):
            return

        profile_url = str(candidate.get("url", ""))
        expected_handle = profile_url.rstrip("/").rsplit("/", 1)[-1].lower()
        host_ok = self._is_x_profile_host(page_url)
        handle_in_url = bool(page_url and f"/{expected_handle}" in page_url)
        if page_url and (not host_ok or ("/i/flow/login" not in page_url and not handle_in_url)):
            if retries_left > 0:
                self._enqueue_scan_log(
                    f"Worker {worker_idx + 1}: URL no confiable para {expected_handle}, reintentando..."
                )
                self.scan_tab.worker_views[worker_idx].setUrl(QUrl(profile_url))
                return
            self._skip_worker_profile(worker_idx, profile_url, "URL no confiable")
            return
        row = build_profile_row_from_html(
            profile_url=profile_url,
            profile_html=html or "",
        )
        title_text = str(row.get("title", "")).strip()
        description_text = str(row.get("description", "")).strip()
        if not description_text:
            if retries_left > 0:
                self._enqueue_scan_log(
                    f"Worker {worker_idx + 1}: descripcion no cargada, reintentando..."
                )
                self.scan_tab.worker_views[worker_idx].reload()
                QTimer.singleShot(
                    700,
                    lambda: self._collect_worker_data(worker_idx, retries_left - 1),
                )
                return
            self._skip_worker_profile(worker_idx, profile_url, "descripcion no cargada")
            return
        if not title_text:
            row["title"] = expected_handle
        detected_handle = str(row.get("detected_handle", "")).strip().lower()
        url_mismatch = (
            page_url
            and "/i/flow/login" not in page_url
            and f"/{expected_handle}" not in page_url
        )
        if (detected_handle and detected_handle != expected_handle or url_mismatch) and retries_left > 0:
            self._enqueue_scan_log(
                f"Worker {worker_idx + 1}: DOM/url desfasado, reintentando..."
            )
            self.scan_tab.worker_views[worker_idx].page().runJavaScript(
                "window.scrollBy(0, Math.max(250, window.innerHeight * 0.4));"
            )
            QTimer.singleShot(
                450,
                lambda: self._collect_worker_data(worker_idx, retries_left - 1),
            )
            return
        row.pop("detected_handle", None)
        self._start_resolving_row_urls_with_worker(worker_idx, row)
        return

    def _skip_worker_profile(self, worker_idx: int, profile_url: str, reason: str) -> None:
        if self.scan_tab is None:
            return
        self.scan_results.append(
            {
                "url": profile_url,
                "title": profile_url.rsplit("/", 1)[-1],
                "description": "",
                "urls": [],
            }
        )
        self.scan_worker_states[worker_idx] = {"busy": False, "candidate": None}
        self._enqueue_scan_log(
            f"[{len(self.scan_results)}/{self.scan_total_candidates}] Worker {worker_idx + 1} omitido: {profile_url} ({reason})"
        )
        self._schedule_profile_workers()

    def _finish_worker_row(self, worker_idx: int, row: dict[str, str | int | list[str]]) -> None:
        profile_url = str(row.get("url", "")).strip()
        self._classify_profile_row(row)
        self._enqueue_related_profiles(row)
        if not self._save_worker_row(row):
            return
        self.scan_results.append(row)
        self.scan_worker_states[worker_idx] = {"busy": False, "candidate": None}
        self._enqueue_scan_log(
            f"[{len(self.scan_results)}/{self.scan_total_candidates}] Worker {worker_idx + 1} finalizado: {profile_url}"
        )
        self._show_scan_dashboard(
            f"Escaneando... perfiles guardados: {self.scan_saved_count}"
        )
        self._schedule_profile_workers()

    def _finalize_scan_results(self) -> None:
        if not self.scan_running:
            return
        self._on_scan_finished(
            {
                "profiles_found": self.scan_saved_count,
                "db": self._scan_db_label(),
            }
        )

    def _on_scan_finished(self, result: dict) -> None:
        self.scan_running = False
        self.scan_url_resolve_cache = {}
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            report_html = str(result.get("report_html", "") or "")
            if report_html:
                self.scan_tab.web_view.setHtml(
                    report_html, QUrl("https://artbrowser.local/scan-compare")
                )
            else:
                self._show_scan_dashboard("Escaneo finalizado")
            self._enqueue_scan_log(
                f"Escaneo finalizado. Perfiles: {result.get('profiles_found', 0)}"
            )
            self._enqueue_scan_log(f"MySQL: {result.get('db', '')}")
        self.scan_superficial_state = None
        self.statusBar().showMessage("Escaneo finalizado", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _on_scan_failed(self, error: str) -> None:
        self.scan_running = False
        self.scan_url_resolve_cache = {}
        try:
            if self.scan_tab is not None:
                self.scan_tab.web_view.loadFinished.disconnect(self._on_superficial_load_finished)
        except Exception:
            pass
        self.scan_superficial_state = None
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self._show_scan_dashboard(f"Escaneo fallido: {error}")
            self._enqueue_scan_log(f"ERROR: {error}")
        self.statusBar().showMessage("Escaneo fallido", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _enqueue_scan_log(self, message: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
        try:
            with SCAN_LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if self.scan_tab is not None:
            self.scan_tab.log_output.append(message)

    def _show_scan_dashboard(self, status_text: str) -> None:
        if self.scan_tab is None:
            return
        try:
            data = get_scan_dashboard_data(self._scan_db_config())
            html = self._build_scan_dashboard_html(status_text, data)
        except Exception as exc:
            html = self._build_scan_dashboard_html(
                status_text,
                {
                    "total_profiles": 0,
                    "categories": [],
                    "rows": [],
                    "error": str(exc),
                },
            )
        self.scan_tab.web_view.setHtml(html, QUrl("https://artbrowser.local/scan-dashboard"))

    def _build_scan_dashboard_html(self, status_text: str, data: dict[str, object]) -> str:
        rows = data.get("rows", [])
        categories = data.get("categories", [])
        total = int(data.get("total_profiles", 0) or 0)
        error = str(data.get("error", "") or "")

        category_html = ""
        if isinstance(categories, list):
            for item in categories:
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                category_html += (
                    "<span class='pill'>"
                    f"{escape(str(item[0]))}: {escape(str(item[1]))}"
                    "</span>"
                )

        row_html = ""
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                raw_urls = item.get("urls", [])
                urls = raw_urls if isinstance(raw_urls, list) else []
                raw_categories = item.get("artist_categories", [])
                categories = raw_categories if isinstance(raw_categories, list) else []
                categories_text = ", ".join(str(value) for value in categories if str(value).strip())
                if not categories_text:
                    categories_text = str(item.get("artist_category", "general"))
                urls_html = " ".join(
                    f"<a href='{escape(str(url), quote=True)}'>{escape(str(url))}</a>"
                    for url in urls[:4]
                )
                profile_url = str(item.get("profile_url", ""))
                row_html += (
                    "<tr>"
                    f"<td><a href='{escape(profile_url, quote=True)}'>{escape(profile_url)}</a></td>"
                    f"<td>{escape(str(item.get('title', '')))}</td>"
                    f"<td>{escape(categories_text)}</td>"
                    f"<td>{escape(str(item.get('content_rating', 'unknown')))}</td>"
                    f"<td>{escape(str(item.get('description', '')))}</td>"
                    f"<td>{urls_html}</td>"
                    f"<td>{escape(str(item.get('generated_at', '')))}</td>"
                    "</tr>"
                )

        error_html = f"<p class='error'>{escape(error)}</p>" if error else ""
        if not row_html:
            row_html = "<tr><td colspan='7'>Sin perfiles guardados todavia.</td></tr>"
        return f"""
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; color: #1f2933; }}
                h1 {{ font-size: 22px; margin: 0 0 8px; }}
                .meta {{ margin-bottom: 14px; color: #52606d; }}
                .pill {{ display: inline-block; padding: 4px 8px; margin: 0 6px 8px 0; background: #e4e7eb; border-radius: 4px; }}
                .error {{ color: #b42318; }}
                table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
                th, td {{ border-bottom: 1px solid #d9e2ec; padding: 8px; vertical-align: top; text-align: left; }}
                th {{ background: #f5f7fa; position: sticky; top: 0; }}
                a {{ color: #0967d2; word-break: break-all; }}
                td:nth-child(5) {{ max-width: 420px; }}
            </style>
        </head>
        <body>
            <h1>ArtBrowser Scan SQL</h1>
            <div class="meta">{escape(status_text)} | perfiles en tabla: {total}</div>
            {error_html}
            <div>{category_html}</div>
            <table>
                <thead>
                    <tr>
                        <th>Perfil</th>
                        <th>Titulo</th>
                        <th>Categoria</th>
                        <th>Rating</th>
                        <th>Descripcion</th>
                        <th>URLs</th>
                        <th>Fecha</th>
                    </tr>
                </thead>
                <tbody>{row_html}</tbody>
            </table>
        </body>
        </html>
        """

    def _build_compare_report_html(self, data: dict[str, object]) -> str:
        sources = data.get("sources", {})
        duplicates = data.get("duplicates", [])
        unique_by_source = data.get("unique_by_source", {})
        total_duplicates = int(data.get("total_duplicates", 0) or 0)

        source_cards = ""
        if isinstance(sources, dict):
            for source_name, handles in sources.items():
                if not isinstance(handles, list):
                    continue
                source_cards += (
                    "<div class='card'>"
                    f"<h3>{escape(str(source_name))}</h3>"
                    f"<p>{len(handles)} perfiles</p>"
                    "</div>"
                )

        duplicate_rows = ""
        if isinstance(duplicates, list):
            for item in duplicates:
                if not isinstance(item, dict):
                    continue
                sources_text = ", ".join(str(value) for value in item.get("sources", []) if str(value).strip())
                duplicate_rows += (
                    "<tr>"
                    f"<td>{escape(str(item.get('handle', '')))}</td>"
                    f"<td>{escape(sources_text)}</td>"
                    "</tr>"
                )
        if not duplicate_rows:
            duplicate_rows = "<tr><td colspan='2'>Sin repetidos detectados.</td></tr>"

        unique_sections = ""
        if isinstance(unique_by_source, dict):
            for source_name, handles in unique_by_source.items():
                if not isinstance(handles, list):
                    continue
                chips = " ".join(
                    f"<span class='pill'>{escape(str(handle))}</span>" for handle in handles[:80]
                )
                if not chips:
                    chips = "<span class='muted'>Sin unicos</span>"
                unique_sections += (
                    "<div class='card'>"
                    f"<h3>{escape(str(source_name))}</h3>"
                    f"<div>{chips}</div>"
                    "</div>"
                )

        if not unique_sections:
            unique_sections = "<p class='muted'>Sin datos unicos para mostrar.</p>"

        return f"""
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; color: #1f2933; }}
                h1 {{ font-size: 22px; margin: 0 0 8px; }}
                .meta {{ margin-bottom: 16px; color: #52606d; }}
                .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 18px; }}
                .card {{ background: #f5f7fa; border: 1px solid #d9e2ec; border-radius: 8px; padding: 12px; }}
                .pill {{ display: inline-block; padding: 4px 8px; margin: 0 6px 8px 0; background: #e4e7eb; border-radius: 4px; }}
                .muted {{ color: #66788a; }}
                table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }}
                th, td {{ border-bottom: 1px solid #d9e2ec; padding: 8px; vertical-align: top; text-align: left; }}
                th {{ background: #f5f7fa; position: sticky; top: 0; }}
            </style>
        </head>
        <body>
            <h1>Comparación de listas</h1>
            <div class="meta">Repetidos: {total_duplicates}</div>
            <div class="grid">{source_cards}</div>
            <h2>Repetidos</h2>
            <table>
                <thead>
                    <tr>
                        <th>Handle</th>
                        <th>Fuentes</th>
                    </tr>
                </thead>
                <tbody>{duplicate_rows}</tbody>
            </table>
            <h2>Unicos por fuente</h2>
            {unique_sections}
        </body>
        </html>
        """

    def _is_x_profile_host(self, page_url: str) -> bool:
        if not page_url:
            return False
        qurl = QUrl(page_url)
        host = qurl.host().strip().lower()
        return host in {"x.com", "www.x.com", "mobile.x.com"}

    def _is_web_url(self, raw_url: str) -> bool:
        qurl = QUrl(raw_url.strip())
        return qurl.scheme().strip().lower() in {"http", "https"}

    def _sanitize_web_url(self, raw_url: str) -> str:
        value = str(raw_url).strip()
        value = re.sub(r"^(https?://)\s+", r"\1", value, flags=re.IGNORECASE)
        return value

    def _is_tco_url(self, raw_url: str) -> bool:
        qurl = QUrl(raw_url.strip())
        host = qurl.host().strip().lower()
        return host == "t.co"

    def _host_matches(self, raw_url: str, hosts: set[str]) -> bool:
        qurl = QUrl(str(raw_url).strip())
        host = qurl.host().strip().lower()
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in hosts)

    def _can_expand_resolved_page_links(self, raw_url: str) -> bool:
        return self._host_matches(raw_url, self.LINK_EXPANSION_HOSTS)

    def _should_skip_qwebengine_resolution(self, raw_url: str) -> bool:
        return self._host_matches(raw_url, self.LINK_NO_OPEN_HOSTS)

    def _should_open_url_in_worker(self, raw_url: str) -> bool:
        return (
            self._is_tco_url(raw_url)
            or self._can_expand_resolved_page_links(raw_url)
            or self._host_matches(raw_url, self.LINK_ABOUT_HOSTS)
            or self._host_matches(raw_url, self.LINK_CONTENT_HOSTS)
        )

    def _normalize_profile_link_url(self, raw_url: str) -> str:
        value = self._sanitize_web_url(raw_url)
        if not value:
            return ""
        if self._host_matches(value, self.LINK_ABOUT_HOSTS):
            qurl = QUrl(value)
            path = qurl.path().rstrip("/")
            if path.endswith("/about"):
                qurl.setPath(path[:-6] or "/")
                value = qurl.toString()
        return value

    def _url_to_open_in_worker(self, raw_url: str) -> str:
        value = self._sanitize_web_url(raw_url)
        if not value or not self._host_matches(value, self.LINK_ABOUT_HOSTS):
            return value
        qurl = QUrl(value)
        path = qurl.path().rstrip("/")
        if not path.endswith("/about"):
            qurl.setPath(f"{path}/about" if path else "/about")
        return qurl.toString()

    def _is_ignored_linktree_url(self, raw_url: str, source_url: str = "") -> bool:
        value = str(raw_url).strip().lower()
        if not value:
            return False
        if self._host_matches(source_url, {"linktr.ee"}):
            return "linktree" in value or "linktr.ee" in value
        qurl = QUrl(str(raw_url).strip())
        return self._host_matches(qurl.toString(), {"linktr.ee"}) and "utm_" in qurl.query().lower()

    def _is_ignored_profile_link_url(self, raw_url: str) -> bool:
        return is_ignored_profile_url(raw_url)

    def _classify_profile_row(self, row: dict[str, str | int | list[str]]) -> None:
        title = str(row.get("title", ""))
        description = str(row.get("description", ""))
        profile_url = str(row.get("url", ""))
        profile_handle = profile_url.rstrip("/").rsplit("/", 1)[-1]
        urls = row.get("urls", [])
        url_blob = " ".join(str(item) for item in urls) if isinstance(urls, list) else ""
        haystack = f"{title} {description} {url_blob}".lower()
        categories: list[str] = []
        vtuber_markers = (
            "vtuber" in haystack
            or "ママ" in description
            or "パパ" in description
            or "VT" in title
            or "VT" in profile_handle
            or profile_handle.lower().endswith("_vt")
        )
        if vtuber_markers:
            categories.append("vtuber")
        if any(token in haystack for token in ("twitch.tv", "kick.com", "youtube.com", "youtu.be")):
            categories.append("stream")
        if any(token in haystack for token in ("illustrator", "draw", "dibujo", "pixiv", "fanbox.cc")):
            categories.append("illustrator")
        if not categories:
            categories = ["general"]
        row["artist_categories"] = categories
        row["artist_category"] = categories[0]
        metadata = extract_vtuber_metadata(description)
        group_hint = str(row.get("group_hint", "")).strip()
        agency_hint = str(row.get("agency_hint", "")).strip()
        if group_hint and not metadata.get("grupo"):
            metadata["grupo"] = group_hint
        if not metadata.get("grupo") and "katabasis" in f"{title} {description} {profile_handle}".lower():
            metadata["grupo"] = "Katabasis"
        if agency_hint and not metadata.get("agencia"):
            metadata["agencia"] = agency_hint
        row["vtuber_metadata"] = metadata
        if re.search(r"(^|[^a-z0-9])r18([^a-z0-9]|$)", title, re.IGNORECASE) or "+18" in title or "🔞" in title:
            row["content_rating"] = "nsfw"
        else:
            row["content_rating"] = "unknown"

    def _scan_db_config(self) -> dict[str, object]:
        return {
            "host": str(BrowserWindow.app_settings.get("mysql_host", "127.0.0.1")).strip() or "127.0.0.1",
            "port": self._scan_setting_int("mysql_port", 3306, minimum=1),
            "database": str(BrowserWindow.app_settings.get("mysql_database", "artbrowser")).strip() or "artbrowser",
            "user": str(BrowserWindow.app_settings.get("mysql_user", "root")).strip() or "root",
            "password": str(BrowserWindow.app_settings.get("mysql_password", "")),
        }

    def _scan_db_label(self) -> str:
        config = self._scan_db_config()
        return f"{config['host']}:{config['port']}/{config['database']}.{SCAN_DB_TABLE}"

    def _save_worker_row(self, row: dict[str, str | int | list[str]]) -> bool:
        try:
            saved = save_scan_results(
                db_config=self._scan_db_config(),
                rows=[row],
                ensure_db=False,
            )
            self.scan_saved_count += saved
            return True
        except Exception as exc:
            self._on_scan_failed(f"Error guardando resultado de worker: {exc}")
            return False

    def _start_resolving_row_urls_with_worker(
        self, worker_idx: int, row: dict[str, str | int | list[str]]
    ) -> None:
        raw_urls = row.get("urls", [])
        urls: list[str] = []
        seen: set[str] = set()
        if isinstance(raw_urls, list):
            for item in raw_urls:
                url = self._normalize_profile_link_url(str(item).strip())
                if not url or url in seen:
                    continue
                if self._is_ignored_profile_link_url(url):
                    continue
                if self._is_ignored_linktree_url(url):
                    continue
                seen.add(url)
                urls.append(url)
        if not urls:
            row["urls"] = []
            self._finish_worker_row(worker_idx, row)
            return

        current_profile = ""
        state = self.scan_worker_states[worker_idx]
        if isinstance(state, dict):
            candidate = state.get("candidate")
            if isinstance(candidate, dict):
                current_profile = str(candidate.get("url", "")).strip()

            state["phase"] = "resolving_links"
            state["resolving_row"] = row
            state["resolve_pending"] = list(urls)
            state["resolve_urls"] = []
            state["resolve_donation_sites"] = []
            state["resolve_seen"] = set()
            state["resolve_expansion_count"] = 0
            state["resolve_max_expansion"] = 40
            state["resolve_current_profile"] = current_profile
        self._resolve_next_worker_url(worker_idx)

    def _resolve_next_worker_url(self, worker_idx: int) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict):
            return
        pending = state.get("resolve_pending")
        resolved_urls = state.get("resolve_urls")
        resolved_seen = state.get("resolve_seen")
        donation_sites = state.get("resolve_donation_sites")
        row = state.get("resolving_row")
        if not isinstance(pending, list) or not isinstance(resolved_urls, list):
            return
        if not isinstance(resolved_seen, set) or not isinstance(donation_sites, list) or not isinstance(row, dict):
            return

        expansion_count = int(state.get("resolve_expansion_count", 0))
        max_expansion = int(state.get("resolve_max_expansion", 40))
        while pending and expansion_count < max_expansion:
            original = self._normalize_profile_link_url(str(pending.pop(0)).strip())
            if not original:
                continue
            if self._is_ignored_profile_link_url(original):
                continue
            if self._is_ignored_linktree_url(original):
                continue
            if original not in resolved_seen and not self._is_tco_url(original):
                resolved_seen.add(original)
                resolved_urls.append(original)
            if not self._is_web_url(original):
                continue
            if self._should_skip_qwebengine_resolution(original):
                continue
            if not self._should_open_url_in_worker(original):
                continue
            if original in self.scan_opened_link_urls:
                continue
            self.scan_opened_link_urls.add(original)
            current_profile = str(state.get("resolve_current_profile", "")).strip()
            self._enqueue_scan_log(
                f"Worker {worker_idx + 1} [{current_profile}]: resolviendo enlace {original}"
            )
            self._open_worker_url_for_resolution(worker_idx, original)
            return

        row["urls"] = resolved_urls
        row["donation_sites"] = donation_sites
        for key in (
            "resolving_row",
            "resolve_pending",
            "resolve_urls",
            "resolve_donation_sites",
            "resolve_seen",
            "resolve_expansion_count",
            "resolve_max_expansion",
            "resolve_current_profile",
            "resolve_original",
            "resolve_open_url",
            "resolve_final_url",
            "resolve_loaded_ok",
            "resolve_deadline",
            "resolve_last_url",
            "resolve_last_logged_url",
            "resolve_stable_ms",
            "resolve_about_redirected",
        ):
            state.pop(key, None)
        state["phase"] = "loading_profile"
        self._finish_worker_row(worker_idx, row)

    def _open_worker_url_for_resolution(self, worker_idx: int, target_url: str) -> None:
        if self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict):
            return
        final_url = self._normalize_profile_link_url(target_url)
        open_url = self._url_to_open_in_worker(final_url)
        state["resolve_original"] = final_url
        state["resolve_open_url"] = open_url
        state["resolve_final_url"] = final_url
        state["resolve_loaded_ok"] = False
        state["resolve_deadline"] = time.monotonic() + (self.LINK_LOAD_WATCHDOG_MS / 1000.0)
        state["resolve_last_url"] = ""
        state["resolve_last_logged_url"] = ""
        state["resolve_stable_ms"] = 0
        view = self.scan_tab.worker_views[worker_idx]
        view.stop()
        view.setUrl(QUrl("about:blank"))
        # self._enqueue_scan_log(f"Worker {worker_idx + 1}: abriendo en UI -> {final_url}")
        QTimer.singleShot(50, lambda: self._load_worker_resolution_url(worker_idx, open_url))

    def _load_worker_resolution_url(self, worker_idx: int, target_url: str) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict) or state.get("phase") != "resolving_links":
            return
        self.scan_tab.worker_views[worker_idx].setUrl(QUrl(target_url))
        QTimer.singleShot(150, lambda: self._poll_worker_resolution_url(worker_idx))

    def _poll_worker_resolution_url(self, worker_idx: int) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict) or state.get("phase") != "resolving_links":
            return
        view = self.scan_tab.worker_views[worker_idx]
        current_url = view.url().toString().strip()
        last_logged_url = str(state.get("resolve_last_logged_url", ""))
        if current_url and current_url != last_logged_url:
            state["resolve_last_logged_url"] = current_url
            # self._enqueue_scan_log(f"Worker {worker_idx + 1}: navegando -> {current_url}")

        stable_ms = int(state.get("resolve_stable_ms", 0))
        last_url = str(state.get("resolve_last_url", ""))
        if current_url == last_url:
            stable_ms += 150
        else:
            stable_ms = 0
            state["resolve_last_url"] = current_url
        state["resolve_stable_ms"] = stable_ms

        if not view.page().isLoading() and current_url:
            if self._host_matches(current_url, self.LINK_ABOUT_HOSTS):
                about_url = self._url_to_open_in_worker(current_url)
                if about_url != current_url and not bool(state.get("resolve_about_redirected")):
                    state["resolve_about_redirected"] = True
                    view.setUrl(QUrl(about_url))
                    QTimer.singleShot(150, lambda: self._poll_worker_resolution_url(worker_idx))
                    return
            state["resolve_loaded_ok"] = True
            state["resolve_final_url"] = current_url
            original = str(state.get("resolve_original", "")).strip()
            if self._is_tco_url(original) and not self._is_tco_url(current_url):
                if (
                    not self._can_expand_resolved_page_links(current_url)
                    and not self._host_matches(current_url, self.LINK_ABOUT_HOSTS)
                    and not self._host_matches(current_url, self.LINK_CONTENT_HOSTS)
                ):
                    view.stop()
                    QTimer.singleShot(
                        0,
                        lambda: self._on_resolved_worker_html_ready(worker_idx, ""),
                    )
                    return
            if stable_ms >= self.LINK_STABILIZE_MS:
                QTimer.singleShot(
                    self.LINK_VISIBLE_DWELL_MS,
                    lambda: self._collect_resolved_worker_html(worker_idx),
                )
                return

        if time.monotonic() >= float(state.get("resolve_deadline", 0.0)):
            QTimer.singleShot(
                self.LINK_VISIBLE_DWELL_MS,
                lambda: self._collect_resolved_worker_html(worker_idx),
            )
            return
        QTimer.singleShot(150, lambda: self._poll_worker_resolution_url(worker_idx))

    def _collect_resolved_worker_html(self, worker_idx: int) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict) or state.get("phase") != "resolving_links":
            return
        page = self.scan_tab.worker_views[worker_idx].page()
        page.toHtml(lambda html: self._on_resolved_worker_html_ready(worker_idx, html or ""))

    def _on_resolved_worker_html_ready(self, worker_idx: int, html: str) -> None:
        if not self.scan_running:
            return
        state = self.scan_worker_states[worker_idx]
        if not isinstance(state, dict) or state.get("phase") != "resolving_links":
            return
        extracted = extract_urls_from_html_document(html)
        original = str(state.get("resolve_original", "")).strip()
        final_url = self._normalize_profile_link_url(str(state.get("resolve_final_url", "")).strip() or original)

        if self._is_tco_url(final_url):
            for candidate in extracted:
                candidate_url = self._normalize_profile_link_url(str(candidate).strip())
                if not candidate_url or not self._is_web_url(candidate_url):
                    continue
                if self._is_ignored_profile_link_url(candidate_url):
                    continue
                if self._is_tco_url(candidate_url):
                    continue
                if self._is_ignored_linktree_url(candidate_url):
                    continue
                final_url = candidate_url
                self._enqueue_scan_log(
                    f"Worker {worker_idx + 1}: destino inferido desde HTML t.co -> {final_url}"
                )
                break

        if original and not (self._is_tco_url(original) and self._is_tco_url(final_url)):
            self.scan_url_resolve_cache[original] = final_url
        if final_url != original:
            self._enqueue_scan_log(f"Worker {worker_idx + 1}: enlace resuelto -> {final_url}")
        if final_url:
            self.scan_opened_link_urls.add(final_url)

        resolved_urls = state.get("resolve_urls")
        resolved_seen = state.get("resolve_seen")
        donation_sites = state.get("resolve_donation_sites")
        pending = state.get("resolve_pending")
        can_expand_links = self._can_expand_resolved_page_links(final_url)
        if isinstance(resolved_urls, list) and isinstance(resolved_seen, set):
            if (
                final_url
                and not self._is_tco_url(final_url)
                and not self._is_ignored_profile_link_url(final_url)
                and not self._is_ignored_linktree_url(final_url)
                and final_url not in resolved_seen
            ):
                resolved_seen.add(final_url)
                resolved_urls.append(final_url)
        if isinstance(donation_sites, list):
            content = extract_link_page_content(final_url, html)
            if content:
                donation_sites.append({"url": final_url, "content": content})
        if bool(state.get("resolve_loaded_ok")) and can_expand_links and isinstance(pending, list):
            expansion_count = int(state.get("resolve_expansion_count", 0))
            max_expansion = int(state.get("resolve_max_expansion", 40))
            for item in extracted:
                url_item = self._normalize_profile_link_url(str(item).strip())
                if not url_item or not isinstance(resolved_seen, set) or url_item in resolved_seen:
                    continue
                if self._is_ignored_profile_link_url(url_item):
                    continue
                if self._is_tco_url(url_item) or self._is_ignored_linktree_url(url_item, final_url):
                    continue
                pending.append(url_item)
                expansion_count += 1
                if expansion_count >= max_expansion:
                    break
            state["resolve_expansion_count"] = expansion_count
        elif bool(state.get("resolve_loaded_ok")) and extracted:
            # self._enqueue_scan_log(
            #     f"Worker {worker_idx + 1}: links internos ignorados en dominio no permitido"
            # )
            pass

        QTimer.singleShot(0, lambda: self._resolve_next_worker_url(worker_idx))

    def _enqueue_related_profiles(self, row: dict[str, str | int | list[str]]) -> None:
        if not self.scan_running:
            return
        if len(self.scan_results) + len(self.scan_pending_candidates) >= self.scan_max_profiles:
            return

        candidates: list[str] = []
        description = str(row.get("description", "")).strip()
        metadata = row.get("vtuber_metadata", {})
        group_hint = ""
        agency_hint = ""
        if isinstance(metadata, dict):
            group_hint = str(metadata.get("grupo", "") or "").strip()
            agency_hint = str(metadata.get("agencia", "") or "").strip()
        for handle in extract_handles_from_description(description):
            if self._is_blocked_related_handle(handle):
                continue
            candidates.append(f"https://x.com/{handle}")

        urls = row.get("urls", [])
        if isinstance(urls, list):
            for item in urls:
                raw = str(item).strip()
                if not raw:
                    continue
                qurl = QUrl(raw)
                host = qurl.host().strip().lower()
                path = qurl.path().strip("/")
                if host in {"x.com", "www.x.com", "mobile.x.com"} and re.fullmatch(
                    r"[A-Za-z0-9_]{1,15}", path
                ):
                    if self._is_blocked_related_handle(path):
                        continue
                    candidates.append(f"https://x.com/{path}")

        added = 0
        for raw in candidates:
            lower = raw.strip().lower()
            if not lower or lower in self.scan_known_profile_urls:
                continue
            if len(self.scan_results) + len(self.scan_pending_candidates) >= self.scan_max_profiles:
                break
            self.scan_known_profile_urls.add(lower)
            candidate = {"url": raw}
            if group_hint:
                candidate["group_hint"] = group_hint
            if agency_hint:
                candidate["agency_hint"] = agency_hint
            self.scan_pending_candidates.append(candidate)
            self.scan_total_candidates += 1
            added += 1
        if added > 0:
            self._enqueue_scan_log(f"Perfiles relacionados encolados: +{added}")

    def _is_blocked_related_handle(self, handle: str) -> bool:
        value = str(handle).strip().lower()
        if not value:
            return True
        return value in self.RELATED_PROFILE_BLOCKLIST

    def _save_current_page(self) -> None:
        view = self.current_view()
        if view is None:
            self.statusBar().showMessage("No hay una pagina web activa para guardar", 4000)
            return
        target_dir = BASE_DIR / "saved_pages"
        target_dir.mkdir(parents=True, exist_ok=True)
        host = view.url().host() or "page"
        safe_host = re.sub(r"[^a-zA-Z0-9_-]+", "_", host).strip("_") or "page"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_file = target_dir / f"{safe_host}_{timestamp}.html"

        def _write_html(html: str) -> None:
            target_file.write_text(html, encoding="utf-8")
            self.statusBar().showMessage(f"Pagina guardada en {target_file.name}", 5000)

        view.page().toHtml(_write_html)

    def closeEvent(self, event):
        self.scan_running = False
        remaining = [window for window in BrowserWindow.open_windows if window is not self]
        save_session(remaining if remaining else [self])
        BrowserWindow.open_windows.remove(self)
        super().closeEvent(event)
