"""Prototype dodge & burn: build a 50%-gray 'soft light' layer for the whole picture (no skin masking yet).

Idea: in log-luminance, find pixels that are darker/brighter than their local surroundings at a *medium* size
(blemishes, under-eye shadows, bumps: a few mm up to a few cm on the subject) and nudge them toward the local
mean. Fine detail (pores, hair, noise; below ~1.5 mm) and large-scale lighting (above the size range) are left
alone. All sizes are in millimetres and converted to pixels with S (pixels per 63 mm, see README.md).

Steps
  1. luminance (linear light) -> log; work on a downscaled copy (the correction is smooth anyway)
  2. reference = edge-aware (guided filter) local mean at the size range; dev = fine-smoothed - reference
  3. change = -strength * dev, fading out for large deviations (real features: eyes, edges), then clamped
  4. upsample, refined with a guided filter on the full-res image so it doesn't leak across edges
  5. invert the Soft Light blend formula: the gray value that, blended onto the picture, gives that change

Outputs (in --out): <name>_db_layer16.png (16-bit gray, use in Soft Light mode), <name>_db_layer8.png,
<name>_db_preview.jpg (before | after | change map), <name>_db_crop.jpg (full-res crop around the subject).

Usage: uv run python -m shp_photocraft_tools.modules.dnb.dodge_burn IMAGE (--S PX | --auto) [--strength 0.7] [--fine-mm 1.5]
           [--scale-mm 12] [--edge 0.12] [--tau 0.35] [--max-ev 0.3] [--out DIR]
"""
import argparse
import math
from pathlib import Path

import cv2
import numpy as np

IPD_MM = 63.0


COLOUR = {"mode": "srgb"}  # "srgb" or "adobe" (Adobe RGB 1998: gamma 2.2, its own luminance weights)
ADOBE_GAMMA = 2.19921875


def set_colour_mode(mode):
    COLOUR["mode"] = "adobe" if mode == "adobe" else "srgb"


def to_linear(c):
    if COLOUR["mode"] == "adobe":
        return np.power(np.clip(c, 0, 1), ADOBE_GAMMA)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def to_srgb(c):
    """Encode linear light with the TRC of the current colour mode (the name is historical)."""
    c = np.clip(c, 0, 1)
    if COLOUR["mode"] == "adobe":
        return np.power(c, 1.0 / ADOBE_GAMMA)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def luminance_linear(bgr_srgb):
    lin = to_linear(bgr_srgb)
    if COLOUR["mode"] == "adobe":
        return 0.0753 * lin[..., 0] + 0.6273 * lin[..., 1] + 0.2974 * lin[..., 2]
    return 0.0722 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.2126 * lin[..., 2]


def guided_filter(guide, src, r, eps):
    k = (2 * r + 1, 2 * r + 1)
    mean = lambda x: cv2.boxFilter(x, -1, k, borderType=cv2.BORDER_REFLECT)
    mg, ms = mean(guide), mean(src)
    cov = mean(guide * src) - mg * ms
    var = mean(guide * guide) - mg * mg
    a = cov / (var + eps)
    b = ms - a * mg
    return mean(a) * guide + mean(b)


def softlight(cb, cs):
    """W3C soft-light blend (Photoshop's differs slightly)."""
    d = np.where(cb <= 0.25, ((16 * cb - 12) * cb + 4) * cb, np.sqrt(np.maximum(cb, 0)))
    return np.where(cs <= 0.5, cb - (1 - 2 * cs) * cb * (1 - cb), cb + (2 * cs - 1) * (d - cb))


def invert_softlight(cb, target):
    """Gray value cs in [0,1] so that softlight(cb, cs) == target (as close as the blend allows)."""
    eps = 1e-6
    d = np.where(cb <= 0.25, ((16 * cb - 12) * cb + 4) * cb, np.sqrt(np.maximum(cb, 0)))
    burn = 0.5 - (cb - target) / (2 * np.maximum(cb * (1 - cb), eps))
    dodge = 0.5 + (target - cb) / (2 * np.maximum(d - cb, eps))
    return np.clip(np.where(target <= cb, burn, dodge), 0.0, 1.0)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(e1 - e0, 1e-6), 0, 1)
    return t * t * (3 - 2 * t)


