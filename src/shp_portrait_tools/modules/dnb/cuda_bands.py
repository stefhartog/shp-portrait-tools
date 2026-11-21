"""Optional CUDA 13 band renderer and source-mask edge refinement."""

import ctypes as C
from contextlib import contextmanager
from functools import lru_cache
import logging
import math
import os
from pathlib import Path
import sys
import threading

import cv2
import numpy as np

from . import dodge_burn as D


class CudaUnavailable(RuntimeError):
    pass


logger = logging.getLogger(__name__)
_jobs = threading.Lock()


def _function(dll, name, *types):
    try:
        fn = getattr(dll, name)
    except AttributeError as exc:
        raise CudaUnavailable(f"CUDA library is missing {name}") from exc
    fn.argtypes = types
    fn.restype = C.c_int
    error_name = getattr(dll, "cuGetErrorName", None)
    if error_name is not None:
        error_name.argtypes = (I, C.POINTER(S))
        error_name.restype = I

    def checked(*args):
        code = fn(*args)
        if code:
            detail = S()
            if error_name is not None and error_name(code, C.byref(detail)) == 0 and detail.value:
                raise CudaUnavailable(f"{name}: {detail.value.decode('ascii')} ({code})")
            raise CudaUnavailable(f"{name} returned CUDA error {code}")

    return checked


P, I, Z, F, S = C.c_void_p, C.c_int, C.c_size_t, C.c_float, C.c_char_p


def _compile(major, minor):
    candidates = (Path(entry) / "nvidia" / "cu13" / "bin" / suffix
                  for entry in sys.path for suffix in ("x86_64", ""))
    dll_path = next((p / "nvrtc64_130_0.dll" for p in candidates if (p / "nvrtc64_130_0.dll").is_file()), None)
    if dll_path is None:
        raise CudaUnavailable("CUDA 13 NVRTC is not installed")
    builtins = list(dll_path.parent.glob("nvrtc-builtins64_*.dll"))
    if len(builtins) != 1:
        raise CudaUnavailable("CUDA 13 NVRTC needs one matching builtins DLL")
    try:
        with os.add_dll_directory(str(dll_path.parent)):
            dependencies = C.CDLL(str(builtins[0]))
            dll = C.CDLL(str(dll_path))
            return _compile_with(dll, major, minor, dependencies)
    except OSError as exc:
        raise CudaUnavailable(f"Cannot load CUDA 13 NVRTC: {exc}") from exc


def _compile_with(dll, major, minor, _builtins):
    source = Path(__file__).with_name("dnb_cuda.cu")
    if not source.is_file():
        raise CudaUnavailable("D&B CUDA kernel source is missing")
    program = P()
    _function(dll, "nvrtcCreateProgram", C.POINTER(P), S, S, I, P, P)(
        C.byref(program), source.read_bytes(), b"dnb_cuda.cu", 0, None, None)
    try:
        opts = (S * 2)(f"--gpu-architecture=sm_{major}{minor}".encode(), b"--fmad=false")
        try:
            _function(dll, "nvrtcCompileProgram", P, I, C.POINTER(S))(program, len(opts), opts)
        except CudaUnavailable as exc:
            size = Z()
            _function(dll, "nvrtcGetProgramLogSize", P, C.POINTER(Z))(program, C.byref(size))
            log = C.create_string_buffer(size.value)
            _function(dll, "nvrtcGetProgramLog", P, P)(program, log)
            raise CudaUnavailable(log.value.decode(errors="replace").strip() or str(exc)) from exc
        size = Z()
        _function(dll, "nvrtcGetCUBINSize", P, C.POINTER(Z))(program, C.byref(size))
        data = C.create_string_buffer(size.value)
        _function(dll, "nvrtcGetCUBIN", P, P)(program, data)
        return data
    finally:
        _function(dll, "nvrtcDestroyProgram", C.POINTER(P))(C.byref(program))


