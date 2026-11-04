"""Estimate subject scale from faces: YuNet finds the face, MediaPipe Face Landmarker (478 pts incl. irises) measures it.

Scale measures per face (all in original-image pixels):
  ipd3d    distance between the two iris centres using x, y and z (z is predicted depth),
           which should stay roughly constant as the head turns
  ipd2d    the same without z (shrinks with yaw)
  iris_d   mean iris diameter; the visible iris is ~11.7 mm across in most adults
  S        the scale used downstream (ipd3d), in pixels
Agreement between the ipd and iris based mm/px values is a sanity check, not ground truth.

Usage: uv run python -m shp_photocraft_tools.modules.dnb.estimate_scale IMAGE [IMAGE ...] [--draw] [--mesh] [--out DIR]
  --draw   write <name>_overlay.jpg (whole image) and <name>_face<N>.jpg (zoomed face) with the detected points
  --mesh   also draw all 478 landmarks
"""
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from . import dnb_gpu  # noqa: F401  (must come before onnxruntime sessions are created)
import onnxruntime as ort

from .paths import model_path
MAX_SIDE = 1600  # YuNet is trained for faces up to ~300 px, so downscale big images for detection
IPD_MM = 63.0    # rough adult average; individuals vary roughly 54-74 mm
IRIS_MM = 11.7   # rough adult average visible iris diameter

IRIS_L = [468, 469, 470, 471, 472]  # centre, right, top, left, bottom
IRIS_R = [473, 474, 475, 476, 477]

_sess = None


def session():
    global _sess
    if _sess is None:
        _sess = ort.InferenceSession(str(model_path("face_landmarks.onnx")), providers=dnb_gpu.PROVIDERS)
    return _sess


def detect(img, score=0.7):
    h, w = img.shape[:2]
    k = min(1.0, MAX_SIDE / max(h, w))
    small = cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else img
    det = cv2.FaceDetectorYN.create(str(model_path("yunet.onnx")), "", (small.shape[1], small.shape[0]), score, 0.3, 5000)
    _, faces = det.detect(small)
    out = []
    for f in ([] if faces is None else faces):
        f = f.astype(np.float64).copy()
        f[:14] /= k
        out.append(f)
    return out