def tone_from_mid(mid, rng, taper):
    """Midpoint / range / taper -> the (shadows, highlights) pairs used by tone_weight.
    The effect is 0 outside mid +- rng/2 and 1 in the middle; the two edges fade over `taper` (brightness units,
    0-1), measured inside that range."""
    lo, hi = mid - rng / 2, mid + rng / 2
    t = min(max(taper, 1e-3), rng / 2)
    return (lo, lo + t), (hi - t, hi)


def tone_weight(v, shadows, highlights):
    """1 in the midtones, fading to 0 in the darkest and lightest parts. v = sRGB-encoded brightness 0..1;
    shadows = (zero_below, full_above), highlights = (full_below, zero_above)."""
    return smoothstep(shadows[0], shadows[1], v) * (1 - smoothstep(highlights[0], highlights[1], v))


def parse_band(spec, default_taper=0.5):
    """'LO:HI:GAIN[:TAPER]' -> (lo_mm, hi_mm, gain, taper_octaves)."""
    parts = [float(x) for x in spec.split(":")]
    if len(parts) not in (3, 4):
        raise ValueError(f"band '{spec}' must look like LO:HI:GAIN[:TAPER]")
    lo, hi, gain = parts[:3]
    taper = parts[3] if len(parts) == 4 else default_taper
    if not (0 <= lo < hi):
        raise ValueError(f"band '{spec}': need 0 <= LO < HI")
    return lo, hi, gain, max(taper, 1e-3)


def band_gain(size_mm, bands):
    """Total gain at a feature size for a list of bands (lo, hi, gain, taper).
    A band edge that is shared with a neighbouring band (hi of one == lo of the next) is a crossfade centred on that
    size, using the lower band's taper. A free edge fades inside the band: 0 at the edge, full at edge * 2**taper
    (or down from hi / 2**taper)."""
    total = 0.0
    for i, (lo, hi, gain, taper) in enumerate(bands):
        lo_shared = next((b for b in bands if abs(b[1] - lo) < 1e-9), None)
        hi_shared = next((b for b in bands if abs(b[0] - hi) < 1e-9), None)
        x_lo = math.log2(size_mm / lo) if lo > 0 else 99.0
        x_hi = math.log2(hi / size_mm)
        if lo_shared is not None:
            t = lo_shared[3]
            up = smoothstep(-t / 2, t / 2, x_lo)
        else:
            up = smoothstep(0.0, taper, x_lo)
        if hi_shared is not None:
            down = smoothstep(-taper / 2, taper / 2, x_hi)
        else:
            down = smoothstep(0.0, taper, x_hi)
        total += gain * float(up * down)
    return total


