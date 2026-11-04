"""Combine face and body cues into one scale estimate per person.

S is the interpupillary distance (IPD) in pixels, i.e. 'how many pixels is a 63 mm face unit'.
Cues (each gets an assumed relative error sigma; all are population-average based, not calibrated):
  face_ipd   iris-centre distance from the landmark model (sigma 8%, 15% if it disagrees with YuNet's eye distance)
  iris       visible iris diameter * (63 / 11.7)                (sigma 12%, only if the iris is >= 20 px)
  yunet_ipd  YuNet's eye distance, only when the landmark model rejected the face   (sigma 15%)
  body       RTMPose body segments -> body height -> IPD        (sigma 20%)
With 3+ cues, any cue more than 25% from the median is dropped. The rest are averaged in log space,
weighted by 1/sigma^2. Confidence is reported, not hidden: high / medium / low / none.

Whole-body extras (hand length, eye-corner width) are added as further cues. If the file carries a Sony
autofocus point, the subject nearest to it is marked as the target (useful with several people or mirrors).

Usage: uv run python -m shp_photocraft_tools.modules.dnb.combine_scale IMAGE [IMAGE ...] [--draw] [--out DIR]
"""
import math
import sys
from pathlib import Path

import cv2
import numpy as np

from . import estimate_body_scale as B
from . import estimate_scale as F
from . import exif_af

IPD_MM, IRIS_MM = F.IPD_MM, F.IRIS_MM
OUTLIER = math.log(1.25)


def yaw_sigma(yaw_deg, base):
    """A turned head shortens the eye-to-eye distance. The yaw estimate is rough, so it is used to trust
    the cue less (not to 'un-turn' it): sigma grows with 1-cos(yaw), capped at 75 degrees."""
    return base + 0.25 * (1 - math.cos(math.radians(min(abs(yaw_deg), 75))))


def face_cues(rgb, f):
    p1, p, pres = F.landmarks_for(rgb, f)
    m = F.measures(p, f)
    cues = []
    if pres >= 0.5:
        dis = abs(m["ipd3d"] / m["yunet_ipd"] - 1) if m["yunet_ipd"] else 0
        cues.append(("face_ipd", m["ipd3d"], yaw_sigma(m["yaw_deg"], 0.15 if dis > 0.15 else 0.08)))
        if m["iris_d"] >= 20:
            cues.append(("iris", m["iris_d"] * IPD_MM / IRIS_MM, 0.12))
    else:
        cues.append(("yunet_ipd", m["yunet_ipd"], 0.15))
    return cues, (p, m, pres)


def match_body(f, bodies):
    x, y, w, h = f[:4]
    x0, y0, x1, y1 = x - 0.2 * w, y - 0.2 * h, x + 1.2 * w, y + 1.2 * h
    for bi, (kp, sc) in enumerate(bodies):
        for name in ("nose", "l_eye", "r_eye", "l_ear", "r_ear"):
            i = B.I[name]
            if sc[i] >= B.MIN_SCORE and x0 <= kp[i][0] <= x1 and y0 <= kp[i][1] <= y1:
                return bi
    return None


def combine(cues):
    """cues: list of (name, S, sigma). Returns dict."""
    if not cues:
        return {"S": None, "confidence": "none", "used": [], "dropped": [], "sigma": None}
    used, dropped = list(cues), []
    if len(cues) >= 3:
        med = float(np.median([math.log(c[1]) for c in cues]))
        used = [c for c in cues if abs(math.log(c[1]) - med) <= OUTLIER]
        dropped = [c for c in cues if c not in used]
    w = np.array([1 / c[2] ** 2 for c in used])
    S = math.exp(float(np.sum(w * np.log([c[1] for c in used])) / w.sum()))
    sigma = 1 / math.sqrt(float(w.sum()))
    # cues already down-weighted heavily (sigma > 20%, e.g. a turned face) are averaged but don't decide confidence
    trusted = [c for c in used if c[2] <= 0.20] or used
    logs = [math.log(c[1]) for c in trusted]
    spread = math.exp(max(logs) - min(logs)) - 1 if len(trusted) > 1 else 0.0
    if len(trusted) >= 2 and spread <= 0.12:
        conf = "high"
    elif len(trusted) >= 2 and spread <= 0.25:
        conf = "medium"
    elif len(used) == 1 and used[0][2] <= 0.10:
        conf = "medium"
    else:
        conf = "low"
    if len(trusted) == 2 and spread > 0.25:
        conf = "low (cues disagree)"
    return {"S": S, "confidence": conf, "used": used, "dropped": dropped, "sigma": sigma, "spread": spread}


def build_subjects(path, img):
    """Detect faces and bodies, gather cues per person, and pick the AF target (if the file has an AF point).
    Returns (subjects, bodies, af, target, faces); a subject is (kind, face, face_info, body_index, body_estimate, cues)."""
    h, w = img.shape[:2]
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    faces = F.detect(img)
    kps, scs = B.detect(img)
    bodies = list(zip(kps, scs))
    used_body = set()
    subjects = []
    for f in faces:
        cues, face_info = face_cues(rgb, f)
        bi = match_body(f, bodies)
        est = None
        if bi is not None:
            used_body.add(bi)
            est = B.estimate(*bodies[bi])
            if est:
                cues.append(("body", est["S"], 0.20))
            yaw = face_info[1]["yaw_deg"]
            cues += [(n_, S_, yaw_sigma(yaw, sg) if n_ == "eye_corners" else sg) for n_, S_, sg in B.extra_cues(*bodies[bi])]
        subjects.append(("face", f, face_info, bi, est, cues))
    for bi, (kp, sc) in enumerate(bodies):
        if bi in used_body:
            continue
        est = B.estimate(kp, sc)
        cues = ([("body", est["S"], 0.20)] if est else []) + B.extra_cues(kp, sc)
        subjects.append(("body", None, None, bi, est, cues))
    af = exif_af.af_point(path, w, h)
    target = None
    if af is not None and subjects:
        def dist(sub):
            kind, f, _, bi, _, _ = sub
            if f is not None:
                box = np.array([f[0], f[1], f[0] + f[2], f[1] + f[3]])
            else:
                pts = bodies[bi][0][:17][bodies[bi][1][:17] >= 0.3]
                if len(pts) == 0:
                    return 1e18
                box = np.array([*pts.min(0), *pts.max(0)])
            dx = max(box[0] - af[0], 0, af[0] - box[2])
            dy = max(box[1] - af[1], 0, af[1] - box[3])
            return math.hypot(dx, dy)
        target = min(range(len(subjects)), key=lambda i: dist(subjects[i]))
    return subjects, bodies, af, target, faces