def run_landmarks(img_rgb, centre, size, roll):
    """Crop a square of `size` px around `centre`, rotated so the eyes are level, run the model.
    Returns (478x3 points in image px with z scaled the same as x/y, presence 0..1)."""
    s = 256.0 / size
    c, sn = math.cos(roll), math.sin(roll)
    A = s * np.array([[c, sn], [-sn, c]])
    t = 128.0 - A @ np.asarray(centre)
    M = np.hstack([A, t[:, None]])
    crop = cv2.warpAffine(img_rgb, M, (256, 256), flags=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    out = session().run(None, {"input_12": (crop.astype(np.float32) / 255.0)[None]})
    pts = out[0].reshape(478, 3).astype(np.float64)
    presence = 1.0 / (1.0 + math.exp(-float(out[1].ravel()[0])))
    inv = np.linalg.inv(A)
    xy = (pts[:, :2] - t) @ inv.T
    z = pts[:, 2] / s
    return np.column_stack([xy, z]), presence


def landmarks_for(img_rgb, f):
    x, y, w, h = f[:4]
    re, le = f[4:6], f[6:8]
    roll = math.atan2(le[1] - re[1], le[0] - re[0])
    centre = (x + w / 2, y + h / 2)
    p1, pres = run_landmarks(img_rgb, centre, max(w, h) * 1.5, roll)
    # Second pass: re-centre and re-roll from the first pass's own points (how MediaPipe tracks)
    lo, hi = p1[:, :2].min(0), p1[:, :2].max(0)
    centre2 = (lo + hi) / 2
    size2 = float(max(hi - lo)) * 1.3
    ir, il = p1[IRIS_R[0], :2], p1[IRIS_L[0], :2]
    roll2 = math.atan2(il[1] - ir[1], il[0] - ir[0])
    if roll2 > math.pi / 2 or roll2 < -math.pi / 2:  # model's "left"/"right" order vs image order
        roll2 = math.atan2(ir[1] - il[1], ir[0] - il[0])
    p2, pres2 = run_landmarks(img_rgb, centre2, size2, roll2)
    return p1, p2, pres2


def yaw_proxy(p):
    """Head turn in degrees (sign = direction) from where the nose tip sits between the two face edges,
    measured along the eye line. Rough: the occluded edge is a guess of the model, and +-90 means 'in profile'."""
    a, b = p[IRIS_L[0], :2], p[IRIS_R[0], :2]
    u = (a - b) / (np.linalg.norm(a - b) or 1.0)
    s1 = float(np.dot(p[1, :2] - p[234, :2], u))
    s2 = float(np.dot(p[454, :2] - p[1, :2], u))
    tot = s1 + s2
    r = (s1 - s2) / tot if abs(tot) > 1e-6 else 0.0
    return math.degrees(math.asin(max(-1.0, min(1.0, r))))


def measures(p, f):
    ci_l, ci_r = p[IRIS_L[0]], p[IRIS_R[0]]
    ipd2d = float(np.linalg.norm(ci_l[:2] - ci_r[:2]))
    ipd3d = float(np.linalg.norm(ci_l - ci_r))
    d_l = float(np.linalg.norm(p[IRIS_L[1], :2] - p[IRIS_L[3], :2]))
    d_r = float(np.linalg.norm(p[IRIS_R[1], :2] - p[IRIS_R[3], :2]))
    # a turned head squeezes the iris horizontally but not vertically, so use the larger of the two
    v_l = float(np.linalg.norm(p[IRIS_L[2], :2] - p[IRIS_L[4], :2]))
    v_r = float(np.linalg.norm(p[IRIS_R[2], :2] - p[IRIS_R[4], :2]))
    iris_d = (max(d_l, v_l) + max(d_r, v_r)) / 2
    yaw_deg = yaw_proxy(p)
    return {
        "ipd2d": ipd2d, "ipd3d": ipd3d, "iris_d": iris_d, "yaw_deg": yaw_deg,
        "mm_per_px_ipd": IPD_MM / ipd3d, "mm_per_px_iris": IRIS_MM / iris_d if iris_d else float("nan"),
        "yunet_ipd": float(np.linalg.norm(f[6:8] - f[4:6])),
    }


def draw_face(canvas, f, p, m, mesh):
    u = max(1.0, f[2] / 400)  # drawing unit relative to the face, so marks stay legible
    line, dot = max(1, round(1.5 * u)), max(2, round(2.5 * u))
    font, fs, th = cv2.FONT_HERSHEY_SIMPLEX, 0.9 * u, max(1, round(2 * u))
    x, y, w, h = (int(v) for v in f[:4])
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 0, 255), line)  # red: YuNet box
    if mesh:
        for q in p[:468]:
            cv2.circle(canvas, (int(q[0]), int(q[1])), max(1, round(u)), (255, 255, 0), -1)  # cyan: mesh
    for q in f[4:14].reshape(5, 2):
        cv2.circle(canvas, (int(q[0]), int(q[1])), dot + 2, (0, 255, 255), line)  # yellow rings: YuNet's 5 pts
    for idx in (IRIS_L, IRIS_R):
        c = p[idx[0]]
        r = max(m["iris_d"] / 2, 2)
        cv2.circle(canvas, (int(c[0]), int(c[1])), int(r), (0, 255, 0), line)  # green: iris circle
        for j in idx[1:]:
            cv2.circle(canvas, (int(p[j][0]), int(p[j][1])), max(1, dot - 1), (0, 255, 0), -1)
        cv2.circle(canvas, (int(c[0]), int(c[1])), dot, (255, 0, 255), -1)  # magenta: iris centre
    a, b = p[IRIS_L[0], :2], p[IRIS_R[0], :2]
    cv2.line(canvas, tuple(int(v) for v in a), tuple(int(v) for v in b), (255, 0, 255), line)
    mid = ((a + b) / 2).astype(int)
    label = f"IPD3D {m['ipd3d']:.0f}px (2D {m['ipd2d']:.0f}, yaw~{m['yaw_deg']:.0f}deg) iris {m['iris_d']:.0f}px"
    for col, t in (((0, 0, 0), th + 2), ((255, 255, 255), th)):
        cv2.putText(canvas, label, (x, max(int(30 * u), y - int(12 * u))), font, fs * 0.7, col, t, cv2.LINE_AA)
    cv2.putText(canvas, f"S={m['ipd3d']:.0f}px", (int(mid[0]) - int(40 * u), int(mid[1]) - int(25 * u)),
                font, fs * 0.8, (255, 0, 255), th, cv2.LINE_AA)


