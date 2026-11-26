"""One Microsoft Edge or Google Chrome session driven by Selenium, plus the page-reading JavaScript.

Everything the model can do to the page goes through Browser: it reads the page as an outline with
numbered elements ([12:button Next]), pulls whole tables, clicks/types by those numbers, and runs two
macros that save model round-trips: paginate (extract a table, click Next, repeat) and click_all
(click every "Show more" / expand button).

All calls take a lock, so the Flask threads and the agent loop never drive the browser at once.
"""
import base64
import collections
import contextlib
import hashlib
import io
import os
import re
import threading
import time

from selenium import webdriver
from selenium.common.exceptions import (ElementClickInterceptedException, ElementNotInteractableException,
                                        JavascriptException, NoSuchElementException, NoSuchFrameException, NoSuchWindowException,
                                        StaleElementReferenceException, TimeoutException, WebDriverException)
from selenium.webdriver.common.actions.action_builder import ActionBuilder
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
const POINTER = %POINTER%;
const POINTER_TAGS = /^(DIV|SPAN|LI|TD|TH|TR|IMG|I|P|LABEL|DT|DD|H[1-6]|STRONG|B|EM|SMALL|SECTION|ARTICLE)$/;
// Clickable by look only: a pointer cursor (set by the page's own click handler code), the outermost one of its
// kind, with no proper button or link inside or around it. Common in React/Vue/Angular apps.
function pointerish(el) {
  if (!POINTER || !POINTER_TAGS.test(el.tagName)) return false;
  let s; try { s = getComputedStyle(el); } catch (e) { return false; }
  if (s.cursor !== 'pointer') return false;
  const p = el.parentElement;
  if (p && getComputedStyle(p).cursor === 'pointer') return false;
  if ((p && p.closest(INTER)) || el.querySelector(INTER)) return false;
  return true;
}
function isInter(el) {
  if (!el.matches) return false;
  if (!el.matches(INTER)) return pointerish(el);
  if (el.matches('[tabindex="-1"]:not(a,button,input,select,textarea)')) return false;
  return true;
}
function disabled(el) {
  return !!(el.disabled || el.getAttribute('aria-disabled') === 'true' ||
            /(^|\s)disabled(\s|$)/i.test(el.className && el.className.baseVal === undefined ? el.className : ''));
}
function kind(el) {
  const tag = el.tagName.toLowerCase(), role = el.getAttribute('role');
  if (!el.matches(INTER)) return 'clickable';
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
// querySelectorAll that also looks inside shadow roots (UI5 Web Components, used by SuccessFactors, and other
// custom elements keep their tables and buttons there)
function deepAll(sel, root) {
  root = root || document;
  const out = Array.from(root.querySelectorAll(sel));
  const hosts = [];
  const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  for (let n = w.currentNode; n; n = w.nextNode()) if (n.shadowRoot) hosts.push(n);
  for (const h of hosts) out.push(...deepAll(sel, h.shadowRoot));
  return out;
}
function hostContains(o, t) {        // contains, across shadow boundaries
  for (let n = t; n; n = n.parentNode || n.host) if (n === o) return true;
  return false;
}
// A <table> used for page layout (common in SAP Ariba, which builds its pages from nested tables): marked as
// presentation, or holding other tables in its cells. Its data tables are listed instead of it.
function layoutTable(t) {
  if (t.tagName !== 'TABLE') return false;
  const role = (t.getAttribute('role') || '').toLowerCase();
  if (role === 'presentation' || role === 'none') return true;
  if (t.querySelector('td table, th table')) return true;
  // a single row with no header cells: a strip of page layout (title bar, toolbar), not data
  const rows = Array.from(t.rows).filter(r => r.closest('table') === t);
  return rows.length === 1 && !t.querySelector('th') && t.closest('td') !== null;
}
function headerOnly(t) {
  const rows = tableRows(t);
  return rows.length > 0 && rows.length <= 3 && rows.every(r => isHeaderRow(r) || (t.tHead && t.tHead.contains(r)));
}
function allTables() {
  let list = deepAll(TABLES).filter(t => !layoutTable(t));
  // a <table> inside a role=grid (or the reverse) is the same table: keep the outermost
  list = list.filter(t => !list.some(o => o !== t && hostContains(o, t)));
  // a header row in a table of its own, followed by the table with the data (SAP Ariba, fixed-header grids):
  // that pair is one table, so the header-only one isn't listed (splitHead finds it again)
  return list.filter((t, i) => !(headerOnly(t) && list.slice(i + 1).some(b => near(t, b))));
}
function near(a, b) {           // b follows a within a few levels of the same container
  if (!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
  let p = a.parentElement;
  for (let i = 0; p && i < 6; i++, p = p.parentElement) if (p.contains(b)) return true;
  return false;
}
// The header of a table whose header row lives in a separate table just before it.
function splitHead(t, width) {
  let p = t.parentElement;
  for (let i = 0; p && i < 6; i++, p = p.parentElement) {
    const cands = Array.from(p.querySelectorAll('table,[role=row]')).filter(h => h !== t && !h.contains(t) && !t.contains(h) &&
      (h.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING));
    for (const h of cands.reverse()) {
      const rows = h.tagName === 'TABLE' ? Array.from(h.rows) : [h];
      const hr = rows.reverse().find(r => isHeaderRow(r));
      if (!hr) continue;
      const cells = rowCells(hr).flatMap(c => Array(c.colSpan || 1).fill(inlineText(c, false)));
      if (!width || Math.abs(cells.length - width) <= 1) return cells.slice(0, Math.max(width, 1) || cells.length);
    }
  }
  return [];
}
function tableRows(t) {
  if (t.tagName === 'TABLE') return Array.from(t.rows).filter(r => r.closest('table') === t);
  return deepAll('[role=row]', t);
}
function rowCells(r) {
  if (r.tagName === 'TR') return Array.from(r.cells);
  return deepAll('[role=cell],[role=gridcell],[role=columnheader],[role=rowheader]', r);
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
  if (!head.length && rows.length) {
    head = splitHead(t, Math.max(...rows.slice(0, 5).map(r => rowCells(r).length)));
  }
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
_JS_HELPERS = _JS_HELPERS.replace("%POINTER%", "true" if os.getenv("POINTER_CLICKABLES", "1").strip() != "0" else "false")


# Records what the user does in the window by hand: clicks, typing, choices, Enter. Runs in every page (also
# installed for new documents through CDP), keeps the steps in sessionStorage so a click that leads to another page
# of the same site isn't lost, and Python collects them every couple of seconds. Passwords are never recorded.
_JS_RECORDER = "(() => {\n" + "%HELPERS%" + r"""
if (window.__psqRec) return; window.__psqRec = 1;
const KEY = '__psq_log';
function push(e) {
  e.t = Date.now(); e.url = location.href; e.title = document.title;
  try {
    const a = JSON.parse(sessionStorage.getItem(KEY) || '[]'); a.push(e);
    sessionStorage.setItem(KEY, JSON.stringify(a.slice(-100)));
  } catch (_) { (window.__psqMem = window.__psqMem || []).push(e); }
}
function target(el) {      // the clickable thing that was hit: the element itself or a clickable parent
  for (let n = el, i = 0; n && n.nodeType === 1 && i < 8; n = n.parentElement, i++) if (isInter(n)) return n;
  return el && el.nodeType === 1 ? el : null;
}
function info(el) {
  return {id: +psqId(el), kind: isInter(el) ? kind(el) : el.tagName.toLowerCase(),
          label: (label(el) || cut(el.innerText || '', 60)).replace(/ = ".*$/, '')};
}
document.addEventListener('click', e => {
  if (!e.isTrusted) return;
  const el = target(e.target); if (!el) return;
  if (el.matches('input,textarea,select') && !/^(checkbox|radio|button|submit|reset|image)$/.test(el.type || '')) return;
  push(Object.assign({ev: 'click'}, info(el)));
}, true);
document.addEventListener('change', e => {
  const el = e.target; if (!el || !el.matches || !el.matches('input,select,textarea')) return;
  if (!e.isTrusted && el.tagName !== 'SELECT') return;     // some dropdowns report the choice as an untrusted event
  if (/^(checkbox|radio)$/.test(el.type || '')) return;            // the click already says it
  const secret = (el.type || '').toLowerCase() === 'password';
  const value = el.tagName === 'SELECT' ? clean((el.options[el.selectedIndex] || {}).text || '') : cut(el.value, 120);
  push(Object.assign({ev: el.tagName === 'SELECT' ? 'select' : 'type', value: secret ? null : value}, info(el)));
}, true);
document.addEventListener('keydown', e => {
  if (!e.isTrusted || e.key !== 'Enter') return;
  const el = e.target; if (!el || !el.matches || !el.matches('input')) return;
  const secret = (el.type || '').toLowerCase() === 'password';
  push(Object.assign({ev: 'enter', value: secret ? null : cut(el.value, 120)}, info(el)));
}, true);
})();
"""

_JS_RECORDER = _JS_RECORDER.replace("%HELPERS%", _JS_HELPERS)

_JS_COLLECT = r"""
let a = [];
try { a = JSON.parse(sessionStorage.getItem('__psq_log') || '[]'); sessionStorage.removeItem('__psq_log'); } catch (_) {}
a = a.concat(window.__psqMem || []); window.__psqMem = [];
return a;
"""

# Numbered boxes over every clickable element in view, for a marked screenshot. The numbers are the same ids
# the page text uses, so the model can click what it sees. arguments[0] false removes them.
_JS_MARKS = _JS_HELPERS + r"""
const old = document.getElementById('__psq_marks'); if (old) old.remove();
if (!arguments[0]) return [];
const vw = innerWidth, vh = innerHeight, cap = arguments[1] || 200;
const layer = document.createElement('div');
layer.id = '__psq_marks';
layer.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647';
const colours = ['#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4', '#008080', '#9a6324', '#c71585'];
const out = [];
function scan(root) {
  for (const el of root.querySelectorAll('*')) {
    if (out.length >= cap) return;
    if (el.shadowRoot) scan(el.shadowRoot);
    if (SKIP.has(el.tagName.toUpperCase()) || !isInter(el) || !visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) continue;
    const cx = Math.min(vw - 1, Math.max(0, r.left + r.width / 2)), cy = Math.min(vh - 1, Math.max(0, r.top + r.height / 2));
    const top = document.elementFromPoint(cx, cy);
    if (top && top !== el && !el.contains(top) && !top.contains(el)) continue;   // hidden behind something
    const id = psqId(el), c = colours[out.length % colours.length];
    const b = document.createElement('div');
    b.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;` +
      `outline:2px solid ${c};outline-offset:-1px;box-sizing:border-box`;
    const t = document.createElement('div');
    t.textContent = id;
    // wide rows sit close together: label inside at the right end, where it covers least; small things: above
    const inside = r.width > 160;
    const lx = inside ? Math.min(vw, r.right) - 2 - (String(id).length * 7 + 6) : Math.max(0, r.left);
    const ly = inside ? Math.max(0, r.top + 1) : Math.max(0, r.top - 14);
    t.style.cssText = `position:fixed;left:${lx}px;top:${ly}px;background:${c};` +
      'color:#fff;font:bold 11px/13px Arial,sans-serif;padding:0 3px;border-radius:2px;white-space:nowrap';
    layer.append(b, t);
    out.push({id: +id, kind: kind(el), label: label(el), x: Math.round(cx), y: Math.round(cy)});
  }
}
scan(document);
document.documentElement.appendChild(layer);
return out;
"""

_JS_AT_POINT = _JS_HELPERS + r"""
const el = document.elementFromPoint(arguments[0], arguments[1]);
if (!el) return null;
const t = (el.closest && el.closest(INTER)) || el;
return {id: +psqId(t), kind: isInter(t) ? kind(t) : t.tagName.toLowerCase(), label: label(t) || cut(t.innerText || '', 60)};
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

# Lazily loaded tables: infinite scroll (rows added as you scroll) and virtual grids (only the rows in view exist,
# recycled as you scroll, e.g. ag-Grid, MUI DataGrid). op: info | top | read | step.
_JS_LAZY = _JS_HELPERS + r"""
const idx = arguments[0], op = arguments[1];
const tables = allTables();
if (idx < 0 || idx >= tables.length) return {error: 'no table ' + idx + ' (page has ' + tables.length + ')'};
const t = tables[idx];
const page = document.scrollingElement || document.documentElement;
function scrollsY(n) {
  if (n.scrollHeight <= n.clientHeight + 4) return false;
  const s = getComputedStyle(n);
  return /(auto|scroll|overlay)/.test(s.overflowY);
}
function scroller() {
  // The scrolling box that moves the rows: inside the grid (virtual grids keep their rows in one), a separate
  // scrollbar next to it (SAP UI5 tables), then around it, then the page. The one with the most to scroll wins.
  const r = t.getBoundingClientRect();
  const cands = Array.from(t.querySelectorAll('div,section,tbody')).slice(0, 4000).filter(scrollsY);
  let p = t.parentElement;
  for (let i = 0; p && i < 3; i++, p = p.parentElement) {
    for (const n of Array.from(p.querySelectorAll('div')).slice(0, 2000)) {
      if (n.contains(t) || t.contains(n) || !scrollsY(n)) continue;
      const b = n.getBoundingClientRect();     // a scrollbar of its own, beside the rows
      if (b.bottom > r.top && b.top < r.bottom && b.width < 40) cands.push(n);
    }
  }
  if (cands.length) return cands.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
  for (let n = t; n && n !== document.body && n !== document.documentElement; n = n.parentElement) if (scrollsY(n)) return n;
  return page;
}
// "More [20/45]" (SAP UI5 growing lists), "Show more", "Load more" just under the table
const MORE = /^\s*(more|show more|load more|view more|see more|show all|more items|load more items)\b|\[\s*\d+\s*\/\s*\d+\s*\]/i;
function moreTrigger() {
  const cands = Array.from(t.querySelectorAll('.sapMGrowingListTrigger,[id$="-trigger"],button,a,[role=button]'));
  let p = t.parentElement;
  for (let i = 0; p && i < 3; i++, p = p.parentElement)
    for (const n of p.querySelectorAll('.sapMGrowingListTrigger,[id$="-trigger"],button,a,[role=button]'))
      if (!t.contains(n) && (t.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING)) cands.push(n);
  return cands.find(n => visible(n) && !disabled(n) && MORE.test(clean(n.innerText || n.getAttribute('aria-label') || '')));
}
const box = scroller(), isPage = box === page;
const view = isPage ? innerHeight : box.clientHeight;
const pos = () => ({top: Math.round(isPage ? scrollY : box.scrollTop), height: box.scrollHeight, view: view,
                    atEnd: (isPage ? scrollY : box.scrollTop) + view >= box.scrollHeight - 4, inner: !isPage});
if (op === 'info') {
  const rows = tableRows(t).filter(visible), last = rows[rows.length - 1];
  const below = last ? (isPage ? page.scrollHeight - (last.getBoundingClientRect().bottom + scrollY) : 0) : 0;
  const keyed = rows.some(r => r.hasAttribute('aria-rowindex') || r.hasAttribute('row-index') || r.hasAttribute('data-row-index'));
  const more = moreTrigger(), m = more && /\[\s*(\d+)\s*\/\s*(\d+)\s*\]/.exec(more.innerText || '');
  return Object.assign(pos(), {below: Math.round(below), keyed: keyed, rows: rows.length, more: !!more,
    // virtual grids say how many rows there are in all; UI5 growing lists show "More [20/45]"
    count: +(t.getAttribute('aria-rowcount') || 0) || (m ? +m[2] : 0)});
}
if (op === 'more') {
  const more = moreTrigger();
  if (!more) return {clicked: false};
  more.scrollIntoView({block: 'center'}); more.click();
  return {clicked: true, text: cut(more.innerText || '', 40)};
}
if (op === 'top') { if (isPage) scrollTo(0, 0); else box.scrollTop = 0; return pos(); }
if (op === 'bottom') { if (isPage) scrollTo(0, page.scrollHeight); else box.scrollTop = box.scrollHeight; return pos(); }
if (op === 'up') { if (isPage) scrollBy(0, -innerHeight * 0.85); else box.scrollTop -= box.clientHeight * 0.85; return pos(); }
if (op === 'step') {
  const before = isPage ? scrollY : box.scrollTop;
  if (isPage) scrollBy(0, Math.max(200, innerHeight * 0.8)); else box.scrollTop = before + Math.max(100, box.clientHeight * 0.8);
  const p = pos(); p.moved = Math.round((isPage ? scrollY : box.scrollTop) - before); return p;
}
// read: rows in the DOM now, keyed by their row number when the grid gives one; a row split over pinned
// column blocks (same row number in several containers) is joined back into one
let head = [];
const out = [], byKey = new Map();
for (const r of tableRows(t)) {
  if (!visible(r) && !(r.getAttribute('aria-rowindex') || r.getAttribute('row-index'))) continue;
  const cells = rowCells(r).flatMap(c => [inlineText(c, false)].concat(Array(Math.max(0, (c.colSpan || 1) - 1)).fill('')));
  if (!cells.length) continue;
  if (isHeaderRow(r) || (t.tagName === 'TABLE' && t.tHead && t.tHead.contains(r))) {
    if (!head.length || cells.join('').length > head.join('').length) head = cells;
    continue;
  }
  const k = r.getAttribute('aria-rowindex') || r.getAttribute('row-index') || r.getAttribute('data-row-index') ||
            r.getAttribute('data-sap-ui-rowindex') ||
            r.getAttribute('data-rowindex') || r.getAttribute('data-index') || null;
  if (k !== null && byKey.has(k)) { byKey.get(k).c.push(...cells); continue; }
  const row = {k: k, c: cells};
  if (k !== null) byKey.set(k, row);
  out.push(row);
}
if (!head.length && out.length) head = splitHead(t, Math.max(...out.slice(0, 5).map(r => r.c.length)));
return Object.assign(pos(), {head: head, rows: out});
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
// "busy" overlays and spinners (SAP UI5 busy indicators, SAP Ariba's wait panel, aria-busy, generic spinners)
const BUSY = '[aria-busy="true"],.sapUiLocalBusyIndicator,.sapUiBusyIndicator,.sapMBusyIndicator,.sapMBusyDialog,' +
  '.sapUiLocalBusy,#awwaitAlertDiv,.awwaitAlertDiv,.w-wait,.loading-indicator,.loadingIndicator,[class*="busy-indicator"],' +
  '[class*="spinner"]:not(:has(input)),[class*="Spinner"]:not(:has(input)),[role="progressbar"]:not([aria-valuenow])';
function busy() {
  if (Date.now() - last > %BUSY_MAX%) return false;   // still for a long while: a decorative spinner, not loading
  for (const el of document.querySelectorAll(BUSY)) {
    if (el.matches('input,select,textarea,button')) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 4 && r.height > 4) {
      const s = getComputedStyle(el);
      if (s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05) return true;
    }
  }
  return false;
}
(function tick() {
  const now = Date.now();
  if ((document.readyState === 'complete' && now - last >= quiet && !busy()) || now - start > max) { obs.disconnect(); done(true); }
  else setTimeout(tick, 100);
})();
"""


_JS_SETTLE = _JS_SETTLE.replace("%BUSY_MAX%", str(int(os.getenv("BUSY_MAX_MS", "6000") or 6000)))


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
        self.notices = []             # things the model should hear with its next action's result
        # steps the user took in the window by hand (see _JS_RECORDER), and when Datasquint itself was acting
        self.manual = collections.deque(maxlen=300)
        self.ai_windows = collections.deque(maxlen=200)
        self._seq = 0
        self._last_url = None
        self._last_poll = 0.0
        self._registered = set()      # tabs with the recorder set up for new pages
        self._poller = None

    # ----------------------------------------------------------------- lifecycle
    def status(self):
        with self.lock:
            if not self.alive():
                return {"running": False}
            base = {"running": True, "mode": self.mode, "browser": self.kind, "browser_name": BROWSERS[self.kind]["name"]}
            try:
                self._ensure_tab()
                return {**base, "url": self.driver.current_url, "title": self.driver.title,
                        "tabs": len(self._tabs()), "tab": self._tab_index(), "frame": self.frame_path}
            except WebDriverException as e:
                return {**base, "error": str(e).splitlines()[0]}

    def alive(self):
        """Is there a browser with at least one tab to work in? If its windows were all closed, let go of it,
        so the next action starts a fresh one (or reports that the attached browser is gone)."""
        if not self.driver:
            return False
        try:
            if any(h not in self.hidden for h in self.driver.window_handles):
                return True
        except WebDriverException:
            pass
        name = BROWSERS[self.kind]["name"]
        self._release()
        self.notices.append(f"The {name} window had been closed" + (
            ", so a new one was opened: open the page again if you need it." if _env_bool("AUTO_START", True)
            and self.mode != "attached" else "."))
        return False

    def _release(self):
        """Forget the current session without touching the user's own windows."""
        d, self.driver = self.driver, None
        try:
            if self.mode == "attached":
                d.service.stop()
            else:
                d.quit()
        except Exception:
            pass
        self.frame_path, self.hidden = [], set()

    def _ensure_tab(self):
        """If the tab being used was closed but others are open, carry on in the most recently opened one."""
        d = self.driver
        try:
            d.current_window_handle
            return
        except NoSuchWindowException:
            pass
        except WebDriverException as e:
            if "no such window" not in str(e).lower() and "target window already closed" not in str(e).lower():
                raise
        tabs = self._tabs()
        if not tabs:
            return
        d.switch_to.window(tabs[-1])
        self.frame_path = []
        self._watch()
        self.notices.append(f"The tab in use had been closed, so this continues in another open tab: "
                            f"{d.title!r} ({d.current_url}).")

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
            self._registered = set()
            self._watch()
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
            if self.mode == "attached" and not os.getenv(BROWSERS[self.kind]["env"] + "_DEBUGGER", "").strip():
                self.mode = None
            if _env_bool("AUTO_START", True):
                try:
                    self.start()
                except BrowserError as e:
                    self.notices.clear()
                    raise BrowserError(f"The {BROWSERS[self.kind]['name']} window was closed and a new one couldn't "
                                       f"be opened: {e}")
            else:
                raise BrowserError("The browser isn't running. Press Start browser first.")
        self._ensure_tab()
        return self.driver

    # ----------------------------------------------------------------- the user's own steps
    TRACK = os.getenv("TRACK_CLICKS", "1").strip() != "0"

    @contextlib.contextmanager
    def ai_step(self):
        """Around anything Datasquint does in the page, so those clicks aren't reported as the user's."""
        start = time.time()
        try:
            yield
        finally:
            self.ai_windows.append((start, time.time()))
            if self.lock.acquire(timeout=5):
                try:
                    if self.driver:
                        self._last_url = self.driver.current_url
                except WebDriverException:
                    pass
                finally:
                    self.lock.release()

    def _watch(self):
        """Set up the recorder in the current tab (now and for every page it opens later) and the collector thread."""
        if not self.TRACK or not self.driver:
            return
        d = self.driver
        try:
            h = d.current_window_handle
            if h not in self._registered:
                try:
                    d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _JS_RECORDER})
                except Exception:
                    pass          # no CDP (unusual): the collector installs it on each page it visits instead
                self._registered.add(h)
            d.execute_script(_JS_RECORDER)
            self._last_url = d.current_url
        except WebDriverException:
            pass
        if not self._poller or not self._poller.is_alive():
            self._poller = threading.Thread(target=self._poll_loop, name="click-recorder", daemon=True)
            self._poller.start()

    def _poll_loop(self):
        every = max(0.5, float(os.getenv("TRACK_INTERVAL", "1.5") or 1.5))
        while True:
            time.sleep(every)
            if not self.driver or not self.lock.acquire(blocking=False):
                continue          # an action is running: collect next time
            try:
                self.collect()
            except Exception:
                pass
            finally:
                self.lock.release()

    def _by_ai(self, t):
        return any(s - 0.3 <= t <= e + 0.8 for s, e in self.ai_windows)

    def collect(self):
        """Move the user's steps from the page into self.manual. Called with the lock held."""
        d = self.driver
        if not d:
            return
        try:
            d.current_window_handle
        except WebDriverException:
            return
        events = []
        try:
            if self.frame_path:
                d.switch_to.default_content()
            events = d.execute_script(_JS_RECORDER + _JS_COLLECT) or []
            url = d.current_url
            title = d.title
        except WebDriverException:
            return
        finally:
            if self.frame_path:
                try:
                    self._restore_frame()
                except Exception:
                    pass
        now = time.time()
        for e in sorted(events, key=lambda e: e.get("t") or 0):
            t = (e.get("t") or 0) / 1000
            if not t or self._by_ai(t):
                continue
            self._add(dict(e, t=t))
        if url != self._last_url:
            # a page change nobody clicked for here (typed address, back button, a link) and not Datasquint's doing
            if not self._by_ai(now) and not (self.ai_windows and self.ai_windows[-1][1] >= self._last_poll - 0.5):
                self._add({"ev": "page", "url": url, "title": title, "t": now})
            self._last_url = url
        self._last_poll = now

    def _add(self, e):
        if e.get("ev") == "type" and self.manual and self.manual[-1].get("ev") == "enter" \
                and self.manual[-1].get("id") == e.get("id") and abs(self.manual[-1]["t"] - e["t"]) < 2:
            return            # Enter already recorded the value
        if e.get("ev") == "page" and self.manual and self.manual[-1].get("url") == e["url"] \
                and e["t"] - self.manual[-1]["t"] < 5:
            return            # the click just before already led here
        self._seq += 1
        self.manual.append(dict(e, seq=self._seq))

    def manual_steps(self, limit=50, minutes=None):
        """The user's latest steps, oldest first. Collects first, if the browser isn't busy."""
        if self.driver and self.lock.acquire(timeout=2):
            try:
                self.collect()
            except Exception:
                pass
            finally:
                self.lock.release()
        steps = list(self.manual)
        if minutes:
            cut = time.time() - minutes * 60
            steps = [s for s in steps if s["t"] >= cut]
        return steps[-limit:]

    def clear_manual(self):
        self.manual.clear()

    def take_notices(self):
        """Notes for the model about what changed by itself (window or tab closed), once."""
        with self.lock:
            out, self.notices = self.notices, []
            return out

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
            self._watch()
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

    def lazy_info(self, index=0):
        """Does the table look lazily loaded: in its own scrolling box, or with the page scrolling on past it?"""
        with self.lock:
            self._d()
            info = self._js(_JS_LAZY, int(index), "info")
            if info.get("error"):
                raise BrowserError(info["error"])
            lazy = bool(info["inner"] and not info["atEnd"]) or bool(info["inner"] and info.get("keyed")) or \
                bool(info.get("more")) or \
                (not info["inner"] and not info["atEnd"] and info["below"] < 1.5 * info["view"]) or \
                bool(info.get("count") and info["count"] > info["rows"] + 1)
            return dict(info, lazy=lazy)

    def table_scrolled(self, index=0, max_rounds=None, max_rows=None, should_stop=None, on_progress=None):
        """All rows of a table that loads as you scroll: start at the top, read the rows, scroll a screen,
        wait for new rows, repeat until nothing new comes. Virtual grids (rows replaced while scrolling) are
        handled by collecting rows as they pass, keyed by their row number."""
        max_rounds = int(max_rounds or os.getenv("LAZY_MAX_SCROLLS", "200"))
        max_rows = int(max_rows or os.getenv("TABLE_MAX_ROWS", "5000"))
        with self.lock:
            self._d()
            self._js(_JS_LAZY, int(index), "top")
            self._settle(300, 3000)
            head, order, rows, keyed = [], [], {}, False
            rounds, idle, clicks, reason = 0, 0, 0, f"reached {max_rounds} scrolls (LAZY_MAX_SCROLLS)"
            last_height = None
            while True:
                if should_stop and should_stop():
                    reason = "stopped by the user"
                    break
                r = self._js(_JS_LAZY, int(index), "read")
                if r.get("error"):
                    if rows:
                        reason = "the table disappeared"
                        break
                    raise BrowserError(r["error"])
                if r["head"] and len("".join(r["head"])) > len("".join(head)):
                    head = r["head"]
                new = 0
                for row in r["rows"]:
                    k = row["k"]
                    if k is not None:
                        keyed = True
                        key = ("k", str(k))
                    else:
                        key = ("c",) + tuple(row["c"])
                    if key not in rows:
                        rows[key] = row["c"]
                        order.append(key)
                        new += 1
                    elif len(row["c"]) > len(rows[key]):
                        rows[key] = row["c"]          # more of the row has rendered now
                if on_progress:
                    on_progress(len(order), rounds)
                if len(order) >= max_rows:
                    reason = f"reached {max_rows} rows (TABLE_MAX_ROWS)"
                    break
                if rounds >= max_rounds:
                    break
                grew = last_height is not None and r["height"] > last_height
                last_height = r["height"]
                if new == 0 and r["atEnd"] and not grew:
                    more = self._js(_JS_LAZY, int(index), "more")      # a "More [20/45]" / "Show more" control
                    if more.get("clicked"):
                        clicks += 1
                        idle = 0
                        rounds += 1
                        self._settle(400, 8000)
                        continue
                    idle += 1
                    if idle >= 2:                     # gave it two waits at the end: nothing more is coming
                        reason = "reached the end of the list"
                        break
                else:
                    idle = 0
                self._js(_JS_LAZY, int(index), "step")
                rounds += 1
                # a grid with its own scrollbar usually redraws from data it already has: a short wait will do
                self._settle(150 if r["inner"] and not r["atEnd"] else 350, 4000)
                if r["atEnd"]:
                    time.sleep(0.4)                   # at the bottom: give the next batch a moment
            if keyed and all(k[1].lstrip("-").isdigit() for k in order):
                order.sort(key=lambda k: int(k[1]))
            body = [rows[k] for k in order][:max_rows]
            width = max([len(head)] + [len(b) for b in body]) if body else len(head)
            body = [b + [""] * (width - len(b)) for b in body]
            return {"head": head, "rows": body, "scrolls": rounds, "stopped": reason, "keyed": keyed,
                    "total": len(order), "more_clicks": clicks}

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

    def screenshot(self, marks=True):
        """A picture of what's in view, scaled so 1 image pixel = 1 CSS pixel (what click_at uses). With marks,
        every clickable element in view gets a numbered box first (the ids in the page text).
        Returns (png bytes, [{id, kind, label, x, y}], (width, height))."""
        with self.lock:
            d = self._d()
            d.switch_to.default_content()
            listed = []
            try:
                if marks:
                    listed = d.execute_script(_JS_MARKS, True, int(os.getenv("SCREENSHOT_MAX_MARKS", "200"))) or []
                png = d.get_screenshot_as_png()
            finally:
                if marks:
                    try:
                        d.execute_script(_JS_MARKS, False)
                    except WebDriverException:
                        pass
                self._restore_frame()
            vw, vh = d.execute_script("return [innerWidth, innerHeight]")
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(png))
                if im.width != vw and vw:
                    im = im.resize((int(vw), max(1, round(im.height * vw / im.width))), Image.LANCZOS)
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "PNG", optimize=True)
                png = buf.getvalue()
            except Exception:
                pass     # Pillow missing: send it unscaled (on 100% display scaling it's the same)
            return png, listed, (int(vw), int(vh))

    def click_at(self, x, y):
        """Click at a point of the latest screenshot (CSS pixels from the top-left of the window's page area)."""
        with self.lock:
            d = self._d()
            try:
                x, y = float(x), float(y)
            except (TypeError, ValueError):
                raise BrowserError("click_at needs numbers: {\"action\": \"click_at\", \"x\": 420, \"y\": 310}.")
            d.switch_to.default_content()
            self.frame_path = []
            vw, vh = d.execute_script("return [innerWidth, innerHeight]")
            if not (0 <= x < vw and 0 <= y < vh):
                raise BrowserError(f"({x:.0f}, {y:.0f}) is outside the window ({vw} x {vh}). Use a point from the "
                                   "latest screenshot, or scroll first.")
            hit = d.execute_script(_JS_AT_POINT, x, y) or {}
            before, old_url = list(d.window_handles), d.current_url
            ab = ActionBuilder(d)
            ab.pointer_action.move_to_location(int(x), int(y))
            ab.pointer_action.click()
            ab.perform()
            time.sleep(0.15)
            note = self._handle_new_tab(before)
            self._settle()
            if d.current_url != old_url:
                note += f" Page is now {d.current_url}."
            what = f" on [{hit['id']}:{hit['kind']} {hit['label']}]" if hit.get("id") else ""
            return f"Clicked at ({x:.0f}, {y:.0f}){what}.{note}"

    def tabs(self):
        """Open tabs: index, title, url, current (the one Datasquint works in), shown (in front in its window)."""
        with self.lock:
            d = self._d()
            cur = d.current_window_handle
            out = []
            for i, h in enumerate(self._tabs()):
                try:
                    d.switch_to.window(h)
                    shown = d.execute_script("return document.visibilityState") == "visible"
                    out.append({"index": i, "title": d.title, "url": d.current_url, "current": h == cur, "shown": shown})
                except WebDriverException:
                    continue      # closed meanwhile
            d.switch_to.window(cur)
            self._restore_frame()
            return out

    FOLLOW = os.getenv("FOLLOW_ACTIVE_TAB", "1").strip() != "0"

    def follow_shown_tab(self):
        """If the user brought another tab to the front, work in that one from now on (FOLLOW_ACTIVE_TAB).
        Returns a note when it switched, else None. Quick: does nothing with a single tab."""
        if not self.FOLLOW or not self.driver:
            return None
        with self.lock:
            if not self.alive():
                return None
            d = self.driver
            try:
                hs = self._tabs()
                if len(hs) < 2:
                    return None
                cur = d.current_window_handle
                d.switch_to.window(cur)
                if d.execute_script("return document.visibilityState") == "visible":
                    self._restore_frame()
                    return None
                for h in hs:
                    if h == cur:
                        continue
                    d.switch_to.window(h)
                    if d.execute_script("return document.visibilityState") == "visible":
                        self.frame_path = []
                        self._watch()
                        note = (f"The user switched to another tab, so this now works in tab {hs.index(h)}: "
                                f"{d.title!r} ({d.current_url}).")
                        self.notices.append(note)
                        return note
                d.switch_to.window(cur)
                self._restore_frame()
            except WebDriverException:
                pass
            return None

    def activate_tab(self, index):
        """Switch to a tab and bring it to the front of its window (Browser menu)."""
        with self.lock:
            msg = self.switch_tab(index)
            try:
                self.driver.execute_cdp_cmd("Page.bringToFront", {})
            except Exception:
                pass
            return msg

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

    def scroll_to_end(self, table=None, eid=None, should_stop=None):
        """Keep scrolling to the bottom (of the page, a table's box, or the box around element eid) until no more
        content loads: for lists that add items as you scroll, so everything is on the page before reading it."""
        max_rounds = int(os.getenv("LAZY_MAX_SCROLLS", "200"))
        with self.lock:
            d = self._d()
            if table is not None:
                go = lambda: self._js(_JS_LAZY, int(table), "bottom")
            else:
                target = self._el(eid) if eid is not None else None
                go = lambda: d.execute_script(r"""
                  let n = arguments[0];
                  for (; n && n !== document.body; n = n.parentElement) {
                    const s = getComputedStyle(n);
                    if (/(auto|scroll|overlay)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 4) break;
                  }
                  const box = (!n || n === document.body) ? (document.scrollingElement || document.documentElement) : n;
                  box.scrollTop = box.scrollHeight;
                  return {height: box.scrollHeight, inner: box !== document.scrollingElement};""", target)
            first = last = None
            rounds = idle = 0
            while rounds < max_rounds and not (should_stop and should_stop()):
                p = go() or {}
                if p.get("error"):
                    raise BrowserError(p["error"])
                h = p.get("height") or 0
                first = h if first is None else first
                self._settle(400, 8000)
                rounds += 1
                if last is not None and h <= last:
                    idle += 1
                    if idle >= 2:
                        break
                else:
                    idle = 0
                last = h
            grew = f"grew from {first:,} to {last:,} px" if last and first and last > first else "didn't grow"
            return f"Scrolled to the end {rounds} times; the content {grew}. Everything that loads by scrolling is on the page now: read it with snapshot or extract_text."

    def scroll(self, direction="down", eid=None, inside=False, table=None):
        if str(direction) == "end":
            return self.scroll_to_end(table=table, eid=eid if inside or table is None else None)
        with self.lock:
            if table is not None:
                op = {"down": "step", "up": "up", "top": "top", "bottom": "bottom"}.get(str(direction), "step")
                p = self._js(_JS_LAZY, int(table), op)
                if p.get("error"):
                    raise BrowserError(p["error"])
                self._settle(400, 4000)
                where = "its own scrolling box" if p["inner"] else "the page"
                return (f"Scrolled table {table} {direction} ({where}): now at {p['top']} of {p['height']}px"
                        + (", the end." if p["atEnd"] else "."))
            if eid is not None and inside:
                # scroll the scrolling box that holds this element (a list or grid with its own scrollbar)
                el = self._el(eid)
                moved = self.driver.execute_script(r"""
                  let n = arguments[0];
                  for (; n && n !== document.body; n = n.parentElement) {
                    const s = getComputedStyle(n);
                    if (/(auto|scroll|overlay)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 4) break;
                  }
                  if (!n || n === document.body) return null;
                  const b = n.scrollTop, d = arguments[1];
                  n.scrollTop = d === 'top' ? 0 : d === 'bottom' ? n.scrollHeight : b + (d === 'up' ? -1 : 1) * n.clientHeight * 0.85;
                  return Math.round(n.scrollTop - b);""", el, str(direction))
                if moved is None:
                    raise BrowserError(f"[{eid}] isn't inside a scrolling box; use {{\"action\": \"scroll\", \"direction\": \"{direction}\"}} for the page.")
                self._settle(400, 4000)
                return f"Scrolled the box around [{eid}] {direction} ({moved:+d}px)."
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
            self._watch()
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
