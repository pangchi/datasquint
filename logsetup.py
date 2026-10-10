"""
Error logging for Datasquint.

Everything goes to a rotating log file (logs/datasquint.log by default): errors with full tracebacks,
warnings (files that couldn't become tables, failed queries, connection problems, errors in the browser),
and one INFO line per upload, query and chat reply so an error can be seen in context. Warnings and errors
are also printed to the console.

Each line says when, how serious, which part of the app, and for web requests who and what
(client address, method and path). Secrets are masked before anything is written: passwords in
connection strings, password=/api_key= settings, bearer tokens and sk-... API keys.

Settings (.env): LOG_FILE, LOG_LEVEL (DEBUG/INFO/WARNING/ERROR), LOG_MAX_MB, LOG_BACKUPS.
"""
import logging
import logging.handlers
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.getenv("LOG_FILE", os.path.join(HERE, "logs", "datasquint.log"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
LOG_MAX_MB = float(os.getenv("LOG_MAX_MB", 10))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", 5))

log = logging.getLogger("datasquint")

_SECRETS = [
    (re.compile(r"(\w+://[^:/@\s]+:)[^@\s]+(@)"), r"\1***\2"),                                # scheme://user:pass@host
    (re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|token)(\s*[=:]\s*)(['\"]?)[^\s'\",;&]+"), r"\1\2\3***"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[private key removed]"),
]


def redact(text):
    text = str(text)
    for rx, sub in _SECRETS:
        text = rx.sub(sub, text)
    return text


class _Context(logging.Filter):
    """Adds the web request (client, method, path) and masks secrets in the message and traceback."""
    def filter(self, record):
        record.ctx = ""
        try:
            from flask import has_request_context, request
            if has_request_context():
                record.ctx = f" [{request.remote_addr} {request.method} {request.path}]"
        except Exception:
            pass
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None   # the masked text replaces the raw traceback
        return True


def setup():
    """Configure the 'datasquint' logger (and Flask's) once. Returns the log file path, or None."""
    if getattr(setup, "done", None) is not None:
        return setup.done
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s%(ctx)s: %(message)s")
    ctx = _Context()
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    log.setLevel(level)
    log.propagate = False
    path = None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=int(LOG_MAX_MB * 1024 * 1024),
                                                  backupCount=LOG_BACKUPS, encoding="utf-8", delay=True)
        fh.setFormatter(fmt)
        fh.addFilter(ctx)
        log.addHandler(fh)
        path = os.path.abspath(LOG_FILE)
    except OSError as e:   # e.g. a read-only folder: keep running, log to the console only
        print(f" * Can't write the log file {LOG_FILE}: {e}")
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    console.addFilter(ctx)
    log.addHandler(console)
    # Flask's own logger ("app") reports unhandled errors: send it to the same place
    for name in ("app", "flask.app"):
        other = logging.getLogger(name)
        other.handlers[:] = log.handlers
        other.setLevel(level)
        other.propagate = False
    setup.done = path
    return path


def child(name):
    """Logger for one part of the app, e.g. child('db') -> 'datasquint.db'."""
    return log.getChild(name)


def tail(n=200):
    """Last n lines of the log file (for /api/logs on localhost)."""
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except OSError:
        return []