def shrink(img, max_side):
    h, w = img.shape[:2]
    k = min(1.0, max_side / max(h, w))
    return cv2.resize(img, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else img


def main():
    argv = sys.argv[1:]
    out_dir = Path("/tmp/opencode")
    if "--out" in argv:
        i = argv.index("--out")
        out_dir = Path(argv[i + 1])
        del argv[i:i + 2]
    draw, mesh = "--draw" in argv, "--mesh" in argv
    paths = [a for a in argv if not a.startswith("--")]
    for path in paths:
        img = cv2.imread(path)
        if img is None:
            print(f"{path}: cannot read")
            continue
        h, w = img.shape[:2]
        faces = detect(img)
        print(f"{Path(path).name}: {w}x{h}, {len(faces)} face(s)")
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        canvas = img.copy()
        crops = []
        for i, f in enumerate(faces):
            p1, p, pres = landmarks_for(rgb, f)
            m = measures(p, f)
            m1 = measures(p1, f)
            if pres < 0.5:
                print(f"  #{i} landmark model rejected this face (presence={pres:.2f}); falling back to YuNet: "
                      f"S={m['yunet_ipd']:.0f}px (YuNet eye distance), not drawn")
                continue
            shift = max(np.linalg.norm(p[IRIS_L[0], :2] - p1[IRIS_L[0], :2]), np.linalg.norm(p[IRIS_R[0], :2] - p1[IRIS_R[0], :2]))
            print(f"  #{i} yunet={f[14]:.2f} presence={pres:.2f} S(ipd3d)={m['ipd3d']:.0f}px "
                  f"ipd2d={m['ipd2d']:.0f} yaw~{m['yaw_deg']:.0f}deg iris={m['iris_d']:.0f}px")
            print(f"     mm/px: from ipd {m['mm_per_px_ipd']:.4f} vs from iris {m['mm_per_px_iris']:.4f} "
                  f"({100 * (m['mm_per_px_iris'] / m['mm_per_px_ipd'] - 1):+.0f}%)  "
                  f"yunet ipd2d={m['yunet_ipd']:.0f}px  pass1->2 ipd3d {m1['ipd3d']:.0f}->{m['ipd3d']:.0f}, iris centre moved {shift:.1f}px")
            if draw:
                draw_face(canvas, f, p, m, mesh)
                crops.append((i, f))
        if draw:
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = Path(path).stem
            cv2.imwrite(str(out_dir / f"{stem}_overlay.jpg"), shrink(canvas, 2000), [cv2.IMWRITE_JPEG_QUALITY, 90])
            print(f"  wrote {out_dir / f'{stem}_overlay.jpg'}")
            for i, f in crops:
                x, y, bw, bh = f[:4]
                cx, cy, half = x + bw / 2, y + bh / 2, max(bw, bh) * 0.9
                x0, y0 = max(0, int(cx - half)), max(0, int(cy - half))
                x1, y1 = min(w, int(cx + half)), min(h, int(cy + half))
                cv2.imwrite(str(out_dir / f"{stem}_face{i}.jpg"), shrink(canvas[y0:y1, x0:x1], 1400), [cv2.IMWRITE_JPEG_QUALITY, 92])
                print(f"  wrote {out_dir / f'{stem}_face{i}.jpg'}")


if __name__ == "__main__":
    main()
