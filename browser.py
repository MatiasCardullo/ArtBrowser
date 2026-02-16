"""ArtBrowser launcher."""

import multiprocessing as mp
import sys

from PyQt6.QtWidgets import QApplication

from config import configure_profile, ensure_profile_storage, load_session, load_settings
from window import BrowserWindow


def main() -> None:
    mp.freeze_support()
    app = QApplication(sys.argv)
    app.setApplicationName("ArtBrowser")

    ensure_profile_storage()
    BrowserWindow.profile = configure_profile(app)
    BrowserWindow.app_settings = load_settings()

    session = load_session()
    windows: list[BrowserWindow] = []
    for urls in session:
        window = BrowserWindow(urls)
        window.show()
        windows.append(window)

    if not windows:
        window = BrowserWindow()
        window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
