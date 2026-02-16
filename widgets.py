"""Reusable UI widgets."""

from collections.abc import Callable
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
)

from config import DEFAULT_FOLLOWING_SCAN_URL, POPUP_URL

class SettingsTab(QWidget):
    def __init__(
        self,     initial_url: str,
        on_scan: Callable[[str], None],
        on_save: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._on_scan = on_scan
        self._on_save = on_save
        self._build_ui(initial_url)

    def _build_ui(self, initial_url: str) -> None:
        layout = QVBoxLayout()
        self.setLayout(layout)

        title = QLabel("Configuracion")
        title.setStyleSheet("font-weight: bold; font-size: 16px;")
        layout.addWidget(title)

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
        layout.addLayout(form)

        save_button = QPushButton("Guardar configuracion")
        save_button.clicked.connect(self._save)
        layout.addWidget(save_button)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
        layout.addStretch()

    def _save(self) -> None:
        self._on_save(self.following_url_input.text().strip())
        self.status_label.setText("Configuracion guardada")

    def _start_scan(self) -> None:
        self._on_scan(self.following_url_input.text().strip())

    def set_scan_running(self, running: bool) -> None:
        self.scan_button.setEnabled(not running)
        self.scan_button.setText("Escaneando..." if running else "Escanear")


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
    def __init__(self, web_view: QWebEngineView) -> None:
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
        layout.addWidget(self.web_view)

    def set_running(self, running: bool) -> None:
        self.state_label.setText("Escaneando..." if running else "Listo")
