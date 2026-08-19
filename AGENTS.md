# ArtBrowser - Guia Operativa para Agentes

## Vision del Proyecto
- ArtBrowser es un proyecto para visualizar y organizar contenido de artistas desde multiples redes sociales.
- El enfoque actual esta en X/Twitter (escaneo de followings y perfiles), pero la arquitectura debe mantenerse extensible.
- Redes/plataformas objetivo a incorporar progresivamente: Misskey, Bluesky, Instagram, Pixiv y otros imageboards.
- Regla de diseño: evitar logica atada a una sola red cuando sea posible; preferir componentes reutilizables de parsing, almacenamiento y UI.

## Project Entry Point
- Archivo de entrada: `browser.py`.
- `main()` hace:
  - `QApplication` y nombre de app.
  - Inicializacion de perfil persistente (`ensure_profile_storage`, `configure_profile`).
  - Carga de settings/sesion (`load_settings`, `load_session`).
  - Creacion de una o mas `BrowserWindow` y `app.exec()`.
- `browser.py` es launcher; no contiene logica de scraping/parsing.

## Runtime Architecture
- `window.py`:
  - Orquestacion principal de UI y tabs.
  - Flujo de escaneo desde configuracion (`start_following_scan`).
  - Carga de HTML renderizado de la pestana de followings (`toHtml`).
  - Apertura de perfiles en paralelo con workers `QWebEngineView`.
  - Espera de nodos de perfil en DOM antes de extraer datos.
- `widgets.py`:
  - Componentes UI reutilizables (`SettingsTab`, `WebEngineView`, `ScanTab`).
  - `ScanTab` define panel de estado/logs, vista principal y grilla de workers.
- `scanner.py`:
  - Parsing de followings y perfiles con BeautifulSoup.
  - Persistencia SQLite de resultados de escaneo.
- `config.py`:
  - Paths base, archivos de sesion/settings, DB de escaneo.
  - Configuracion de `QWebEngineProfile` persistente.

## Data & Persistence
- `session.json`: pestanas/ventanas restaurables.
- `settings.json`: opciones de app (incluye URL de followings y paralelismo).
- `scan_results/followings.db`: SQLite con tablas de escaneo/resultados.
- `profile_data/`: cookies/cache/perfil persistente de Qt WebEngine.
- `saved_pages/`: export HTML manual de la pestana actual.

## Debug Workflow
- Checklist antes de cerrar una tarea:
  - Confirmar alcance de archivos tocados.
  - Confirmar que no se alteraron paths/persistencia fuera del objetivo.
  - Confirmar que `browser.py` sigue siendo solo launcher.
  - Resumir explicitamente riesgos o pruebas no ejecutadas.
