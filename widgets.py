"""Reusable UI widgets."""

from collections.abc import Callable
from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QCheckBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QSplitter, QTextEdit, QVBoxLayout, QWidget
)

from config import DEFAULT_FOLLOWING_SCAN_URL, POPUP_URL

class SettingsTab(QWidget):
    def __init__(
        self,
        initial_settings: dict[str, str | bool | int | float],
        on_scan: Callable[[dict[str, str | bool | int | float]], None],
        on_save: Callable[[dict[str, str | bool | int | float]], None],
    ) -> None:
        super().__init__()
        self._on_scan = on_scan
        self._on_save = on_save
        self._build_ui(initial_settings)

    def _build_ui(self, initial_settings: dict[str, str | bool | int | float]) -> None:
        def as_int(value: object, default: int) -> int:
            try:
                return int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("Configuracion")
        title.setStyleSheet("font-weight: bold; font-size: 16px;")
        layout.addWidget(title)

        initial_url = str(initial_settings.get("following_scan_url", DEFAULT_FOLLOWING_SCAN_URL))
        form = QFormLayout()
        self.following_url_input = QLineEdit()
        self.following_url_input.setText(initial_url or DEFAULT_FOLLOWING_SCAN_URL)
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

        self.max_scroll_rounds_input = QLineEdit()
        self.max_scroll_rounds_input.setText(
            str(as_int(initial_settings.get("scan_following_max_scroll_rounds", 60), 60))
        )
        form.addRow("Max scroll rounds:", self.max_scroll_rounds_input)

        self.max_profiles_input = QLineEdit()
        self.max_profiles_input.setText(
            str(as_int(initial_settings.get("scan_following_max_profiles", 800), 800))
        )
        form.addRow("Max perfiles:", self.max_profiles_input)

        self.parallel_workers_input = QLineEdit()
        self.parallel_workers_input.setText(
            str(as_int(initial_settings.get("scan_parallel_requests", 4), 4))
        )
        form.addRow("Workers paralelos (2 o 4):", self.parallel_workers_input)

        self.skip_already_ok_checkbox = QCheckBox("Saltar perfiles con descripcion y URLs")
        self.skip_already_ok_checkbox.setChecked(
            bool(initial_settings.get("scan_skip_already_ok", True))
        )
        form.addRow("Reusar resultados:", self.skip_already_ok_checkbox)

        self.mysql_host_input = QLineEdit()
        self.mysql_host_input.setText(str(initial_settings.get("mysql_host", "127.0.0.1")))
        form.addRow("MySQL host:", self.mysql_host_input)

        self.mysql_port_input = QLineEdit()
        self.mysql_port_input.setText(str(as_int(initial_settings.get("mysql_port", 3306), 3306)))
        form.addRow("MySQL port:", self.mysql_port_input)

        self.mysql_database_input = QLineEdit()
        self.mysql_database_input.setText(str(initial_settings.get("mysql_database", "artbrowser")))
        form.addRow("MySQL database:", self.mysql_database_input)

        self.mysql_user_input = QLineEdit()
        self.mysql_user_input.setText(str(initial_settings.get("mysql_user", "root")))
        form.addRow("MySQL user:", self.mysql_user_input)

        self.mysql_password_input = QLineEdit()
        self.mysql_password_input.setText(str(initial_settings.get("mysql_password", "")))
        self.mysql_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("MySQL password:", self.mysql_password_input)

        layout.addLayout(form)

        save_button = QPushButton("Guardar configuracion")
        save_button.clicked.connect(self._save)
        layout.addWidget(save_button)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        layout.addStretch()

    def _save(self) -> None:
        settings_values = self._collect_settings_values()
        if settings_values is None:
            return
        self._on_save(settings_values)
        self.status_label.setText("Configuracion guardada")

    def _start_scan(self) -> None:
        settings_values = self._collect_settings_values()
        if settings_values is None:
            return
        self._on_scan(settings_values)

    def set_scan_running(self, running: bool) -> None:
        self.scan_button.setEnabled(not running)
        self.scan_button.setText("Escaneando..." if running else "Escanear")

    def _collect_settings_values(self) -> dict[str, str | bool | int | float] | None:
        url = self.following_url_input.text().strip()
        try:
            max_scroll_rounds = int(self.max_scroll_rounds_input.text().strip())
            max_profiles = int(self.max_profiles_input.text().strip())
            parallel_workers = int(self.parallel_workers_input.text().strip())
            mysql_port = int(self.mysql_port_input.text().strip())
        except ValueError:
            self.status_label.setText("Valores invalidos: revisa los numeros")
            return None

        if max_scroll_rounds <= 0 or max_profiles <= 0:
            self.status_label.setText("Max scroll/perfiles deben ser > 0")
            return None
        if parallel_workers not in {2, 4}:
            self.status_label.setText("Workers paralelos debe ser 2 o 4")
            return None
        if mysql_port <= 0:
            self.status_label.setText("MySQL port debe ser > 0")
            return None
        return {
            "following_scan_url": url or DEFAULT_FOLLOWING_SCAN_URL,
            "scan_following_max_scroll_rounds": max_scroll_rounds,
            "scan_following_max_profiles": max_profiles,
            "scan_parallel_requests": parallel_workers,
            "scan_skip_already_ok": self.skip_already_ok_checkbox.isChecked(),
            "mysql_host": self.mysql_host_input.text().strip() or "127.0.0.1",
            "mysql_port": mysql_port,
            "mysql_database": self.mysql_database_input.text().strip() or "artbrowser",
            "mysql_user": self.mysql_user_input.text().strip() or "root",
            "mysql_password": self.mysql_password_input.text(),
        }


class WebEngineView(QWebEngineView):
    def __init__(
        self,
        popup_tab_factory: Callable[[str], QWebEngineView],
        profile: QWebEngineProfile | None,
    ) -> None:
        super().__init__()
        self._popup_tab_factory = popup_tab_factory
        if profile is not None:
            self.setPage(QWebEnginePage(profile, self))

    def createWindow(self, _type):
        return self._popup_tab_factory(POPUP_URL)


class ScanTab(QWidget):
    def __init__(self, web_view: QWebEngineView, worker_views: list[QWebEngineView]) -> None:
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
        self.web_view = web_view
        self.worker_views = worker_views

        splitter = QSplitter()
        left_container = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.web_view)
        left_container.setLayout(left_layout)

        right_container = QWidget()
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        workers_grid = QGridLayout()
        workers_grid.setContentsMargins(0, 0, 0, 0)
        for idx, view in enumerate(self.worker_views):
            row = idx // 2
            col = idx % 2
            workers_grid.addWidget(view, row, col)
        right_layout.addLayout(workers_grid)
        right_container.setLayout(right_layout)

        splitter.addWidget(left_container)
        splitter.addWidget(right_container)
        splitter.setSizes([700, 500])
        layout.addWidget(splitter)

    def set_running(self, running: bool) -> None:
        self.state_label.setText("Escaneando..." if running else "Listo")

    def clear_workers(self) -> None:
        for view in self.worker_views:
            view.setUrl(QUrl("about:blank"))

    def set_active_workers(self, count: int) -> None:
        active = 4 if count >= 4 else 2
        for idx, view in enumerate(self.worker_views):
            view.setVisible(idx < active)
