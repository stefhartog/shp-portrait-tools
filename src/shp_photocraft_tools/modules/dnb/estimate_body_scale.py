"""Estimate subject scale from body pose when no usable face is found.

RTMPose (via rtmlib, Apache-2.0) finds a person and 17 COCO keypoints. Each visible body segment
implies a body height from an average proportion (Drillis & Contini style fractions of height).
2D lengths can only be shortened by foreshortening, so the estimate leans on the longer implied
heights. The result is reported as 'H' (body height in px) and as an IPD-equivalent S, so it can be
compared with the face-based S.  All ratios are population averages, so expect roughly +-10-15%.

Usage: uv run python -m shp_photocraft_tools.modules.dnb.estimate_body_scale IMAGE [IMAGE ...] [--draw] [--out DIR]
"""
import sys
from pathlib import Path

from . import dnb_gpu  # noqa: F401
import cv2
import numpy as np
from rtmlib import Wholebody

MAX_SIDE = 1600
BODY_H_MM = 1680.0  # rough adult mixed-sex average
IPD_MM = 63.0
KP = ["nose", "l_eye", "r_eye", "l_ear", "r_ear", "l_sho", "r_sho", "l_elb", "r_elb", "l_wri", "r_wri",
      "l_hip", "r_hip", "l_kne", "r_kne", "l_ank", "r_ank"]
I = {n: i for i, n in enumerate(KP)}
# (name, keypoint a, keypoint b, segment length as a fraction of body height)
SEGMENTS = [
    ("upper_arm_l", "l_sho", "l_elb", 0.186), ("upper_arm_r", "r_sho", "r_elb", 0.186),
    ("forearm_l", "l_elb", "l_wri", 0.146), ("forearm_r", "r_elb", "r_wri", 0.146),
    ("thigh_l", "l_hip", "l_kne", 0.245), ("thigh_r", "r_hip", "r_kne", 0.245),
    ("shank_l", "l_kne", "l_ank", 0.246), ("shank_r", "r_kne", "r_ank", 0.246),
    ("shoulders", "l_sho", "r_sho", 0.22), ("torso", "mid_sho", "mid_hip", 0.30),
]
MIN_SCORE = 0.5
SKELETON = [("l_sho", "r_sho"), ("l_sho", "l_elb"), ("l_elb", "l_wri"), ("r_sho", "r_elb"), ("r_elb", "r_wri"),
            ("l_sho", "l_hip"), ("r_sho", "r_hip"), ("l_hip", "r_hip"), ("l_hip", "l_kne"), ("l_kne", "l_ank"),
            ("r_hip", "r_kne"), ("r_kne", "r_ank"), ("nose", "l_eye"), ("nose", "r_eye"), ("l_eye", "l_ear"), ("r_eye", "r_ear")]

_body = None


def body():
    global _body
    if _body is None:
        _body = Wholebody(mode="balanced", backend="onnxruntime", device="cuda")
    return _body


def detect(img, pose_config=None):
    if pose_config is not None:
        from . import pose_backend
        pose_config = pose_backend.settings(pose_config)
    h, w = img.shape[:2]
    k = min(1.0, MAX_SIDE / max(h, w))
    small = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else img
    if pose_config and pose_config["backend"] in pose_backend.SCORED_BACKENDS:
        kps, scores = pose_backend.detect(small, pose_config)
    else:
        kps, scores = body()(small)
    return dedupe(kps / k, scores)


def _box(kp, sc):
    pts = kp[sc >= 0.3]
    if len(pts) == 0:
        return None
    return np.array([*pts.min(0), *pts.max(0)])


def dedupe(kps, scores, iou_thr=0.6):
    """The detector sometimes reports one person twice; keep the one with the higher mean visible score."""
    keep = []
    order = sorted(range(len(kps)), key=lambda i: -float(scores[i][scores[i] >= 0.3].sum()))
    for i in order:
        bi = _box(kps[i], scores[i])
        dup = False
        for j in keep:
            bj = _box(kps[j], scores[j])
            if bi is None or bj is None:
                continue
            iw = min(bi[2], bj[2]) - max(bi[0], bj[0])
            ih = min(bi[3], bj[3]) - max(bi[1], bj[1])
            inter = max(0, iw) * max(0, ih)
            union = (bi[2] - bi[0]) * (bi[3] - bi[1]) + (bj[2] - bj[0]) * (bj[3] - bj[1]) - inter
            if union > 0 and inter / union > iou_thr:
                dup = True
                break
        if not dup:
            keep.append(i)
    return kps[keep], scores[keep]


def point(kp, sc, name):
    if name == "mid_sho":
        a, b = I["l_sho"], I["r_sho"]
    elif name == "mid_hip":
        a, b = I["l_hip"], I["r_hip"]
    else:
        i = I[name]
        return (kp[i], sc[i]) if sc[i] >= MIN_SCORE else (None, sc[i])
    if sc[a] >= MIN_SCORE and sc[b] >= MIN_SCORE:
        return (kp[a] + kp[b]) / 2, min(sc[a], sc[b])
    return None, min(sc[a], sc[b])


