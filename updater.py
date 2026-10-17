"""
Update Datasquint from GitHub.

Checks the version.py on GitHub (UPDATE_REPO, branch UPDATE_BRANCH) against the running version. When a
newer one is there, Settings → Updates offers to install it:

- In a git checkout (a .git folder next to app.py) it runs `git pull --ff-only`.
- Otherwise it downloads the branch as a zip and copies the code over the current folder. Your own files are
  never touched: .env, secret.key, connections.sqlite, ssh_known_hosts, usage.json, uploads/, logs/ and
  backups/. The code files it replaces are first copied to backups/<old version>-<time>/.
- If requirements.txt changed, it runs pip install -r requirements.txt with the same Python.
- Started with `python app.py`, Datasquint then restarts itself. Under gunicorn or a service manager it
  asks you to restart it.

Settings (.env):
  UPDATES        on (default): check and allow installing from the app; check: only say when an update is
                 available; off: never contact GitHub
  UPDATE_REPO    owner/repo on GitHub (default pangchi/datasquint)
  UPDATE_BRANCH  branch to follow (default main)
  UPDATE_RAW_URL, UPDATE_ZIP_URL  templates for a mirror or GitHub Enterprise; {repo}, {branch}, {path}
"""
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from version import __version__ as CURRENT

HERE = os.path.dirname(os.path.abspath(__file__))
MODE = (os.getenv("UPDATES", "on").strip().lower() or "on")
if MODE in ("0", "false", "no", "disable", "disabled"):
    MODE = "off"
elif MODE not in ("on", "check", "off"):
    MODE = "on"
REPO = os.getenv("UPDATE_REPO", "pangchi/datasquint").strip().strip("/") or "pangchi/datasquint"
BRANCH = os.getenv("UPDATE_BRANCH", "main").strip() or "main"
RAW_URL = os.getenv("UPDATE_RAW_URL", "https://raw.githubusercontent.com/{repo}/{branch}/{path}")
ZIP_URL = os.getenv("UPDATE_ZIP_URL", "https://github.com/{repo}/archive/refs/heads/{branch}.zip")
CHECK_EVERY = 6 * 3600          # seconds between automatic checks
TIMEOUT = 20
MAX_ZIP = 50 * 1024 * 1024

# Never overwritten or removed by an update: settings, secrets, data and history.
KEEP = {".env", "secret.key", "connections.sqlite", "ssh_known_hosts", "usage.json", "usage.json.tmp"}
KEEP_DIRS = {"uploads", "logs", "backups", ".git", "__pycache__", "venv", ".venv"}

log = logging.getLogger("datasquint.update")
SELF_RESTART = False            # set by app.py when started as `python app.py`
_lock = threading.Lock()
_state = {"checked_at": 0, "latest": None, "error": None, "notes": ""}
_busy = False


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def newer(a, b):
    """True if version a is newer than version b."""
    return _vtuple(a) > _vtuple(b)


def _get(url, limit=2 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": f"Datasquint/{CURRENT}", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:   # honours HTTPS_PROXY / HTTP_PROXY
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"download from {url} is larger than expected")
    return data


def _raw(path):
    return RAW_URL.format(repo=REPO, branch=BRANCH, path=path)


def _notes(changelog, current):
    """The CHANGELOG sections newer than the running version, as plain text."""
    out = []
    for block in re.split(r"(?m)^(?=## )", changelog):
        m = re.match(r"## +v?(\d+\.\d+\.\d+)", block)
        if not m:
            continue
        if not newer(m.group(1), current):
            break
        out.append(block.strip())
    text = "\n\n".join(out)
    return text if len(text) <= 6000 else text[:6000].rsplit("\n", 1)[0] + "\n…"


