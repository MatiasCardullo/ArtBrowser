# ArtBrowser

ArtBrowser lets you view and organize artist content from multiple social networks.

Current status:
- Active development is focused on X/Twitter following and profile scanning.

Source roadmap:
- Misskey
- Bluesky
- Instagram
- Pixiv
- Other imageboards and similar networks

Quick start:
- `python -m pip install PyQt6 PyQt6-WebEngine beautifulsoup4 PyMySQL`
- `python browser.py` starts the browser, stores cookies/cache in `profile_data/`, and restores tabs from `session.json`.
- The `Configuration` tab sets the followings URL and scan parameters (`settings.json`).
- `Scan` runs parallel `QWebEngine` workers and stores results in MySQL (`artbrowser.twitter_profiles` by default).
- MySQL can be configured in the app settings or with `ARTBROWSER_MYSQL_HOST`, `ARTBROWSER_MYSQL_PORT`, `ARTBROWSER_MYSQL_DATABASE`, `ARTBROWSER_MYSQL_USER`, and `ARTBROWSER_MYSQL_PASSWORD`; plain `MYSQL_*` names are also accepted from `.env`.
- `Save page` exports the current tab as HTML in `saved_pages/`.

The session file is rewritten each time a browser window closes so the next launch restores the same tabs. Cookies are handled by the shared `QWebEngineProfile` with an on-disk storage path.
