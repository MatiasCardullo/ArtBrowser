"""Main window implementation."""

import re
from datetime import datetime

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QAction
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtWidgets import (
    QLineEdit, QMainWindow, QTabWidget, QToolBar, QVBoxLayout, QWidget
)

from config import (
    BASE_DIR, DEFAULT_URL, SCAN_DB_FILE,
    SCAN_FOLLOWING_MAX_IDLE_ROUNDS,
    SCAN_FOLLOWING_MAX_PROFILES,
    SCAN_FOLLOWING_MAX_SCROLL_ROUNDS,
    SCAN_RESOLVE_TCO,
    SCAN_URL_RESOLVE_TIMEOUT_S,
    save_session, save_settings
)
from scanner import (
    build_profile_row_from_html,
    extract_profile_candidates,
    save_scan_results,
)
from widgets import ScanTab, SettingsTab, WebEngineView


class BrowserWindow(QMainWindow):
    open_windows = []
    profile: QWebEngineProfile | None = None
    app_settings: dict[str, str | bool | int | float] = {}

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
        self.scan_total_candidates = 0
        self.scan_worker_states: list[dict[str, object] | None] = [None, None, None, None]
        self.scan_url_resolve_cache: dict[str, str] = {}

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
            "scan_following_max_scroll_rounds",
            "scan_following_max_profiles",
            "scan_following_max_idle_rounds",
            "scan_resolve_tco",
            "scan_url_resolve_timeout_s",
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
        self.scan_running = True
        self.scan_following_url = scan_url
        self.scan_pending_candidates = []
        self.scan_results = []
        self.scan_total_candidates = 0
        self.scan_worker_states = [None, None, None, None]
        self.scan_url_resolve_cache = {}
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
        max_idle_rounds = self._scan_setting_int(
            "scan_following_max_idle_rounds", SCAN_FOLLOWING_MAX_IDLE_ROUNDS, minimum=1
        )
        self._enqueue_scan_log(
            "Recolector followings: "
            f"max_rounds={max_scroll_rounds}, max_profiles={max_profiles}, max_idle={max_idle_rounds}"
        )

        collected_candidates: list[dict[str, str]] = []
        collected_urls: set[str] = set()
        idle_rounds = 0

        def finalize_candidate_collection(reason: str) -> None:
            if not self.scan_running:
                return
            if not collected_candidates:
                self._on_scan_failed(
                    "No se encontraron perfiles en la pagina. Verifica que el perfil sea visible."
                )
                return
            self.scan_pending_candidates = collected_candidates[:max_profiles]
            self.scan_total_candidates = len(self.scan_pending_candidates)
            self._enqueue_scan_log(
                f"Recoleccion finalizada ({reason}). Perfiles detectados: {self.scan_total_candidates}"
            )
            self._enqueue_scan_log("Iniciando 4 QWebEngine para abrir perfiles en paralelo")
            self._schedule_profile_workers()

        def collect_candidates_round(round_idx: int) -> None:
            if not self.scan_running:
                return
            web_view.page().toHtml(lambda html: on_following_html(round_idx, html or ""))

        def on_following_html(round_idx: int, html: str) -> None:
            nonlocal idle_rounds
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

            if new_in_round == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0
            self._enqueue_scan_log(
                f"Ronda {round_idx}: +{new_in_round} perfiles, "
                f"total={len(collected_candidates)}, idle={idle_rounds}/{max_idle_rounds}"
            )

            if len(collected_candidates) >= max_profiles:
                finalize_candidate_collection("tope max_profiles")
                return
            if round_idx >= max_scroll_rounds:
                finalize_candidate_collection("tope max_scroll_rounds")
                return
            if idle_rounds >= max_idle_rounds:
                finalize_candidate_collection("sin nuevos perfiles")
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

    def _schedule_profile_workers(self) -> None:
        if self.scan_tab is None or not self.scan_running:
            return
        for idx, worker_view in enumerate(self.scan_tab.worker_views):
            state = self.scan_worker_states[idx]
            is_busy = bool(state and state.get("busy"))
            if is_busy or not self.scan_pending_candidates:
                continue
            candidate = self.scan_pending_candidates.pop(0)
            self.scan_worker_states[idx] = {"busy": True, "candidate": candidate}
            url = str(candidate.get("url", ""))
            worker_view.setUrl(QUrl(url))
            QTimer.singleShot(
                20000, lambda worker_idx=idx, expected_url=url: self._on_worker_timeout(worker_idx, expected_url)
            )
            self._enqueue_scan_log(
                f"[{len(self.scan_results) + 1}/{self.scan_total_candidates}] Worker {idx + 1}: {url}"
            )

        if (
            self.scan_running
            and self.scan_total_candidates > 0
            and not self.scan_pending_candidates
            and all(not (state and state.get("busy")) for state in self.scan_worker_states)
        ):
            self._finalize_scan_results()

    def _on_worker_load_finished(self, worker_idx: int, ok: bool) -> None:
        if not self.scan_running or self.scan_tab is None:
            return
        state = self.scan_worker_states[worker_idx]
        if not state or not state.get("busy"):
            return
        candidate = state.get("candidate")
        if not isinstance(candidate, dict):
            return
        if not ok:
            profile_url = str(candidate.get("url", ""))
            self.scan_results.append(
                {
                    "url": profile_url,
                    "status_code": 0,
                    "title": profile_url.rsplit("/", 1)[-1],
                    "description": "ERROR: no se pudo cargar perfil en QWebEngine",
                    "urls": [],
                }
            )
            self.scan_worker_states[worker_idx] = {"busy": False, "candidate": None}
            self._enqueue_scan_log(f"Worker {worker_idx + 1}: fallo de carga")
            self._schedule_profile_workers()
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
            const ready = Boolean(userNameNode && (descNode || headerItemsNode));
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
        title_hint = str(candidate.get("title_hint", ""))
        description_hint = str(candidate.get("description_hint", ""))
        expected_handle = profile_url.rstrip("/").rsplit("/", 1)[-1].lower()
        row = build_profile_row_from_html(
            profile_url=profile_url,
            profile_html=html or "",
            title_hint=title_hint,
            description_hint=description_hint,
            status_code=0 if "/i/flow/login" in page_url else 200,
            resolve_tco=self._scan_setting_bool("scan_resolve_tco", SCAN_RESOLVE_TCO),
            resolve_timeout_s=self._scan_setting_float(
                "scan_url_resolve_timeout_s",
                SCAN_URL_RESOLVE_TIMEOUT_S,
                minimum=1.0,
                maximum=20.0,
            ),
            url_cache=self.scan_url_resolve_cache,
        )
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
        if row["status_code"] == 0 and not str(row.get("description", "")).strip():
            row["description"] = "ERROR: redireccionado a login wall"
        if str(row.get("title", "")).strip().startswith(("http://", "https://")):
            row["title"] = title_hint or expected_handle
        self.scan_results.append(row)
        self.scan_worker_states[worker_idx] = {"busy": False, "candidate": None}
        self._enqueue_scan_log(
            f"[{len(self.scan_results)}/{self.scan_total_candidates}] Worker {worker_idx + 1} finalizado: {profile_url}"
        )
        self._schedule_profile_workers()

    def _on_worker_timeout(self, worker_idx: int, expected_url: str) -> None:
        if not self.scan_running:
            return
        state = self.scan_worker_states[worker_idx]
        if not state or not state.get("busy"):
            return
        candidate = state.get("candidate")
        if not isinstance(candidate, dict):
            return
        current_expected = str(candidate.get("url", ""))
        if current_expected != expected_url:
            return
        self.scan_results.append(
            {
                "url": expected_url,
                "status_code": 0,
                "title": expected_url.rsplit("/", 1)[-1],
                "description": "ERROR: timeout en QWebEngine",
                "urls": [],
            }
        )
        self.scan_worker_states[worker_idx] = {"busy": False, "candidate": None}
        self._enqueue_scan_log(f"Worker {worker_idx + 1}: timeout en {expected_url}")
        self._schedule_profile_workers()

    def _finalize_scan_results(self) -> None:
        if not self.scan_running:
            return
        try:
            scan_id, profiles_found = save_scan_results(
                db_path=str(SCAN_DB_FILE),
                following_url=self.scan_following_url,
                rows=self.scan_results,
            )
            self._on_scan_finished(
                {
                    "scan_id": scan_id,
                    "profiles_found": profiles_found,
                    "db": str(SCAN_DB_FILE),
                }
            )
        except Exception as exc:
            self._on_scan_failed(f"Error guardando resultados: {exc}")

    def _on_scan_finished(self, result: dict) -> None:
        self.scan_running = False
        self.scan_url_resolve_cache = {}
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self._enqueue_scan_log(
                f"Escaneo finalizado. Perfiles: {result.get('profiles_found', 0)}"
            )
            self._enqueue_scan_log(
                f"SQLite: {result.get('db', '')} (scan_id={result.get('scan_id', '')})"
            )
        self.statusBar().showMessage("Escaneo finalizado", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _on_scan_failed(self, error: str) -> None:
        self.scan_running = False
        self.scan_url_resolve_cache = {}
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self._enqueue_scan_log(f"ERROR: {error}")
        self.statusBar().showMessage("Escaneo fallido", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _enqueue_scan_log(self, message: str) -> None:
        if self.scan_tab is not None:
            self.scan_tab.log_output.append(message)

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
