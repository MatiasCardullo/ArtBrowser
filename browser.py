"""Minimal PyQt6-based browser with navigation toolbar and tabbed windows."""

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests
from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
PROFILE_DIR = BASE_DIR / "profile_data"
OLD_PROFILE_DIR = BASE_DIR / "profile_storage"
DEFAULT_URL = "https://www.google.com"
POPUP_URL = "about:blank"
DEFAULT_FOLLOWING_SCAN_URL = "https://x.com/my_profile/following"
WEB_PROFILE: QWebEngineProfile | None = None
APP_SETTINGS: dict[str, str | bool] = {}


def _ensure_profile_storage() -> None:
    if OLD_PROFILE_DIR.exists() and not PROFILE_DIR.exists():
        shutil.move(str(OLD_PROFILE_DIR), str(PROFILE_DIR))
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / "cache").mkdir(exist_ok=True)


def _configure_profile(app: QApplication) -> QWebEngineProfile:
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


def _load_session() -> list[list[str]]:
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


def _save_session(windows: Iterable["BrowserWindow"] | None) -> None:
    if windows is None:
        data = []
    else:
        data = [window.tab_urls() for window in windows if window.tab_urls()]
    with SESSION_FILE.open("w", encoding="utf-8") as handle:
        json.dump({"windows": data}, handle, indent=2)


def _default_settings() -> dict[str, str | bool]:
    return {
        "following_scan_url": DEFAULT_FOLLOWING_SCAN_URL,
    }


def _load_settings() -> dict[str, str | bool]:
    if not SETTINGS_FILE.exists():
        return _default_settings()
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            merged = _default_settings()
            merged.update(raw)
            return merged
    except (json.JSONDecodeError, OSError):
        pass
    return _default_settings()


def _save_settings(settings: dict[str, str | bool]) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)


class SettingsTab(QWidget):
    """Simple settings page embedded as a browser tab."""

    def __init__(self, browser_window: "BrowserWindow") -> None:
        super().__init__()
        self.browser_window = browser_window
        self._build_ui()
        self._load_from_state()

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("Configuracion")
        title.setStyleSheet("font-weight: bold; font-size: 16px;")
        layout.addWidget(title)

        form = QFormLayout()
        self.following_url_input = QLineEdit()
        self.following_url_input.setPlaceholderText(DEFAULT_FOLLOWING_SCAN_URL)
        self.scan_button = QPushButton("Escanear")
        self.scan_button.clicked.connect(self._start_scan)
        url_row = QWidget()
        url_row_layout = QHBoxLayout()
        url_row_layout.setContentsMargins(0, 0, 0, 0)
        url_row_layout.addWidget(self.following_url_input)
        url_row_layout.addWidget(self.scan_button)
        url_row.setLayout(url_row_layout)
        form.addRow("URL para escanear followings:", url_row)

        layout.addLayout(form)

        save_button = QPushButton("Guardar configuracion")
        save_button.clicked.connect(self._save)
        layout.addWidget(save_button)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        layout.addStretch()

    def _load_from_state(self) -> None:
        self.following_url_input.setText(str(APP_SETTINGS.get("following_scan_url", "")))

    def _save(self) -> None:
        value = self.following_url_input.text().strip()
        if not value:
            value = DEFAULT_FOLLOWING_SCAN_URL
        qurl = QUrl(value)
        if not qurl.scheme():
            qurl.setScheme("https")
        APP_SETTINGS["following_scan_url"] = qurl.toString()
        _save_settings(APP_SETTINGS)
        self.status_label.setText("Configuracion guardada")

    def _start_scan(self) -> None:
        self.browser_window.start_following_scan(self.following_url_input.text(), self)

    def set_scan_running(self, running: bool) -> None:
        self.scan_button.setEnabled(not running)
        self.scan_button.setText("Escaneando..." if running else "Escanear")


