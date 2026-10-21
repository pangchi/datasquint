"""
Datasquint: chat with your PostgreSQL databases and files using any OpenAI-compatible model.

Configure with environment variables (or a .env file):
    OPENAI_API_KEY   - API key
    OPENAI_BASE_URL  - e.g. https://example.com/v1
    OPENAI_MODEL     - default model name
    SYSTEM_PROMPT    - optional default system prompt
    UPLOAD_DIR       - where uploaded files are stored (default ./uploads)
    MAX_UPLOAD_MB    - per-file upload limit (default 20)
    FILE_TABLES      - 1 (default) loads CSV/TSV/Excel into DuckDB as SQL tables; 0 reads them as text instead
    MAX_TABLE_UPLOAD_MB - per-file limit for CSV/TSV/Excel loaded as SQL tables (default 200)
    MAX_TEXT_CHARS   - max characters of text sent per file or zip (default 400000, ~100k tokens)
    MAX_SEND_CHARS   - max characters per request; larger sends are split into parts (default 100000)
    PART_NOTE_TOKENS - max tokens for the notes taken from each part (default 2000)
    ZIP_MAX_FILES    - max files read from one .zip (default 300)
    ZIP_MAX_TOTAL_MB - max total uncompressed size of one .zip (default 100)
    ZIP_MAX_IMAGES   - max images sent from one .zip (default 10)
    DATABASE_URL     - optional PostgreSQL connection shown alongside saved ones (see db.py)
    CONFIG_DB        - SQLite file for saved connections (default ./connections.sqlite)
    APP_SECRET_KEY   - Fernet key for saved passwords (default: generated into ./secret.key)
    ALLOWED_EXTENSIONS - optional comma list overriding the allowed upload types, e.g. ".png,.pdf,.csv"
"""
import base64
import csv
import io
import gc
import json
import mimetypes
import os
import re
import shutil
import stat
import sys
import threading
import time
import uuid
import zipfile
import posixpath

import openai

# Read .env first: the modules below read their settings (LOG_*, FILE_TABLES, UPDATES, …) when imported.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import logsetup
LOG_PATH = logsetup.setup()   # before the other modules log anything
import db
import filesql
import scriptgen
import updater
import usage
import webtools
from version import __version__ as VERSION
log = logsetup.child("web")
ulog = logsetup.child("upload")
qlog = logsetup.child("query")
clog = logsetup.child("chat")
blog = logsetup.child("browser")
from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory, stream_with_context
from werkzeug.utils import secure_filename

API_KEY = os.getenv("OPENAI_API_KEY", "your_api_key")
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://example.com")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", 20))
# CSV/TSV/Excel go into DuckDB rather than the model's context, so they can be much larger.
MAX_TABLE_UPLOAD_MB = int(os.getenv("MAX_TABLE_UPLOAD_MB", 200)) if filesql.available() else MAX_UPLOAD_MB
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", 400_000))   # ~100k tokens
MAX_SEND_CHARS = int(os.getenv("MAX_SEND_CHARS", 100_000))      # per request to the API
PART_NOTE_TOKENS = int(os.getenv("PART_NOTE_TOKENS", 2000))     # max reply length for each part's notes
ZIP_MAX_FILES = int(os.getenv("ZIP_MAX_FILES", 300))
ZIP_MAX_TOTAL_MB = int(os.getenv("ZIP_MAX_TOTAL_MB", 100))
ZIP_MAX_IMAGES = int(os.getenv("ZIP_MAX_IMAGES", 10))

