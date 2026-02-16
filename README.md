# ArtBrowser session persistence

- `python -m pip install PyQt6 PyQt6-WebEngine requests`
- `python browser.py` starts the browser; it now keeps cookies and cache in `profile_data/` and persists the last set of open tabs (see `session.json`).
- Use the `Configuracion` button in the navbar to open a settings tab and save the X followings scan URL (`settings.json`).
- In `Configuracion`, use `Escanear` to run a requests-based scan and save results to `scan_results/`.
- Use `Guardar pagina` in the navbar to export the current web tab as HTML into `saved_pages/`.

The session file is rewritten each time a browser window closes so the next launch restores the same tabs. Cookies are handled by the shared `QWebEngineProfile` with an on-disk storage path.
