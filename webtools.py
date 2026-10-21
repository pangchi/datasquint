"""Browser data source for Datasquint: the model reads and drives a Microsoft Edge or Google Chrome window (Selenium).

Works like SQL: the model writes an ```action code block, the app runs it (automatically for actions
that only read the page, after a click on Run — or automatically with Auto-click — for actions that
change it), and the result goes back to the model as the next message.

Tables extracted from pages are saved as CSV files attached to the chat, so they become SQL tables like
any uploaded CSV (see app.py: browser_action). The current page is put in the system prompt of every
request while the browser is on for a chat, so the model always sees what's on screen now.

Needs `pip install selenium` (optional, like psycopg for PostgreSQL).
"""
import ast
import base64
import csv
import io
import json
import os
import re
import threading
import time

import logsetup

log = logsetup.child("browser")

# BROWSER_ENABLED=0 turns the whole feature off: selenium isn't even imported, the Browser switch is hidden,
# the /api/browser routes refuse and chats get no browser instructions.
ENABLED = os.getenv("BROWSER_ENABLED", "1").strip().lower() not in ("0", "off", "false", "no")


class _Off(Exception):
    pass


browser, _import_error = None, None
BrowserError = _Off
if ENABLED:
    try:
        from browser import BrowserError, browser
    except ImportError as e:      # selenium not installed: the Browser switch says how to add it
        _import_error = e


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


SNAPSHOT_CHARS = _int("BROWSER_SNAPSHOT_CHARS", "8000")      # page text per request; long pages come in parts
OBS_TABLE_ROWS = _int("BROWSER_RESULT_ROWS", "30")           # rows of an extracted table shown to the model
PAGINATE_MAX_PAGES = _int("PAGINATE_MAX_PAGES", "200")
VISION = os.getenv("VISION", "0").strip() == "1"

# Actions that only read the page run without asking; the rest need Run (or Auto-click).
READ_ACTIONS = {"snapshot", "extract_table", "extract_text", "find", "tabs", "frame", "switch_tab", "scroll", "wait"}
ACT_ACTIONS = {"navigate", "click", "type", "select", "press", "back", "paginate", "click_all"}
if VISION:
    READ_ACTIONS.add("screenshot")
ALL_ACTIONS = READ_ACTIONS | ACT_ACTIONS

cancel_event = threading.Event()      # set by the Stop button; checked between pages / clicks


def available():
    return browser is not None


def off_reason():
    if browser is not None:
        return None
    if not ENABLED:
        return "The browser is turned off on this server (BROWSER_ENABLED=0 in .env)."
    return f"Browser support isn't installed on the server ({_import_error}). Run: pip install selenium"


def status_line():
    """For the start-up banner."""
    if not ENABLED:
        return "off (BROWSER_ENABLED=0)"
    if browser is None:
        return "not available: pip install selenium"
    from browser import BROWSERS, default_kind
    return f"on, {BROWSERS[default_kind()]['name']} by default (Edge or Chrome, per chat)"


# ---------------------------------------------------------------------------------------------- parsing
def parse(body):
    """Parse one action block's JSON. Returns (action, error)."""
    body = (body or "").strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        try:   # small local models: single quotes, True/False, trailing commas
            fixed = re.sub(r",\s*([}\]])", r"\1", body)
            fixed = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", re.sub(r"\bnull\b", "None", fixed)))
            obj = ast.literal_eval(fixed)
        except (ValueError, SyntaxError):
            return None, 'The action block isn\'t valid JSON. Write it like {"action": "click", "id": 12}.'
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if not isinstance(obj, dict) or not obj.get("action"):
        return None, 'The action block needs an "action" field, e.g. {"action": "snapshot"}.'
    obj["action"] = str(obj["action"]).strip().lower()
    if obj["action"] not in ALL_ACTIONS:
        return None, f"Unknown action {obj['action']!r}. Use one of: {', '.join(sorted(ALL_ACTIONS))}."
    return obj, None