class _Device:
    def __init__(self):
        if sys.platform != "win32":
            raise CudaUnavailable("D&B CUDA currently supports Windows only")
        try:
            dll = C.WinDLL("nvcuda.dll", winmode=0x800)
        except OSError as exc:
            raise CudaUnavailable("NVIDIA CUDA driver is not available") from exc
        bind = lambda name, *types: _function(dll, "cu" + name, *types)
        bind("Init", C.c_uint)(0)
        device, major, minor = I(), I(), I()
        bind("DeviceGet", C.POINTER(I), I)(C.byref(device), 0)
        attribute = bind("DeviceGetAttribute", C.POINTER(I), I, I)
        attribute(C.byref(major), 75, device)
        attribute(C.byref(minor), 76, device)
        if major.value < 8:
            raise CudaUnavailable("D&B CUDA requires an NVIDIA GPU with compute capability 8.0 or newer")
        binary = _compile(major.value, minor.value)
        self.push = bind("CtxPushCurrent_v2", P)
        self.pop = bind("CtxPopCurrent_v2", C.POINTER(P))
        self.alloc = bind("MemAlloc_v2", C.POINTER(P), Z)
        self.free = bind("MemFree_v2", P)
        self.memory = bind("MemGetInfo_v2", C.POINTER(Z), C.POINTER(Z))
        # Use the same per-thread default stream for transfers, fills, and kernels.
        self.upload = bind("MemcpyHtoD_v2_ptds", P, P, Z)
        self.download = bind("MemcpyDtoH_v2_ptds", P, P, Z)
        self.clear = bind("MemsetD32_v2_ptds", P, C.c_uint, Z)
        self.launch = bind("LaunchKernel", P, I, I, I, I, I, I, I, P, C.POINTER(P), P)
        self.get_function = bind("ModuleGetFunction", C.POINTER(P), P, S)
        self.context, self.module = P(), P()
        self.kernels = {}
        bind("DevicePrimaryCtxRetain", C.POINTER(P), I)(C.byref(self.context), device)
        try:
            with self.current():
                bind("ModuleLoadData", C.POINTER(P), P)(C.byref(self.module), binary)
        except CudaUnavailable:
            bind("DevicePrimaryCtxRelease_v2", I)(device)
            raise

    @contextmanager
    def current(self):
        self.push(self.context)
        try:
            yield self
        except BaseException:
            try:
                self.pop(C.byref(P()))
            except CudaUnavailable:
                logger.exception("Could not pop the D&B CUDA context")
            raise
        else:
            self.pop(C.byref(P()))

    def run(self, name, count, *args):
        if name not in self.kernels:
            fn = P()
            self.get_function(C.byref(fn), self.module, name.encode())
            self.kernels[name] = fn
        values = [I(arg) if isinstance(arg, int) else F(arg) if isinstance(arg, float) else arg for arg in args]
        pointers = (P * len(values))(*(C.addressof(value) for value in values))
        self.launch(self.kernels[name], (count + 127) // 128, 1, 1, 128, 1, 1, 0, P(2), pointers, None)


def device():
    dev, reason = _load_device()
    if dev is None:
        raise CudaUnavailable(reason)
    return dev


@lru_cache(maxsize=1)
def _load_device():
    try:
        return _Device(), None
    except CudaUnavailable as exc:
        return None, str(exc)


class _Buffers:
    def __init__(self, dev):
        self.dev = dev
        self.buffers = {}
        self.free_slots = {}
        self.active = []
        self.bytes = 0
        free, total = Z(), Z()
        dev.memory(C.byref(free), C.byref(total))
        self.limit = min(3 * 1024**3, free.value // 2, max(0, free.value - 768 * 1024**2))

    def get(self, count):
        slots = self.free_slots.setdefault(count, [])
        if slots:
            buf = slots.pop()
        else:
            if self.bytes + count * 4 > self.limit:
                raise CudaUnavailable("Not enough available VRAM within the D&B buffer budget")
            buf = P()
            self.dev.alloc(C.byref(buf), count * 4)
            self.buffers[buf.value] = buf
            self.bytes += count * 4
        self.active.append((count, buf))
        return buf

    @contextmanager
    def scratch(self):
        mark = len(self.active)
        try:
            yield
        finally:
            for count, buf in self.active[mark:]:
                self.free_slots[count].append(buf)
            del self.active[mark:]

    def upload(self, array):
        buf = self.get(array.size)
        self.dev.upload(buf, P(array.ctypes.data), array.nbytes)
        return buf

    def gaussian(self, src, out, w, h, sigma, border=2, *, channels=1, wide=False):
        radius = round(sigma * 8 + 1) // 2
        if radius > 128 and not wide:
            raise CudaUnavailable("Gaussian radius exceeds the CUDA limit (128 px)")
        taps = 2*radius + 1
        if taps > 2**31 - 1 or taps*4 > self.limit:
            raise CudaUnavailable("Feather kernel exceeds the CUDA buffer budget")
        with self.scratch():
            count = w*h*channels
            temp = self.get(count)
            weights = self.upload(cv2.getGaussianKernel(taps, sigma, cv2.CV_32F))
            for axis, source, dest in ((0, src, temp), (1, temp, out)):
                # Bound each launch without changing the tap order or truncating the kernel.
                for start in range(0, taps, 257):
                    self.dev.run("gaussian", count, source, dest, weights, w, h, radius, axis,
                                 border, channels, start, min(start + 257, taps))

    def close(self):
        # cuMemFree waits for outstanding work using the allocation.
        error = None
        for buf in self.buffers.values():
            try:
                self.dev.free(buf)
            except CudaUnavailable as exc:
                error = exc
        if error is not None:
            raise error


class _Band:
    def __init__(self, dev, buf, shape, scale, bands, strength, edge, tau, max_ev):
        self.run, self.buf = dev.run, buf
        self.h, self.w = shape
        self.count = self.h * self.w
        self.ppm = scale / D.IPD_MM
        self.fine = max(min(b[0] for b in bands), .3)
        self.fine_px = self.fine * self.ppm
        f = min(1., 1. / max(self.fine_px, 1e-6))
        self.ow, self.oh = max(8, round(self.w * f)), max(8, round(self.h * f))
        self.work_count = self.ow * self.oh
        self.bands, self.strength, self.edge, self.tau = bands, strength, edge, tau
        self.cap = max_ev * math.log(2)
        if self.ow > self.w or self.oh > self.h:
            raise CudaUnavailable("Image is too small for CUDA band analysis")

    def gaussian(self, src, out, w, h, sigma, border=2):
        self.buf.gaussian(src, out, w, h, sigma, border)

    def guide(self, guide, src, out, w, h, radius, eps):
        _guided_filter(self.buf, guide, src, out, w, h, radius, eps)

    def fill_skin(self, work, m, stats):
        w, h, n = self.ow, self.oh, self.work_count
        with self.buf.scratch():
            self.run("mask_stats", n, work, m, stats, n)
            prod, n1, n2, d1, d2 = (self.buf.get(n) for _ in range(5))
            self.run("product", n, work, m, prod, n)
            near = max(.8, 1.5 * self.ppm * w / self.w)
            far = max(1., 8. * self.ppm * w / self.w)
            for src, dst, sigma in ((prod, n1, near), (prod, n2, far), (m, d1, near), (m, d2, far)):
                self.gaussian(src, dst, w, h, sigma)
            self.run("fill_mask", n, work, m, n1, d1, n2, d2, stats, n)

    def analyze(self, log_y, guide, out, mask=None, stats=None):
        w, h, n = self.ow, self.oh, self.work_count
        with self.buf.scratch():
            work, prev, nxt, band = (self.buf.get(n) for _ in range(4))
            self.run("area", n, log_y, work, self.w, self.h, w, h)
            if mask is not None:
                m = self.buf.get(n)
                self.run("area", n, mask, m, self.w, self.h, w, h)
                self.fill_skin(work, m, stats)
            self.gaussian(work, prev, w, h, 1.)
            self.buf.dev.clear(band, 0, n)
            size, top = self.fine, max(b[1] for b in self.bands)
            while size <= top:
                next_size = size * math.sqrt(2)
                radius = max(1, round((next_size * self.ppm * w / self.w - 1) / 2))
                self.guide(prev, prev, nxt, w, h, radius, self.edge**2)
                gain = D.band_gain(math.sqrt(size * next_size), self.bands)
                self.run("accumulate", n, band, prev, nxt, n, gain)
                prev, nxt, size = nxt, prev, next_size
            self.run("change", n, band, n, self.strength, self.tau, self.cap)
            up = self.buf.get(self.count)
            self.run("cubic", self.count, band, up, w, h, self.w, self.h)
            self.guide(guide, up, out, self.w, self.h, max(2, round(1.73 * self.fine_px)), .02**2)


def _guided_filter(buf, guide, src, out, w, h, radius, eps):
    count = w*h
    run = buf.dev.run
    with buf.scratch():
        temp = buf.get(2*count)
        product, mg, a, b = (buf.get(count) for _ in range(4))

        def mean(x, result):
            run("box_rows", h, x, temp, w, h, radius)
            run("box_cols", w, temp, result, w, h, radius)

        mean(guide, mg)
        ms = mg
        if guide is not src:
            ms = buf.get(count)
            mean(src, ms)
        run("product", count, guide, src, product, count)
        mean(product, a)
        if guide is not src:
            run("product", count, guide, guide, product, count)
        mean(product, b)
        run("coefficients", count, a, b, mg, ms, count, eps)
        mean(a, a)
        mean(b, b)
        run("guided", count, guide, a, b, out, count)


def refine_skin(guide, probability, radius):
    """Keep the filter and sharpening on-device; download only the refined crop."""
    if (guide.ndim != 2 or not guide.size or guide.shape != probability.shape
            or any(a.dtype != np.float32 or not a.flags.c_contiguous for a in (guide, probability))):
        raise CudaUnavailable("Skin refinement requires matching contiguous float32 planes")
    if not _jobs.acquire(blocking=False):
        raise CudaUnavailable("GPU is busy with another Tools CUDA job")
    try:
        dev = device()
        with dev.current():
            buf = _Buffers(dev)
            try:
                h, w = probability.shape
                n = h * w
                if 40 * n > buf.limit:
                    raise CudaUnavailable("Not enough available VRAM within the skin refinement budget")
                g, p = buf.upload(guide), buf.upload(probability)
                out = buf.get(n)
                _guided_filter(buf, g, p, out, w, h, radius, 1e-3)
                dev.run("sharpen_skin", n, out, n)
                result = np.empty_like(probability)
                dev.download(P(result.ctypes.data), out, result.nbytes)
                return result
            finally:
                buf.close()
    finally:
        _jobs.release()


def render_final_mask(img, scale, base, shadows, highlights, tone_strength, protect, protect_mm, protect_thr,
                      *, feather=0., inward=True, color=None):
    """Apply enabled cache filters to a prepared CPU base, with one round trip."""
    tone = shadows is not None and highlights is not None
    structure = protect > 0
    has_feather = base is not None and feather >= .5
    if not (tone or structure or has_feather or color):
        return base, None
    if img.shape[2:] != (3,) or not img.size or img.dtype != np.float32 or not img.flags.c_contiguous:
        raise CudaUnavailable("CUDA mask requires contiguous float32 BGR input")
    if base is not None and (base.shape != img.shape[:2] or base.dtype != np.float32 or not base.flags.c_contiguous):
        raise CudaUnavailable("CUDA mask requires a matching contiguous float32 base")
    ppm = scale / D.IPD_MM
    scales = [max(.8, .5 * protect_mm * ppm * f) for f in (.7, 1., 1.4)] if structure else []
    blur = max(.8, .25 * protect_mm * ppm)
    color_smooth = color["smooth_mm"] * ppm if color else 0.
    color_blur = color["blur_mm"] * ppm if color else 0.
    sigmas = scales + [blur if structure else 0., max(1., ppm) if tone else 0., color_smooth, color_blur]
    if any(round(s * 8 + 1) // 2 > 128 for s in sigmas):
        raise CudaUnavailable("Mask Gaussian radius exceeds the CUDA limit (128 px)")
    if not _jobs.acquire(blocking=False):
        raise CudaUnavailable("GPU is busy with another Tools CUDA job")
    try:
        with device().current() as dev:
            buf = _Buffers(dev)
            try:
                h, w = img.shape[:2]
                n = h*w
                # The pool retains its three BGR slots when smoothing, plus up to five planes.
                if (56 if color_smooth > .3 else 32)*n + 8192 > buf.limit:
                    raise CudaUnavailable("Not enough available VRAM within the final-mask budget")
                has_base = base is not None
                total = buf.upload(base) if has_base else buf.get(n)
                smooth = buf.get(n)
                if has_feather:
                    buf.gaussian(total, smooth, w, h, feather, wide=True)
                    dev.run("mask_feather", n, total, smooth, n, int(inward))

                logs = buf.get(n) if tone or structure else None
                adobe = int((D._colour_mode.get() or D.COLOUR["mode"]) == "adobe")
                if color or logs is not None:
                    with buf.scratch():
                        rgb = buf.upload(img)
                        if logs is not None:
                            with buf.scratch():
                                dev.run("luminance", n, rgb, buf.get(n), logs, n, adobe)
                        if color:
                            with buf.scratch():
                                if color_smooth > .3:
                                    smoothed_rgb = buf.get(3*n)
                                    buf.gaussian(rgb, smoothed_rgb, w, h, color_smooth, channels=3)
                                else:
                                    smoothed_rgb = rgb
                                hr, sr = float(color["hue_range"]), float(color["saturation_range"])
                                dev.run("mask_color_range", n, smoothed_rgb, smooth, n,
                                        float(color["hue"]), float(color["saturation"]),
                                        .8*hr, max(hr-.8*hr, 1e-6), .8*sr, max(sr-.8*sr, 1e-6))
                if color:
                    if color_blur > .3:
                        buf.gaussian(smooth, smooth, w, h, color_blur)
                    dev.run("mask_apply", n, smooth, total, n, int(has_base), float(color["strength"]), 1)
                    has_base = True
                if tone:
                    buf.gaussian(logs, smooth, w, h, max(1., ppm))
                    dev.run("mask_tone", n, smooth, total, n, adobe, int(has_base),
                            float(shadows[0]), float(max(shadows[1]-shadows[0], 1e-6)),
                            float(highlights[0]), float(max(highlights[1]-highlights[0], 1e-6)),
                            float(tone_strength))
                st = None
                if structure:
                    resp = buf.get(n)
                    dev.clear(resp, 0, n)
                    for sigma in scales:
                        buf.gaussian(logs, smooth, w, h, sigma)
                        dev.run("mask_hessian", n, smooth, resp, w, h, sigma)
                    dev.run("mask_structure_ramp", n, resp, n, float(protect_thr), float(max(protect_thr, 1e-6)))
                    buf.gaussian(resp, smooth, w, h, blur)
                    dev.run("mask_apply", n, smooth, total, n, int(tone or has_base), float(min(protect, 1.)), 0)
                    st = np.empty((h,w), np.float32)
                    dev.download(P(st.ctypes.data), smooth, st.nbytes)
                mask = np.empty((h,w), np.float32)
                dev.download(P(mask.ctypes.data), total, mask.nbytes)
                return mask, st
            finally:
                buf.close()
    finally:
        _jobs.release()


def render_band(img, scale, bands, strength, edge, tau, max_ev, sigma, *, mask, invert):
    if img.dtype != np.float32 or not img.flags.c_contiguous or img.shape[2:] != (3,):
        raise CudaUnavailable("CUDA D&B requires contiguous float32 BGR input")
    if edge < .01:
        raise CudaUnavailable("Edge setting is below the verified CUDA range")
    with _jobs, device().current() as dev:
        buf = _Buffers(dev)
        try:
            plan = _Band(dev, buf, img.shape[:2], scale, bands, strength, edge, tau, max_ev)
            n, w, h = plan.count, plan.w, plan.h
            adobe = int((D._colour_mode.get() or D.COLOUR["mode"]) == "adobe")
            y, log_y, guide, change, layer = (buf.get(n) for _ in range(5))
            with buf.scratch():
                rgb = buf.upload(img)
                dev.run("luminance", n, rgb, y, log_y, n, adobe)
            plan.gaussian(log_y, guide, w, h, max(1., plan.fine_px * .7))
            plain, weight, stats = P(), P(), P()
            if mask is not None:
                # Keep the CPU's exact distance-transform edge definition; upload once.
                distance = cv2.distanceTransform((mask <= .5).astype(np.uint8), cv2.DIST_L2, 5)
                weight = buf.upload(1 - D.smoothstep(0., max(1., 6 * scale / D.IPD_MM), distance))
                del distance
                stats, plain = buf.get(8), buf.get(n)
                dev.clear(stats, 0, 8)
                with buf.scratch():
                    base = buf.upload(np.ascontiguousarray(mask, dtype=np.float32))
                    plan.analyze(log_y, guide, change, base, stats)
                plan.analyze(log_y, guide, plain)
            else:
                plan.analyze(log_y, guide, change)
            dev.run("layer", n, y, change, plain, weight, stats, layer, n, plan.cap, adobe)
            if sigma:
                blurred = buf.get(n)
                plan.gaussian(layer, blurred, w, h, sigma, border=0)
                layer = blurred
            if invert:
                dev.run("invert", n, layer, n)
            result = np.empty((h, w), np.float32)
            dev.download(P(result.ctypes.data), layer, result.nbytes)
        except BaseException:
            try:
                buf.close()
            except CudaUnavailable:
                logger.exception("Could not release the D&B CUDA buffers")
            raise
        else:
            buf.close()
            return result
