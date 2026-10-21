# Changelog

All notable changes to Datasquint (called pgPeek up to 1.4.0). Versions follow `MAJOR.MINOR.PATCH`:

- **PATCH** (1.0.**1**) for fixes,
- **MINOR** (1.**1**.0) for new features,
- **MAJOR** (**2**.0.0) for changes that break existing setups, such as settings, saved data or the API.

The version is set in `version.py`. It's shown at the bottom of the sidebar and on the Help page, returned by `/api/version`, and recorded in chat exports.

## 2.16.0 — 2026-10-21

### Fixed
- **Wide files were invisible to the model.** When one table's columns and sample rows took more than the 20,000 characters allowed for the file tables, that table was left out entirely: a 600-column CSV loaded fine, but the model was told only "1 more table(s) not shown" and couldn't query it. Every table is now listed. When they don't fit, the detail drops table by table (sample rows, then column types, then, for tables with thousands of columns, the names past the space left), and the model is told how to look columns up with SQL.
- The file preview the model gets with each data file is limited to short lines (it was about 60,000 characters for a 3,000-column CSV).
- **Browser: the model could end up working in an invisible "tab".** With a profile folder, current Chrome starts on its New Tab page, whose footer appears to Selenium as a second window (`chrome://newtab-footer`); depending on which one the driver picked, clicks and reading went to the footer instead of the page. A browser Datasquint launches now opens its own tab and closes the start page, and the footer never counts as a tab (tab count, `tabs`, `switch_tab`). Browsers you attach to are left as they are.
- **Browser left running after Datasquint stopped**, keeping its profile folder in use so the next start failed (*Another Chrome window is using its profile folder*). Stopping Datasquint (Ctrl+C or a service stop) now closes the browser it launched. After an unclean stop, the profile is marked as shut down cleanly before the next start, so the browser doesn't offer to restore the old tabs.

### Added
- **`.csv.gz`, `.tsv.gz` and `.parquet` uploads**, loaded as SQL tables like CSV. Gzipped files are unpacked on the server (up to `MAX_UNCOMPRESSED_MB`, default 10,000 MB) and then handled exactly like a CSV, including Windows-encoded text and several tables in one file; they are typically 2–5 times smaller to upload. Parquet keeps its column types; column names are normalised like CSV ones. Python scripts exported from a chat rebuild these tables from the original files. Other `.gz` files are refused.

### Changed
- **`MAX_TABLE_UPLOAD_MB` defaults to 1000** (was 200). Measured on a 2-core, 8 GB machine: a 1.46 GB, 5-million-row CSV loads in about 40 s (about 50 s as a 680 MB `.csv.gz`), uses about 2.4 GB of memory while loading, and answers GROUP BY queries in under 0.1 s. Behind nginx, raise `client_max_body_size` to match and keep `proxy_read_timeout` at 300 s or more (README → Running in production).
- README: guidance for very large data (memory, disk, `FILES_SQL_MEMORY`, when to use PostgreSQL instead).

## 2.15.0 — 2026-10-21

