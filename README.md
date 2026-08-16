# ArtBrowser

ArtBrowser lets you view and organize artist content from multiple social networks.

Current status:
- Active development is focused on X/Twitter following and profile scanning.
- The scanner now has a superficial mode that only collects profile handles and saves them.
- List URLs can be scanned superficially after switching the target type to `Lista`.
- Multiple list URLs can be compared to report repeated handles.

Source roadmap:
- Misskey
- Bluesky
- Instagram
- Pixiv
- Other imageboards and similar networks

Quick start:
- `python -m pip install PyQt6 PyQt6-WebEngine beautifulsoup4 PyMySQL`
- `python browser.py` starts the browser, stores cookies/cache in `profile_data/`, and restores tabs from `session.json`.
- The `Configuration` tab sets the main URL, scan mode, superficial mode, and comparison list URLs (`settings.json`).
- `Scan` can run in deep mode for followings or in superficial mode that only collects profile URLs and saves them.
- List scans are superficial and click through to the `Members` view before collecting profiles.
- The scan view shows the saved profiles dashboard for database-backed scans and a comparison report for list cross-checks.
- Cached skips use completed profiles with a non-empty description and at least one URL; the profile table no longer stores `status_code` or embedded `link_details`.
- Profile categories include `vtuber` when the description mentions `vtuber`, `ママ`, or `パパ`, and `stream` when Twitch or Kick links are present.
- MySQL is configured from `.env` only via `ARTBROWSER_MYSQL_HOST`, `ARTBROWSER_MYSQL_PORT`, `ARTBROWSER_MYSQL_DATABASE`, `ARTBROWSER_MYSQL_USER`, and `ARTBROWSER_MYSQL_PASSWORD`; plain `MYSQL_*` names are also accepted.
- `Save page` exports the current tab as HTML in `saved_pages/`.

The session file is rewritten each time a browser window closes so the next launch restores the same tabs. Cookies are handled by the shared `QWebEngineProfile` with an on-disk storage path.
