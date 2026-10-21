"""One Microsoft Edge or Google Chrome session driven by Selenium, plus the page-reading JavaScript.

Everything the model can do to the page goes through Browser: it reads the page as an outline with
numbered elements ([12:button Next]), pulls whole tables, clicks/types by those numbers, and runs two
macros that save model round-trips: paginate (extract a table, click Next, repeat) and click_all
(click every "Show more" / expand button).

All calls take a lock, so the Flask threads and the agent loop never drive the browser at once.
"""
import base64
import hashlib
import os
import re
import threading
import time

from selenium import webdriver
from selenium.common.exceptions import (ElementClickInterceptedException, ElementNotInteractableException,
                                        JavascriptException, NoSuchElementException, NoSuchFrameException,
                                        StaleElementReferenceException, TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select


class BrowserError(Exception):
    """Something the model or user asked for can't be done; the message is shown to them as is."""


# ---------------------------------------------------------------------------------------------- page JS
# Shared helpers, prepended to the scripts below. Elements get a stable number in data-psq the first
# time they're seen; numbers survive re-reads of the same page and restart after a navigation.
_JS_HELPERS = r"""
const INTER = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
  '[role=menuitem],[role=option],[role=checkbox],[role=radio],[role=switch],[role=treeitem],' +
  '[onclick],[contenteditable=true]';
const TABLES = 'table,[role=grid],[role=table],[role=treegrid]';
const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD','META','LINK']);
if (window.__psqNext === undefined) window.__psqNext = 1;
function psqId(el) {
  if (!el.dataset.psq) el.dataset.psq = String(window.__psqNext++);
  return el.dataset.psq;
}
function visible(el) {
  if (!el.getClientRects().length) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0;
}
function clean(t) { return (t || '').replace(/\s+/g, ' ').trim(); }
function cut(t, n) { t = clean(t); return t.length > n ? t.slice(0, n - 1) + '…' : t; }
function isInter(el) {
  if (!el.matches || !el.matches(INTER)) return false;
  if (el.matches('[tabindex="-1"]:not(a,button,input,select,textarea)')) return false;
  return true;
}
function disabled(el) {
  return !!(el.disabled || el.getAttribute('aria-disabled') === 'true' ||
            /(^|\s)disabled(\s|$)/i.test(el.className && el.className.baseVal === undefined ? el.className : ''));
}
function kind(el) {
  const tag = el.tagName.toLowerCase(), role = el.getAttribute('role');
  if (tag === 'input') return 'input:' + (el.type || 'text');
  if (tag === 'a') return 'link';
  if (role) return role;
  return tag;
}
function label(el) {
  const tag = el.tagName;
  let t = el.getAttribute('aria-label') || '';
  if (!t && (tag === 'INPUT' || tag === 'TEXTAREA')) {
    const lab = el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    t = (lab && lab.innerText) || el.placeholder || el.name || '';
    if (el.type === 'checkbox' || el.type === 'radio') t += el.checked ? ' (checked)' : ' (unchecked)';
    else if (el.type !== 'password' && el.value) t += ' = "' + cut(el.value, 40) + '"';
  } else if (!t && tag === 'SELECT') {
    const o = el.options[el.selectedIndex];
    t = (el.name || '') + ' = "' + (o ? clean(o.text) : '') + '" options: ' +
        Array.from(el.options).slice(0, 15).map(o => clean(o.text)).join(' | ') +
        (el.options.length > 15 ? ' | …(' + el.options.length + ')' : '');
  }
  if (!t) t = el.innerText || el.value || el.title || '';
  if (!clean(t)) { const img = el.querySelector && el.querySelector('img[alt]'); if (img) t = img.alt; }
  if (!clean(t)) t = el.title || el.getAttribute('name') || (el.href ? el.getAttribute('href') : '') || '';
  return cut(t, 80);
}
function marker(el) {
  return '[' + psqId(el) + ':' + kind(el) + (disabled(el) ? ' disabled' : '') + ' ' + label(el) + ']';
}
// Text of a cell/fragment with interactive children shown as markers (withIds) or plain.
function inlineText(node, withIds) {
  let out = '';
  (function walk(n) {
    if (n.nodeType === 3) { out += n.nodeValue; return; }
    if (n.nodeType !== 1 || SKIP.has(n.tagName.toUpperCase())) return;
    if (!visible(n)) return;
    if (withIds && isInter(n)) { out += ' ' + marker(n) + ' '; return; }
    if (n.tagName === 'BR') out += ' ';
    for (const c of n.childNodes) walk(c);
    if (n.shadowRoot) for (const c of n.shadowRoot.childNodes) walk(c);
  })(node);
  return clean(out);
}
function allTables() {
  const list = Array.from(document.querySelectorAll(TABLES));
  // a <table> inside a role=grid (or the reverse) is the same table: keep the outermost
  return list.filter(t => !list.some(o => o !== t && o.contains(t)));
}
function tableRows(t) {
  if (t.tagName === 'TABLE') return Array.from(t.rows).filter(r => r.closest('table') === t);
  return Array.from(t.querySelectorAll('[role=row]'));
}
function rowCells(r) {
  if (r.tagName === 'TR') return Array.from(r.cells);
  return Array.from(r.querySelectorAll('[role=cell],[role=gridcell],[role=columnheader],[role=rowheader]'));
}
function isHeaderRow(r) {
  const cells = rowCells(r);
  return cells.length > 0 && cells.every(c => c.tagName === 'TH' || /columnheader/.test(c.getAttribute('role') || ''));
}
function readTable(t, withIds, maxRows) {
  let rows = tableRows(t).filter(visible);
  let head = [];
  const thead = t.tagName === 'TABLE' && t.tHead;
  if (thead && thead.rows.length) {
    const hr = thead.rows[thead.rows.length - 1];
    head = rowCells(hr).flatMap(c => Array(c.colSpan || 1).fill(inlineText(c, false)));
    rows = rows.filter(r => !thead.contains(r));
  } else if (rows.length && isHeaderRow(rows[0])) {
    head = rowCells(rows[0]).flatMap(c => Array(c.colSpan || 1).fill(inlineText(c, false)));
    rows = rows.slice(1);
  }
  const total = rows.length;
  const body = rows.slice(0, maxRows).map(r => {
    const cells = rowCells(r).flatMap(c => {
      const v = inlineText(c, withIds);
      return [v].concat(Array(Math.max(0, (c.colSpan || 1) - 1)).fill(''));
    });
    if (withIds && isInter(r)) cells.unshift(marker(r));
    return cells;
  });
  return {head: head, rows: body, total: total};
}
"""

# Page outline: text in reading order, interactive elements as markers, tables as a short sample.
_JS_SNAPSHOT = _JS_HELPERS + r"""
const maxChars = arguments[0], sampleRows = arguments[1];
const tables = allTables();
const out = [];
let len = 0;
function emit(s) { if (len < maxChars) { out.push(s); len += s.length; } }
const BLOCK = /^(P|DIV|SECTION|ARTICLE|MAIN|ASIDE|NAV|HEADER|FOOTER|UL|OL|LI|DL|DT|DD|FORM|FIELDSET|TR|PRE|BLOCKQUOTE|FIGURE|LABEL)$/;
let frames = 0;
function walk(n) {
  if (len >= maxChars) return;
  if (n.nodeType === 3) { const t = clean(n.nodeValue); if (t) emit(t + ' '); return; }
  if (n.nodeType !== 1 || SKIP.has(n.tagName.toUpperCase())) return;
  if (n.tagName === 'IFRAME' || n.tagName === 'FRAME') {
    emit('\n<<frame ' + (frames++) + ': ' + cut(n.title || n.name || n.src || '', 80) + ' — use {"action":"frame","index":' + (frames - 1) + '} to read it>>\n');
    return;
  }
  if (!visible(n)) return;
  const ti = tables.indexOf(n);
  if (ti >= 0) {
    const d = readTable(n, true, sampleRows);
    const cols = Math.max(d.head.length, ...d.rows.map(r => r.length), 0);
    emit('\n<<table ' + ti + ': ' + d.total + ' rows x ' + cols + ' cols' +
         (d.total > d.rows.length ? ', first ' + d.rows.length + ' shown — use extract_table for all' : '') + '>>\n');
    if (d.head.length) emit('| ' + d.head.join(' | ') + ' |\n');
    for (const r of d.rows) emit('| ' + r.join(' | ') + ' |\n');
    emit('<</table ' + ti + '>>\n');
    return;
  }
  if (isInter(n)) { emit(marker(n) + ' '); return; }
  const h = /^H([1-6])$/.exec(n.tagName);
  if (h) { emit('\n' + '#'.repeat(+h[1]) + ' ' + cut(n.innerText, 200) + '\n'); return; }
  const block = BLOCK.test(n.tagName);
  if (block) emit('\n');
  if (n.tagName === 'BR') emit('\n');
  for (const c of n.childNodes) walk(c);
  if (n.shadowRoot) for (const c of n.shadowRoot.childNodes) walk(c);
  if (block) emit('\n');
}
if (document.body) walk(document.body);
const text = out.join('').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
return {title: document.title, url: location.href, text: text, truncated: len >= maxChars,
        tables: tables.length, frames: frames,
        scroll: {y: Math.round(scrollY), max: Math.max(0, document.documentElement.scrollHeight - innerHeight)}};
"""

_JS_TABLE = _JS_HELPERS + r"""
const idx = arguments[0], withIds = arguments[1], maxRows = arguments[2];
const tables = allTables();
if (idx < 0 || idx >= tables.length) return {error: 'no table ' + idx + ' (page has ' + tables.length + ')'};
return readTable(tables[idx], withIds, maxRows);
"""

_JS_FIND = _JS_HELPERS + r"""
const q = arguments[0].toLowerCase(), exact = arguments[1], limit = arguments[2];
const res = [];
const seen = new Set();
function consider(el) {
  if (seen.has(el) || !visible(el)) return;
  seen.add(el);
  const texts = [el.innerText, el.value, el.getAttribute('aria-label'), el.title].map(t => clean(t).toLowerCase());
  const hit = exact ? texts.some(t => t === q) : texts.some(t => t && t.includes(q));
  if (hit) res.push({id: psqId(el), kind: kind(el), label: label(el), disabled: disabled(el)});
}
(function scan(root) {
  for (const el of root.querySelectorAll('*')) {
    if (isInter(el)) consider(el);
    if (el.shadowRoot) scan(el.shadowRoot);
    if (res.length >= limit) return;
  }
})(document);
return res;
"""

_JS_BY_ID = r"""
const id = arguments[0];
let found = null;
(function scan(root) {
  const el = root.querySelector('[data-psq="' + id + '"]');
  if (el) { found = el; return; }
  for (const h of root.querySelectorAll('*')) if (h.shadowRoot && !found) scan(h.shadowRoot);
})(document);
return found;
"""

_JS_SETTLE = r"""
const done = arguments[arguments.length - 1], quiet = arguments[0], max = arguments[1];
let last = Date.now();
const obs = new MutationObserver(() => { last = Date.now(); });
obs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
const start = Date.now();
(function tick() {
  const now = Date.now();
  if ((document.readyState === 'complete' && now - last >= quiet) || now - start > max) { obs.disconnect(); done(true); }
  else setTimeout(tick, 100);
})();
"""


# ---------------------------------------------------------------------------------------------- browser
def _env_bool(name, default=False):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


BROWSERS = {
    "edge": {"name": "Edge", "env": "EDGE", "driver": "msedgedriver", "profile_env": "PROFILE_DIR",
             "profile": "./edge-profile", "version_page": "edge://version"},
    "chrome": {"name": "Chrome", "env": "CHROME", "driver": "chromedriver", "profile_env": "CHROME_PROFILE_DIR",
               "profile": "./chrome-profile", "version_page": "chrome://version"},
}


def _mark_clean_exit(profile):
    """Mark the profile as shut down cleanly, so the browser doesn't offer to restore (or reopen) the tabs of
    a session that ended uncleanly, e.g. when Datasquint was stopped with the browser open."""
    import json
    pref = os.path.join(profile, "Default", "Preferences")
    try:
        with open(pref, encoding="utf-8") as fh:
            data = json.load(fh)
        prof = data.setdefault("profile", {})
        if prof.get("exit_type") == "Normal" and prof.get("exited_cleanly", True):
            return
        prof["exit_type"], prof["exited_cleanly"] = "Normal", True
        with open(pref, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except (OSError, ValueError):
        pass        # new profile, or the browser changed its format: nothing to fix


def default_kind():
    k = os.getenv("BROWSER", "edge").strip().lower()
    return k if k in BROWSERS else "edge"


def _start_error(e, kind, driver_path, attach):
    """Selenium's start-up errors, with what to do about the common ones."""
    b = BROWSERS[kind]
    text = str(e)
    first = text.strip().splitlines()[0] if text.strip() else type(e).__name__
    low = text.lower()
    if "user data directory is already in use" in low or "instance exited" in low or "devtoolsactiveport" in low:
        hint = (f" Another {b['name']} window is using its profile folder ({b['profile_env']}); close it or set a "
                f"different {b['profile_env']}.")
    elif "only supports" in low and "version" in low:
        hint = (f" {b['name']} updated: get the {b['driver']} that matches it ({b['version_page']}) and set "
                f"{b['env']}_DRIVER, or remove {b['env']}_DRIVER to let Selenium fetch it (needs internet).")
    elif ("cannot find" in low and "binary" in low) or ("no such file" in low and not driver_path):
        hint = (f" {b['name']} wasn't found. Install it, or set {b['env']}_BINARY to its program file.")
    elif attach and ("cannot connect" in low or "unable to discover" in low or "unreachable" in low):
        hint = (f" Nothing is listening at {attach}. Start {b['name']} with --remote-debugging-port first, "
                f"or clear {b['env']}_DEBUGGER.")
    elif not driver_path and ("unable to obtain" in low or "driver" in low):
        hint = (f" Selenium couldn't get {b['driver']} (offline?). Download the {b['driver']} that matches your "
                f"{b['name']} version ({b['version_page']}) and set {b['env']}_DRIVER to it in .env.")
    else:
        hint = ""
    return f"Couldn't start {b['name']}: {first}.{hint}"


class Browser:
    def __init__(self):
        self.lock = threading.RLock()
        self.driver = None
        self.frame_path = []          # indexes of nested frames we're inside, [] = top page
        self.mode = None              # "launched" or "attached"
        self.kind = default_kind()    # "edge" or "chrome": the one running, or the next to start
        self.hidden = set()           # handles that aren't real tabs (the New Tab page footer)

    # ----------------------------------------------------------------- lifecycle
    def status(self):
        with self.lock:
            if not self.alive():
                return {"running": False}
            base = {"running": True, "mode": self.mode, "browser": self.kind, "browser_name": BROWSERS[self.kind]["name"]}
            try:
                return {**base, "url": self.driver.current_url, "title": self.driver.title,
                        "tabs": len(self._tabs()), "frame": self.frame_path}
            except WebDriverException as e:
                return {**base, "error": str(e).splitlines()[0]}

    def alive(self):
        if not self.driver:
            return False
        try:
            self.driver.window_handles
            return True
        except WebDriverException:
            self.driver = None
            return False

    def start(self, headless=None, attach=None, kind=None):
        """Launch Edge or Chrome (or attach to one started with --remote-debugging-port). Reuses a live session;
        asking for the other browser while one runs is refused (close it first)."""
        with self.lock:
            kind = (kind or "").strip().lower() or (self.kind if self.alive() else default_kind())
            if kind not in BROWSERS:
                raise BrowserError(f"Unknown browser {kind!r}: use edge or chrome.")
            if self.alive():
                if kind != self.kind:
                    raise BrowserError(f"{BROWSERS[self.kind]['name']} is already open. Close it first to use "
                                       f"{BROWSERS[kind]['name']}.")
                return self.status()
            b = BROWSERS[kind]
            attach = (attach if attach is not None else os.getenv(b["env"] + "_DEBUGGER", "")).strip()
            headless = _env_bool("HEADLESS") if headless is None else headless
            if kind == "chrome":
                opts, Service, Driver = webdriver.ChromeOptions(), webdriver.ChromeService, webdriver.Chrome
            else:
                opts, Service, Driver = webdriver.EdgeOptions(), webdriver.EdgeService, webdriver.Edge
            binary = os.getenv(b["env"] + "_BINARY")
            driver_path = os.getenv(b["env"] + "_DRIVER")
            profile = ""
            if attach:
                opts.add_experimental_option("debuggerAddress", attach)
            else:
                if binary:
                    opts.binary_location = binary
                if headless:
                    opts.add_argument("--headless=new")
                opts.add_argument("--window-size=" + os.getenv("WINDOW_SIZE", "1400,950"))
                # each browser keeps its own profile: Edge and Chrome can't share one
                profile = os.getenv(b["profile_env"], b["profile"]).strip()
                if profile and profile.lower() != "off":
                    profile = os.path.expanduser(profile)
                    if not os.path.isabs(profile):      # relative to the app folder, not the current one
                        profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), profile)
                    opts.add_argument("--user-data-dir=" + profile)
                    _mark_clean_exit(profile)
                for arg in filter(None, os.getenv("BROWSER_ARGS", "").split()):
                    opts.add_argument(arg)
                if _env_bool("IGNORE_CERT_ERRORS"):
                    opts.add_argument("--ignore-certificate-errors")
                opts.add_argument("--no-first-run")
                opts.add_argument("--no-default-browser-check")
                # after an unclean stop, don't show "Restore pages?" or reopen the old tabs
                opts.add_argument("--hide-crash-restore-bubble")
                opts.add_argument("--disable-session-crashed-bubble")
            service_args = os.getenv("DRIVER_ARGS", "").split() or None
            service = (Service(executable_path=driver_path, service_args=service_args) if driver_path
                       else Service(service_args=service_args))
            try:
                self.driver = Driver(options=opts, service=service)
            except WebDriverException as e:
                raise BrowserError(_start_error(e, kind, driver_path, attach))
            self.kind = kind
            if not attach:
                self._fresh_tab()
            self.driver.set_page_load_timeout(float(os.getenv("PAGE_LOAD_TIMEOUT", "60")))
            self.frame_path = []
            self.mode = "attached" if attach else "launched"
            return self.status()

    START_PAGES = ("chrome://newtab", "chrome://new-tab-page", "chrome://newtab-footer", "edge://newtab",
                   "about:blank", "data:", "chrome://welcome", "edge://welcome")

    def _fresh_tab(self):
        """Work in a tab of our own. With a profile, Chrome/Edge start on the New Tab page, whose footer shows
        up to WebDriver as a second window (chrome://newtab-footer); acting on it goes nowhere. So open a new
        tab and close the start pages. Only for a browser Datasquint launched, never for one it attached to."""
        d = self.driver
        try:
            before = list(d.window_handles)
            d.switch_to.new_window("tab")
            keep = d.current_window_handle
            self.hidden = set()
            for h in before:
                try:
                    d.switch_to.window(h)
                    url = d.current_url
                    if url.startswith("chrome://newtab-footer"):
                        self.hidden.add(h)        # not a real tab; may outlive closing: never count or show it
                    if url.startswith(self.START_PAGES):
                        d.close()
                except WebDriverException:
                    pass        # already gone with its page
            d.switch_to.window(keep)
        except WebDriverException:
            try:
                d.switch_to.window(d.window_handles[-1])
            except (WebDriverException, IndexError):
                pass

    def stop(self):
        with self.lock:
            if self.driver:
                try:
                    if self.mode == "attached":
                        self.driver.service.stop()     # leave the user's own browser window open
                    else:
                        self.driver.quit()
                except WebDriverException:
                    pass
            self.driver = None
            return {"running": False}

    def _tabs(self):
        """Window handles of real tabs."""
        return [h for h in self.driver.window_handles if h not in self.hidden]

    def _d(self):
        if not self.alive():
            if _env_bool("AUTO_START", True):
                self.start()
            else:
                raise BrowserError("The browser isn't running. Press Start browser first.")
        return self.driver

    # ----------------------------------------------------------------- helpers
    def _settle(self, quiet_ms=None, max_ms=None):
        quiet_ms = int(os.getenv("SETTLE_QUIET_MS", "600")) if quiet_ms is None else quiet_ms
        max_ms = int(os.getenv("SETTLE_MAX_MS", "8000")) if max_ms is None else max_ms
        d = self.driver
        d.set_script_timeout(max_ms / 1000 + 5)
        end = time.time() + max_ms / 1000
        while time.time() < end:
            try:
                d.execute_async_script(_JS_SETTLE, quiet_ms, int((end - time.time()) * 1000))
                return
            except (JavascriptException, TimeoutException, StaleElementReferenceException):
                time.sleep(0.25)      # page was replaced mid-wait (a navigation): wait on the new one
            except WebDriverException as e:
                if "unexpected alert" in str(e).lower():
                    return
                time.sleep(0.25)

    def _restore_frame(self):
        d = self.driver
        d.switch_to.default_content()
        try:
            for i in self.frame_path:
                d.switch_to.frame(i)
        except (NoSuchFrameException, WebDriverException):
            self.frame_path = []
            d.switch_to.default_content()

    def _el(self, eid):
        d = self._d()
        self._restore_frame()
        try:
            el = d.execute_script(_JS_BY_ID, str(int(eid)))
        except (TypeError, ValueError):
            raise BrowserError(f"Element id must be a number like 12, not {eid!r}.")
        if el is None:
            raise BrowserError(f"No element [{eid}] on the page now (it may have changed). Take a new snapshot.")
        return el

    def _js(self, script, *args):
        d = self._d()
        self._restore_frame()
        return d.execute_script(script, *args)

    def _handle_new_tab(self, before):
        d = self.driver
        new = [h for h in d.window_handles if h not in before and h not in self.hidden]
        if new:
            d.switch_to.window(new[-1])
            self.frame_path = []
            return " It opened a new tab; now working in that tab."
        return ""

    def _check_url(self, url):
        allowed = [a.strip().lower() for a in os.getenv("ALLOWED_DOMAINS", "").split(",") if a.strip()]
        if not allowed:
            return
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if not any(host == a or host.endswith("." + a) for a in allowed):
            raise BrowserError(f"{host or url} isn't in ALLOWED_DOMAINS ({', '.join(allowed)}).")

    # ----------------------------------------------------------------- reading
    def snapshot(self, max_chars=None, sample_rows=None):
        with self.lock:
            max_chars = int(max_chars or os.getenv("SNAPSHOT_MAX_CHARS", "200000"))
            sample_rows = int(sample_rows or os.getenv("TABLE_SAMPLE_ROWS", "5"))
            self._d()
            self._settle(300, 3000)
            snap = self._js(_JS_SNAPSHOT, max_chars, sample_rows)
            snap["frame"] = list(self.frame_path)
            snap["tab"] = self._tab_index()
            return snap

    def _tab_index(self):
        d = self.driver
        try:
            return self._tabs().index(d.current_window_handle)
        except (ValueError, WebDriverException):
            return 0

    def table(self, index=0, with_ids=False, max_rows=None):
        with self.lock:
            max_rows = int(max_rows or os.getenv("TABLE_MAX_ROWS", "5000"))
            res = self._js(_JS_TABLE, int(index), bool(with_ids), max_rows)
            if res.get("error"):
                raise BrowserError(res["error"])
            return res

    def text(self, selector=None):
        with self.lock:
            if selector:
                d = self._d()
                self._restore_frame()
                els = d.find_elements(By.CSS_SELECTOR, selector)
                if not els:
                    raise BrowserError(f"Nothing matches the CSS selector {selector!r}.")
                return "\n\n".join(e.text for e in els[:50])
            return self._js("return document.body ? document.body.innerText : ''")

    def find(self, text, exact=False, limit=30):
        with self.lock:
            return self._js(_JS_FIND, str(text), bool(exact), int(limit))

    def screenshot_b64(self):
        with self.lock:
            return base64.b64encode(self._d().get_screenshot_as_png()).decode()

    def tabs(self):
        with self.lock:
            d = self._d()
            cur = d.current_window_handle
            out = []
            for i, h in enumerate(self._tabs()):
                d.switch_to.window(h)
                out.append({"index": i, "title": d.title, "url": d.current_url, "current": h == cur})
            d.switch_to.window(cur)
            self._restore_frame()
            return out

    # ----------------------------------------------------------------- acting
    def navigate(self, url):
        with self.lock:
            url = str(url).strip()
            if "://" not in url and not url.startswith(("about:", "edge:", "chrome:", "file:", "data:")):
                host = url.split("/")[0].split(":")[0].lower()
                internal = (host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".internal"))
                            or host.replace(".", "").isdigit())
                url = ("http://" if internal else "https://") + url     # intranet servers are mostly plain HTTP
            self._check_url(url)
            d = self._d()
            try:
                d.get(url)
            except TimeoutException:
                pass       # slow page: read what has loaded
            except WebDriverException as e:
                m = re.search(r"net::(ERR_[A-Z_]+)", str(e))
                raise BrowserError(f"Couldn't open {url}: {m.group(1) if m else str(e).strip().splitlines()[0]}.")
            self.frame_path = []
            self._settle()
            return f"Opened {d.current_url} — {d.title!r}."

    def click(self, eid):
        with self.lock:
            d = self._d()
            el = self._el(eid)
            before = list(d.window_handles)
            old_url = d.current_url
            try:
                d.execute_script("arguments[0].scrollIntoView({block:'center'})", el)
                el.click()
            except (ElementClickInterceptedException, ElementNotInteractableException):
                d.execute_script("arguments[0].click()", el)     # covered by an overlay: click it directly
            except StaleElementReferenceException:
                raise BrowserError(f"[{eid}] disappeared before it could be clicked. Take a new snapshot.")
            time.sleep(0.15)
            note = self._handle_new_tab(before)
            self._settle()
            if d.current_url != old_url:
                self.frame_path = []
                note += f" Page is now {d.current_url}."
            return f"Clicked [{eid}].{note}"

    def type(self, eid, text, enter=False, clear=True):
        with self.lock:
            el = self._el(eid)
            if (el.get_attribute("type") or "").lower() == "password":
                raise BrowserError(f"Typing passwords isn't allowed. Ask the user to type it in the {BROWSERS[self.kind]['name']} window.")
            if clear:
                try:
                    el.clear()
                except WebDriverException:
                    pass
            el.send_keys(str(text))
            if enter:
                el.send_keys(Keys.ENTER)
            self._settle()
            return f"Typed into [{eid}]" + (" and pressed Enter." if enter else ".")

    def select(self, eid, option):
        with self.lock:
            el = self._el(eid)
            if el.tag_name.lower() != "select":
                raise BrowserError(f"[{eid}] isn't a dropdown (<select>); click it and pick the option instead.")
            s = Select(el)
            try:
                s.select_by_visible_text(str(option))
            except NoSuchElementException:
                opts = [o.text.strip() for o in s.options]
                match = [o for o in s.options if str(option).lower() in o.text.strip().lower()]
                if not match:
                    try:
                        s.select_by_value(str(option))
                    except NoSuchElementException:
                        raise BrowserError(f"[{eid}] has no option {option!r}. Options: {', '.join(opts[:30])}")
                else:
                    match[0].click()
            self._settle()
            return f"Selected {option!r} in [{eid}]."

    def scroll(self, direction="down", eid=None):
        with self.lock:
            if eid is not None:
                el = self._el(eid)
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'})", el)
            else:
                js = {"down": "scrollBy(0, innerHeight*0.85)", "up": "scrollBy(0, -innerHeight*0.85)",
                      "top": "scrollTo(0,0)", "bottom": "scrollTo(0, document.documentElement.scrollHeight)"}
                self._js(js.get(str(direction), js["down"]))
            self._settle(400, 4000)        # infinite-scroll lists load here
            return "Scrolled " + (f"to [{eid}]." if eid is not None else f"{direction}.")

    def back(self):
        with self.lock:
            d = self._d()
            d.back()
            self.frame_path = []
            self._settle()
            return f"Went back to {d.current_url}."

    def frame(self, index):
        with self.lock:
            d = self._d()
            if str(index).lower() in ("top", "main", "-1", "none"):
                self.frame_path = []
                d.switch_to.default_content()
                return "Now reading the main page."
            if str(index).lower() == "parent":
                self.frame_path = self.frame_path[:-1]
                self._restore_frame()
                return "Moved up to the parent frame."
            self._restore_frame()
            try:
                d.switch_to.frame(int(index))
            except (NoSuchFrameException, ValueError, WebDriverException):
                raise BrowserError(f"No frame {index} here.")
            self.frame_path.append(int(index))
            return f"Now reading frame {self.frame_path}."

    def switch_tab(self, index):
        with self.lock:
            d = self._d()
            hs = self._tabs()
            try:
                d.switch_to.window(hs[int(index)])
            except (IndexError, ValueError):
                raise BrowserError(f"No tab {index}; there are {len(hs)}.")
            self.frame_path = []
            return f"Switched to tab {index}: {d.title!r}."

    def press(self, key):
        with self.lock:
            name = str(key).upper().replace(" ", "_")
            k = getattr(Keys, name, None)
            if k is None:
                raise BrowserError(f"Unknown key {key!r}. Try Enter, Escape, Tab, Page_Down, Arrow_Down.")
            self._d().switch_to.active_element.send_keys(k)
            self._settle()
            return f"Pressed {key}."

    # ----------------------------------------------------------------- macros
    def _next_button(self, hint):
        """Find the 'next page' control again after the page changed (ids may be new)."""
        for exact in (True, False):
            for m in self.find(hint, exact=exact, limit=20):
                if not m["disabled"]:
                    return m
        return None

    def paginate(self, table=0, next_hint="Next", max_pages=20, on_page=None, should_stop=None):
        """Extract table, click the next control, repeat. on_page(page_no, data) gets each page."""
        with self.lock:
            # next_hint may be an element id from the snapshot: remember its label so we can find it again
            if isinstance(next_hint, int) or str(next_hint).isdigit():
                el = self._el(next_hint)
                lab = (el.get_attribute("aria-label") or el.text or el.get_attribute("title")
                       or el.get_attribute("value") or "").strip()
                if not lab:
                    raise BrowserError(f"[{next_hint}] has no text to find it by on later pages; "
                                       "give next as its text instead, e.g. \"next\": \"Next\".")
                next_hint = lab
            pages, rows, seen, reason = 0, 0, set(), f"reached max_pages ({max_pages})"
            while pages < max_pages:
                if should_stop and should_stop():
                    reason = "stopped by the user"
                    break
                data = self.table(table)
                sig = hashlib.sha1(repr(data["rows"]).encode()).hexdigest()
                if sig in seen:
                    reason = "the table didn't change after clicking next (last page?)"
                    break
                seen.add(sig)
                pages += 1
                rows += len(data["rows"])
                if on_page:
                    on_page(pages, data)
                if pages >= max_pages:
                    break
                nxt = self._next_button(next_hint)
                if not nxt:
                    reason = f"no enabled {next_hint!r} control left (last page)"
                    break
                self.click(nxt["id"])
            return {"pages": pages, "rows": rows, "stopped": reason, "next": next_hint}

    def click_all(self, text, max_clicks=50, exact=False, should_stop=None):
        """Click every control whose text matches, e.g. all 'Show more' or '+' expanders."""
        # Distinct matches (row expanders) are each clicked once. A control that stays on the page
        # ("Load more") is clicked again as long as each click still changes the page.
        size_js = "return document.body ? document.body.innerHTML.length : 0"
        with self.lock:
            clicked, labels, stale = {}, [], set()
            while len(labels) < max_clicks:
                if should_stop and should_stop():
                    break
                matches = [m for m in self.find(text, exact=exact, limit=200) if not m["disabled"]]
                todo = [m for m in matches if m["id"] not in clicked] or \
                       [m for m in matches if m["id"] not in stale and clicked.get(m["id"]) == m["label"]]
                if not todo:
                    break
                m = todo[0]
                before = self._js(size_js)
                try:
                    self.click(m["id"])
                except BrowserError:
                    stale.add(m["id"])
                    clicked[m["id"]] = m["label"]
                    continue
                labels.append(m["label"])
                if m["id"] in clicked and self._js(size_js) == before:
                    stale.add(m["id"])           # re-click did nothing: done with this one
                clicked[m["id"]] = m["label"]
            return {"clicked": len(labels), "labels": labels[:20]}


browser = Browser()


def _close_on_exit():
    """Quit the browser Datasquint launched when Datasquint stops (Ctrl+C, a service stop), so its profile
    folder isn't left in use by an orphaned window. Attached browsers are left open."""
    try:
        if browser.driver and browser.mode == "launched":
            browser.driver.quit()
    except Exception:
        pass


import atexit                  # noqa: E402
import signal                  # noqa: E402
atexit.register(_close_on_exit)
if hasattr(signal, "SIGTERM"):
    _prev_term = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        _close_on_exit()
        if callable(_prev_term):
            _prev_term(signum, frame)
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:         # not the main thread (e.g. imported by a WSGI worker thread): atexit still applies
        pass