### Added
- **`BROWSER_ENABLED`** turns the Browser data source on or off for the whole server. It's on by default (`1`), so nothing changes unless you set it. With `BROWSER_ENABLED=0`:
  - the **Browser** chip and the **Auto-click** setting are hidden;
  - selenium isn't loaded and no Edge or Chrome is started;
  - the `/api/browser/*` routes refuse with *The browser is turned off on this server*;
  - chats get no browser instructions, even ones that had Browser on;
  - ```` ```action ```` blocks in replies show as plain code, with no Run or Allow button.
- The start-up banner and log show the browser state, e.g. *Browser (web pages in Edge/Chrome): on, Edge by default*.
- `/api/config` and `/api/browser/status` include `enabled`.

## 2.14.0 — 2026-10-21

### Added
- **Google Chrome** as well as Microsoft Edge for the Browser data source. Pick one in the **Browser** menu before starting; the choice is remembered in this browser, and `BROWSER=edge|chrome` in `.env` sets the default. The model's instructions, the Browser chip and the menu name the browser in use, and a browser the model opens itself (by going to a URL) is the one you picked.
- Each browser keeps its own profile with its own logins: `PROFILE_DIR` (Edge, `./edge-profile`) and `CHROME_PROFILE_DIR` (Chrome, `./chrome-profile`). The two can't share one.
- Chrome settings: `CHROME_DRIVER` (chromedriver, needed offline), `CHROME_BINARY`, `CHROME_PROFILE_DIR`, `CHROME_DEBUGGER` (attach to a Chrome started with `--remote-debugging-port`).
- API: `/api/browser/status` adds `browser`, `browser_name`, `default_browser` and `browsers`; `/api/browser/start`, `/navigate` and `/action` take `browser` (`edge`/`chrome`).

### Changed
- Clearer start-up errors for both browsers: missing or mismatched driver (with the `…_DRIVER` setting to use and the version page to check), browser not installed, profile folder in use (including after Datasquint was stopped while the browser was open), and nothing listening at the debugger address.
- Opening the other browser while one is open is refused with "Close it first".
- The updater keeps the `chrome-profile` folder.

## 2.13.0 — 2026-10-21

### Added
- **Browser data source.** The **Browser** switch in the header lets the model read and use a Microsoft Edge window in that chat, driven by Selenium: internal web pages such as MES screens, equipment HMIs and report portals. Merged in from the separate Pagesquint prototype, which is retired.
  - The page open now (text, numbered clickable elements, tables, frames) is added to the system prompt of every request, so questions about what's on screen need no extra step.
  - The model writes ```` ```action ```` blocks, which work like ```` ```sql ```` blocks: actions that only read the page run straight away, and clicking, typing and opening pages show **Allow** first. **Settings → Auto-click** (or `AUTO_CLICK=1`) runs them without asking. Browser steps pause after 15 in a row.
  - Actions: snapshot, navigate, click, type, select, press, scroll, back, find, extract_table, extract_text, frame, tabs, switch_tab, wait, plus screenshot with `VISION=1`. `paginate` extracts a table from every page by clicking Next, and `click_all` expands every "Show more", each as a single step.
  - **Extracted tables become CSV files attached to the chat**, so they are SQL tables straight away (source `files`). They can be filtered, totalled and joined with other attached files, browsed in **Tables**, downloaded, and listed in **Storage**. Screenshots are attached as images.
  - The Browser menu has Start/Close Edge, open a URL, Screenshot, and **What the model sees**. Edge uses its own profile (`PROFILE_DIR`), so logins persist. The model never types passwords. **Stop** also stops a running `paginate`/`click_all`.
  - Chat export to HTML and JSON and **Import** keep browser steps and the chat's Browser switch.
- New settings: `EDGE_DRIVER`, `EDGE_BINARY`, `PROFILE_DIR`, `HEADLESS`, `START_URL`, `EDGE_DEBUGGER`, `ALLOWED_DOMAINS`, `IGNORE_CERT_ERRORS`, `AUTO_CLICK`, `VISION`, `BROWSER_SNAPSHOT_CHARS`, `BROWSER_RESULT_ROWS`, `TABLE_MAX_ROWS`, `PAGINATE_MAX_PAGES` and others (see README → Configuration). Optional dependency: `selenium`.
- API: `GET /api/browser/status`, `POST /api/browser/start`, `/close`, `/navigate`, `/action`, `/cancel`, `GET /api/browser/screenshot`, `GET /api/browser/page`; `browser: true` in the `/api/chat` body.

### Changed
- The updater keeps the `edge-profile` folder.

## 2.12.0 — 2026-10-17

### Added
- **Updates from GitHub**, on by default. Datasquint checks `pangchi/datasquint` (`main`) at startup and every 6 hours; the sidebar says when an update is available, and **Settings → Updates** shows what's new and installs it: `git pull --ff-only` in a git checkout, otherwise download and copy over, keeping `.env`, secrets, connections, uploads, logs and token counts, with the replaced files backed up in `backups/`. Runs `pip install -r requirements.txt` if it changed, then restarts itself when started with `python app.py` (otherwise asks for a restart) and the page reloads. Settings: `UPDATES` (`on`/`check`/`off`), `UPDATE_REPO`, `UPDATE_BRANCH`, `UPDATE_RAW_URL`, `UPDATE_ZIP_URL`. API: `GET /api/update`, `POST /api/update/install`.

### Fixed
- **Settings in `.env` that were read when Datasquint loads were ignored** unless also set as environment variables: `LOG_FILE`, `LOG_LEVEL`, `LOG_MAX_MB`, `LOG_BACKUPS`, `FILE_TABLES`, `SPLIT_TABLES`, `EXCEL_RECALC` and the other file-table and formula settings, `USAGE_FILE`, and several database/SSH settings. `.env` is now read first, from the folder `app.py` is in, so it also works when Datasquint is started from another folder.
- `POST /api/usage/reset` (and the new install route) only accept JSON requests, so another website can't trigger them through your browser.

## 2.11.0 — 2026-10-17

### Added
- **Token counters.** Each answer shows the tokens it used (*in · out*, over all requests when files are read in parts). **Settings → Token usage** shows totals per model since the last reset, plus the current chat's total, with **Reset counters**. Counts come from the AI service's usage report (`stream_options.include_usage` on streamed replies); services that reject the option are detected once, and services that don't report usage are estimated (≈4 characters per token, marked ≈ and counted separately). Stopped answers count what they used. Kept on the server in `usage.json` (`USAGE_FILE`), shared by everyone using it.
- `GET /api/usage`, `POST /api/usage/reset`; `{"usage": …}` event in the `/api/chat` stream; the token count is added to each reply's log line.
- Settings: `USAGE_FILE`, `STREAM_USAGE` (`auto`/`off`).

## 2.10.1 — 2026-10-16

### Changed
- The sidebar resize edge has a visible grip: a small pill with three dots in the middle of the edge, in the theme's colours, highlighted on hover, while dragging and when focused.

## 2.10.0 — 2026-10-16

### Added
- **Adjustable sidebar width.** Drag the edge between the sidebar and the chat (180–520 px, at most half the window); double-click to reset. Keyboard: Tab to the edge, then ←/→ (Shift for bigger steps), Home to reset. Remembered per browser. Hidden on narrow screens, where the sidebar opens over the chat.

## 2.9.1 — 2026-10-14

### Changed
- Sidebar buttons reordered: Database, Storage, Export all chats, Import chats, Delete all chats, Settings, Help.

## 2.9.0 — 2026-10-13

### Added
- **Paste tables from Excel, Word, Outlook or web pages.** Pasting cells into the message box attaches them as `pasted-table-N.csv`, a table the model can query. Excel's clipboard quoting is handled (line breaks, tabs and quotes inside cells), HTML tables are used when the copied text has no tabs, and numeric columns with thousands separators load as numbers. Ctrl+Shift+V pastes as plain text; text that isn't a table pastes as before.
- **Excel reminder** when attaching .xlsx/.xlsm: values may differ from what Excel shows; CSV (or pasting the cells) is preferred for exact results. Can be hidden, and turned back on in Settings.

## 2.8.0 — 2026-10-12

### Added
- **Switch chats while a reply is being written.** Clicking another chat, **New chat**, or a chat in the Storage panel no longer waits for the reply: it keeps streaming into its own chat, and auto-run queries and the follow-up answer happen there too. Several chats can be answered at the same time.
- Sidebar marks: a spinner on chats with a reply in progress, and a dot with a bold title on chats that finished while you were elsewhere (cleared when you open them).

### Changed
- Stop (■), **Esc**, Run, Run again, Regenerate and Edit act on the chat on screen only. Deleting a chat stops its reply. Removing an attachment or exporting waits only for that chat; importing chats and deleting files in Storage still wait until no reply is in progress.

## 2.7.1 — 2026-10-12

### Fixed
- **Excel cells with line breaks (Alt+Enter) could stop a sheet from loading, or lose text.** A header with a line break, or a cell starting or ending with one, made the sheet fail to load as a table; a blank line inside a cell (two Alt+Enters) was silently removed. Blank rows between tables are now found from the Excel row numbers instead of empty lines in the intermediate CSV, so line breaks inside cells are never touched, and DuckDB retries with its single-threaded reader when quoted line breaks need it. Splitting several tables in one sheet works as before.
- Windows-style line breaks (`\r\n`) inside cells are stored as `\n`, the same with either Excel reader and in Python scripts.

## 2.7.0 — 2026-10-11

### Added
- **Formulas saved without results can be calculated without LibreOffice.** Datasquint now tries, in order: **Microsoft Excel** (Windows, if installed; hidden, macros disabled, in a copy), **LibreOffice**, then a **built-in calculator** (`pycel`) for smaller files: up to 5,000 such formulas, 20 MB and 120 s (`PYCEL_MAX_FORMULAS`, `PYCEL_MAX_MB`, `PYCEL_TIME_LIMIT`). The built-in calculator matched LibreOffice on test workbooks (IF, NA, cross-sheet SUM/AVERAGEIF, errors); formulas using functions it doesn't support stay empty and are reported per sheet.
- The chip says which calculator was used, or why none could; startup lists the calculators found.
- Python scripts embed the same calculation code (`recalc.py`), so they calculate such workbooks the same way.
- `RECALC_ENGINES`, `PYCEL_MAX_FORMULAS`, `PYCEL_MAX_MB`, `PYCEL_TIME_LIMIT` settings; `pycel` and (Windows only) `pywin32` in `requirements.txt`.

## 2.6.0 — 2026-10-11

### Added
- **Excel formulas saved without their results are now handled.** Datasquint always reads formula results, not formula text, but some programs (report exporters, Python libraries) save formulas without results, which used to load silently as empty cells. Uploads are now scanned for this (under a second even for 200 MB workbooks), and:
  - with LibreOffice installed on the server, the formulas are calculated in a copy and the values loaded; the chip notes it;
  - without it, the cells load empty, the chip shows a ⚠ note with the fix (open and save in Excel), and the AI is told those cells are empty.
- Python scripts made from such workbooks calculate them with LibreOffice too, so their tables still match Datasquint's.
- `EXCEL_RECALC` (`auto`/`off`) and `LIBREOFFICE` settings.

## 2.5.0 — 2026-10-11

### Added
- **Esc stops a reply** while it's being written, like the ■ button: the text so far is kept and a notice suggests typing "continue". With a dialog or the message editor open, Esc closes that instead. The stop button's tooltip shows the shortcut.

## 2.4.0 — 2026-10-11

### Added
- **Error log**: `logs/datasquint.log`, rotating at `LOG_MAX_MB` (10 MB) with `LOG_BACKUPS` (5) old files; the path is printed at startup. Each line has the time, level, part of the app and the client and request.
  - ERROR: unexpected errors with full tracebacks (unhandled errors now return a short JSON/text message instead of a server error page), and AI provider failures.
  - WARNING: files not loaded as tables or loaded with notes, failed queries/downloads/previews with their SQL, databases becoming unreachable (logged once per change), failed uploads and deletes, and **browser errors**: JavaScript errors plus every red error message shown to a user.
  - INFO: startup, and one line per upload, query and AI reply, plus file-loader fallbacks.
  - Passwords, API keys, bearer tokens and private keys are masked before writing.
- `LOG_FILE`, `LOG_LEVEL`, `LOG_MAX_MB`, `LOG_BACKUPS` settings; `POST /api/log`; `GET /api/logs` (from the server itself only); `logsetup.py`.

## 2.3.0 — 2026-10-11

### Fixed
- **CSV and Excel files could silently end up as truncated text instead of tables.** Any error while loading (a reader crash on one sheet, DuckDB misreading the format, a very long line) made the whole file fall back to text with no explanation. Now:
  - each Excel sheet is retried with `openpyxl` if `python-calamine` fails on it, and one unreadable sheet no longer stops the others;
  - DuckDB retries with more forgiving settings (lenient parsing, long lines up to 200 MB, every column as text, then skipping malformed lines) before giving up, and the CSVs Datasquint writes from sheets are read with their exact format instead of being guessed;
  - when a file still isn't a table, its chip shows **⚠ read as text, not a table** with the reason (password-protected workbook, `.xls` renamed to `.xlsx`, `FILE_TABLES=0`, DuckDB not installed…) and a notice appears; partial problems show as **⚠ N notes**.
- A sheet starting with a few one-cell lines ("Company XYZ", "Generated on…") no longer produces a junk one-row table; the lines name the table below.
- Blank rows in a one-column sheet are no longer loaded as empty rows.
- The startup port check reported "already in use" for up to a minute after restarting Datasquint (connections still closing). It now behaves like the web server and only refuses a port something is really using.

### Changed
- Excel error cells (`#N/A`, `#DIV/0!`…) load as empty values, and date-times at exactly midnight as dates, whichever Excel reader is used. Python scripts do the same, so their tables match Datasquint's (checked on 33 tables with both readers).
- Python scripts reuse the exact DuckDB settings each table was loaded with.

