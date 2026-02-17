# ArtBrowser session persistence

- `python -m pip install PyQt6 PyQt6-WebEngine beautifulsoup4`
- `python browser.py` starts the browser; it now keeps cookies and cache in `profile_data/` and persists the last set of open tabs (see `session.json`).
- Use the `Configuracion` button in the navbar to open a settings tab and save the X followings scan URL (`settings.json`).
- In `Configuracion`, use `Escanear` to run a process-isolated scan and save results in SQLite (`scan_results/followings.db`).
- El escaneo de perfiles usa workers de `QWebEngine` en paralelo para capturar HTML renderizado.
- Use `Guardar pagina` in the navbar to export the current web tab as HTML into `saved_pages/`.

The session file is rewritten each time a browser window closes so the next launch restores the same tabs. Cookies are handled by the shared `QWebEngineProfile` with an on-disk storage path.