def target_scale(path, img=None):
    """S for the person to retouch: the AF target if known, otherwise the most trustworthy subject.
    Returns (S, confidence, description, (cx, cy)) or (None, 'none', reason, None); (cx, cy) is the centre of the
    subject's face box (or its body keypoints) in image pixels."""
    img = cv2.imread(str(path)) if img is None else img
    subjects, bodies, af, target, _ = build_subjects(str(path), img)
    results = [(i, combine(sub[5])) for i, sub in enumerate(subjects)]
    results = [(i, r) for i, r in results if r["S"] is not None]
    if not results:
        return None, "none", "no usable scale cue", None
    if target is not None and any(i == target for i, _ in results):
        i, r = next((i, r) for i, r in results if i == target)
        why = "AF target"
    else:
        i, r = min(results, key=lambda t: t[1]["sigma"])
        why = "most trustworthy subject (no AF point)" if af is None else "most trustworthy subject (AF target has no cue)"
    kind, f, _, bi, _, _ = subjects[i]
    if f is not None:
        centre = (float(f[0] + f[2] / 2), float(f[1] + f[3] / 2))
    else:
        kp, sc = bodies[bi]
        pts = kp[:17][sc[:17] >= 0.3]
        centre = (float(pts[:, 0].mean()), float(pts[:, 1].mean())) if len(pts) else None
    return (r["S"], r["confidence"], f"subject #{i}, {why}, cues " + ", ".join(f"{c[0]}={c[1]:.0f}" for c in r["used"]), centre)


def main():
    argv = sys.argv[1:]
    out_dir = Path("/tmp/opencode")
    if "--out" in argv:
        i = argv.index("--out")
        out_dir = Path(argv[i + 1])
        del argv[i:i + 2]
    do_draw = "--draw" in argv
    for path in [a for a in argv if not a.startswith("--")]:
        img = cv2.imread(path)
        if img is None:
            print(f"{path}: cannot read")
            continue
        h, w = img.shape[:2]
        canvas = img.copy()
        subjects, bodies, af, target, faces = build_subjects(path, img)
        print(f"{Path(path).name}: {w}x{h}, {len(faces)} face(s), {len(bodies)} body(ies)"
              + (f", AF point ({af[0]:.0f},{af[1]:.0f})" if af else ", no AF point"))
        for n, (kind, f, face_info, bi, est, cues) in enumerate(subjects):
            r = combine(cues)
            tag = "  <- AF target" if n == target else ""
            if r["S"] is None:
                print(f"  #{n} ({kind}) no usable cue{tag}")
            else:
                cue_txt = ", ".join(f"{c[0]}={c[1]:.0f}" for c in r["used"])
                drop_txt = ("  dropped: " + ", ".join(f"{c[0]}={c[1]:.0f}" for c in r["dropped"])) if r["dropped"] else ""
                yawtxt = f" yaw~{face_info[1]['yaw_deg']:.0f}deg" if face_info is not None else ""
                print(f"  #{n} ({kind}{yawtxt}) S={r['S']:.0f}px +-{100 * r['sigma']:.0f}%  confidence={r['confidence']}  [{cue_txt}]{drop_txt}{tag}")
                if r["used"]:
                    print("       sigma: " + ", ".join(f"{c[0]} {100 * c[2]:.0f}%" for c in r["used"]))
            if do_draw:
                if face_info is not None and face_info[2] >= 0.5:
                    F.draw_face(canvas, f, face_info[0], face_info[1], False)
                if bi is not None:
                    B.draw(canvas, bodies[bi][0], bodies[bi][1], est, n + 1)
                if r["S"] is not None:
                    u = max(1.0, w / 1000)
                    lab = f"#{n} S={r['S']:.0f}px {r['confidence']}" + (" TARGET" if n == target else "")
                    y = int(h - (40 + 45 * n) * u)
                    for col, t in (((0, 0, 0), max(3, round(5 * u))), ((255, 255, 255), max(1, round(2 * u)))):
                        cv2.putText(canvas, lab, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 1.4 * u, col, t, cv2.LINE_AA)
        if do_draw:
            if af is not None:
                u = max(1.0, w / 1000)
                a = (int(af[0]), int(af[1]))
                cv2.drawMarker(canvas, a, (0, 165, 255), cv2.MARKER_CROSS, int(80 * u), max(3, round(4 * u)))
                cv2.circle(canvas, a, int(40 * u), (0, 165, 255), max(2, round(3 * u)))
            out_dir.mkdir(parents=True, exist_ok=True)
            k = min(1.0, 2000 / max(h, w))
            o = out_dir / f"{Path(path).stem}_combined.jpg"
            cv2.imwrite(str(o), cv2.resize(canvas, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 90])
            print(f"  wrote {o}")


if __name__ == "__main__":
    main()
