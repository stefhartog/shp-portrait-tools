"""Coordinate full-resolution jobs without holding the preview lock during preparation."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import threading
import time

from . import progress
from .mask_precache import MaskPrecache, Skipped, Superseded


class CacheCoordinator:
    def __init__(self, *, state, capture_inputs, parse_params, colour_mode, mask_key, mask_limit,
                 compute_mask, layer_cache, prepare_layers):
        self._state = state
        self._capture_inputs = capture_inputs
        self._parse_params = parse_params
        self._colour_mode = colour_mode
        self._mask_key = mask_key
        self._mask_limit = mask_limit
        self._compute_mask = compute_mask
        self._layer_cache = layer_cache
        self._prepare_layers = prepare_layers
        self.preview_lock = threading.Lock()
        self.full_resolution_lock = threading.Lock()
        self.foreground_waiting = threading.Event()
        self._waiters_lock = threading.Lock()
        self._waiters = 0
        self.mask_jobs = MaskPrecache(self.precache_mask)
        self.layer_jobs = MaskPrecache(self.precache_layers, delay=1.5, name="layer-precache")
        self.layer_records = {}
        self.layer_active = {"mode": None, "stamp": None, "generation": None, "tabs": {}, "budget_note": None}

    @contextmanager
    def foreground_lock(self, *, full_resolution=False):
        with self._waiters_lock:
            self._waiters += 1
            self.foreground_waiting.set()
        try:
            # A Send can wait for an in-flight full-resolution stage without
            # holding the preview lock. Always acquire the full-resolution lock first.
            with (self.full_resolution_lock if full_resolution else nullcontext()), self.preview_lock:
                yield
        finally:
            with self._waiters_lock:
                self._waiters -= 1
                if not self._waiters:
                    self.foreground_waiting.clear()

    def precache_mask(self, job, guard):
        p, state = job
        # Background progress owns its record; never append to a completed preview.
        token = f"mask-precache-{time.monotonic_ns()}"
        try:
            with self.full_resolution_lock, progress.operation(token), self._colour_mode(state["colour_mode"]):
                self._compute_mask(p, state=state, commit_guard=guard)
        finally:
            progress.snapshot(token, consume=True)

    def mask_cache_status(self):
        status = self.mask_jobs.status()
        state = self._state()
        saved = state.get("full_mask_cache")
        entry = state.get("full_mask_status_entry")
        status["cached"] = bool(saved is not None and entry is not None
                                and entry[0] == saved[0] and entry[1] == state.get("mask_status_key"))
        status["enabled"] = state.get("mask_precache_enabled", True)
        return status

    def queue_mask_precache(self, p):
        if p["mode"] not in ("mask", "dnb", "frequency", "combine"):
            return
        key = self._mask_key(p)
        state = self._state()
        state["mask_status_key"] = key
        h, w = state["img"].shape[:2]
        count = h * w * 4 * (2 if p["protect"] else 1)
        if count > self._mask_limit():
            self.mask_jobs.skip(key, "Background mask skipped: exceeds the 256 MiB cache budget.")
            return
        self.mask_jobs.request(key, (deepcopy(p), self._capture_inputs()))

    def precache_layers(self, job, scheduler_guard):
        modes, state, stamp = job
        generation = state["landmark_generation"]
        held = False

        @contextmanager
        def guard():
            with scheduler_guard() as current:
                live = self._state()
                yield (current and generation == live.get("landmark_generation")
                       and all(live.get(key) is state[key] for key in ("source_img", "img", "masks")))

        def check(message=None):
            with guard() as current:
                if not current:
                    raise Superseded()
                if message is not None:
                    self.layer_jobs.message = message

        def higher_priority_work():
            return self.foreground_waiting.is_set() or self.mask_jobs.status()["status"] in ("queued", "running")

        def acquire():
            nonlocal held
            while True:
                check()
                if not higher_priority_work() and self.full_resolution_lock.acquire(blocking=False):
                    if not higher_priority_work():
                        held = True
                        return
                    self.full_resolution_lock.release()
                time.sleep(.05)

        def cooperate():
            nonlocal held
            check()
            if higher_priority_work():
                self.full_resolution_lock.release()
                held = False
                acquire()
                check()

        check("Waiting for mask preparation or foreground work")
        acquire()
        token = f"layer-precache-{time.monotonic_ns()}"
        try:
            with progress.operation(token), self._colour_mode(state["colour_mode"]):
                check()

                def record(mode, keys):
                    with guard() as current:
                        if not current:
                            raise Superseded()
                        self.layer_records[mode] = (generation, stamp, tuple(keys))

                def tab(mode, state, message, keys=None):
                    with guard() as current:
                        if not current:
                            raise Superseded()
                        self.layer_active["tabs"][mode] = {"state": state, "message": message}
                        if keys:
                            self.layer_records[mode] = (generation, stamp, tuple(keys))

                self._prepare_layers(modes, state, guard, check, cooperate, tab, record)
                with guard() as current:
                    if not current:
                        raise Superseded()
                    outcomes = list(self.layer_active["tabs"].values())
                if not any(item["state"] == "ready" for item in outcomes):
                    raise Skipped(next((item["message"] for item in outcomes if item["state"] == "unavailable"),
                                       "No reusable layers for these settings."))
        finally:
            progress.snapshot(token, consume=True)
            if held:
                self.full_resolution_lock.release()

    def layer_cache_status(self):
        with self.layer_jobs.condition:
            status = self.layer_jobs.status()
            active = deepcopy(self.layer_active)
            records = dict(self.layer_records)
        status.update(active)
        live_generation = self._state().get("landmark_generation")
        storage = self._layer_cache()
        status["records"] = {
            mode: {"generation": generation, "stamp": stamp,
                   "cached": generation == live_generation and storage.contains_all(keys)}
            for mode, (generation, stamp, keys) in records.items()
        }
        return status

    def queue_layer_precache(self, p, stamp=None, raw=None):
        state = self._capture_inputs()
        source, generation = state["source_img"], state["landmark_generation"]
        modes, invalid = {}, {}
        if raw is None:
            modes[p["mode"]] = p
        else:
            for mode in ("frequency", "dnb", "combine"):
                try:
                    modes[mode] = self._parse_params({**raw, "mode": mode})
                except (KeyError, TypeError, ValueError) as error:
                    invalid[mode] = {"state": "unavailable", "message": f"Invalid {mode} settings: {error}"}
        signature = (generation, id(source), stamp, json.dumps(modes, sort_keys=True, allow_nan=False))
        tabs = {mode: invalid.get(mode, {"state": "queued", "message": "Waiting to check layer components"})
                for mode in modes.keys() | invalid.keys()}
        note = None
        if source is not None and len(modes) > 1:
            freq, bands = modes.get("frequency"), modes.get("dnb")
            n = source.shape[0] * source.shape[1]
            required = (source.nbytes * (2 + int(bool(freq and freq["frequency"]["on"]))) if freq else 0)
            required += n * 4 * 2 * (int(bands["band1_on"]) + int(bands["second_band"]["on"])) if bands else 0
            limit = self._layer_cache().limit
            if required > limit:
                note = (f"All layers need approximately {required / 1024**3:.2f} GiB; cache limit is "
                        f"{limit / 1024**3:.2f} GiB. Some components may be evicted.")
        with self.layer_jobs.condition:
            self.layer_active.update(mode=p["mode"], generation=generation, stamp=stamp,
                                        tabs=tabs, budget_note=note)
            if source is None:
                self.layer_jobs.skip(signature, "No source image to prepare.")
            elif not state["source_opaque"]:
                self.layer_jobs.skip(signature, "Layer preparation is unavailable for a transparent source.")
            else:
                self.layer_jobs.request(signature, (deepcopy(modes), state, stamp))

    def matching_layer_preparation(self, req):
        if req.get("mix_only") is not True or req.get("precache_layers") is not True:
            return False
        with self.layer_jobs.condition:
            signature = self.layer_jobs.signature
            if not isinstance(signature, tuple) or len(signature) != 4:
                return False
            generation, source_id, stamp, settings = signature
            state = self._state()
            if (generation != state.get("landmark_generation") or source_id != id(state.get("source_img"))
                    or stamp != req.get("layer_cache_revision")
                    or req.get("precache_mask", True) != state.get("mask_precache_enabled", True)):
                return False
            previous = json.loads(settings)
        try:
            current = {mode: self._parse_params({**req, "mode": mode}) for mode in previous}
        except (KeyError, TypeError, ValueError):
            return False
        for mode in previous:
            previous[mode].pop("combine", None)
            current[mode].pop("combine", None)
        return json.dumps(previous, sort_keys=True, allow_nan=False) == json.dumps(current, sort_keys=True, allow_nan=False)

    def reset_layers_if_scheduled(self, message):
        # A preview can queue another job while a foreground request waits.
        with self.layer_jobs.condition:
            if self.layer_jobs.signature is not None:
                self.layer_jobs.reset(message)

    def close(self):
        self.layer_jobs.close()
        self.mask_jobs.close()