class ScanWorker(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, following_url: str, source_html: str | None = None) -> None:
        super().__init__()
        self.following_url = following_url
        self.source_html = source_html or ""

    def run(self) -> None:
        try:
            self.progress.emit(f"Obteniendo followings desde: {self.following_url}")
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
                    )
                }
            )

            html_source = self.source_html
            if html_source:
                self.progress.emit("Usando HTML renderizado de la pestaña para detectar perfiles")
            else:
                response = session.get(self.following_url, timeout=20)
                response.raise_for_status()
                html_source = response.text

            profile_urls = self._extract_profile_urls(html_source)
            if not profile_urls:
                self.failed.emit(
                    "No se encontraron perfiles en la pagina. Verifica que el perfil sea visible."
                )
                return

            self.progress.emit(f"Perfiles detectados: {len(profile_urls)}")
            rows: list[dict[str, str | int]] = []
            for index, profile_url in enumerate(profile_urls, start=1):
                self.progress.emit(f"[{index}/{len(profile_urls)}] Solicitando {profile_url}")
                try:
                    profile_response = session.get(profile_url, timeout=20)
                    title = self._extract_title(profile_response.text)
                    description = self._extract_meta_description(profile_response.text)
                    rows.append(
                        {
                            "url": profile_url,
                            "status_code": profile_response.status_code,
                            "title": title,
                            "description": description,
                        }
                    )
                except requests.RequestException as exc:
                    rows.append(
                        {
                            "url": profile_url,
                            "status_code": 0,
                            "title": "",
                            "description": f"ERROR: {exc}",
                        }
                    )

            output_dir = BASE_DIR / "scan_results"
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = output_dir / f"x_following_scan_{timestamp}.json"
            payload = {
                "following_url": self.following_url,
                "profiles_found": len(profile_urls),
                "generated_at": datetime.now().isoformat(),
                "profiles": rows,
            }
            output_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

            self.finished.emit(
                {
                    "file": str(output_file),
                    "profiles_found": len(profile_urls),
                }
            )
        except requests.RequestException as exc:
            self.failed.emit(f"Fallo de red durante el escaneo: {exc}")
        except Exception as exc:
            self.failed.emit(f"Error inesperado en escaneo: {exc}")

    def _extract_profile_urls(self, html: str) -> list[str]:
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
        owner_match = re.search(r"x\.com/([^/?#]+)/following", self.following_url, re.IGNORECASE)
        owner_handle = owner_match.group(1).lower() if owner_match else ""
        html_text = html.replace("&quot;", '"')

        def is_valid_handle(handle: str) -> bool:
            value = handle.strip().lower()
            if not value or value in blocked or value == owner_handle:
                return False
            return re.fullmatch(r"[a-zA-Z0-9_]{1,15}", handle) is not None

        urls: list[str] = []
        seen: set[str] = set()

        # Prefer user cells in the rendered following timeline.
        for marker in re.finditer(r'data-testid="UserCell"', html_text):
            chunk = html_text[marker.start() : marker.start() + 14000]
            for handle in re.findall(r'href="/([A-Za-z0-9_]{1,15})"', chunk):
                lower = handle.lower()
                if not is_valid_handle(handle) or lower in seen:
                    continue
                seen.add(lower)
                urls.append(f"https://x.com/{handle}")

        if urls:
            return urls

        # Fallback: scan within timeline section only.
        timeline_match = re.search(
            r'aria-label="[^"]*(Siguiendo|Following)[^"]*".*',
            html_text,
            re.IGNORECASE | re.DOTALL,
        )
        scope = timeline_match.group(0) if timeline_match else html_text
        for handle in re.findall(r'href="/([A-Za-z0-9_]{1,15})"', scope):
            lower = handle.lower()
            if not is_valid_handle(handle) or lower in seen:
                continue
            seen.add(lower)
            urls.append(f"https://x.com/{handle}")
        return urls

    def _extract_title(self, html: str) -> str:
        match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip()

    def _extract_meta_description(self, html: str) -> str:
        match = re.search(
            r'<meta\s+name="description"\s+content="(.*?)"',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group(1)).strip()


