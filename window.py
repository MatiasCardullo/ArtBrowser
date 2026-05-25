"""Main window implementation."""

import re
import time
from datetime import datetime
from html import escape

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
    ensure_scan_db,
    extract_handles_from_description,
    extract_link_page_content,
    extract_profile_candidates,
    extract_urls_from_html_document,
    get_scan_dashboard_data,
    get_success_profile_urls,
    is_ignored_profile_url,
    save_scan_results,
)
from widgets import ScanTab, SettingsTab, WebEngineView


class BrowserWindow(QMainWindow):
    open_windows = []
    profile: QWebEngineProfile | None = None
    app_settings: dict[str, str | bool | int | float] = {}
    LINK_VISIBLE_DWELL_MS = 8000
    LINK_STABILIZE_MS = 1200
    LINK_LOAD_WATCHDOG_MS = 45000
    LINK_EXPANSION_HOSTS = {"carrd.co", "crd.co", "linktr.ee", "potofu.me"}
    LINK_NO_OPEN_HOSTS = {"discord.gg"}
    LINK_ABOUT_HOSTS = {"kick.com", "patreon.com", "twitch.tv"}
    LINK_CONTENT_HOSTS = {"buymeacoffee.com", "ko-fi.com"}

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

        BrowserWindow.open_windows.append(self)
        self._create_toolbar()
        self._create_tabs()

        urls = initial_urls or [DEFAULT_URL]
        for url in urls:
            self.add_tab(url)

    def _create_web_view(self) -> WebEngineView:
        return WebEngineView(self.add_tab, BrowserWindow.profile)

    def _create_toolbar(self) -> None:
        toolbar = QToolBar("Barra de navegación")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        back_action = QAction("Atrás", self)
        back_action.triggered.connect(self._go_back)
        toolbar.addAction(back_action)

        forward_action = QAction("Adelante", self)
        forward_action.triggered.connect(self._go_forward)
        toolbar.addAction(forward_action)

        reload_action = QAction("Recargar", self)
        reload_action.triggered.connect(self._reload_current)
        toolbar.addAction(reload_action)

        toolbar.addSeparator()
        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("https://www.example.com")
        self.url_bar.returnPressed.connect(self._load_url_from_bar)
        toolbar.addWidget(self.url_bar)

        new_tab_action = QAction("Nueva pestaña", self)
        new_tab_action.triggered.connect(lambda: self.add_tab(DEFAULT_URL))
        toolbar.addAction(new_tab_action)

        new_window_action = QAction("Nueva ventana", self)
        new_window_action.triggered.connect(self._open_new_window)
        toolbar.addAction(new_window_action)

        settings_action = QAction("Configuracion", self)
        settings_action.triggered.connect(self.open_settings_tab)
        toolbar.addAction(settings_action)

        save_page_action = QAction("Guardar pagina", self)
        save_page_action.triggered.connect(self._save_current_page)
        toolbar.addAction(save_page_action)

    def _create_tabs(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        container = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        container.setLayout(layout)
        self.setCentralWidget(container)

    def _on_tab_changed(self, index: int) -> None:
        _ = index
        view = self.current_view()
        self.url_bar.setText(view.url().toString() if view else "artbrowser://settings")

    def _on_tab_close_requested(self, index: int) -> None:
        if self.tabs.count() == 1:
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
        index = self.tabs.addTab(view, "Nueva pestaña")
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
            "mysql_host",
            "mysql_port",
            "mysql_database",
            "mysql_user",
            "mysql_password",
        ):
            if key in values:
                BrowserWindow.app_settings[key] = values[key]

    def _start_scan_from_settings(self, values: dict[str, str | bool | int | float]) -> None:
        if self.scan_settings_tab is None:
            return
        self._apply_scan_settings(values)
        save_settings(BrowserWindow.app_settings)
        self.start_following_scan(self.scan_settings_tab)

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
        try:
            ensure_scan_db(self._scan_db_config())
        except Exception as exc:
            self._on_scan_failed(f"Error inicializando MySQL: {exc}")
            return
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
                        f"Saltados por cache MySQL con descripcion y URLs: {skipped}"
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
            if not ok:
                self._enqueue_scan_log(
                    "No se pudo cargar la pestaña, usando HTML vacio para detectar perfiles"
                )
                collect_candidates_round(1)
                return
            self._enqueue_scan_log("Pagina cargada, esperando lista de perfiles en el DOM")

            def wait_for_user_cells(retries_left: int) -> None:
                script = """
                (() => {
                    const nodes = Array.from(document.querySelectorAll('[aria-label]'));
                    const timeline = nodes.find((node) => {
                        const aria = node.getAttribute('aria-label') || '';
                        return (aria.includes('Cronología') || aria.includes('Timeline'))
                            && (aria.includes('Siguiendo') || aria.includes('Following'));
                    });
                    if (!timeline) return 0;
                    return timeline.querySelectorAll('[data-testid="UserCell"]').length;
                })()
                """

                def on_count(result: object) -> None:
                    try:
                        count = int(result)
                    except (TypeError, ValueError):
                        count = 0
                    if not self.scan_running:
                        return
                    if count > 0:
                        self._enqueue_scan_log(
                            f"Perfiles visibles detectados: {count}. Iniciando recoleccion incremental"
                        )
                        collect_candidates_round(1)
                        return
                    if retries_left > 0:
                        web_view.page().runJavaScript(
                            "window.scrollBy(0, Math.max(400, window.innerHeight * 0.6));"
                        )
                        QTimer.singleShot(900, lambda: wait_for_user_cells(retries_left - 1))
                        return
                    self._enqueue_scan_log("No aparecieron UserCell a tiempo, iniciando con HTML actual")
                    collect_candidates_round(1)

                web_view.page().runJavaScript(script, on_count)

            wait_for_user_cells(8)

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
                "phase": "loading_profile",
            }
            url = str(candidate.get("url", ""))
            worker_view.setUrl(QUrl(url))
            self._enqueue_scan_log(
                f"[{len(self.scan_results) + 1}/{self.scan_total_candidates}] Worker {idx + 1}: {url}"
            )

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
            self._show_scan_dashboard("Escaneo finalizado")
            self._enqueue_scan_log(
                f"Escaneo finalizado. Perfiles: {result.get('profiles_found', 0)}"
            )
            self._enqueue_scan_log(f"MySQL: {result.get('db', '')}")
        self.statusBar().showMessage("Escaneo finalizado", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _on_scan_failed(self, error: str) -> None:
        self.scan_running = False
        self.scan_url_resolve_cache = {}
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
                urls_html = " ".join(
                    f"<a href='{escape(str(url), quote=True)}'>{escape(str(url))}</a>"
                    for url in urls[:4]
                )
                profile_url = str(item.get("profile_url", ""))
                row_html += (
                    "<tr>"
                    f"<td><a href='{escape(profile_url, quote=True)}'>{escape(profile_url)}</a></td>"
                    f"<td>{escape(str(item.get('title', '')))}</td>"
                    f"<td>{escape(str(item.get('artist_category', 'general')))}</td>"
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
        urls = row.get("urls", [])
        url_blob = " ".join(str(item) for item in urls) if isinstance(urls, list) else ""
        haystack = f"{title} {description} {url_blob}".lower()
        description_lower = description.lower()
        category = ""
        if "vtuber" in description_lower or "ママ" in description or "パパ" in description:
            category = "vtuber"
        elif any(token in haystack for token in ("twitch.tv", "kick.com")):
            category = "stream"
        elif any(token in haystack for token in ("illustrator", "draw", "dibujo", "pixiv", "fanbox.cc")):
            category = "illustrator"
        row["artist_category"] = category or "general"
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
        for handle in extract_handles_from_description(description):
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
                    candidates.append(f"https://x.com/{path}")

        added = 0
        for raw in candidates:
            lower = raw.strip().lower()
            if not lower or lower in self.scan_known_profile_urls:
                continue
            if len(self.scan_results) + len(self.scan_pending_candidates) >= self.scan_max_profiles:
                break
            self.scan_known_profile_urls.add(lower)
            self.scan_pending_candidates.append({"url": raw})
            self.scan_total_candidates += 1
            added += 1
        if added > 0:
            self._enqueue_scan_log(f"Perfiles relacionados encolados: +{added}")

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