client = openai.OpenAI(api_key=API_KEY, base_url=BASE_URL)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = max(MAX_UPLOAD_MB, MAX_TABLE_UPLOAD_MB) * 1024 * 1024 + 64 * 1024  # + form overhead
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Allowed types, per the model's stated support: images, documents, data files and code.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DOC_EXTS = {".pdf", ".docx", ".xlsx", ".xlsm"}                      # converted to text on the server
ARCHIVE_EXTS = {".zip"}                                    # extracted; members processed like uploads
TEXT_EXTS = {
    # documents / data
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".log",
    # code
    ".py", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css", ".scss", ".vue", ".svelte",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt", ".swift", ".go", ".rs", ".rb", ".php",
    ".pl", ".lua", ".r", ".m", ".scala", ".dart", ".sh", ".bash", ".bat", ".ps1", ".sql", ".ino", ".v",
    ".sv", ".vhd", ".asm", ".s", ".make", ".cmake", ".gradle",
}
_env_allowed = os.getenv("ALLOWED_EXTENSIONS", "").strip()
ALLOWED_EXTS = (
    {("." + e.strip().lower().lstrip(".")) for e in _env_allowed.split(",") if e.strip()}
    if _env_allowed else IMAGE_EXTS | DOC_EXTS | TEXT_EXTS | ARCHIVE_EXTS
)
SPECIAL_NAMES = {"dockerfile", "makefile"}                 # code files with no extension
SKIP_DIRS = {"__macosx", ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------- uploads
def extract_text(path, name, mime):
    """Return text content of a document, or raise ValueError if unsupported."""
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDF support needs 'pip install pypdf'")
        reader = PdfReader(path)
        return "\n\n".join(f"[Page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise ValueError("Word support needs 'pip install python-docx'")
        d = docx.Document(path)
        out = [p.text for p in d.paragraphs]
        for t in d.tables:
            out += [" | ".join(c.text.strip() for c in row.cells) for row in t.rows]
        return "\n".join(out)
    if ext in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ValueError("Excel support needs 'pip install openpyxl'")
        import csv, io
        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            buf = io.StringIO()
            w = csv.writer(buf)
            for row in ws.iter_rows(values_only=True):
                if any(v is not None for v in row):
                    w.writerow(["" if v is None else v for v in row])
            out.append(f"[Sheet: {ws.title}]\n{buf.getvalue()}")
        return "\n".join(out)
    with open(path, "rb") as fh:  # only what can be kept: MAX_TEXT_CHARS characters, up to 4 bytes each
        raw = fh.read((MAX_TEXT_CHARS + 1) * 4)
    if b"\x00" in raw[:4096]:
        raise ValueError("This looks like a binary file, not text.")
    cut = os.path.getsize(path) > len(raw)
    for trim in (0, 1, 2, 3) if cut else (0,):  # a cut can split a UTF-8 character
        try:
            return raw[:len(raw) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def load_meta(fid):
    if not isinstance(fid, str) or not ID_RE.match(fid):
        return None
    try:
        with open(os.path.join(UPLOAD_DIR, fid, "meta.json")) as fh:
            return json.load(fh)
    except OSError:
        return None


def image_to_sendable(path, ext):
    """Return (path, mime) for an image, converting BMP to PNG if Pillow is available."""
    if ext == ".bmp":
        try:
            from PIL import Image
        except ImportError:
            return path, "image/bmp"
        png = os.path.splitext(path)[0] + ".png"
        Image.open(path).save(png, "PNG")
        os.remove(path)
        return png, "image/png"
    return path, mimetypes.guess_type(path)[0] or "image/" + ext.lstrip(".").replace("jpg", "jpeg")


def process_zip(zpath, folder):
    """Extract a zip safely and turn its allowed members into one text block plus images.

    Nothing is extracted by its archive path, so '../' entries can't escape the folder.
    Returns (text, images, stats) where images is a list of {file, mime, name}.
    """
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise ValueError("This isn't a valid zip file.")
    members = [i for i in zf.infolist() if not i.is_dir()]
    if any(i.flag_bits & 0x1 for i in members):
        raise ValueError("Password-protected zip files aren't supported.")

    work = os.path.join(folder, "members")
    os.makedirs(work, exist_ok=True)
    budget = ZIP_MAX_TOTAL_MB * 1024 * 1024
    included, skipped, texts, images = [], [], [], []

    for idx, info in enumerate(sorted(members, key=lambda i: i.filename)):
        arc = info.filename.replace("\\", "/")
        parts = [p for p in arc.split("/") if p not in ("", ".", "..")]
        base = parts[-1] if parts else ""
        ext = posixpath.splitext(base)[1].lower()
        if (not base or base.startswith("._") or base == ".DS_Store"
                or any(p.lower() in SKIP_DIRS for p in parts[:-1])):
            continue  # OS/tooling junk: ignore silently
        shown = "/".join(parts)
        if len(included) >= ZIP_MAX_FILES:
            skipped.append((shown, "file limit reached")); continue
        if ext in ARCHIVE_EXTS:
            skipped.append((shown, "nested zip")); continue
        if ext not in ALLOWED_EXTS and base.lower() not in SPECIAL_NAMES:
            skipped.append((shown, "type not allowed")); continue
        if info.file_size > MAX_UPLOAD_MB * 1024 * 1024:
            skipped.append((shown, f"over {MAX_UPLOAD_MB} MB")); continue
        if info.file_size > budget:
            skipped.append((shown, "zip size limit reached")); continue
        budget -= info.file_size

        tmp = os.path.join(work, f"{idx}{ext}")
        with zf.open(info) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)

        if ext in IMAGE_EXTS:
            if len(images) >= ZIP_MAX_IMAGES:
                os.remove(tmp); skipped.append((shown, f"over {ZIP_MAX_IMAGES} images")); continue
            try:
                ipath, imime = image_to_sendable(tmp, ext)
            except Exception:
                skipped.append((shown, "unreadable image")); continue
            images.append({"file": os.path.relpath(ipath, folder), "mime": imime, "name": shown})
            included.append(shown)
            continue

        try:
            body = extract_text(tmp, base, mimetypes.guess_type(base)[0] or "")
        except Exception as e:
            skipped.append((shown, str(e))); continue
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        if not body.strip():
            skipped.append((shown, "no readable text")); continue
        texts.append((shown, body))
        included.append(shown)

    zf.close()

    # Share the text budget fairly: smallest files first, each gets at most an equal share
    # of what's left, so small files stay whole and only the largest ones are trimmed.
    alloc, left = {}, MAX_TEXT_CHARS
    order = sorted(range(len(texts)), key=lambda i: len(texts[i][1]))
    for k, i in enumerate(order):
        alloc[i] = min(len(texts[i][1]), left // (len(order) - k))
        left -= alloc[i]
    sections, trimmed = [], []
    for i, (shown, body) in enumerate(texts):
        if alloc[i] < len(body):
            trimmed.append(shown)
            body = body[:alloc[i]] + f"\n[... truncated: showing {alloc[i]:,} of {len(body):,} characters]"
        sections.append(f"=== {shown} ===\n{body}")
    truncated = bool(trimmed)
    header = [f"Archive contents ({len(included)} file(s) included):"] + [
        f"  {n}" + ("  (truncated)" if n in trimmed else "") for n in included]
    if skipped:
        header += [f"Skipped ({len(skipped)}):"] + [f"  {n} - {why}" for n, why in skipped[:100]]
        if len(skipped) > 100:
            header.append(f"  ... and {len(skipped) - 100} more")
    text = "\n".join(header) + ("\n\n" + "\n\n".join(sections) if sections else "")
    stats = {"files": len(included), "skipped": len(skipped), "images": len(images), "trimmed": len(trimmed)}
    return text, images, stats, truncated


@app.errorhandler(Exception)
def unhandled(e):
    """Anything a route didn't catch: log it with the traceback, answer with JSON (no debugger pages)."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error: %s", e)
    if request.path.startswith("/api/"):
        return jsonify(error=f"Internal error: {type(e).__name__}: {e}. Details are in the server log."), 500
    return Response("Internal error. Details are in the server log.", status=500, mimetype="text/plain")


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"File is larger than the upload limit ({limit_text()})."), 413


def limit_text():
    if MAX_TABLE_UPLOAD_MB > MAX_UPLOAD_MB:
        return f"{MAX_UPLOAD_MB} MB, or {MAX_TABLE_UPLOAD_MB} MB for CSV, TSV and Excel"
    return f"{MAX_UPLOAD_MB} MB"


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No file received."), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS and f.filename.lower() not in SPECIAL_NAMES:
        return jsonify(error=f"{ext or 'Files without an extension'} isn't an allowed file type."), 400
    fid = uuid.uuid4().hex
    folder = os.path.join(UPLOAD_DIR, fid)
    with _uploads_lock:   # while processing, Storage shows it as uploading and it can't be deleted
        _in_progress[fid] = (f.filename, time.time())
    try:
        os.makedirs(folder)
        return _process_upload(f, ext, fid, folder)
    except Exception as e:  # e.g. the folder was removed by hand mid-way: answer cleanly, don't leave half a folder
        ulog.exception("Upload of %s failed: %s", f.filename, e)
        remove_tree(folder)
        return jsonify(error=f"The upload couldn't be processed: {e}"), 500
    finally:
        with _uploads_lock:
            _in_progress.pop(fid, None)


def _process_upload(f, ext, fid, folder):
    name = secure_filename(f.filename) or "file"
    path = os.path.join(folder, name)
    f.save(path)
    mime = f.mimetype if f.mimetype and f.mimetype != "application/octet-stream" else (
        mimetypes.guess_type(name)[0] or "application/octet-stream")
    meta = {"id": fid, "name": f.filename, "file": name, "mime": mime,
            "size": os.path.getsize(path), "kind": "image", "truncated": False, "uploaded": time.time()}
    tabular = filesql.available() and filesql.is_tabular(name)
    limit_mb = MAX_TABLE_UPLOAD_MB if tabular else MAX_UPLOAD_MB
    if meta["size"] > limit_mb * 1024 * 1024:
        remove_tree(folder)
        return jsonify(error=f"File is larger than the {limit_mb} MB limit for this type ({limit_text()})."), 413

    if ext in ARCHIVE_EXTS:
        try:
            text, images, stats, truncated = process_zip(path, folder)
        except Exception as e:
            ulog.warning("Zip %s rejected: %s", f.filename, e)
            remove_tree(folder)
            return jsonify(error=str(e)), 400
        if not stats["files"]:
            remove_tree(folder)
            return jsonify(error="The zip has no files of an allowed type."), 400
        meta.update(kind="zip", mime="application/zip", images=images, stats=stats, truncated=truncated)
        with open(os.path.join(folder, "extracted.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
    elif ext in IMAGE_EXTS:
        meta["mime"] = mimetypes.guess_type(name)[0] or mime
        if ext == ".bmp":  # most APIs reject BMP, so convert to PNG when Pillow is available
            try:
                from PIL import Image
                png = os.path.splitext(name)[0] + ".png"
                Image.open(path).save(os.path.join(folder, png), "PNG")
                os.remove(path)
                meta.update(file=png, mime="image/png", size=os.path.getsize(os.path.join(folder, png)))
            except ImportError:
                pass
            except Exception:
                remove_tree(folder)
                return jsonify(error="This BMP image couldn't be read."), 400
    else:
        tables, warnings, table_error = [], [], None
        if tabular:
            try:
                tables = filesql.prepare(path, name, folder, warnings)
            except Exception as e:   # not loadable as a table: say why, and fall back to text below
                ulog.warning("%s (%s bytes) not loaded as a table: %s", f.filename, meta["size"], e,
                             exc_info=not isinstance(e, ValueError))
                table_error, tables = str(e).strip() or type(e).__name__, []
        elif filesql.is_tabular(name):
            table_error = filesql.off_reason()
        if table_error:
            meta["table_error"] = table_error
        if warnings:
            meta["warnings"] = warnings[:20]
            ulog.warning("%s loaded with notes: %s", f.filename, " | ".join(warnings[:20]))
        if tables:
            # The model gets a preview plus SQL access, so only the first lines are kept as text.
            text = filesql.head_text(folder, tables)
            filesql.cleanup(folder)
            meta["tables"] = tables
        else:
            if meta["size"] > MAX_UPLOAD_MB * 1024 * 1024:
                remove_tree(folder)
                return jsonify(error=f"This file couldn't be loaded as a table ({table_error or 'unknown reason'}), "
                                     f"and files read as text are limited to {MAX_UPLOAD_MB} MB."), 400
            try:
                text = extract_text(path, name, mime)
            except Exception as e:
                ulog.warning("%s couldn't be read: %s%s", f.filename, e, f" (table loader: {table_error})" if table_error else "")
                remove_tree(folder)
                return jsonify(error=table_error or str(e)), 400   # the table loader's reason is the clearer one
        if not text.strip():
            remove_tree(folder)
            return jsonify(error="No readable text found in this file (scanned PDFs need OCR)."), 400
        meta["kind"] = "text"
        meta["truncated"] = len(text) > MAX_TEXT_CHARS
        meta["chars"] = len(text)
        with open(os.path.join(folder, "extracted.txt"), "w", encoding="utf-8") as fh:
            fh.write(text[:MAX_TEXT_CHARS])

    with open(os.path.join(folder, "meta.json"), "w") as fh:
        json.dump(meta, fh)
    out = {k: meta[k] for k in ("id", "name", "mime", "size", "kind", "truncated", "stats", "table_error", "warnings") if k in meta}
    ulog.info("Uploaded %s: %s bytes, %s", meta["name"], f"{meta['size']:,}",
              f"{len(meta['tables'])} table(s), {sum(t['rows'] for t in meta['tables']):,} rows" if meta.get("tables")
              else meta["kind"] + (" (truncated)" if meta.get("truncated") else ""))
    if meta.get("tables"):
        out["tables"] = [{"rows": t["rows"], "columns": len(t["columns"]), "sheet": t["sheet"]} for t in meta["tables"]]
    return jsonify(out)


def ingest_generated(filename, data, mime):
    """Store a file the app made itself (a table or screenshot taken from the browser) exactly like an
    upload, so it's attached to the chat, listed in Storage and, for CSV, loaded as an SQL table.
    Returns the same JSON an upload returns; raises ValueError with the reason if it's rejected."""
    from werkzeug.datastructures import FileStorage
    ext = os.path.splitext(filename)[1].lower()
    fid = uuid.uuid4().hex
    folder = os.path.join(UPLOAD_DIR, fid)
    with _uploads_lock:
        _in_progress[fid] = (filename, time.time())
    try:
        os.makedirs(folder)
        resp = _process_upload(FileStorage(stream=io.BytesIO(data), filename=filename, content_type=mime),
                               ext, fid, folder)
    except Exception:
        remove_tree(folder)
        raise
    finally:
        with _uploads_lock:
            _in_progress.pop(fid, None)
    if isinstance(resp, tuple):          # (response, status): rejected, e.g. too large
        raise ValueError((resp[0].get_json() or {}).get("error") or "the file was rejected")
    return resp.get_json()


# ---------------------------------------------------------------- stored uploads (Storage panel)
_uploads_lock = threading.Lock()
_in_progress = {}   # file id -> (original name, start time) for uploads still being processed


def remove_tree(folder, attempts=5):
    """Delete a folder, retrying: on Windows, antivirus or indexing often holds new files open for a moment.

    Clears read-only flags as it goes. Returns None when the folder is gone, else the last error.
    """
    def retry_writable(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    handler = {"onexc": retry_writable} if sys.version_info >= (3, 12) else {"onerror": retry_writable}
    err = None
    for i in range(attempts):
        if not os.path.exists(folder):
            return None
        try:
            shutil.rmtree(folder, **handler)
        except Exception as e:
            err = e
        if not os.path.exists(folder):
            return None
        gc.collect()                    # release any file handles this process still holds
        time.sleep(0.15 * 2 ** i)       # 0.15 s, 0.3 s, 0.6 s, 1.2 s
    return err or OSError("the folder or a file in it is in use")


def _delete_error_text(folder, err):
    left = [os.path.relpath(os.path.join(r, n), folder) for r, _, fs in os.walk(folder) for n in fs]
    what = f" ({', '.join(left[:3])}{'…' if len(left) > 3 else ''} still there)" if left else ""
    return (f"Couldn't delete{what}: {err}. Another program may have it open (antivirus, search indexing, "
            "a file manager window, or Excel); try again in a moment.")
def _folder_bytes(folder):
    total = 0
    for root, _, files in os.walk(folder):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@app.get("/api/files")
def list_files():
    """Every upload on the server: name, original size, size on disk (incl. tables), upload time, tables."""
    out, total = [], 0
    try:
        names = os.listdir(UPLOAD_DIR)
    except OSError:
        names = []
    for fid in names:
        folder = os.path.join(UPLOAD_DIR, fid)
        if not ID_RE.match(fid) or not os.path.isdir(folder):
            continue
        with _uploads_lock:
            busy = _in_progress.get(fid)
        disk = _folder_bytes(folder)
        meta = load_meta(fid)
        if busy:
            total += disk
            out.append({"id": fid, "name": busy[0], "kind": "uploading", "size": 0, "disk": disk,
                        "uploaded": busy[1], "processing": True, "tables": []})
            continue
        if not meta and not any(fs for _, _, fs in os.walk(folder)) and time.time() - os.path.getmtime(folder) > 60:
            if remove_tree(folder, attempts=1) is None:   # an empty leftover folder: nothing to show, just tidy it
                continue
        total += disk
        if meta:
            uploaded = meta.get("uploaded") or os.path.getmtime(os.path.join(folder, "meta.json"))
            out.append({"id": fid, "name": meta["name"], "kind": meta["kind"], "size": meta.get("size", 0),
                        "disk": disk, "uploaded": uploaded, "stats": meta.get("stats"),
                        "tables": [{"rows": t["rows"], "columns": len(t["columns"]), "sheet": t["sheet"]}
                                   for t in meta.get("tables") or []]})
        else:  # an upload that never finished, or a delete that couldn't remove everything
            out.append({"id": fid, "name": "(incomplete upload)", "kind": "incomplete", "size": 0, "disk": disk,
                        "uploaded": os.path.getmtime(folder), "incomplete": True, "tables": []})
    out.sort(key=lambda f: f["uploaded"], reverse=True)
    return jsonify(files=out, total_disk=total)


def delete_uploads(ids):
    """Delete uploads. Already-missing ones count as deleted; ones that can't be removed are reported."""
    deleted, failed, freed = [], [], 0
    for fid in ids:
        if not isinstance(fid, str) or not ID_RE.match(fid):
            continue
        with _uploads_lock:
            busy = fid in _in_progress
        if busy:
            failed.append({"id": fid, "error": "It's still uploading. Try again when it has finished."})
            continue
        folder = os.path.join(UPLOAD_DIR, fid)
        if not os.path.isdir(folder):
            deleted.append(fid)
            continue
        size = _folder_bytes(folder)
        err = remove_tree(folder)
        if err is None:
            deleted.append(fid)
            freed += size
        else:
            failed.append({"id": fid, "error": _delete_error_text(folder, err)})
            ulog.warning("Couldn't delete upload %s: %s", fid, failed[-1]["error"])
            freed += size - _folder_bytes(folder)
    return {"deleted": deleted, "failed": failed, "freed": freed}


@app.delete("/api/files/<fid>")
def delete_file(fid):
    if not ID_RE.match(fid) or not os.path.isdir(os.path.join(UPLOAD_DIR, fid)):
        return jsonify(error="That file no longer exists."), 404
    r = delete_uploads([fid])
    if r["failed"]:
        return jsonify(dict(r, error=r["failed"][0]["error"])), 409
    return jsonify(r)


@app.post("/api/files/delete")
def delete_files():
    ids = (request.get_json(force=True) or {}).get("ids") or []
    return jsonify(delete_uploads(ids[:5000] if isinstance(ids, list) else []))


# ---------------------------------------------------------------- user guide (Help link)
GUIDE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "USER_GUIDE.md")
GUIDE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Datasquint user guide</title>
<script>
// same colour theme as the app (saved by Settings in this browser)
try { const t = JSON.parse(localStorage.getItem("datasquint.theme") || localStorage.getItem("pgpeek.theme") || "{}"), r = document.documentElement, ok = c => /^#[0-9a-f]{6}$/i.test(c || "");
  if (t.mode === "light" || t.mode === "dark") r.dataset.mode = t.mode;
  if (ok(t.light)) r.style.setProperty("--accent-l", t.light);
  if (ok(t.dark)) r.style.setProperty("--accent-d", t.dark); } catch (e) {}
</script>
<style>
:root{--bg:#fff;--ink:#1b2430;--muted:#66727f;--line:#dfe3e8;--code:#f4f6f8;--accent:var(--accent-l,#2f6f8f);color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-mode=light]){--bg:#141a21;--ink:#e3e8ee;--muted:#8c99a6;--line:#2a3440;--code:#1d252f;--accent:var(--accent-d,#5ba3c6);color-scheme:dark}}
:root[data-mode=dark]{--bg:#141a21;--ink:#e3e8ee;--muted:#8c99a6;--line:#2a3440;--code:#1d252f;--accent:var(--accent-d,#5ba3c6);color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:820px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:28px;margin:0 0 12px}h2{font-size:21px;margin:36px 0 10px;padding-top:12px;border-top:1px solid var(--line)}
a{color:var(--accent)}code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em}
pre{background:var(--code);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}pre code{padding:0}
table{border-collapse:collapse;margin:10px 0;font-size:15px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}th{background:var(--code)}
blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid var(--accent);background:var(--code);border-radius:0 8px 8px 0}
#raw{white-space:pre-wrap}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
</head><body><main id="doc">Loading…</main>
<footer style="max-width:820px;margin:0 auto;padding:0 20px 40px;color:var(--muted);font-size:13px">Datasquint __VERSION__</footer>
<script>
fetch("guide.md").then(r => r.text()).then(text => {
  const doc = document.getElementById("doc");
  if (!window.marked || !window.DOMPurify) { doc.innerHTML = '<pre id="raw"></pre>'; doc.firstChild.textContent = text; return; }
  doc.innerHTML = DOMPurify.sanitize(marked.parse(text));
  // GitHub-style heading ids so the contents links work
  doc.querySelectorAll("h1,h2,h3").forEach(h => { h.id = h.textContent.trim().toLowerCase().replace(/[^a-z0-9 -]/g, "").replace(/ /g, "-"); });
  if (location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
});
</script></body></html>"""


@app.get("/guide")
def guide():
    return Response(GUIDE_PAGE.replace("__VERSION__", VERSION), mimetype="text/html")


@app.get("/guide.md")
def guide_md():
    try:
        with open(GUIDE_FILE, encoding="utf-8") as fh:
            return Response(fh.read(), mimetype="text/markdown; charset=utf-8")
    except OSError:
        abort(404)


@app.get("/files/<fid>")
def get_file(fid):
    meta = load_meta(fid)
    if not meta:
        abort(404)
    return send_from_directory(os.path.join(UPLOAD_DIR, fid), meta["file"], mimetype=meta["mime"],
                               download_name=meta["name"])


def chat_file_ids(raw):
    """Ids of tabular (CSV/TSV/Excel) attachments in a chat, in message order."""
    ids = []
    for m in raw:
        if m.get("role") != "user":
            continue
        for a in m.get("attachments") or []:
            fid = a.get("id") if isinstance(a, dict) else a
            if isinstance(fid, str) and fid not in ids:
                ids.append(fid)
    return ids


def file_tables(ids):
    """Chat-level file tables (empty if DuckDB isn't installed or FILE_TABLES=0)."""
    if not filesql.available():
        return []
    return filesql.chat_tables([i for i in ids if isinstance(i, str)], load_meta, UPLOAD_DIR)


def collect_attachments(msg, ftables=None):
    """Return (docs, images) for a message: docs = [(label, text)], images = [data URLs].

    ftables maps file id -> its SQL tables; those files are sent as a short preview, not in full.
    """
    docs, images = [], []
    for a in msg.get("attachments") or []:
        fid = a.get("id") if isinstance(a, dict) else a
        meta = load_meta(fid)
        if not meta:
            name = a.get("name", fid) if isinstance(a, dict) else fid
            docs.append((name, "[This attachment is no longer available]"))
            continue
        folder = os.path.join(UPLOAD_DIR, fid)
        if meta["kind"] == "image":
            with open(os.path.join(folder, meta["file"]), "rb") as fh:
                images.append(f"data:{meta['mime']};base64,{base64.b64encode(fh.read()).decode()}")
            continue
        with open(os.path.join(folder, "extracted.txt"), encoding="utf-8") as fh:
            body = fh.read()
        if meta["kind"] == "zip":
            label = f"Zip archive: {meta['name']}" + (" (text truncated)" if meta.get("truncated") else "")
            for im in meta.get("images", []):
                with open(os.path.join(folder, im["file"]), "rb") as fh:
                    images.append(f"data:{im['mime']};base64,{base64.b64encode(fh.read()).decode()}")
        elif ftables and ftables.get(fid):
            label = meta["name"] + " (preview)"
            body = filesql.preview(body, ftables[fid])
        elif meta.get("tables"):   # loaded as a table earlier, but SQL over files is now turned off
            label = meta["name"] + " (first lines only)"
        else:
            label = meta["name"] + (" (truncated)" if meta.get("truncated") else "")
        docs.append((label, body))
    return docs, images


def fmt_doc(label, body):
    return f"--- File: {label} ---\n{body}\n--- End of {label} ---"


def build_content(msg, ftables=None):
    """Turn a stored message plus attachment ids into OpenAI message content."""
    text = msg.get("content") or ""
    if msg.get("role") != "user" or not msg.get("attachments"):
        return text
    docs, images = collect_attachments(msg, ftables)
    text = "\n\n".join([text] + [fmt_doc(l, b) for l, b in docs]).strip()
    return with_images(text, images)


def with_images(text, images):
    if not images:
        return text
    parts = [{"type": "text", "text": text or "See the attached image(s)."}]
    parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
    return parts


def char_len(msgs):
    """Characters of text in a message list (images don't count)."""
    n = 0
    for m in msgs:
        c = m["content"]
        n += len(c) if isinstance(c, str) else sum(len(p.get("text", "")) for p in c)
    return n


# ---------------------------------------------------------------- splitting large sends
PART_PROMPT = """My files are too large to send in one message, so I'm sending them in {n} parts. This is part {k} of {n}.

{chunk}

--- End of part {k} of {n} ---

My request: {question}

Using only this part, write notes on everything relevant to my request: key facts, findings, code details, numbers, names, and short quotes, each with the file name it came from. These notes will be combined with notes from the other parts to write the final answer, so be thorough but concise and don't answer the request yet. If nothing in this part is relevant, reply only: Nothing relevant in this part."""

MERGE_PROMPT = """Merge these notes into one set, keeping every detail and file name relevant to this request: {question}

{notes}"""

FINAL_PROMPT = """My files were too large to send at once, so they were read in {n} parts and notes were taken from each part. Here are the notes:

{notes}

--- End of notes ---

Using these notes{img}, respond to my request as if you had read the files directly. Only mention that the files were read in parts if it matters, for example if information may be missing.

My request: {question}"""


def split_text(text, size):
    """Split text into pieces of at most `size` characters, preferring line breaks."""
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def pack(blocks, size):
    """Greedily join text blocks into chunks of at most `size` characters."""
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > size:
            chunks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    return chunks


def doc_chunks(docs, size):
    """Turn documents into chunks of at most `size`, splitting big files into labelled sections."""
    blocks = []
    for label, body in docs:
        segs = split_text(body, max(1000, size - len(label) * 2 - 200))
        for i, seg in enumerate(segs):
            l = label if len(segs) == 1 else f"{label} (section {i + 1} of {len(segs)})"
            blocks.append(fmt_doc(l, seg))
    return pack(blocks, size)


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.get("/api/version")
def version():
    return jsonify(name="Datasquint", version=VERSION)


@app.get("/api/update")
def update_status():
    """Running and latest version on GitHub (?refresh=1 checks again now)."""
    if updater.MODE == "off":
        return jsonify(updater.status())
    return jsonify(updater.check(force=request.args.get("refresh") == "1"))


@app.post("/api/update/install")
def update_install():
    # Only from Datasquint's own page: other websites can't send a JSON request here without the browser asking first
    if not request.is_json:
        return jsonify(error="Send this request as JSON (Content-Type: application/json)."), 415
    log.info("Update requested from %s", request.remote_addr)
    try:
        result = updater.install()
    except PermissionError as e:
        return _err(e, 403)
    except Exception as e:
        log.warning("Update failed: %s", e)
        return _err(e, 400)
    if result["restart"] == "auto":
        updater.restart_soon()
    return jsonify(result)


@app.get("/api/config")
def config():
    return jsonify(version=VERSION, default_model=DEFAULT_MODEL, system_prompt=SYSTEM_PROMPT, base_url=BASE_URL,
                   max_upload_mb=MAX_UPLOAD_MB, max_table_upload_mb=MAX_TABLE_UPLOAD_MB,
                   table_extensions=sorted(filesql.TABLE_EXTS) if filesql.available() else [], max_send_chars=MAX_SEND_CHARS, allowed_extensions=sorted(ALLOWED_EXTS),
                   special_names=sorted(SPECIAL_NAMES),
                   browser={"enabled": webtools.ENABLED, "available": webtools.available(), "error": webtools.off_reason(),
                            "read_actions": sorted(webtools.READ_ACTIONS), "vision": webtools.VISION,
                            "auto_click": os.getenv("AUTO_CLICK", "0") == "1"})


@app.get("/api/models")
def models():
    """List models from the endpoint; fall back to the default if unsupported."""
    try:
        ids = sorted(m.id for m in client.models.list())
        if DEFAULT_MODEL not in ids:
            ids.insert(0, DEFAULT_MODEL)
        return jsonify(models=ids)
    except Exception as e:  # many compatible servers don't implement /models
        log.warning("Model list unavailable from %s: %s", BASE_URL, e)
        return jsonify(models=[DEFAULT_MODEL], warning=str(e))


def ndjson(obj):
    return json.dumps(obj) + "\n"


# Ask streaming replies to report their token usage (OpenAI's stream_options). Servers that reject the option
# are asked once, then no more; their usage is estimated instead. STREAM_USAGE=off never asks.
_stream_usage = os.getenv("STREAM_USAGE", "auto").strip().lower() != "off"


def stream_completion(model, messages, temperature, tally):
    global _stream_usage
    extra = {"stream_options": {"include_usage": True}} if _stream_usage else {}
    try:
        stream = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                stream=True, **extra)
    except openai.BadRequestError as e:
        if not extra or not re.search(r"stream_options|include_usage", str(e), re.I):
            raise
        _stream_usage = False
        clog.info("%s doesn't accept stream_options; token usage will be estimated", BASE_URL)
        stream = client.chat.completions.create(model=model, messages=messages, temperature=temperature, stream=True)
    out_chars, reported = 0, None
    try:
        for chunk in stream:
            if getattr(chunk, "usage", None):
                reported = chunk.usage
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                out_chars += len(text)
                yield ndjson({"delta": text})
    finally:   # also when the user stops the reply: count what was used so far
        if reported and (reported.prompt_tokens or reported.completion_tokens):
            tally.add(model, reported.prompt_tokens or 0, reported.completion_tokens or 0, False)
        elif out_chars:
            tally.add(model, usage.estimate(char_len(messages)), usage.estimate(out_chars), True)


def complete(model, messages, temperature, tally):
    r = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                       max_tokens=PART_NOTE_TOKENS)
    text = (r.choices[0].message.content or "").strip()
    u = getattr(r, "usage", None)
    if u and (u.prompt_tokens or u.completion_tokens):
        tally.add(model, u.prompt_tokens or 0, u.completion_tokens or 0, False)
    else:
        tally.add(model, usage.estimate(char_len(messages)), usage.estimate(len(text)), True)
    return text


def split_send(model, raw, system, temperature, tally, ftables=None):
    """Answer a request that exceeds MAX_SEND_CHARS by reading the files in parts.

    Each part is a separate request under the limit; notes from all parts are then
    merged (in more rounds if needed) and the final answer is streamed.
    """
    sys_msgs = [{"role": "system", "content": system}] if system else []
    docs, images = [], []
    for m in raw:
        if m.get("role") == "user":
            d, i = collect_attachments(m, ftables)
            docs += d
            images += i
    conv = [{"role": m.get("role", "user"), "content": m.get("content") or ""} for m in raw]
    question = conv[-1]["content"] if conv and conv[-1]["role"] == "user" else ""
    history = conv[:-1]

    hist_budget = MAX_SEND_CHARS // 5
    while history and char_len(history) > hist_budget:      # drop oldest turns first
        history.pop(0)
    if history and history[0]["role"] == "assistant":
        history.pop(0)
    if len(question) > hist_budget:                          # huge pasted text becomes a document
        docs.append(("Pasted message", question))
        question = "Respond to my message, which is included above as 'Pasted message'."
    question = question or "Review the attached files and summarise them."

    overhead = len(system) + char_len(history) + len(question) + len(PART_PROMPT) + 100
    size = MAX_SEND_CHARS - overhead
    chunks = doc_chunks(docs, size)
    n = len(chunks)
    yield ndjson({"parts": n})

    notes = []
    for k, chunk in enumerate(chunks, 1):
        yield ndjson({"status": f"Reading part {k} of {n}…"})
        prompt = PART_PROMPT.format(n=n, k=k, chunk=chunk, question=question)
        notes.append(f"[Notes from part {k} of {n}]\n" + complete(model, sys_msgs + history + [
            {"role": "user", "content": prompt}], temperature, tally))

    final_overhead = len(system) + char_len(history) + len(question) + len(FINAL_PROMPT) + 100
    rounds = 0
    while sum(len(x) + 2 for x in notes) > MAX_SEND_CHARS - final_overhead and len(notes) > 1:
        rounds += 1
        groups = pack(notes, MAX_SEND_CHARS - final_overhead)
        if len(groups) >= len(notes) or rounds > 5:          # can't shrink further: trim instead
            share = (MAX_SEND_CHARS - final_overhead) // len(notes)
            notes = [x[:share] for x in notes]
            break
        merged = []
        for g, group in enumerate(groups, 1):
            yield ndjson({"status": f"Combining notes ({g} of {len(groups)})…"})
            merged.append(complete(model, sys_msgs + [{"role": "user", "content": MERGE_PROMPT.format(
                question=question, notes=group)}], temperature, tally))
        notes = merged

    yield ndjson({"status": "Writing answer…"})
    final = FINAL_PROMPT.format(n=n, notes="\n\n".join(notes), question=question,
                                img=" and the attached images" if images else "")
    yield from stream_completion(model, sys_msgs + history + [
        {"role": "user", "content": with_images(final, images)}], temperature, tally)


# ---------------------------------------------------------------- PostgreSQL
def _err(e, code=400):
    (log.error if code >= 500 else log.warning)("%s", str(e).strip().strip("'"), exc_info=code >= 500)
    return jsonify(error=str(e).strip().strip("'")), code


@app.get("/api/db/connections")
def db_list():
    try:
        return jsonify(connections=db.connections(), available=db.available(),
                       ssh_available=db.ssh_tunnel.available())
    except Exception as e:
        return _err(e, 500)


@app.post("/api/db/connections")
def db_create():
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.save(data, force=bool(data.get("force"))))
    except Exception as e:
        return _err(e)


@app.put("/api/db/connections/<cid>")
def db_update(cid):
    data = request.get_json(force=True) or {}
    try:
        return jsonify(db.save(data, cid=cid, force=bool(data.get("force"))))
    except KeyError as e:
        return _err(e, 404)
    except Exception as e:
        return _err(e)


@app.delete("/api/db/connections/<cid>")
def db_delete(cid):
    try:
        db.delete(cid)
        return jsonify(ok=True)
    except Exception as e:
        return _err(e)


@app.get("/api/db/connections/<cid>/status")
def db_status(cid):
    try:
        return jsonify(db.status(cid))
    except KeyError as e:
        return _err(e, 404)


@app.get("/api/fs/keys")
def browse_keys():
    """Folder listing for the SSH key-file picker (names and key types only)."""
    try:
        return jsonify(db.ssh_tunnel.browse(request.args.get("path") or None))
    except PermissionError as e:
        return _err(e, 403)
    except FileNotFoundError as e:
        return _err(e, 404)
    except Exception as e:
        return _err(e)


@app.get("/api/db/connections/<cid>/schema")
def db_schema(cid):
    try:
        return jsonify(db.schema(cid, refresh=request.args.get("refresh") == "1"))
    except KeyError as e:
        return _err(e, 404)
    except Exception as e:
        return _err(e)


@app.post("/api/db/query")
def db_query():
    data = request.get_json(force=True) or {}
    sql, cids = data.get("sql", ""), data.get("dbs") or []
    try:
        tables = file_tables(data.get("files") or [])
        target = db.resolve_target(sql, cids, has_files=bool(tables))
        if target == db.FILES_SOURCE:
            res = filesql.run_query(sql, tables, db.DB_MAX_ROWS, db.DB_TIMEOUT_MS)
        else:
            res = db.run_query(sql, cids, target)
        qlog.info("Query on %s: %s rows in %s ms", res["db_name"], res["row_count"], res["ms"])
        return jsonify(sql=sql, text=db.result_text(sql, res, db_name=res["db_name"]), **res)
    except Exception as e:
        err = str(e).strip()
        qlog.warning("Query failed: %s | SQL: %s", err, _sql_for_log(sql))
        return jsonify(sql=sql, error=err, text=db.result_text(sql, error=err))


@app.post("/api/db/export")
def db_export():
    """Re-run a result card's query (read-only) and return the full result as CSV, up to DB_EXPORT_MAX_ROWS."""
    data = request.get_json(force=True) or {}
    sql, cids = data.get("sql", ""), data.get("dbs") or []
    try:
        tables = file_tables(data.get("files") or [])
        target = db.resolve_target(sql, cids, has_files=bool(tables))
        if target == db.FILES_SOURCE:
            res = filesql.run_query(sql, tables, db.DB_EXPORT_MAX_ROWS, db.DB_TIMEOUT_MS, full=True)
        else:
            res = db.run_query(sql, cids, target, max_rows=db.DB_EXPORT_MAX_ROWS, full=True)
    except Exception as e:
        qlog.warning("CSV download failed: %s | SQL: %s", str(e).strip(), _sql_for_log(sql))
        return jsonify(error=str(e).strip()), 400
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(res["columns"])
    w.writerows(["" if v is None else v for v in row] for row in res["rows"])
    resp = Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8")  # BOM: Excel opens it as UTF-8
    resp.headers["X-Row-Count"] = str(res["row_count"])
    resp.headers["X-Truncated"] = "1" if res["truncated"] else "0"
    resp.headers["X-Max-Rows"] = str(db.DB_EXPORT_MAX_ROWS)
    return resp


# ---------------------------------------------------------------- table browser
@app.post("/api/tables")
def tables_list():
    """Every table the chat can use: its databases' tables and views, and its attached files' tables."""
    data = request.get_json(force=True) or {}
    sources = []
    ftables = file_tables(data.get("files") or [])
    if ftables:
        sources.append({"id": db.FILES_SOURCE, "name": "Files", "kind": "files", "tables": [
            {"name": t["name"], "rows": t["rows"], "columns": t["columns"], "file": t["file"],
             "sheet": t.get("sheet") or "", "title": t.get("title") or ""} for t in ftables]})
    for cid in data.get("dbs") or []:
        try:
            c = db.config(cid)
        except KeyError:
            continue
        src = {"id": cid, "name": c["name"], "kind": "db", "tables": []}
        try:
            src["tables"] = db.table_list(cid, refresh=bool(data.get("refresh")))
        except Exception as e:
            src["error"] = str(e).strip()
            qlog.warning("Table list for %s failed: %s", c["name"], src["error"])
        sources.append(src)
    return jsonify(sources=sources)


def _browse_target(data):
    """(run(sql, max_rows) -> result, quoted table, column names) for a table the chat may use, or ValueError."""
    source, table = data.get("source"), data.get("table")
    if source == db.FILES_SOURCE:
        ftables = file_tables(data.get("files") or [])
        t = next((t for t in ftables if t["name"] == table), None)
        if not t:
            raise ValueError("That table isn't attached to this chat any more.")
        run = lambda sql, n: filesql.run_query(sql, ftables, n, db.DB_TIMEOUT_MS)
        return run, filesql._q(t["name"]), [c for c, _ in t["columns"]], t["rows"]
    if source not in (data.get("dbs") or []):
        raise ValueError("That database isn't turned on for this chat.")
    t = next((t for t in db.table_list(source) if t["name"] == table), None)
    if not t:
        raise ValueError(f"No table {table} in that database (it may have been renamed or dropped).")
    c = db.config(source)
    run = lambda sql, n: db.run_query(sql, [source], c, max_rows=n)
    return run, f"{db.quote_ident(t['schema'])}.{db.quote_ident(t['table'])}", [col for col, _ in t["columns"]], t["rows"]


@app.post("/api/tables/preview")
def tables_preview():
    """A page of rows from one table, optionally sorted and filtered. Runs through the same read-only checks."""
    data = request.get_json(force=True) or {}
    try:
        run, qtable, cols, known = _browse_target(data)
        limit = max(1, min(200, int(data.get("limit") or 50)))
        offset = max(0, int(data.get("offset") or 0))
        where = ""
        q = str(data.get("q") or "").strip()[:200]
        if q:
            lit = "'" + q.lower().replace("'", "''") + "'"
            where = " WHERE " + " OR ".join(f"strpos(lower({db.quote_ident(c)}::text), {lit}) > 0" for c in cols)
        order = ""
        if data.get("sort") in cols:
            order = f" ORDER BY {db.quote_ident(data['sort'])} {'DESC' if data.get('desc') else 'ASC'} NULLS LAST"
        res = run(f"SELECT * FROM {qtable}{where}{order} LIMIT {limit} OFFSET {offset}", limit)
        total = known if not where and data.get("source") == db.FILES_SOURCE else None
        if total is None and (where or known is None):   # filtered, or a table PostgreSQL has no estimate for
            try:
                total = run(f"SELECT count(*) FROM {qtable}{where}", 1)["rows"][0][0]
            except Exception:
                total = None   # e.g. a filtered count that times out on a huge table
        return jsonify(columns=res["columns"], rows=res["rows"], offset=offset, limit=limit, total=total,
                       estimate=None if where or data.get("source") == db.FILES_SOURCE else known,
                       ms=res["ms"], sql=f"SELECT * FROM {qtable}{where}{order}")
    except Exception as e:
        qlog.warning("Table preview of %s (%s) failed: %s", data.get("table"), data.get("source"), str(e).strip())
        return jsonify(error=str(e).strip()), 400


# ---------------------------------------------------------------- Python script
@app.post("/api/script")
def make_script():
    """A standalone .py that re-runs the given queries offline (see scriptgen.py)."""
    data = request.get_json(force=True) or {}
    cids = data.get("dbs") or []
    ftables = file_tables(data.get("files") or [])
    queries, used_dbs, seen = [], {}, set()
    for item in data.get("queries") or []:
        sql = str(item.get("sql") or "").strip()
        if not sql or sql in seen:
            continue
        seen.add(sql)
        try:
            target = db.resolve_target(sql, cids, has_files=bool(ftables))
        except ValueError:
            continue
        first = next((ln.strip() for ln in sql.splitlines() if ln.strip() and not ln.strip().startswith("--")), sql)
        title = str(item.get("title") or "").strip() or first[:70]
        if target == db.FILES_SOURCE:
            queries.append({"title": title, "sql": sql, "source": db.FILES_SOURCE})
        else:
            pub = db.public(target)
            used_dbs[target["name"]] = {"host": pub.get("host"), "port": pub.get("port"), "dbname": pub.get("dbname"),
                                        "user": pub.get("user"), "sslmode": pub.get("sslmode") or "",
                                        "ssh": {k: pub["ssh"].get(k) for k in ("host", "port", "user")} if pub.get("ssh") else None}
            queries.append({"title": title, "sql": sql, "source": target["name"]})
    if not queries:
        qlog.warning("Python script requested, but none of %d queries could be resolved", len(data.get("queries") or []))
        return jsonify(error="There are no queries to put in a script: run a query first."), 400
    file_sql = " ".join(q["sql"] for q in queries if q["source"] == db.FILES_SOURCE).lower()
    used_tables = []
    for t in ftables:
        if re.search(r"\b" + re.escape(t["name"].lower()) + r"\b", file_sql):
            load = t.get("load")
            used_tables.append({"name": t["name"], "file": t["file"], "sheet": t.get("sheet") or "",
                                "approximate": load is None,
                                "load": load or ({"kind": "sheet", "sheet": t["sheet"], "col_offset": 0} if t.get("sheet")
                                                 else {"kind": "csv", "delim": "\t" if t["file"].lower().endswith(".tsv") else None})})
    title = str(data.get("title") or "Datasquint queries").strip()
    text = scriptgen.build(title, queries, used_tables, used_dbs)
    name = (re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:50] or "datasquint_queries") + ".py"
    return Response(text, mimetype="text/x-python",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Queries": str(len(queries))})


def _sql_for_log(sql, limit=500):
    sql = " ".join(str(sql or "").split())
    return sql if len(sql) <= limit else sql[:limit] + "…"


_browser_errors = {}


@app.post("/api/log")
def browser_log():
    """Errors from the page (JavaScript errors, and error messages shown to the user), into the server log."""
    data = request.get_json(force=True, silent=True) or {}
    key = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in _browser_errors.get(key, []) if now - t < 60]
    if len(recent) >= 30:   # at most 30 a minute per client, so a broken page can't flood the log
        return jsonify(ok=False), 429
    _browser_errors[key] = recent + [now]
    kind = str(data.get("kind") or "error")[:20]
    msg = str(data.get("message") or "")[:1000]
    where = " ".join(str(data.get(k) or "")[:200] for k in ("source", "line") if data.get(k))
    stack = str(data.get("stack") or "")[:3000]
    logsetup.child("browser").warning("%s: %s%s%s", kind, msg, f" ({where})" if where else "",
                                      ("\n" + stack) if stack else "")
    return jsonify(ok=True)


@app.get("/api/logs")
def recent_logs():
    """The end of the log file, only for requests from the server itself (logs can contain SQL and file names)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify(error="The log can only be read on the server itself."), 403
    n = max(1, min(2000, int(request.args.get("lines", 200))))
    return Response("\n".join(logsetup.tail(n)) + "\n", mimetype="text/plain")


# ---------------------------------------------------------------- browser (webtools.py, browser.py)
def _browser_ready():
    if not webtools.available():
        raise webtools.BrowserError(webtools.off_reason())


def _browser_locked(fn, *a, **kw):
    """Manual controls wait briefly for a running browser action instead of fighting it."""
    _browser_ready()
    b = webtools.browser
    if not b.lock.acquire(timeout=30):
        raise webtools.BrowserError("The browser is busy with an action; try again in a moment.")
    try:
        return fn(*a, **kw)
    finally:
        b.lock.release()


def _berr(e):
    first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return jsonify(error=first), 400


@app.get("/api/browser/status")
def browser_status():
    if not webtools.available():
        return jsonify(available=False, enabled=webtools.ENABLED, running=False, error=webtools.off_reason())
    from browser import BROWSERS, default_kind
    return jsonify(available=True, vision=webtools.VISION, default_browser=default_kind(),
                   browsers=[{"id": k, "name": b["name"], "attach": os.getenv(b["env"] + "_DEBUGGER", "")}
                             for k, b in BROWSERS.items()],
                   attach=os.getenv(webtools.browser.kind.upper() + "_DEBUGGER", ""),
                   headless=os.getenv("HEADLESS", "0") == "1", start_url=os.getenv("START_URL", ""),
                   **webtools.browser.status())


@app.post("/api/browser/start")
def browser_start():
    data = request.get_json(silent=True) or {}
    try:
        _browser_ready()
        st = _browser_locked(webtools.browser.start, headless=data.get("headless"), kind=data.get("browser"))
        url = (data.get("url") or os.getenv("START_URL", "")).strip()
        if url and st.get("mode") == "launched" and (st.get("url") or "").startswith(("about:", "data:", "edge:", "chrome:")):
            _browser_locked(webtools.browser.navigate, url)
        blog.info("%s started (%s)", st.get("browser_name", "Browser"), webtools.browser.mode)
        return jsonify(available=True, **webtools.browser.status())
    except Exception as e:
        blog.error("Browser didn't start: %s", e)
        return _berr(e)


@app.post("/api/browser/close")
def browser_close():
    try:
        _browser_ready()
        return jsonify(available=True, **_browser_locked(webtools.browser.stop))
    except Exception as e:
        return _berr(e)


@app.post("/api/browser/navigate")
def browser_navigate():
    try:
        _browser_ready()
        data = request.get_json(silent=True) or {}
        if data.get("browser") and not webtools.browser.alive():    # Go with nothing open: start the one picked
            _browser_locked(webtools.browser.start, kind=data.get("browser"))
        _browser_locked(webtools.browser.navigate, data.get("url", ""))
        return jsonify(available=True, **webtools.browser.status())
    except Exception as e:
        return _berr(e)


@app.get("/api/browser/screenshot")
def browser_screenshot():
    try:
        _browser_ready()
        return jsonify(image=_browser_locked(webtools.browser.screenshot_b64))
    except Exception as e:
        return _berr(e)


@app.get("/api/browser/page")
def browser_page():
    """The page text the model gets ("What the AI sees")."""
    try:
        _browser_ready()
        text, chars = _browser_locked(webtools.page_text, int(request.args.get("part", 1)))
        return jsonify(text=text, chars=chars)
    except Exception as e:
        return _berr(e)


@app.post("/api/browser/cancel")
def browser_cancel():
    """Stop pressed while an action runs: paginate / click_all / wait stop at the next page or click."""
    webtools.cancel_event.set()
    return jsonify(ok=True)


@app.post("/api/browser/action")
def browser_action():
    """Run one ```action block. Tables become CSV files attached to the chat (SQL tables), screenshots images.

    Body: {"action": "<the block's JSON text>", "files": [chat's attachment ids, in order]}
    Returns {"action", "title", "text" (for the model), "attachments": [upload JSON], "error"}.
    """
    data = request.get_json(force=True) or {}
    raw = data.get("action")
    action, err = webtools.parse(raw if isinstance(raw, str) else json.dumps(raw))
    if err:
        return jsonify(error=err, title="Action not understood", text=f"```action\n{raw}\n```\nError: {err}")
    title = webtools.describe(action)
    head = f"```action\n{json.dumps(action, ensure_ascii=False)}\n```\n"
    t0 = time.time()
    try:
        _browser_ready()
        if data.get("browser") and not webtools.browser.alive():   # nothing open: start the one picked in the menu
            _browser_locked(webtools.browser.start, kind=data.get("browser"))
        res = webtools.execute(action)
    except Exception as e:
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        if not isinstance(e, webtools.BrowserError):
            blog.exception("Action %s failed", title)
        else:
            blog.warning("Action %s failed: %s", title, msg)
        return jsonify(action=action, title=title, error=msg, text=head + "Error: " + msg)
    atts, text = [], res["text"]
    try:
        if res.get("csv"):
            name, body = res["csv"]
            meta = ingest_generated(name, body, "text/csv")
            atts.append(meta)
            if meta.get("tables"):
                ids = [i for i in (data.get("files") or []) if isinstance(i, str)] + [meta["id"]]
                names = [t["name"] for t in file_tables(ids) if t["fid"] == meta["id"]]
                text += (f"\n\nSaved as {name}, attached to this chat. Query it with SQL as table "
                         f"{', '.join(names) or name} (source '{db.FILES_SOURCE}').")
            else:
                text += f"\n\nSaved as {name}, attached to this chat (not loaded as a SQL table: " \
                        f"{meta.get('table_error') or 'SQL over files is off'})."
        if res.get("png"):
            atts.append(ingest_generated(time.strftime("screenshot-%H%M%S.png"), res["png"], "image/png"))
    except Exception as e:
        blog.warning("Couldn't attach the result of %s: %s", title, e)
        text += f"\n\n(The result couldn't be saved as a file: {e})"
    blog.info("Action %s: %.1f s%s", title, time.time() - t0,
              f", attached {', '.join(a['name'] for a in atts)}" if atts else "")
    return jsonify(action=action, title=title, text=head + text, attachments=atts,
                   url=webtools.browser.status().get("url", ""))


@app.get("/api/usage")
def usage_get():
    """Token counters: totals and per model, since the last reset."""
    return jsonify(usage.summary())


@app.post("/api/usage/reset")
def usage_reset():
    if not request.is_json:
        return jsonify(error="Send this request as JSON (Content-Type: application/json)."), 415
    clog.info("Token usage counters reset from %s", request.remote_addr)
    return jsonify(usage.reset())


@app.post("/api/chat")
def chat():
    """Stream a completion back as newline-delimited JSON.

    Events: {"delta": text}, {"status": text}, {"parts": n}, {"usage": {prompt, completion, estimated}},
    {"done": true}, {"error": text}.
    """
    data = request.get_json(force=True)
    raw = data.get("messages", [])
    model = data.get("model") or DEFAULT_MODEL
    temperature = float(data.get("temperature", 0.7))
    system = (data.get("system") or "").strip()

    dbs = [d for d in (data.get("dbs") or []) if isinstance(d, str)]
    tables = file_tables(chat_file_ids(raw))
    ftables = {}
    for t in tables:
        ftables.setdefault(t["fid"], []).append(t)
    if dbs or tables:
        try:
            system = (system + "\n\n" + db.system_prompt(dbs, filesql.prompt_section(tables) if tables else None)).strip()
        except Exception as e:
            clog.warning("Data sources unavailable for a chat: %s", e)
            system = (system + f"\n\n(Databases are enabled for this chat but unavailable: {e}. "
                      "Tell the user if they ask for data.)").strip()
    if data.get("browser") and webtools.ENABLED:
        # read now, so the model sees the page as it is at this moment (after the last click)
        system = (system + "\n\n" + webtools.system_section()).strip()

    messages = [{"role": m.get("role", "user"), "content": build_content(m, ftables)} for m in raw]
    if system:
        messages = [{"role": "system", "content": system}] + messages

    def generate():
        t0, size = time.time(), char_len(messages)
        tally = usage.Tally()
        try:
            if size <= MAX_SEND_CHARS:
                yield from stream_completion(model, messages, temperature, tally)
            else:
                yield from split_send(model, raw, system, temperature, tally, ftables)
            yield ndjson({"usage": tally.as_dict()})
            yield ndjson({"done": True})
            clog.info("Reply from %s: %s chars sent, %.1f s, tokens %s in / %s out%s", model, f"{size:,}", time.time() - t0,
                      f"{tally.prompt:,}", f"{tally.completion:,}", " (estimated)" if tally.estimated else "")
        except GeneratorExit:   # the user pressed Stop or closed the page
            clog.info("Reply from %s stopped by the user after %.1f s", model, time.time() - t0)
            raise
        except Exception as e:
            # provider errors (bad key, unknown model, rate limit, context too long) are expected: no traceback
            known = isinstance(e, openai.APIError)
            clog.error("Reply from %s failed after %.1f s (%s chars sent): %s: %s", model, time.time() - t0,
                       f"{size:,}", type(e).__name__, e, exc_info=not known)
            yield ndjson({"error": f"{type(e).__name__}: {e}"})

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    # Debug mode is off by default: its in-browser debugger can run code, and its auto-reloader would
    # restart the server whenever a .zip lands in uploads/. FLASK_DEBUG=1 turns it on for development.
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    host = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_text = os.getenv("PORT", "5000").strip() or "5000"
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        sys.exit(f"PORT={port_text!r} in .env isn't a valid port number. Use a number from 1 to 65535, e.g. PORT=8080.")
    port = int(port_text)
    # Fail early with a clear message if something else already uses the port. Right after an update the
    # old process may still be letting go of it, so keep trying for a while then.
    import socket
    tries = 30 if os.getenv("DATASQUINT_RESTARTED") == "1" else 1
    for attempt in range(tries):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
            if os.name != "nt":   # like the web server itself: ignore connections still closing from a restart
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                break
            except OSError as e:
                if attempt + 1 < tries:
                    time.sleep(0.5)
                    continue
                sys.exit(f"Can't use port {port} on {host}: {e.strerror or e}. Another program (or another Datasquint) "
                         f"may be using it. Set a different PORT in .env, e.g. PORT={port + 1}.")
    updater.SELF_RESTART = not debug   # in debug mode Flask's reloader picks up the new code by itself
    print(f" * Datasquint {VERSION}" + (" (restarted after update)" if os.getenv("DATASQUINT_RESTARTED") == "1" else ""))
    print(f" * SQL over CSV/TSV/Excel files: {filesql.status()}"
          + (f", up to {MAX_TABLE_UPLOAD_MB} MB per file" if filesql.available() else f"; those files are read as text, up to {MAX_UPLOAD_MB} MB"))
    shown = "localhost" if host in ("127.0.0.1", "localhost") else ("<this computer's address>" if host in ("0.0.0.0", "::") else host)
    import recalc
    print(" * Excel formulas saved without results: " + ("not calculated (EXCEL_RECALC=off)" if filesql.EXCEL_RECALC == "off"
          else f"calculated with {recalc.available()}"))
    print(f" * Open http://{shown}:{port} in your browser")
    print(f" * Error log: {LOG_PATH or 'console only (the log file could not be opened)'}")
    print(f" * Browser (web pages in Edge/Chrome): {webtools.status_line()}")
    print(" * Updates from GitHub: " + {"on": f"on ({updater.REPO}, {updater.BRANCH}); install from Settings → Updates",
                                        "check": f"check only ({updater.REPO}, {updater.BRANCH})",
                                        "off": "off (UPDATES=off)"}[updater.MODE])
    updater.check_in_background()
    log.info("Datasquint %s starting on %s:%s (SQL over files: %s; browser: %s)", VERSION, host, port, filesql.status(),
             webtools.status_line())
    app.run(host=host, port=port, debug=debug, threaded=True,
            # watchdog matches patterns per path segment, so list each depth under uploads/
            exclude_patterns=[os.path.join(UPLOAD_DIR, *["*"] * n) for n in (1, 2, 3)] + ["*.sqlite", "*.duckdb", "*.log", "*.log.*"]
            if debug else None)
