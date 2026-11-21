"""Small per-request progress snapshots; independent of the image-processing lock."""

from contextlib import contextmanager
from contextvars import ContextVar
import re
import threading
import time


_lock = threading.Lock()
_current = ContextVar("skin_progress", default=None)
_details = ContextVar("skin_progress_details", default=None)
_records = {}


def snapshot(token, consume=False):
    with _lock:
        now = time.monotonic()
        for key in list(_records):
            if _records[key]["done"] and now - _records[key]["updated"] > 600:
                del _records[key]
        record = _records.get(token)
        if record is None:
            return {"token": token, "pending": True}
        result = {key: value for key, value in record.items() if key not in ("updated", "_running")}
        result["steps"] = [dict(step) for step in record["steps"]]
        if "batch" in record:
            result["batch"] = {**record["batch"], "items": [dict(item) for item in record["batch"]["items"]]}
        if consume and record["done"]:
            del _records[token]
        return result


@contextmanager
def operation(token):
    if token is None:
        yield
        return
    if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", token):
        raise ValueError("invalid progress token")
    snapshot(token)
    with _lock:
        if token in _records:
            raise ValueError("progress token already used")
        if len(_records) >= 16:
            oldest = next((key for key, value in _records.items() if value["done"]), None)
            if oldest is None:
                raise ValueError("too many progress operations")
            del _records[oldest]
        record = {"token": token, "steps": [], "active": None, "done": False,
                  "error": None, "updated": time.monotonic(), "_running": []}
        _records[token] = record
    context = _current.set(record)
    started = time.perf_counter()
    try:
        yield
    except Exception as error:
        with _lock:
            record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        with _lock:
            record.update(done=True, active=None, seconds=time.perf_counter() - started,
                          updated=time.monotonic())
        _current.reset(context)


def batch_status(folder, items):
    record = _current.get()
    if record is not None:
        with _lock:
            record["batch"] = {"dir": str(folder), "items": [dict(item) for item in items]}
            record["updated"] = time.monotonic()


class BatchCancelled(Exception):
    pass


def cancel_batch(token):
    with _lock:
        record = _records.get(token)
        if record is None or record["done"] or "batch" not in record:
            raise ValueError("no active batch for this request")
        record["cancel_requested"] = True
    return {"cancelling": True}


def check_batch_cancel():
    record = _current.get()
    with _lock:
        if record is not None and record.get("cancel_requested"):
            raise BatchCancelled("batch cancelled")


def completed(label, seconds, **metadata):
    record = _current.get()
    if record is not None:
        with _lock:
            record["steps"].append({"label": label, "seconds": seconds, "error": None, **metadata})
            record["updated"] = time.monotonic()


def call(label, fn, *args, **kwargs):
    return _call(label, None, fn, args, kwargs)


def cache_step(outcome, label, fn, *args, **kwargs):
    return _call(label, outcome, fn, args, kwargs)


def detailed_call(label, fn, *args, cache=None, **kwargs):
    if _current.get() is None:
        return fn(*args, **kwargs)
    return _call(label, cache, fn, args, kwargs, details=[])


@contextmanager
def detail_stage(label):
    details = _details.get()
    if details is None:
        yield
        return
    started = time.perf_counter()
    error = None
    try:
        yield
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        details.append({"label": label, "seconds": time.perf_counter() - started, "error": error})


def cache_timing():
    """Measure a preparation stage and summarize its own cache operations."""
    record = _current.get()
    with _lock:
        offset = len(record["steps"]) if record is not None else 0
    started = time.perf_counter()

    def finish(*, shared=False):
        seconds = time.perf_counter() - started
        with _lock:
            steps = [dict(step) for step in record["steps"][offset:]
                     if step.get("cache") and step["error"] is None] if record is not None else []
        return summarize_cache_timing(seconds, steps, shared=shared)

    return finish


def summarize_cache_timing(seconds, steps, *, shared=False):
    calculated = any(step["cache"] == "calculated" for step in steps)
    reused = shared or any(step["cache"] == "reused" for step in steps)
    outcome = ("mixed" if reused else "calculated") if calculated else ("reused" if reused else None)
    return {"seconds": seconds, "outcome": outcome, "steps": steps, "shared_fs": shared}


def _call(label, cache, fn, args, kwargs, *, details=None):
    record = _current.get()
    if record is None:
        return fn(*args, **kwargs)
    started = time.perf_counter()
    span = object()
    with _lock:
        record["_running"].append((span, label))
        record["active"] = label
        record["updated"] = time.monotonic()
    error = None
    token = _details.set(details) if details is not None else None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        if token is not None:
            _details.reset(token)
        with _lock:
            step = {"label": label, "seconds": time.perf_counter() - started, "error": error}
            if cache is not None:
                step["cache"] = cache
            if details:
                step["details"] = details
            record["steps"].append(step)
            record["_running"] = [(key, value) for key, value in record["_running"] if key is not span]
            record["active"] = record["_running"][-1][1] if record["_running"] else None
            record["updated"] = time.monotonic()