### Added
- `.xlsm` (macro-enabled) workbooks are accepted and loaded like `.xlsx`; macros are ignored.

## 2.2.0 — 2026-10-11

### Added
- **Table browser** (**Tables** in the top bar): every table the chat can use, from its databases and its attached files (including files not sent yet), with columns and types, row counts, a paged preview, sort by column, row filter, **Insert name**, **Download CSV** and **Python**. Read-only, through the same checks as queries; nothing goes to the AI.
- **Python scripts for offline use**: **Python** on each result card, **Download as Python script** in the export menu (all of a chat's queries), and **Python** in the table browser. The script rebuilds each file's tables from the original files exactly as Datasquint loaded them (same column types, sheets and split tables), connects to PostgreSQL for database queries (password from an environment variable or a prompt, never stored), runs each query and saves the results as CSV. Checked table-for-table against Datasquint on CSV, TSV, Windows-1252, multi-sheet, offset and split-table files.
- Uploads now record where each table sits in its file (Excel rows and columns, or CSV records), so scripts can rebuild split tables.
- `POST /api/tables`, `POST /api/tables/preview`, `POST /api/script`; `scriptgen.py`.

## 2.1.0 — 2026-10-10

### Added
- **Several tables in one sheet or CSV become separate tables.** Blocks separated by blank rows (stacked) or blank columns (side by side) are each loaded as their own table with their own header and column types. Before, they were merged into one misleading table: a second table's header became a data row, or side-by-side tables were glued together row by row.
  - A one-cell title line above a table (e.g. *Weekly QA report*) names it and is no longer mistaken for the header; otherwise tables are named after their first column heading.
  - Blocks that are really one table stay together: rows after a spacing blank row, a repeated header (page breaks), or a lone total line join the table above.
  - The model is told which tables came from the same sheet, and the preview labels each one.
  - Ordinary one-table files are never touched by the splitter and load exactly as before, with no extra time. A file that does hold several tables takes longer to load (about 20 s for 150 MB).
- `SPLIT_TABLES` (default `1`) and `MAX_SPLIT_TABLES` (default `30`) settings.

### Fixed
- A title row at the top of a CSV or sheet no longer becomes the column names (the real header row below it is used).

## 2.0.1 — 2026-10-10

### Changed
- **`PORT` is now in `env.example`** (with `HOST`), so the port is easy to change from 5000. Startup prints the address to open, e.g. ` * Open http://localhost:8080 in your browser`.

### Fixed
- An invalid `PORT` (not a number, or outside 1–65535) stopped with a Python traceback; it now says what's wrong.
- A port already in use now gives a clear message suggesting another `PORT`, instead of a socket error.

## 2.0.0 — 2026-10-10

### Changed
- **Renamed from pgPeek to Datasquint**, since it now works with CSV and Excel files as well as PostgreSQL. The app folder is now `datasquint`, releases are `datasquint-<version>.zip`, and exports are named `datasquint-chats-<date>.json`.

### Action needed
- Unzip into a new `datasquint` folder and copy over `.env`, `connections.sqlite`, `secret.key`, `ssh_known_hosts` and `uploads/` from the `pgpeek` folder. See [Upgrading from pgPeek](README.md#upgrading-from-pgpeek-db-helper-or-flask-chat).

### Carried over automatically
- Chats and the colour theme move from the pgPeek browser storage (`pgpeek.v1`, `pgpeek.theme`) to `datasquint.v1` and `datasquint.theme` on first load, at the same address.
- pgPeek chat exports import unchanged.

## 1.4.0 — 2026-10-10

### Added
- **`FILE_TABLES` switch in `.env`** to allow or disallow loading CSV, TSV and Excel files into DuckDB. `1` (default) keeps today's behaviour: large files up to `MAX_TABLE_UPLOAD_MB` (200 MB) that the model queries with SQL. `0` turns it off: those files are read as text like any other document, limited to `MAX_UPLOAD_MB` (20 MB). Files already loaded as tables are then sent to the model as their first lines only.
- Startup prints whether SQL over files is on, and why if it's off.

## 1.3.0 — 2026-10-09

### Added
- **Colour themes.** Settings → **Appearance**: System (follows the computer's light/dark mode), Light or Dark. **Colour**: Teal (default), Blue, Violet, Green, Amber, Rose or Graphite, each with its own sidebar tint and matching light and dark shades. Choices preview live; Save keeps them, Cancel or Esc reverts. Saved per browser in local storage (`pgpeek.theme`) and applied before the page draws, so there's no flash on load.
- The Help page follows the chosen mode and colour.

### Changed
- Your message bubbles take a light tint of the theme colour.

## 1.2.0 — 2026-10-09

### Changed
- Sidebar items now have icons: Settings (gear), Database (cylinder), Export all chats, Import chats, Storage, Delete all chats (bin) and Help.

## 1.1.1 — 2026-10-09

### Changed
- **`.env.example` is now `env.example`.** Files starting with a dot are hidden on macOS and Linux and can be left out when uploading or copying a folder, so the settings template could go missing from repositories. Setup is now `cp env.example .env`. Existing `.env` files are unaffected.

### Docs
- README notes that `.gitignore` is also a hidden file and how to check it reaches GitHub.

## 1.1.0 — 2026-10-09

### Added
- **Download button on code blocks** in replies, next to Copy (and Run for SQL). Files get the extension of the block's language: `.py`, `.csv`, `.sql`, `.js`, `.json`, `.sh`, `.ps1`, `.html` and about 50 more, `.txt` otherwise. If the reply names the file, either on the block's first line (`# file: report.py`, `-- totals.sql`) or in `code` just above it (*Save this as `summary.csv`*), that name is used; otherwise the chat title, numbered when a reply has several unnamed blocks of the same type. CSV downloads start with a UTF-8 marker so Excel shows accents correctly.
- **Download CSV on query result cards.** It re-runs the card's query (read-only, same checks) and downloads the **full** result, not just the rows shown, up to `DB_EXPORT_MAX_ROWS` (default 100,000), with long text kept in full. If the limit cuts the result off, a message says so. Works for databases and attached files.
- `POST /api/db/export`.

### Changed
- Run, Download and Copy share one toolbar in the corner of code blocks; highlighted code no longer has extra padding.

## 1.0.1 — 2026-10-09

### Fixed
- **Storage: "(incomplete upload)" left behind after Delete all unused.** Deletes used to fail silently: if a file couldn't be removed, the panel said nothing and the half-deleted folder reappeared as *(incomplete upload)*. Deletes now retry for a few seconds (Windows often holds new files open briefly for antivirus or indexing), clear read-only flags, and report anything they still can't remove with the reason and the files left.
- **Deleting an upload while it was still being processed** crashed that upload and could leave a broken folder. Uploads in progress now show in Storage as *still uploading…* and can't be deleted until they finish; **Delete all unused** skips them.
- **Empty leftover folders** (no files at all, older than a minute) are cleaned up automatically when Storage is opened.
- An upload whose folder disappears mid-way now returns a clear error instead of a server error.
- Files that are already gone count as deleted, so Storage no longer keeps showing them.
- The Storage list reloads from the server after deleting, so it always shows what's really there.
- `DELETE /api/files/<id>` returns 409 with the reason when a file can't be deleted.

## 1.0.0 — 2026-10-09

First numbered release. Earlier unnumbered builds were called *flask-chat* and then *DB Helper*.

### Chat
- ChatGPT-style web UI for any OpenAI-compatible API (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` in `.env`), with streaming replies, Stop, model picker, Markdown and code highlighting.
- Inline multi-line editing of sent messages (Ctrl+Enter to resend, Esc to cancel); the message box grows to 40% of the window.
- Chats saved in the browser; export one chat as JSON or as a standalone HTML page, export all chats as JSON, and import with duplicate detection.
- Requests over `MAX_SEND_CHARS` are read in parts and combined automatically.

### Databases
- Multiple saved PostgreSQL connections (SQLite store, passwords and keys encrypted), chosen per chat, with notes for the model.
- Strictly read-only queries: statement allow-list, single prepared statement, read-only transaction, timeout and row cap.
- SSH tunnels with password, key file (with **Browse…** picker), pasted key or agent; host-key checking.
- Run / auto-run of the model's SQL, result cards with Copy CSV and Run again, `-- db: name` routing across several sources.

### Files
- Attach images, PDF, Word, Excel, CSV, JSON, text, code and zip archives (drag, paste or paperclip), with upload progress.
- CSV, TSV and Excel files become SQL tables (DuckDB, read-only sandbox), up to `MAX_TABLE_UPLOAD_MB` (200 MB); the model gets a preview and queries the full data. `python-calamine` for fast Excel loading.
- Remove a file from a sent message, delete chats together with their files, and a **Storage** panel listing every upload with size, date and the chats using it.

### Help
- `USER_GUIDE.md` for everyday users, built into the app under **Help** (`/guide`).

### Fixes and hardening included in this release
- Flask debug mode is now off by default (`FLASK_DEBUG=1` to enable). It had exposed the in-browser debugger and restarted the server whenever a `.zip` was uploaded.
- Excel workbooks with an empty sheet no longer fail to upload.
- Re-importing your own export of a chat with query results no longer creates an "(imported)" duplicate.
- Saving chats reports when browser storage is full instead of failing silently.
