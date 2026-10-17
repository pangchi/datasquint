"""
Token usage counters for Datasquint.

Every request to the AI service adds its input (prompt) and output (completion) tokens here, per model.
The numbers come from the service's own usage report. Services that don't report usage (some
OpenAI-compatible servers) get an estimate of about 4 characters per token, counted separately so the
Settings page can say how much of the total is estimated.

Counters are kept in usage.json next to the app (USAGE_FILE in .env changes the path), so they survive
restarts and are shared by everyone using this server. Reset sets them to zero and restarts the "since" date.
"""
import json
import logging
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_FILE = os.getenv("USAGE_FILE", os.path.join(HERE, "usage.json"))
CHARS_PER_TOKEN = 4

log = logging.getLogger("datasquint.usage")
_lock = threading.Lock()
_data = None


def _empty():
    return {"since": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "models": {}}


def _load():
    global _data
    if _data is None:
        try:
            with open(USAGE_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            if not isinstance(d, dict) or not isinstance(d.get("models"), dict):
                raise ValueError("unexpected layout")
            _data = d
        except FileNotFoundError:
            _data = _empty()
        except Exception as e:   # damaged file: start again rather than fail every reply
            log.warning("Token usage file %s couldn't be read (%s); starting from zero", USAGE_FILE, e)
            _data = _empty()
    return _data


def _save():
    tmp = USAGE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_data, fh, indent=1)
        os.replace(tmp, USAGE_FILE)
    except OSError as e:
        log.warning("Token usage couldn't be saved to %s: %s", USAGE_FILE, e)


def estimate(chars):
    """Rough token count for text the service didn't report on."""
    return max(0, round(chars / CHARS_PER_TOKEN))


def record(model, prompt, completion, estimated=False):
    """Add one request's tokens to the counters."""
    prompt, completion = max(0, int(prompt or 0)), max(0, int(completion or 0))
    if not prompt and not completion:
        return
    with _lock:
        d = _load()
        m = d["models"].setdefault(model or "unknown", {"requests": 0, "prompt": 0, "completion": 0,
                                                        "estimated_requests": 0})
        m["requests"] += 1
        m["prompt"] += prompt
        m["completion"] += completion
        if estimated:
            m["estimated_requests"] += 1
        _save()


def summary():
    """Totals and per-model counters, for the Settings page."""
    with _lock:
        d = _load()
        models = [{"model": k, **v} for k, v in sorted(d["models"].items(), key=lambda kv: -(kv[1]["prompt"] + kv[1]["completion"]))]
    total = {k: sum(m[k] for m in models) for k in ("requests", "prompt", "completion", "estimated_requests")}
    total["total"] = total["prompt"] + total["completion"]
    for m in models:
        m["total"] = m["prompt"] + m["completion"]
    return {"since": d["since"], "total": total, "models": models}


def reset():
    global _data
    with _lock:
        _data = _empty()
        _save()
    log.info("Token usage counters reset")
    return summary()


class Tally:
    """Tokens for one reply, which may take several requests (files read in parts)."""
    def __init__(self):
        self.prompt = self.completion = 0
        self.estimated = False

    def add(self, model, prompt, completion, estimated):
        self.prompt += prompt
        self.completion += completion
        self.estimated = self.estimated or estimated
        record(model, prompt, completion, estimated)

    def as_dict(self):
        return {"prompt": self.prompt, "completion": self.completion, "estimated": self.estimated}