def describe(a):
    """Short text for the result card and the log."""
    x = a["action"]
    if x == "click":
        return f"Click [{a.get('id')}]"
    if x == "navigate":
        return f"Open {a.get('url')}"
    if x == "type":
        return f"Type {a.get('text')!r} into [{a.get('id')}]" + (" + Enter" if a.get("enter") else "")
    if x == "select":
        return f"Choose {a.get('option')!r} in [{a.get('id')}]"
    if x == "paginate":
        return f"Extract table {a.get('table', 0)} from every page (clicking {a.get('next', 'Next')!r})"
    if x == "click_all":
        return f"Click every {a.get('text')!r}"
    if x == "extract_table":
        return f"Extract table {a.get('table', 0)}"
    if x == "snapshot":
        return "Read the page" + (f" (part {a['part']})" if a.get("part") else "")
    return x.replace("_", " ").capitalize() + " " + json.dumps({k: v for k, v in a.items() if k != "action"})


# ---------------------------------------------------------------------------------------------- page text
def _header(snap):
    extra = []
    if snap.get("tab"):
        extra.append(f"tab {snap['tab']}")
    if snap.get("frame"):
        extra.append(f"inside frame {snap['frame']}")
    extra.append(f"{snap.get('tables', 0)} tables")
    if snap.get("frames"):
        extra.append(f"{snap['frames']} frames")
    sc = snap.get("scroll") or {}
    if sc.get("max"):
        extra.append(f"scrolled {sc.get('y', 0)}/{sc['max']}px")
    return f"Page: {snap.get('title') or '(no title)'} — {snap.get('url')} ({', '.join(extra)})"