def check(force=False):
    """Look up the latest version on GitHub (cached for CHECK_EVERY seconds unless force)."""
    if MODE == "off":
        return status()
    with _lock:
        fresh = time.time() - _state["checked_at"] < CHECK_EVERY
    if fresh and not force:
        return status()
    latest, error, notes = None, None, ""
    try:
        text = _get(_raw("version.py")).decode("utf-8", "replace")
        m = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
        if not m:
            raise ValueError("no version number found in version.py on GitHub")
        latest = m.group(1)
        if newer(latest, CURRENT):
            try:
                notes = _notes(_get(_raw("CHANGELOG.md")).decode("utf-8", "replace"), CURRENT)
            except Exception:
                notes = ""
    except Exception as e:
        error = f"Couldn't check GitHub for updates: {e}"
        log.warning("%s (%s, %s)", error, REPO, BRANCH)
    with _lock:
        _state.update(checked_at=time.time(), latest=latest, error=error, notes=notes)
    if latest and newer(latest, CURRENT):
        log.info("Update available: %s (running %s)", latest, CURRENT)
    return status()


def method():
    return "git" if os.path.isdir(os.path.join(HERE, ".git")) and shutil.which("git") else "zip"


def status():
    with _lock:
        s = dict(_state)
    latest = s["latest"]
    return {"mode": MODE, "current": CURRENT, "latest": latest, "available": bool(latest and newer(latest, CURRENT)),
            "checked_at": s["checked_at"] or None, "error": s["error"], "notes": s["notes"],
            "repo": REPO, "branch": BRANCH, "url": f"https://github.com/{REPO}/tree/{BRANCH}",
            "method": method(), "can_install": MODE == "on", "restart": "auto" if SELF_RESTART else "manual",
            "busy": _busy}


def check_in_background():
    """Check once at startup without delaying it."""
    if MODE == "off":
        return
    def run():
        s = check(force=True)
        if s["available"]:
            print(f" * Update available: Datasquint {s['latest']} (running {CURRENT}). Settings → Updates installs it.", flush=True)
    threading.Thread(target=run, daemon=True, name="update-check").start()


# ---------------------------------------------------------------- installing
def _keep(rel):
    parts = rel.replace("\\", "/").split("/")
    return parts[0] in KEEP_DIRS or (len(parts) == 1 and parts[0] in KEEP)


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _pip_if_needed(old_req):
    new_req = _read(os.path.join(HERE, "requirements.txt"))
    if new_req is None or new_req == old_req:
        return None
    cmd = [sys.executable, "-m", "pip", "install", "-r", os.path.join(HERE, "requirements.txt")]
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=900)
        tail = (r.stdout + r.stderr)[-1500:]
        if r.returncode:
            log.warning("pip install after update failed (%s):\n%s", r.returncode, tail)
            return {"ok": False, "message": "Installing the new requirements failed; run "
                    "`pip install -r requirements.txt` yourself, then restart. Details are in the error log."}
        log.info("pip install after update finished")
        return {"ok": True, "message": "New Python packages installed."}
    except Exception as e:
        log.warning("pip install after update couldn't run: %s", e)
        return {"ok": False, "message": f"Couldn't run pip ({e}); run `pip install -r requirements.txt` yourself, then restart."}


def _install_git():
    def git(*args):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=300)
    r = git("pull", "--ff-only", "origin", BRANCH)
    if r.returncode:
        msg = (r.stderr or r.stdout).strip()[-800:]
        raise RuntimeError("git pull didn't succeed. If you changed files in this folder, commit or undo them "
                           f"first, or pull by hand. Git said: {msg}")
    return {"backup": None, "files": None}


