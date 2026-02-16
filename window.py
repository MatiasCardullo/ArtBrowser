"""Main window implementation."""

import multiprocessing as mp
import re
from collections import deque
from datetime import datetime
from queue import Empty

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QAction
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtWidgets import (
    QLineEdit, QMainWindow, QTabWidget, QToolBar, QVBoxLayout, QWidget
)

from config import (
    BASE_DIR, DEFAULT_URL, SCAN_DB_FILE, SCAN_MAX_PARALLEL_REQUESTS,
    save_session, save_settings
)
from scanner import run_scan_process
from widgets import ScanTab, SettingsTab, WebEngineView


class BrowserWindow(QMainWindow):
    open_windows = []
    profile: QWebEngineProfile | None = None
    app_settings: dict[str, str | bool | int] = {}

    def __init__(self, initial_urls: list[str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("ArtBrowser")
        self.resize(1200, 800)
        self.scan_process: mp.Process | None = None
        self.scan_queue: mp.Queue | None = None
        self.scan_tab: ScanTab | None = None
        self.scan_settings_tab: SettingsTab | None = None
        self.scan_log_buffer: deque[str] = deque()
        self.scan_log_timer = QTimer(self)
        self.scan_log_timer.setInterval(150)
        self.scan_log_timer.timeout.connect(self._flush_scan_logs)
        self.scan_event_timer = QTimer(self)
        self.scan_event_timer.setInterval(120)
        self.scan_event_timer.timeout.connect(self._poll_scan_events)

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

        initial_url = str(BrowserWindow.app_settings.get("following_scan_url", DEFAULT_URL))
        settings_widget = SettingsTab(initial_url, self._start_scan_from_settings, self._save_following_url)
        self.scan_settings_tab = settings_widget
        index = self.tabs.addTab(settings_widget, "Configuracion")
        self.tabs.setCurrentIndex(index)

    def _save_following_url(self, url_text: str) -> None:
        qurl = QUrl(url_text or DEFAULT_URL)
        if not qurl.scheme():
            qurl.setScheme("https")
        BrowserWindow.app_settings["following_scan_url"] = qurl.toString()
        save_settings(BrowserWindow.app_settings)

    def _start_scan_from_settings(self, url_text: str) -> None:
        if self.scan_settings_tab is None:
            return
        self.start_following_scan(url_text, self.scan_settings_tab)

    def start_following_scan(self, url_text: str, settings_tab: SettingsTab) -> None:
        if self.scan_process is not None and self.scan_process.is_alive():
            return

        qurl = QUrl(url_text.strip())
        if not qurl.scheme():
            qurl.setScheme("https")
        scan_url = qurl.toString()
        BrowserWindow.app_settings["following_scan_url"] = scan_url
        save_settings(BrowserWindow.app_settings)
        settings_tab.set_scan_running(True)
        self.statusBar().showMessage("Escaneo iniciado", 3000)

        scan_tab = self._open_scan_tab()
        scan_tab.set_running(True)
        self._enqueue_scan_log(f"Cargando pagina de followings: {scan_url}")
        web_view = scan_tab.web_view
        web_view.setUrl(QUrl(scan_url))
        self.url_bar.setText(scan_url)

        def start_worker_with_html(html: str | None) -> None:
            source_html = html or ""
            self._cleanup_scan_process()
            self.scan_queue = mp.Queue()
            try:
                parallel_requests = int(
                    BrowserWindow.app_settings.get(
                        "scan_parallel_requests", SCAN_MAX_PARALLEL_REQUESTS
                    )
                )
            except (TypeError, ValueError):
                parallel_requests = SCAN_MAX_PARALLEL_REQUESTS
            self.scan_process = mp.Process(
                target=run_scan_process,
                args=(scan_url, source_html, str(SCAN_DB_FILE), self.scan_queue, parallel_requests),
                daemon=True,
            )
            self.scan_process.start()
            self.scan_event_timer.start()

        def on_load_finished(ok: bool) -> None:
            try:
                web_view.loadFinished.disconnect(on_load_finished)
            except Exception:
                pass
            if not ok:
                self._enqueue_scan_log(
                    "No se pudo cargar la pestaña, continuando con requests sin HTML renderizado"
                )
                start_worker_with_html(None)
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
                    if count > 0:
                        self._enqueue_scan_log(
                            f"Perfiles visibles detectados: {count}. Capturando HTML renderizado"
                        )
                        web_view.page().toHtml(lambda html: start_worker_with_html(html))
                        return
                    if retries_left > 0:
                        web_view.page().runJavaScript(
                            "window.scrollBy(0, Math.max(400, window.innerHeight * 0.6));"
                        )
                        QTimer.singleShot(900, lambda: wait_for_user_cells(retries_left - 1))
                        return
                    self._enqueue_scan_log("No aparecieron UserCell a tiempo, usando HTML actual")
                    web_view.page().toHtml(lambda html: start_worker_with_html(html))

                web_view.page().runJavaScript(script, on_count)

            wait_for_user_cells(8)

        web_view.loadFinished.connect(on_load_finished)

    def _open_scan_tab(self) -> ScanTab:
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if isinstance(widget, ScanTab):
                self.tabs.setCurrentIndex(i)
                self.scan_tab = widget
                return widget
        web_view = self._create_web_view()
        self.scan_tab = ScanTab(web_view)
        self.scan_tab.web_view.urlChanged.connect(self._update_url_bar)
        index = self.tabs.addTab(self.scan_tab, "Escaneo")
        self.tabs.setCurrentIndex(index)
        return self.scan_tab

    def _on_scan_finished(self, result: dict) -> None:
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self._enqueue_scan_log(
                f"Escaneo finalizado. Perfiles: {result.get('profiles_found', 0)}"
            )
            self._enqueue_scan_log(
                f"SQLite: {result.get('db', '')} (scan_id={result.get('scan_id', '')})"
            )
            self._flush_scan_logs()
        self.statusBar().showMessage("Escaneo finalizado", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _on_scan_failed(self, error: str) -> None:
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self._enqueue_scan_log(f"ERROR: {error}")
            self._flush_scan_logs()
        self.statusBar().showMessage("Escaneo fallido", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _enqueue_scan_log(self, message: str) -> None:
        self.scan_log_buffer.append(message)
        if not self.scan_log_timer.isActive():
            self.scan_log_timer.start()

    def _flush_scan_logs(self) -> None:
        if self.scan_tab is None:
            self.scan_log_buffer.clear()
            self.scan_log_timer.stop()
            return
        if not self.scan_log_buffer:
            self.scan_log_timer.stop()
            return
        chunks: list[str] = []
        while self.scan_log_buffer and len(chunks) < 20:
            chunks.append(self.scan_log_buffer.popleft())
        self.scan_tab.log_output.append("\n".join(chunks))

    def _poll_scan_events(self) -> None:
        if self.scan_queue is None:
            self.scan_event_timer.stop()
            return
        while True:
            current_queue = self.scan_queue
            if current_queue is None:
                self.scan_event_timer.stop()
                return
            try:
                event = current_queue.get_nowait()
            except Empty:
                break
            except (EOFError, OSError, ValueError):
                self._cleanup_scan_process()
                return

            event_type = str(event.get("type", ""))
            if event_type == "progress":
                self._enqueue_scan_log(str(event.get("message", "")))
            elif event_type == "finished":
                self._on_scan_finished(event)
                self._cleanup_scan_process()
                return
            elif event_type == "failed":
                self._on_scan_failed(str(event.get("error", "Error en escaneo")))
                self._cleanup_scan_process()
                return

        if self.scan_process is not None and not self.scan_process.is_alive():
            self._cleanup_scan_process()

    def _cleanup_scan_process(self) -> None:
        self.scan_event_timer.stop()
        if self.scan_process is not None:
            if self.scan_process.is_alive():
                self.scan_process.terminate()
                self.scan_process.join(timeout=1)
            self.scan_process.close()
        self.scan_process = None
        if self.scan_queue is not None:
            try:
                self.scan_queue.close()
            except (OSError, ValueError):
                pass
            self.scan_queue = None

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
        self._cleanup_scan_process()
        remaining = [window for window in BrowserWindow.open_windows if window is not self]
        save_session(remaining if remaining else [self])
        BrowserWindow.open_windows.remove(self)
        super().closeEvent(event)