class ScanTab(QWidget):
    def __init__(self, browser_window: "BrowserWindow") -> None:
        super().__init__()
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.state_label = QLabel("Listo")
        self.state_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.state_label.setMaximumHeight(18)
        layout.addWidget(self.state_label)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFixedHeight(180)
        layout.addWidget(self.log_output)
        self.web_view = WebEngineView(browser_window)
        layout.addWidget(self.web_view)

    def set_running(self, running: bool) -> None:
        self.state_label.setText("Escaneando..." if running else "Listo")

    def log(self, message: str) -> None:
        self.log_output.append(message)


class WebEngineView(QWebEngineView):
    """Web view that delegates new-window requests to its parent browser."""

    def __init__(self, browser_window: "BrowserWindow") -> None:
        super().__init__()
        self.browser_window = browser_window
        if WEB_PROFILE is not None:
            self.setPage(QWebEnginePage(WEB_PROFILE, self))

    def createWindow(self, _type):
        """Open popup requests in a new tab."""
        return self.browser_window.add_tab(POPUP_URL)


class BrowserWindow(QMainWindow):
    """Main window containing a toolbar and a tab widget."""

    open_windows = []

    def __init__(self, initial_urls: list[str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("ArtBrowser")
        self.resize(1200, 800)
        self.scan_thread: QThread | None = None
        self.scan_worker: ScanWorker | None = None
        self.scan_tab: ScanTab | None = None
        self.scan_settings_tab: SettingsTab | None = None

        BrowserWindow.open_windows.append(self)

        self._create_toolbar()
        self._create_tabs()

        urls = initial_urls or [DEFAULT_URL]
        for url in urls:
            self.add_tab(url)

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
        if view:
            self.url_bar.setText(view.url().toString())
        else:
            self.url_bar.setText("artbrowser://settings")

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
        if isinstance(url, QUrl):
            qurl = url
        elif isinstance(url, str):
            qurl = QUrl(url)
        else:
            qurl = QUrl(DEFAULT_URL)
        if not qurl.scheme():
            qurl.setScheme("https")
        view = WebEngineView(self)
        view.setUrl(qurl)
        view.urlChanged.connect(self._update_url_bar)
        view.titleChanged.connect(lambda title: self.tabs.setTabText(self.tabs.indexOf(view), title))

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
        if self.current_view() and self.current_view().url() == qurl:
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
        settings_widget = SettingsTab(self)
        self.scan_settings_tab = settings_widget
        index = self.tabs.addTab(settings_widget, "Configuracion")
        self.tabs.setCurrentIndex(index)

    def start_following_scan(self, url_text: str, settings_tab: SettingsTab) -> None:
        if self.scan_thread is not None and self.scan_thread.isRunning():
            return

        qurl = QUrl(url_text.strip())
        if not qurl.scheme():
            qurl.setScheme("https")
        scan_url = qurl.toString()
        APP_SETTINGS["following_scan_url"] = scan_url
        _save_settings(APP_SETTINGS)

        settings_tab.set_scan_running(True)
        self.statusBar().showMessage("Escaneo iniciado", 3000)

        scan_tab = self._open_scan_tab()
        scan_tab.set_running(True)
        scan_tab.log(f"Cargando pagina de followings: {scan_url}")
        web_view = scan_tab.web_view
        web_view.setUrl(QUrl(scan_url))
        self.url_bar.setText(scan_url)

        def start_worker_with_html(html: str | None) -> None:
            source_html = html or ""
            self.scan_thread = QThread(self)
            self.scan_worker = ScanWorker(scan_url, source_html)
            self.scan_worker.moveToThread(self.scan_thread)
            self.scan_thread.started.connect(self.scan_worker.run)
            self.scan_worker.progress.connect(self._on_scan_progress)
            self.scan_worker.finished.connect(self._on_scan_finished)
            self.scan_worker.failed.connect(self._on_scan_failed)
            self.scan_worker.finished.connect(self._finish_scan_thread)
            self.scan_worker.failed.connect(self._finish_scan_thread)
            self.scan_thread.start()

        def on_load_finished(ok: bool) -> None:
            try:
                web_view.loadFinished.disconnect(on_load_finished)
            except Exception:
                pass
            if not ok:
                scan_tab.log("No se pudo cargar la pestaña, continuando con requests sin HTML renderizado")
                start_worker_with_html(None)
                return
            scan_tab.log("Pagina cargada, esperando lista de perfiles en el DOM")

            def wait_for_user_cells(retries_left: int) -> None:
                script = "document.querySelectorAll('[data-testid=\"UserCell\"]').length"

                def on_count(result: object) -> None:
                    try:
                        count = int(result)
                    except (TypeError, ValueError):
                        count = 0

                    if count > 0:
                        scan_tab.log(f"Perfiles visibles detectados: {count}. Capturando HTML renderizado")
                        web_view.page().toHtml(lambda html: start_worker_with_html(html))
                        return

                    if retries_left > 0:
                        web_view.page().runJavaScript(
                            "window.scrollBy(0, Math.max(400, window.innerHeight * 0.6));"
                        )
                        QTimer.singleShot(900, lambda: wait_for_user_cells(retries_left - 1))
                        return

                    scan_tab.log("No aparecieron UserCell a tiempo, usando HTML actual")
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
        self.scan_tab = ScanTab(self)
        self.scan_tab.web_view.urlChanged.connect(self._update_url_bar)
        index = self.tabs.addTab(self.scan_tab, "Escaneo")
        self.tabs.setCurrentIndex(index)
        return self.scan_tab

    def _on_scan_progress(self, message: str) -> None:
        if self.scan_tab is not None:
            self.scan_tab.log(message)

    def _on_scan_finished(self, result: dict) -> None:
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self.scan_tab.log(
                f"Escaneo finalizado. Perfiles: {result.get('profiles_found', 0)}"
            )
            self.scan_tab.log(f"Archivo: {result.get('file', '')}")
        self.statusBar().showMessage("Escaneo finalizado", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _on_scan_failed(self, error: str) -> None:
        if self.scan_tab is not None:
            self.scan_tab.set_running(False)
            self.scan_tab.log(f"ERROR: {error}")
        self.statusBar().showMessage("Escaneo fallido", 5000)
        if self.scan_settings_tab is not None:
            self.scan_settings_tab.set_scan_running(False)

    def _finish_scan_thread(self) -> None:
        if self.scan_thread is None:
            return
        self.scan_thread.quit()
        self.scan_thread.wait(3000)
        if self.scan_worker is not None:
            self.scan_worker.deleteLater()
        self.scan_thread.deleteLater()
        self.scan_worker = None
        self.scan_thread = None

    def _save_current_page(self) -> None:
        view = self.current_view()
        if view is None:
            self.statusBar().showMessage("No hay una pagina web activa para guardar", 4000)
            return

        target_dir = BASE_DIR / "saved_pages"
        target_dir.mkdir(parents=True, exist_ok=True)
        qurl = view.url()
        host = qurl.host() or "page"
        safe_host = re.sub(r"[^a-zA-Z0-9_-]+", "_", host).strip("_") or "page"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_file = target_dir / f"{safe_host}_{timestamp}.html"

        def _write_html(html: str) -> None:
            target_file.write_text(html, encoding="utf-8")
            self.statusBar().showMessage(f"Pagina guardada en {target_file.name}", 5000)

        view.page().toHtml(_write_html)

    def closeEvent(self, event):
        if self.scan_thread is not None and self.scan_thread.isRunning():
            self.scan_thread.quit()
            self.scan_thread.wait(2000)
        remaining = [window for window in BrowserWindow.open_windows if window is not self]
        if remaining:
            _save_session(remaining)
        else:
            _save_session([self])
        BrowserWindow.open_windows.remove(self)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("ArtBrowser")
    global WEB_PROFILE
    global APP_SETTINGS
    _ensure_profile_storage()
    WEB_PROFILE = _configure_profile(app)
    APP_SETTINGS = _load_settings()

    session = _load_session()
    windows: list[BrowserWindow] = []
    for urls in session:
        window = BrowserWindow(urls)
        window.show()
        windows.append(window)

    if not windows:
        window = BrowserWindow()
        window.show()
        windows.append(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