def compute_change_band(Y, S, strength, bands, edge, tau, max_ev, guided_up=True,
                        shadows=None, highlights=None, fine_mm=None, mask=None, apply_mask=True):
    """Multi-band version. bands = [(lo_mm, hi_mm, gain, taper_octaves), ...]; each band corrects feature sizes
    (width of the structure) between lo and hi with its own gain. Sizes outside every band are left alone.
    fine_mm defaults to the smallest lo; it only sets the working resolution and the first level."""
    h, w = Y.shape
    if fine_mm is None:
        fine_mm = max(min(b[0] for b in bands), 0.3)
    px_mm = S / IPD_MM
    fine_px = fine_mm * px_mm
    f = min(1.0, 1.0 / max(fine_px, 1e-6))
    ww, hw = max(8, round(w * f)), max(8, round(h * f))
    f_x = ww / w
    ly = np.log(Y + 1e-4).astype(np.float32)
    ly_w = cv2.resize(ly, (ww, hw), interpolation=cv2.INTER_AREA)
    M_w = None
    if mask is not None:
        # Skin-only mode. Before the band split, pixels outside the mask are replaced by a smooth local skin average
        # (normalised convolution) so hair, lashes, clothes and background neither pull the local means nor create edges
        # next to the skin. The correction is multiplied by the mask afterwards.
        M_w = cv2.resize(mask.astype(np.float32), (ww, hw), interpolation=cv2.INTER_AREA)
        if float(M_w.max()) < 1e-3:
            return np.zeros((h, w), np.float32), {"work_size": (ww, hw), "dev_std_before": 0.0, "dev_std_after": 0.0,
                                                   "px_per_mm": px_mm, "weight": np.ones_like(M_w), "levels": []}
        mean_in = float(ly_w[M_w > 0.5].mean()) if (M_w > 0.5).any() else float(ly_w.mean())

        def nconv(sig):
            return cv2.GaussianBlur(ly_w * M_w, (0, 0), sig), cv2.GaussianBlur(M_w, (0, 0), sig)

        n1, d1 = nconv(max(0.8, 1.5 * px_mm * f_x))   # near the skin edge: continue the nearest skin values (no rim)
        n2, d2 = nconv(max(1.0, 8.0 * px_mm * f_x))   # farther away: smooth local skin average
        f1 = n1 / np.maximum(d1, 1e-3)
        f2 = np.where(d2 > 1e-3, n2 / np.maximum(d2, 1e-3), mean_in)
        w1 = smoothstep(0.02, 0.3, d1)
        fill = (w1 * f1 + (1 - w1) * f2).astype(np.float32)
        ly_w = (M_w * ly_w + (1 - M_w) * fill).astype(np.float32)
    L_prev = cv2.GaussianBlur(ly_w, (0, 0), 1.0)
    ly_s = L_prev
    top = max(b[1] for b in bands)
    band = np.zeros_like(L_prev)
    levels = []
    size = fine_mm
    while size <= top:
        nxt = size * math.sqrt(2)
        r = max(1, round((nxt * px_mm * f_x - 1) / 2))
        L_next = guided_filter(L_prev, L_prev, r, edge ** 2)
        D = L_prev - L_next
        centre = math.sqrt(size * nxt)
        g = band_gain(centre, bands)
        band += g * D
        levels.append((centre, float(D.std()), g))
        L_prev, size = L_next, nxt
    change_w = -strength * band * np.exp(-((band / tau) ** 2))
    weight_w = np.ones_like(change_w)
    if shadows is not None and highlights is not None:
        weight_w = tone_weight(to_srgb(np.exp(ly_s)), shadows, highlights).astype(np.float32)
        change_w = change_w * weight_w
    if M_w is not None and apply_mask:
        change_w = change_w * M_w
    cap = max_ev * math.log(2)
    change_w = np.clip(change_w, -cap, cap).astype(np.float32)
    up = cv2.resize(change_w, (w, h), interpolation=cv2.INTER_CUBIC)
    if guided_up:
        ly_g = cv2.GaussianBlur(ly, (0, 0), max(1.0, fine_px * 0.7))
        r_up = max(2, round(1.73 * fine_px))
        up = guided_filter(ly_g, up.astype(np.float32), r_up, 0.02 ** 2)
    if mask is not None and apply_mask:
        up = up * mask.astype(np.float32)  # full-resolution mask keeps the boundary sharp
    up = np.clip(up, -cap, cap).astype(np.float32)
    diag = {"work_size": (ww, hw), "dev_std_before": float(band.std()), "dev_std_after": float((band + change_w).std()),
            "px_per_mm": px_mm, "weight": weight_w, "levels": levels}
    return up, diag