def page_text(part=1):
    snap = browser.snapshot()
    text = snap["text"]
    parts = max(1, -(-len(text) // SNAPSHOT_CHARS))
    part = min(max(1, int(part or 1)), parts)
    chunk = text[(part - 1) * SNAPSHOT_CHARS: part * SNAPSHOT_CHARS]
    note = ""
    if parts > 1:
        note = f"\n\n(part {part} of {parts}" + (f' — {{"action": "snapshot", "part": {part + 1}}} for the next part)'
                                                 if part < parts else ")")
    return _header(snap) + "\n---\n" + (chunk or "(page has no visible text)") + note, len(text)


# ---------------------------------------------------------------------------------------------- tables -> CSV
_THOUSANDS = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")


def _clean_cell(v):
    s = str(v).strip()
    return s.replace(",", "") if _THOUSANDS.fullmatch(s) else s     # 1,234 -> 1234 so SQL sees a number


def table_csv(head, rows):
    """CSV bytes with unique, non-empty column names (DuckDB needs them) and even row widths."""
    width = max([len(head)] + [len(r) for r in rows] or [0]) or 1
    names, seen = [], {}
    for i in range(width):
        n = re.sub(r"\s+", " ", str(head[i]).strip()) if i < len(head) else ""
        n = n or f"col{i + 1}"
        k = n.lower()
        seen[k] = seen.get(k, 0) + 1
        names.append(n if seen[k] == 1 else f"{n}_{seen[k]}")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(names)
    for r in rows:
        w.writerow([_clean_cell(c) for c in r] + [""] * (width - len(r)))
    return buf.getvalue().encode("utf-8")


def csv_name(save_as, idx):
    base = re.sub(r"[^\w\-]+", "_", str(save_as or "").strip()).strip("_")[:60] or f"web_table{idx}"
    return base + ".csv"


def _rows_text(head, rows, limit):
    lines = []
    if head:
        lines.append("| " + " | ".join(str(h) for h in head) + " |")
    for r in rows[:limit]:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    if len(rows) > limit:
        lines.append(f"… {len(rows) - limit} more rows (all of them are in the file)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- running
def execute(a):
    """Run one action. Returns {"text", "csv": (name, bytes) | None, "png": bytes | None}.

    Raises BrowserError for things the model should fix (no such element, bad URL, …).
    """
    x = a["action"]
    cancel_event.clear()
    out = {"text": "", "csv": None, "png": None}
    if x == "snapshot":
        out["text"] = page_text(a.get("part", 1))[0]
        return out
    if x in ("extract_table", "paginate"):
        idx = int(a.get("table", 0) or 0)
        name = csv_name(a.get("save_as"), idx)
        if x == "extract_table":
            data = browser.table(idx)
            head, rows = data["head"], data["rows"]
            note = f"Table {idx}: {len(rows)} rows" + (f" (page has {data['total']}; TABLE_MAX_ROWS limit)"
                                                       if data["total"] > len(rows) else "")
        else:
            pages, head, rows = [], [], []

            def on_page(n, data):
                nonlocal head
                head = head or data["head"]
                rows.extend(data["rows"])

            res = browser.paginate(idx, a.get("next", "Next"),
                                   min(int(a.get("max_pages", 20) or 20), PAGINATE_MAX_PAGES),
                                   on_page=on_page, should_stop=cancel_event.is_set)
            note = f"Table {idx} over {res['pages']} pages: {len(rows)} rows, stopped because {res['stopped']}"
        if not rows:
            out["text"] = note + ". The table is empty, so no file was saved."
            return out
        out["csv"] = (name, table_csv(head, rows))
        out["text"] = note + ".\n" + _rows_text(head, rows, OBS_TABLE_ROWS)
        return out
    if x == "screenshot":
        out["png"] = base64.b64decode(browser.screenshot_b64())
        out["text"] = "Screenshot attached."
        return out
    if x == "extract_text":
        txt = browser.text(a.get("selector"))
        out["text"] = (txt[:SNAPSHOT_CHARS] + (f"\n… ({len(txt) - SNAPSHOT_CHARS:,} more characters)"
                                               if len(txt) > SNAPSHOT_CHARS else "")) or "(empty)"
        return out
    if x == "find":
        hits = browser.find(a.get("text", ""), exact=bool(a.get("exact")))
        out["text"] = ("Matches:\n" + "\n".join(f"[{h['id']}:{h['kind']}{' disabled' if h['disabled'] else ''} {h['label']}]"
                                                for h in hits)) if hits else f"No clickable element contains {a.get('text')!r}."
        return out
    if x == "tabs":
        out["text"] = "Tabs:\n" + "\n".join(f"{t['index']}: {t['title']} — {t['url']}" + (" (current)" if t["current"] else "")
                                            for t in browser.tabs())
        return out
    if x == "wait":
        secs = min(float(a.get("seconds", 2) or 2), 30.0)
        cancel_event.wait(secs)
        out["text"] = f"Waited {secs:g} s."
        return out
    # actions that change the page: say what happened; the new page arrives in the next system prompt
    if x == "navigate":
        out["text"] = browser.navigate(a.get("url", ""))
    elif x == "click":
        out["text"] = browser.click(a.get("id"))
    elif x == "type":
        out["text"] = browser.type(a.get("id"), a.get("text", ""), enter=bool(a.get("enter")))
    elif x == "select":
        out["text"] = browser.select(a.get("id"), a.get("option", ""))
    elif x == "press":
        out["text"] = browser.press(a.get("key", "Enter"))
    elif x == "scroll":
        out["text"] = browser.scroll(a.get("direction", "down"), a.get("id"))
    elif x == "back":
        out["text"] = browser.back()
    elif x == "frame":
        out["text"] = browser.frame(a.get("index", "top"))
    elif x == "switch_tab":
        out["text"] = browser.switch_tab(a.get("index", 0))
    elif x == "click_all":
        r = browser.click_all(a.get("text", ""), int(a.get("max", 50) or 50), exact=bool(a.get("exact")),
                              should_stop=cancel_event.is_set)
        out["text"] = f"Clicked {r['clicked']} controls matching {a.get('text')!r}."
    return out


# ---------------------------------------------------------------------------------------------- prompt
INSTRUCTIONS = """You can also read and operate a web browser (BROWSER_NAME) for the user (often internal equipment, production or report pages). The page open now is shown below under "Current page".

To use the browser, write ONE ```action code block containing JSON, then stop and wait: the app runs it and sends you the result. Don't put an action block and a SQL block in the same reply. Example:
```action
{"action": "click", "id": 12}
```

Actions:
- {"action": "snapshot"} read the page again; "part": 2 for the next part of a long page
- {"action": "navigate", "url": "http://..."}
- {"action": "click", "id": 12}
- {"action": "type", "id": 7, "text": "LOT-123", "enter": true}
- {"action": "select", "id": 9, "option": "Last 7 days"}  (dropdowns)
- {"action": "press", "key": "Enter"}  (Enter, Escape, Tab, Page_Down)
- {"action": "scroll", "direction": "down"}  (down, up, top, bottom) or {"action": "scroll", "id": 30}
- {"action": "back"}
- {"action": "find", "text": "Export"}  clickable elements whose text contains this
- {"action": "extract_table", "table": 0, "save_as": "alarms"}  saves ALL rows of table 0 as alarms.csv, attached to this chat, which then becomes an SQL table you can query like other file tables
- {"action": "paginate", "table": 0, "next": "Next", "max_pages": 20, "save_as": "alarms"}  extracts the table on every page by clicking the "next" control until the last page, into one CSV
- {"action": "click_all", "text": "Show more", "max": 50}  clicks every control with that text (expand rows, load more)
- {"action": "extract_text", "selector": "#summary"}  text of elements matching a CSS selector
- {"action": "frame", "index": 0} read inside frame 0 ("top" to go back); {"action": "tabs"}; {"action": "switch_tab", "index": 1}
- {"action": "wait", "seconds": 3}%s

How pages look: text in reading order; interactive elements appear as [12:button Next] or [5:link Lot 4471] — use the number as "id". Tables appear as <<table 0: 240 rows x 6 cols>> with their first rows; use extract_table or paginate to get all rows, then SQL to filter, sort or total them. Ids can change when the page changes: only use ids from the current page below or the latest result.

Rules:
- If the user asks about what's displayed, answer from the current page; no action is needed.
- Click, type, select or navigate when the user asks for more data or to go somewhere; you may take several steps. For a table spread over pages, use paginate rather than clicking next yourself.
- Never type passwords. If a login page appears, ask the user to log in in the BROWSER_NAME window and tell you when done.
- Don't press buttons that change things (save, submit, delete, approve, acknowledge, start, stop, order) unless the user explicitly asked for exactly that.
- Answer only from what the page and results show. Never invent values, rows or ids."""

_SCREENSHOT_LINE = '\n- {"action": "screenshot"}  attaches an image of the window (charts, canvases, colours)'


def system_section():
    """Browser instructions plus the page open now, for the system prompt ("" when turned off)."""
    if not ENABLED:
        return ""
    head = INSTRUCTIONS % (_SCREENSHOT_LINE if VISION else "")
    if browser is not None:
        from browser import BROWSERS
        head = head.replace("BROWSER_NAME", BROWSERS[browser.kind]["name"])
    if browser is None:
        return head + "\n\n=== Current page ===\n(Browser support isn't installed on the server. Tell the user.)"
    if not browser.alive():
        return head + ("\n\n=== Current page ===\n(The browser isn't open. Navigate to a URL to start it, "
                       "or ask the user to start it and open the page.)")
    try:
        t0 = time.time()
        text, _ = page_text()
        log.debug("Page read for the prompt in %.1f s", time.time() - t0)
        return head + "\n\n=== Current page ===\n" + text
    except Exception as e:
        log.warning("Couldn't read the page for the prompt: %s", e)
        return head + f"\n\n=== Current page ===\n(Couldn't read the page: {str(e).strip().splitlines()[0][:200]})"
