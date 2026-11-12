"""Pose backend settings and dispatch for the optional experimental adapters."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
from .paths import MODELS

MODEL_SHA256 = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"
COCO = (0, 2, 5, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)
DEFAULTS = {"backend": "rtmlib", "python": "", "model": ""}
GOOGLE = "google-full"
OPENCV = "opencv-zoo"
OPENCV_MODEL_DIR = MODELS / OPENCV
SCORED_BACKENDS = (GOOGLE, OPENCV)
MAX_RESULT = 65536


def settings(value=None):
    if value is None:
        return dict(DEFAULTS)
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Pose settings must contain backend, python and model only")
    result = {**DEFAULTS, **value}
    if result["backend"] not in ("rtmlib", GOOGLE, OPENCV):
        raise ValueError("Unknown pose backend")
    for key in ("python", "model"):
        if not isinstance(result[key], str) or "\0" in result[key]:
            raise ValueError(f"Pose {key} must be a path string")
    if result["backend"] == OPENCV and not result["model"]:
        result["model"] = str(OPENCV_MODEL_DIR)
    return result


def prerequisites(config):
    config = settings(config)
    if config["backend"] == OPENCV:
        from . import opencv_zoo_pose
        return opencv_zoo_pose.prerequisites(config["model"])
    if not shutil.which("bwrap"):
        raise ValueError("Experimental Google pose requires bubblewrap and unprivileged user/network namespaces")
    python = Path(config["python"]).expanduser()
    model = Path(config["model"]).expanduser()
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Google pose needs an absolute executable path to a separate Python virtualenv")
    runtime = python.parent.parent.resolve()
    if not (runtime / "pyvenv.cfg").is_file() or python.parent.name != "bin":
        raise ValueError("Google pose Python must belong to a separate virtualenv (bin/python)")
    python = runtime / "bin" / python.name
    # Mount only the selected environment and its interpreter installation, never a normal home.
    fields = dict((key.strip(), value.strip()) for key, value in
                  (line.split("=", 1) for line in (runtime / "pyvenv.cfg").read_text().splitlines() if "=" in line))
    base = Path(fields.get("home", "")).parent
    if not base.is_absolute() or base.resolve() != python.resolve().parent.parent:
        raise ValueError("Google pose virtualenv interpreter does not match pyvenv.cfg home")
    if not (base / "bin").is_dir() or runtime in (Path("/"), Path.home()) or base in (Path("/"), Path.home()):
        raise ValueError("Google pose runtime must have a dedicated installation directory")
    if not model.is_absolute() or not model.is_file():
        raise ValueError("Google pose needs the manually provisioned Full float16 v1 .task model")
    if model.stat().st_size != 9398198:
        raise ValueError("Google Full model size mismatch; expected the official float16 v1 artifact")
    with model.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != MODEL_SHA256:
            raise ValueError("Google Full model SHA-256 mismatch")
    return python.absolute(), runtime, base, model.resolve()


def command(python, runtime, base, model, result):
    args = [shutil.which("bwrap"), "--unshare-user", "--unshare-net", "--unshare-pid",
            "--new-session", "--die-with-parent", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr"]
    for path in ("/lib", "/lib64", "/sys/devices/system/cpu", "/etc/fonts"):
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    for path in dict.fromkeys((runtime, base)):
        if not path.is_relative_to("/usr"):
            args += ["--ro-bind", str(path), str(path)]
    args += ["--ro-bind", str(model), "/model.task",
             "--ro-bind", str(Path(__file__).with_name("google_pose_worker.py")), "/worker.py",
             "--bind", str(result), "/result.json", "--proc", "/proc", "--dev", "/dev",
             "--tmpfs", "/tmp", "--tmpfs", "/run", "--clearenv",
             "--setenv", "HOME", "/tmp", "--setenv", "XDG_CACHE_HOME", "/tmp/cache",
             "--setenv", "MPLCONFIGDIR", "/tmp/matplotlib",
             "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
             "--setenv", "OMP_NUM_THREADS", "1", "--setenv", "OPENBLAS_NUM_THREADS", "1",
             "--chdir", "/tmp", "--", str(python), "-I", "-B", "/worker.py"]
    return args


def map_poses(value, width, height):
    """Visibility AND presence >= .5; absent/out-of-frame joints have no usable geometry."""
    if not isinstance(value, dict) or set(value) != {"poses"} or not isinstance(value["poses"], list):
        raise ValueError("Invalid Google pose response")
    if len(value["poses"]) > 2:
        raise ValueError("Google pose response exceeds the two-person limit")
    points, scores = [], []
    for pose in value["poses"]:
        data = np.asarray(pose, dtype=np.float64)
        if data.shape != (33, 4) or not np.isfinite(data).all():
            raise ValueError("Invalid Google landmark shape or non-finite value")
        if np.any((data[:, 2:] < 0) | (data[:, 2:] > 1)):
            raise ValueError("Invalid Google landmark confidence")
        data = data[list(COCO)]
        valid = ((data[:, 2:] >= .5).all(axis=1)
                 & (data[:, :2] >= 0).all(axis=1) & (data[:, :2] < 1).all(axis=1))
        kp = data[:, :2] * [width, height]
        kp[~valid] = np.nan
        sc = np.where(valid, data[:, 2:].min(axis=1), 0)
        if valid.any():
            points.append(kp)
            scores.append(sc)
    return (np.asarray(points, dtype=np.float32).reshape(-1, 17, 2),
            np.asarray(scores, dtype=np.float32).reshape(-1, 17))


def detect(image, config):
    config = settings(config)
    if config["backend"] == OPENCV:
        from . import opencv_zoo_pose
        return opencv_zoo_pose.detect(image, config["model"])
    python, runtime, base, model = prerequisites(config)
    h, w = image.shape[:2]
    if image.dtype != np.uint8 or image.shape != (h, w, 3) or not (1 <= h <= 1600 and 1 <= w <= 1600):
        raise ValueError("Google pose expects RGB8 image with maximum side 1600")
    header = json.dumps({"height": h, "width": w}).encode() + b"\n"
    payload = header + np.ascontiguousarray(image[:, :, ::-1]).tobytes()
    with tempfile.TemporaryDirectory(prefix="shp-google-pose-") as directory:
        snapshot = Path(directory) / "model.task"
        with model.open("rb") as stream:
            model_bytes = stream.read(9398199)
        if hashlib.sha256(model_bytes).hexdigest() != MODEL_SHA256:
            raise ValueError("Google model changed during preparation; SHA-256 mismatch")
        snapshot.write_bytes(model_bytes)
        del model_bytes
        result = Path(directory) / "result.json"
        result.touch(mode=0o600)
        # No inherited sockets/FDs, environment or writable access to app/photos/config.
        with tempfile.TemporaryFile() as logs:
            try:
                process = subprocess.run(command(python, runtime, base, snapshot, result),
                                         input=payload, stdout=logs, stderr=logs,
                                         timeout=90, close_fds=True, env={"PATH": os.defpath})
            except subprocess.TimeoutExpired as error:
                raise ValueError("Isolated Google pose timed out after 90 seconds") from error
            if process.returncode:
                logs.seek(0)
                detail = logs.read(4096).decode("utf-8", errors="replace")
                raise ValueError(f"Isolated Google pose failed (runtime/namespaces required): {detail}")
        if result.stat().st_size > MAX_RESULT:
            raise ValueError("Google pose response exceeds size limit")
        return map_poses(json.loads(result.read_text()), w, h)