def compute_change(Y, S, strength, fine_mm, scale_mm, edge, tau, max_ev, guided_up=True,
                   shadows=None, highlights=None):
    """Returns (change at full res in ln units, dict of diagnostics). change > 0 = brighten (dodge)."""
    h, w = Y.shape
    px_per_mm = S / IPD_MM
    fine_px = fine_mm * px_per_mm
    f = min(1.0, 1.0 / max(fine_px, 1e-6))
    ww, hw = max(8, round(w * f)), max(8, round(h * f))
    f_x = ww / w
    ly = np.log(Y + 1e-4).astype(np.float32)
    ly_w = cv2.resize(ly, (ww, hw), interpolation=cv2.INTER_AREA)
    ly_s = cv2.GaussianBlur(ly_w, (0, 0), 1.0)
    r = max(1, round(1.73 * scale_mm * px_per_mm * f_x))
    ref = guided_filter(ly_s, ly_s, r, edge ** 2)
    dev = ly_s - ref
    change_w = -strength * dev * np.exp(-((dev / tau) ** 2))
    weight_w = np.ones_like(change_w)
    if shadows is not None and highlights is not None:
        weight_w = tone_weight(to_srgb(np.exp(ly_s)), shadows, highlights).astype(np.float32)
        change_w = change_w * weight_w
    cap = max_ev * math.log(2)
    change_w = np.clip(change_w, -cap, cap).astype(np.float32)
    up = cv2.resize(change_w, (w, h), interpolation=cv2.INTER_CUBIC)
    if guided_up:
        ly_g = cv2.GaussianBlur(ly, (0, 0), max(1.0, fine_px * 0.7))
        r_up = max(2, round(1.73 * fine_px))
        up = guided_filter(ly_g, up.astype(np.float32), r_up, 0.02 ** 2)
    up = np.clip(up, -cap, cap).astype(np.float32)
    # how far pixels still are from the *original* local mean after the change (same reference)
    diag = {"work_size": (ww, hw), "guided_radius_work_px": r, "dev_std_before": float(dev.std()),
            "dev_std_after": float((dev + change_w).std()), "px_per_mm": px_per_mm, "weight": weight_w}
    return up, diag


def band_std_fine(Yfull, sigma):
    ly = np.log(Yfull + 1e-4).astype(np.float32)
    return float((ly - cv2.GaussianBlur(ly, (0, 0), sigma)).std())