def _install_zip(latest):
    data = _get(ZIP_URL.format(repo=REPO, branch=BRANCH, path=""), limit=MAX_ZIP)
    with tempfile.TemporaryDirectory(prefix="ds-update-") as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            # GitHub zips hold one top folder (datasquint-main/); find the folder that has app.py
            roots = sorted({n[: -len("app.py")] for n in names if n.endswith("app.py") and n.count("/") <= 1}, key=len)
            if not roots:
                raise ValueError("the download doesn't look like Datasquint (no app.py)")
            root = roots[0]
            src = os.path.join(tmp, "src")
            for n in names:
                if not n.startswith(root):
                    continue
                rel = n[len(root):]
                if not rel or ".." in rel.split("/") or rel.startswith("/") or _keep(rel):
                    continue
                dest = os.path.join(src, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as fin, open(dest, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
        vtext = _read(os.path.join(src, "version.py")) or b""
        m = re.search(rb"""__version__\s*=\s*['"]([^'"]+)['"]""", vtext)
        if not m:
            raise ValueError("the download has no version number")
        got = m.group(1).decode()
        if not newer(got, CURRENT):
            raise ValueError(f"the download is version {got}, not newer than {CURRENT}")
        # back up the files about to be replaced, then copy the new ones in
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(HERE, "backups", f"{CURRENT}-{stamp}")
        changed = []
        for dirpath, _, files in os.walk(src):
            for f in files:
                s = os.path.join(dirpath, f)
                rel = os.path.relpath(s, src)
                d = os.path.join(HERE, rel)
                if _read(d) == _read(s):
                    continue
                if os.path.exists(d):
                    os.makedirs(os.path.dirname(os.path.join(backup, rel)), exist_ok=True)
                    shutil.copy2(d, os.path.join(backup, rel))
                changed.append(rel)
        for rel in changed:
            d = os.path.join(HERE, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            tmpd = d + ".new"
            shutil.copyfile(os.path.join(src, rel), tmpd)
            os.replace(tmpd, d)
    return {"backup": os.path.relpath(backup, HERE) if os.path.isdir(backup) else None, "files": len(changed), "version": got}


def install():
    """Install the latest version. Returns a summary; the caller restarts the app when restart == 'auto'."""
    global _busy
    if MODE != "on":
        raise PermissionError("Installing updates from the app is turned off (UPDATES in .env).")
    with _lock:
        if _busy:
            raise RuntimeError("An update is already being installed.")
        _busy = True
    try:
        s = check(force=True)
        if s["error"]:
            raise RuntimeError(s["error"])
        if not s["available"]:
            raise RuntimeError(f"Datasquint {CURRENT} is already the latest version.")
        old_req = _read(os.path.join(HERE, "requirements.txt"))
        how = method()
        log.info("Updating %s -> %s from %s (%s, %s)", CURRENT, s["latest"], REPO, BRANCH, how)
        result = _install_git() if how == "git" else _install_zip(s["latest"])
        pip = _pip_if_needed(old_req)
        log.info("Updated to %s (%s files changed, backup %s)", s["latest"], result.get("files"), result.get("backup"))
        return {"ok": True, "from": CURRENT, "to": result.get("version") or s["latest"], "method": how,
                "backup": result.get("backup"), "files": result.get("files"), "pip": pip,
                "restart": "auto" if SELF_RESTART else "manual"}
    finally:
        with _lock:
            _busy = False


def restart_soon(delay=1.0):
    """Restart this process with the same command (only when started as `python app.py`)."""
    def run():
        time.sleep(delay)
        log.info("Restarting after update")
        for h in logging.getLogger("datasquint").handlers:
            try:
                h.flush()
            except Exception:
                pass
        env = dict(os.environ, DATASQUINT_RESTARTED="1")
        args = [sys.executable] + sys.argv
        if os.name == "nt":       # Windows: start the new process, then leave so it can take the port
            subprocess.Popen(args, cwd=os.getcwd(), env=env)
            os._exit(0)
        # the web server's listening socket must not survive into the new process, or it can't take the port
        try:
            maxfd = os.sysconf("SC_OPEN_MAX")
        except (ValueError, OSError):
            maxfd = 4096
        os.closerange(3, min(maxfd, 65536))
        os.execve(sys.executable, args, env)
    threading.Thread(target=run, daemon=True, name="update-restart").start()