HAND_MM = 185.0      # wrist joint to middle fingertip, adult average
EYE_CORNER_MM = 88.0  # outer eye corner to outer eye corner, adult average
B_I = {n: i for i, n in enumerate(["nose", "l_eye", "r_eye", "l_ear", "r_ear"])}
HAND = {"l": (91, 103), "r": (112, 124)}  # COCO-WholeBody: wrist, middle fingertip
EYE_CORNERS = (23 + 36, 23 + 45)           # 68-point face: outer corners of the two eyes


def extra_cues(kp, sc, use_hand=False):
    """Whole-body model extras. Returns list of (name, S in px, sigma).

    eye_corners: only for near-frontal faces (both eyes and both ears confidently visible); a profile or
                 hallucinated face gives a far-too-small width.
    hand:        OFF by default. On the test photos the wrist-to-fingertip length came out 2-3x too short
                 even at high keypoint scores (curled or foreshortened hands), so it is not a usable cue."""
    cues = []
    if use_hand:
        hands = []
        for side, (w, t) in HAND.items():
            if len(sc) > max(w, t) and sc[w] >= 0.4 and sc[t] >= 0.4:
                hands.append(float(np.linalg.norm(kp[w] - kp[t])))
        if hands:
            cues.append(("hand", max(hands) * IPD_MM / HAND_MM, 0.18))
    a, b = EYE_CORNERS
    frontal = all(sc[B_I[n]] >= 0.5 for n in ("l_eye", "r_eye", "l_ear", "r_ear"))
    if len(sc) > max(a, b) and frontal and sc[a] >= 0.8 and sc[b] >= 0.8:
        d = float(np.linalg.norm(kp[a] - kp[b]))
        coco = float(np.linalg.norm(kp[B_I["l_eye"]] - kp[B_I["r_eye"]]))
        # outer-corner width / body-model eye distance is ~1.4 for a frontal face; far from that means a turned or hallucinated face
        if coco > 0 and 1.1 <= d / coco <= 1.9:
            cues.append(("eye_corners", d * IPD_MM / EYE_CORNER_MM, 0.15))
    return cues


def estimate(kp, sc, excluded=()):
    rows = []
    for name, a, b, frac in SEGMENTS:
        if name in excluded:
            continue
        pa, _ = point(kp, sc, a)
        pb, _ = point(kp, sc, b)
        if pa is None or pb is None:
            continue
        length = float(np.linalg.norm(pa - pb))
        if not np.isfinite(length) or length <= 1e-6:
            continue
        rows.append((name, length, length / frac))
    if not rows:
        return None
    hs = sorted(r[2] for r in rows)
    top = hs[len(hs) // 2:]  # upper half: foreshortening only shrinks lengths
    H = float(np.median(top))
    return {"rows": rows, "H": H, "S": H * IPD_MM / BODY_H_MM, "n": len(rows),
            "spread": (hs[-1] - hs[0]) / H if len(hs) > 1 else float("nan")}


def draw(canvas, kp, sc, est, person):
    h, w = canvas.shape[:2]
    u = max(1.0, w / 1000)
    for a, b in SKELETON:
        ia, ib = I[a], I[b]
        if sc[ia] >= MIN_SCORE and sc[ib] >= MIN_SCORE:
            cv2.line(canvas, tuple(int(v) for v in kp[ia]), tuple(int(v) for v in kp[ib]), (0, 255, 0), max(2, round(2 * u)))
    for i, name in enumerate(KP):
        ok = sc[i] >= MIN_SCORE
        cv2.circle(canvas, (int(kp[i][0]), int(kp[i][1])), max(4, round(5 * u)), (255, 0, 255) if ok else (0, 0, 255), -1 if ok else max(1, round(u)))
    if est:
        txt = f"H~{est['H']:.0f}px  S(ipd-equiv)~{est['S']:.0f}px  ({est['n']} segments)"
    else:
        txt = "no usable segments"
    x, y = 20, int(40 * u) + person * int(40 * u)
    for col, t in (((0, 0, 0), max(3, round(4 * u))), ((255, 255, 255), max(1, round(2 * u)))):
        cv2.putText(canvas, txt, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.1 * u, col, t, cv2.LINE_AA)


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
        kps, scores = detect(img)
        print(f"{Path(path).name}: {w}x{h}, {len(kps)} person(s)")
        canvas = img.copy()
        for p, (kp, sc) in enumerate(zip(kps, scores)):
            est = estimate(kp, sc)
            vis = [KP[i] for i in range(17) if sc[i] >= MIN_SCORE]
            print(f"  #{p} visible keypoints {len(vis)}/17: {', '.join(vis)}")
            if est:
                print(f"     H~{est['H']:.0f}px ({100 * est['H'] / h:.0f}% of image height)  S(ipd-equiv)~{est['S']:.0f}px  "
                      f"segments={est['n']} spread={100 * est['spread']:.0f}%")
                print("     " + ", ".join(f"{n}={H:.0f}" for n, _, H in est["rows"]) + "  (implied H px)")
            else:
                print("     not enough keypoints for a body-scale estimate")
            if do_draw:
                draw(canvas, kp, sc, est, p)
        if do_draw:
            out_dir.mkdir(parents=True, exist_ok=True)
            o = out_dir / f"{Path(path).stem}_body.jpg"
            k = min(1.0, 2000 / max(h, w))
            cv2.imwrite(str(o), cv2.resize(canvas, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 90])
            print(f"  wrote {o}")


if __name__ == "__main__":
    main()
