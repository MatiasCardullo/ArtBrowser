"""ArtBrowser launcher."""

import faulthandler
import multiprocessing as mp
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from config import BASE_DIR, configure_profile, ensure_profile_storage, load_session, load_settings
from window import BrowserWindow

_CRASH_LOG = None


def install_crash_logging() -> None:
    global _CRASH_LOG
    _CRASH_LOG = (BASE_DIR / "artbrowser_crash.log").open("a", encoding="utf-8")
    faulthandler.enable(file=_CRASH_LOG)

    def log_exception(exc_type, exc, tb) -> None:
        traceback.print_exception(exc_type, exc, tb, file=_CRASH_LOG)
        _CRASH_LOG.flush()
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = log_exception


def main() -> None:
    mp.freeze_support()
    install_crash_logging()
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