def side_by_side(images, width_total=2400):
    k = width_total / sum(i.shape[1] for i in images)
    hh = round(images[0].shape[0] * k)
    return np.hstack([cv2.resize(i, (round(i.shape[1] * k), hh), interpolation=cv2.INTER_AREA) for i in images])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--S", type=float, help="pixels per 63 mm at the subject")
    g.add_argument("--auto", action="store_true", help="estimate S for the AF target / best subject")
    ap.add_argument("--mode", choices=("band", "single"), default="band",
                    help="band: correct only feature sizes in --band-mm; single: flatten against one local mean (--scale-mm)")
    ap.add_argument("--bands", nargs="+", default=["1.5:50:1.0:0.5"], metavar="LO:HI:GAIN[:TAPER]",
                    help="band mode: one or more bands. Feature sizes (mm) in LO..HI are corrected with that gain; sizes in "
                         "no band are left alone. TAPER = edge softness in octaves (default 0.5). Example: "
                         "--bands 1.5:40:1.0 40:80:0.3")
    ap.add_argument("--strength", type=float, default=0.7)
    ap.add_argument("--fine-mm", type=float, default=None,
                    help="single mode: detail below this size is left alone (default 1.5); band mode: ignored, LO is used")
    ap.add_argument("--scale-mm", type=float, default=12.0, help="largest size to even out (also the reference size)")
    ap.add_argument("--edge", type=float, default=0.12, help="log-luminance step treated as an edge (0.12 ~ 13%%)")
    ap.add_argument("--tau", type=float, default=0.35, help="deviations larger than this (ln units) are left alone")
    ap.add_argument("--max-ev", type=float, default=0.3, help="maximum correction in stops")
    ap.add_argument("--tone-mid", type=float, default=0.52, help="brightness (sRGB 0-1) at the centre of the corrected range")
    ap.add_argument("--tone-range", type=float, default=0.87, help="total width of the corrected brightness range; nothing outside it changes")
    ap.add_argument("--tone-taper", type=float, default=0.16, help="softness of both edges, inside the range (brightness units)")
    ap.add_argument("--no-tone", action="store_true", help="apply the same strength at every brightness")
    ap.add_argument("--no-guided-upsample", action="store_true")
    ap.add_argument("--out", default="out7")
    a = ap.parse_args()

    path = Path(a.image)
    raw = cv2.imread(str(path), cv2.IMREAD_COLOR_BGR | cv2.IMREAD_ANYDEPTH)
    if raw is None:
        raise SystemExit(f"cannot read {path}")
    img = raw.astype(np.float32) / (65535.0 if raw.dtype == np.uint16 else 255.0)
    h, w = img.shape[:2]

    from . import exif_af
    af = exif_af.af_point(path, w, h)
    focus = af
    if a.auto:
        from . import combine_scale as C
        S, conf, desc, centre = C.target_scale(path)
        if S is None:
            raise SystemExit(f"cannot estimate S: {desc}")
        focus = centre or af
        print(f"S = {S:.0f} px ({conf}); {desc}")
    else:
        S = a.S

    Y = luminance_linear(img).astype(np.float32)
    tone = (None, None) if a.no_tone else tone_from_mid(a.tone_mid, a.tone_range, a.tone_taper)
    if a.mode == "band":
        bands = [parse_band(b) for b in a.bands]
        change, diag = compute_change_band(Y, S, a.strength, bands, a.edge, a.tau, a.max_ev,
                                           not a.no_guided_upsample, *tone)
        a.fine_mm = max(min(b[0] for b in bands), 0.3)
        print("feature size (mm) | band energy (std, ln) | gain")
        for c, e, g in diag["levels"]:
            print(f"   {c:6.1f}         |   {e:.4f}              | {g:.2f}")
    else:
        a.fine_mm = 1.5 if a.fine_mm is None else a.fine_mm
        change, diag = compute_change(Y, S, a.strength, a.fine_mm, a.scale_mm, a.edge, a.tau, a.max_ev,
                                      not a.no_guided_upsample, *tone)
    px_mm = diag["px_per_mm"]
    rng = ("bands " + ", ".join(f"{b[0]:g}-{b[1]:g} mm x{b[2]:g}" for b in bands)) if a.mode == "band" else f"size range up to {a.scale_mm} mm = {a.scale_mm * px_mm:.0f} px"
    print(f"{w}x{h}, {px_mm:.2f} px/mm; fine < {a.fine_mm} mm = {a.fine_mm * px_mm:.1f} px, "
          f"{rng}; analysis at {diag['work_size'][0]}x{diag['work_size'][1]}")

    cb = to_srgb(Y).astype(np.float32)
    target = to_srgb(Y * np.exp(change)).astype(np.float32)
    cs = invert_softlight(cb, target).astype(np.float32)
    achieved = softlight(cb, cs)
    req = target - cb
    got = achieved - cb
    sig = np.abs(req) > 1e-3
    frac_ok = float(np.mean(np.abs(got[sig]) >= 0.9 * np.abs(req[sig]))) if sig.any() else 1.0

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    cv2.imwrite(str(out / f"{stem}_db_layer16.png"), np.round(cs * 65535).astype(np.uint16))
    cv2.imwrite(str(out / f"{stem}_db_layer8.png"), np.round(cs * 255).astype(np.uint8))

    after = np.clip(softlight(img, cs[..., None]), 0, 1)
    vis = np.clip(0.5 + 0.5 * change / (a.max_ev * math.log(2)), 0, 1)
    heat = np.zeros((h, w, 3), np.float32)
    heat[..., 2] = np.clip(2 * (vis - 0.5), 0, 1)   # red = dodge (brighten)
    heat[..., 0] = np.clip(2 * (0.5 - vis), 0, 1)   # blue = burn (darken)
    heat = 0.35 + 0.65 * heat
    u8 = lambda x: np.round(np.clip(x, 0, 1) * 255).astype(np.uint8)
    cv2.imwrite(str(out / f"{stem}_db_preview.jpg"), side_by_side([u8(img), u8(after), u8(heat)]), [cv2.IMWRITE_JPEG_QUALITY, 90])

    wgt = cv2.resize(diag["weight"], (w, h), interpolation=cv2.INTER_LINEAR)
    cv2.imwrite(str(out / f"{stem}_db_weight.jpg"), cv2.resize(u8(wgt), (w // 4, h // 4), interpolation=cv2.INTER_AREA))
    cx, cy = (int(focus[0]), int(focus[1])) if focus else (w // 2, h // 2)
    half = int(min(2.5 * S, min(w, h) / 2))
    x0, y0 = min(max(0, cx - half), w - 2 * half), min(max(0, cy - half), h - 2 * half)
    sl = (slice(y0, y0 + 2 * half), slice(x0, x0 + 2 * half))
    cv2.imwrite(str(out / f"{stem}_db_crop.jpg"), side_by_side([u8(img[sl]), u8(after[sl])], 2000), [cv2.IMWRITE_JPEG_QUALITY, 92])

    Ya = luminance_linear(after[sl]).astype(np.float32)
    sig_f = max(1.0, a.fine_mm * px_mm * 0.7)
    fine_b, fine_a = band_std_fine(Y[sl], sig_f), band_std_fine(Ya, sig_f)
    cc = change[sl]
    if not a.no_tone:
        print(f"tone weight: {100 * np.mean(wgt < 0.1):.0f}% of pixels ignored (<10% effect), {100 * np.mean(wgt > 0.9):.0f}% get full effect")
    print(f"change: mean {cc.mean() / math.log(2):+.3f} stops, 95th percentile |change| {np.percentile(np.abs(cc), 95) / math.log(2):.3f} stops "
          f"(cap {a.max_ev})")
    print(f"deviation from the local mean (std, ln units): {diag['dev_std_before']:.4f} -> {diag['dev_std_after']:.4f} "
          f"({100 * (diag['dev_std_after'] / diag['dev_std_before'] - 1):+.0f}%)")
    print(f"fine-band detail (crop around the subject): {fine_b:.4f} -> {fine_a:.4f} ({100 * (fine_a / fine_b - 1):+.1f}%)")
    print(f"soft light can realise the requested change for {100 * frac_ok:.0f}% of changed pixels (limits near black/white)")
    print(f"wrote {out}/{stem}_db_layer16.png, _layer8.png, _preview.jpg, _crop.jpg, _weight.jpg")



def layer_from_change(Y, change):
    """Gray Soft Light layer (0..1) that produces the given ln-luminance change on a picture with linear luminance Y."""
    cb = to_srgb(Y).astype(np.float32)
    target = to_srgb(Y * np.exp(change)).astype(np.float32)
    return invert_softlight(cb, target).astype(np.float32)


def tone_mask(Y, S, shadows, highlights):
    """Brightness window (see tone_weight) as a full-resolution mask: 1 in the chosen midtones, 0 in the darkest and
    lightest parts. Brightness is that of the pixel itself, smoothed over about 1 mm."""
    ly = np.log(Y + 1e-4).astype(np.float32)
    lys = cv2.GaussianBlur(ly, (0, 0), max(1.0, S / IPD_MM))
    return tone_weight(to_srgb(np.exp(lys)), shadows, highlights).astype(np.float32)


def color_range_mask(img, hue, saturation, hue_range, saturation_range, blur_px=0.0, strength=1.0, smooth_px=0.0):
    """Soft HSV window around a picked BGR colour; hue is ignored for nearly achromatic picks. The image is smoothed by
    smooth_px (Gaussian sigma) first so pixel noise and texture do not make the mask speckled or blocky."""
    img = np.ascontiguousarray(img, dtype=np.float32)
    if smooth_px > 0.3:
        img = cv2.GaussianBlur(img, (0, 0), smooth_px)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hue_delta = np.abs(hsv[..., 0] - hue)
    hue_delta = np.minimum(hue_delta, 360.0 - hue_delta)
    hue_weight = 1.0 - smoothstep(0.8 * hue_range, hue_range, hue_delta)
    if saturation < 0.05:
        hue_weight.fill(1.0)
    sat_delta = np.abs(hsv[..., 1] - saturation)
    sat_weight = 1.0 - smoothstep(0.8 * saturation_range, saturation_range, sat_delta)
    m = np.clip(hue_weight * sat_weight, 0, 1).astype(np.float32)
    if blur_px > 0.3:
        m = cv2.GaussianBlur(m, (0, 0), blur_px)
    return (1.0 - strength * (1.0 - m)).astype(np.float32)


def structure_map(Y, S, size_mm, threshold):
    """Soft map (0..1) of narrow skin structure: folds, creases, neck lines, muscle definition, body contours. Elongated
    ridges and valleys of the ln-luminance (two-sided Hessian response, blobs suppressed) at widths around size_mm,
    soft-thresholded: 0 below `threshold`, 1 above twice that. Returns float32, full resolution."""
    ly = np.log(Y + 1e-4).astype(np.float32)
    ppm = S / IPD_MM
    resp = np.zeros_like(ly)
    for f in (0.7, 1.0, 1.4):
        sig = max(0.8, 0.5 * size_mm * ppm * f)
        g = cv2.GaussianBlur(ly, (0, 0), sig)
        a, b, c = cv2.Sobel(g, cv2.CV_32F, 2, 0, ksize=3) / 8, cv2.Sobel(g, cv2.CV_32F, 1, 1, ksize=3) / 4, cv2.Sobel(g, cv2.CV_32F, 0, 2, ksize=3) / 8
        r = np.sqrt(((a - c) / 2) ** 2 + b * b)
        l1, l2 = np.abs((a + c) / 2 + r), np.abs((a + c) / 2 - r)
        resp = np.maximum(resp, np.maximum(0.0, np.maximum(l1, l2) - np.minimum(l1, l2)) * sig * sig)
    line = smoothstep(threshold, 2 * threshold, resp).astype(np.float32)
    return cv2.GaussianBlur(line, (0, 0), max(0.8, 0.25 * size_mm * ppm))


def combined_mask(img, S, mask=None, shadows=None, highlights=None, tone_strength=1.0,
                  protect=0.0, protect_mm=4.0, protect_thr=0.03):
    """Mask shared by D&B, standalone skin-mask output and frequency separation."""
    Y = luminance_linear(img).astype(np.float32)
    total = None if mask is None else mask.astype(np.float32)
    if shadows is not None and highlights is not None:
        wt = tone_mask(Y, S, shadows, highlights)
        wt = (1.0 - tone_strength * (1.0 - wt)).astype(np.float32)
        total = wt if total is None else total * wt
    st = None
    if protect > 0:
        st = structure_map(Y, S, protect_mm, protect_thr)
        keep = (1.0 - min(protect, 1.0) * st).astype(np.float32)
        total = keep if total is None else total * keep
    return total, st


def process(img, S, bands, strength=1.0, edge=0.12, tau=1.0, max_ev=1.0, shadows=None, highlights=None, guided_up=True,
            mask=None, blend_mm=6.0, protect=0.0, protect_mm=4.0, protect_thr=0.03, tone_strength=1.0):
    """Whole pipeline for library/UI use. img: float32 BGR 0..1. Returns (cs, after, change, diag, total_mask):
    cs         = gray Soft Light layer (0..1) with NO mask of any kind applied: no skin mask and no tone window. Inside the
                 skin and up to blend_mm outside it the layer comes from the mask-aware analysis (hair and background do
                 not disturb it); farther away it comes from the plain whole-picture analysis, cross-faded smoothly,
                 so the layer is valid and free of edges everywhere,
    after      = img with the layer blended in Soft Light mode through total_mask (the final result),
    change     = the ln-luminance change of that result,
    total_mask = skin mask x tone window x (1 - protect x structure) (None if none is used): the one mask to use as the
                 layer mask of cs. The structure map is returned as diag["structure"] (None if protect is 0)."""
    Y = luminance_linear(img).astype(np.float32)
    change_u, diag = compute_change_band(Y, S, strength, bands, edge, tau, max_ev, guided_up, None, None,
                                         mask=mask, apply_mask=False)
    if mask is not None:
        change_p, _ = compute_change_band(Y, S, strength, bands, edge, tau, max_ev, guided_up, None, None)
        m = mask.astype(np.float32)
        dist = cv2.distanceTransform((m <= 0.5).astype(np.uint8), cv2.DIST_L2, 5)  # px outside the mask
        W = (1.0 - smoothstep(0.0, max(1.0, blend_mm * S / IPD_MM), dist)).astype(np.float32)
        change_u = W * change_u + (1 - W) * change_p
    cs = layer_from_change(Y, change_u)
    total, diag["structure"] = combined_mask(img, S, mask, shadows, highlights, tone_strength,
                                            protect, protect_mm, protect_thr)
    if total is None:
        change, after = change_u, np.clip(softlight(img, cs[..., None]), 0, 1).astype(np.float32)
    else:
        change = change_u * total
        cs_masked = layer_from_change(Y, change)  # same as the layer with `total` as its layer mask (for small changes)
        after = np.clip(softlight(img, cs_masked[..., None]), 0, 1).astype(np.float32)
    return cs, after, change, diag, total


if __name__ == "__main__":
    main()
