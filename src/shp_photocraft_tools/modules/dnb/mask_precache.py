"""One debounced, coalescing background mask calculation."""

from contextlib import contextmanager
import threading
import time


class Superseded(Exception):
    pass


class MaskPrecache:
    def __init__(self, compute, delay=.6):
        self.compute, self.delay = compute, delay
        self.condition = threading.Condition()
        self.signature = self.job = None
        self.sequence = 0
        self.deadline = 0
        self.state, self.error, self.seconds, self.message = "idle", None, None, None
        self.thread = None
        self.closed = False

    def select(self, signature):
        with self.condition:
            if signature != self.signature:
                self.signature = signature
                self.sequence += 1
                self.job = None
                self.state, self.error, self.seconds, self.message = "idle", None, None, None
                self.condition.notify_all()

    def request(self, signature, job):
        with self.condition:
            self.select(signature)
            if self.closed or self.state in ("queued", "running", "ready"):
                return
            self.job = (self.sequence, job)
            self.deadline = time.monotonic() + self.delay
            self.state = "queued"
            self.error = None
            self.message = None
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="skin-mask-precache", daemon=True)
                self.thread.start()
            self.condition.notify_all()

    @contextmanager
    def commit(self, sequence):
        with self.condition:
            yield not self.closed and sequence == self.sequence

    def reset(self):
        with self.condition:
            self.signature = self.job = None
            self.sequence += 1
            self.state, self.error, self.seconds, self.message = "idle", None, None, None
            self.condition.notify_all()

    def status(self):
        with self.condition:
            return {"status": self.state, "sequence": self.sequence,
                    "error": self.error, "seconds": self.seconds, "message": self.message}

    def skip(self, signature, message):
        with self.condition:
            self.select(signature)
            self.job = None
            self.state, self.message = "skipped", message

    def close(self):
        with self.condition:
            self.closed = True
            self.sequence += 1
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()

    def _run(self):
        while True:
            with self.condition:
                while not self.closed and (self.job is None or time.monotonic() < self.deadline):
                    timeout = max(0, self.deadline - time.monotonic()) if self.job else None
                    self.condition.wait(timeout)
                if self.closed:
                    return
                sequence, job = self.job
                self.job = None
                self.state = "running"
            started = time.perf_counter()
            state, error = "ready", None
            try:
                self.compute(job, lambda: self.commit(sequence))
            except Superseded:
                state = "superseded"
            except Exception as exc:
                state, error = "error", f"{type(exc).__name__}: {exc}"
            del job
            with self.condition:
                if sequence == self.sequence:
                    self.state, self.error = state, error
                    self.seconds = time.perf_counter() - started
