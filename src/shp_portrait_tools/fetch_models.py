"""Download the Skin Tools models: the three ONNX files (pinned upstream URLs, SHA-256 checked) and the rtmlib
pose weights (rtmlib's own cache). Run by the installers; safe to run again (verified files are kept).

    python -m shp_portrait_tools.fetch_models [--dest DIR] [--skip-pose]

The model files are not redistributed by this project; each comes from the source listed in MODELS.md.
"""

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from .shared.platform_paths import data_root, env, module_folder

# File name -> (pinned upstream URL, SHA-256). Keep in sync with MODELS.md.
MODELS = {
    "yunet.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/"
        "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_landmarks.onnx": (
        "https://huggingface.co/naklitechie/face-landmarks-onnx/resolve/575c33816c840c5b156398adb485e4f8d138adc2/"
        "face_landmarks.onnx",
        "f38c3321ceffbc9e95103480ad38cc3f52e7e1bde2bcee7cd9355d0b9138ac0c"),
    "selfie_multiclass_256x256.onnx": (
        "https://huggingface.co/FreeHugsForRobots/ps-skin-seg/resolve/f3a72025451688fb893083991fdfd4562814a4dd/"
        "selfie_multiclass_256x256.onnx",
        "13961350bb2099688eb96182ffe26f24eb12c77b1df278a2ec95bbdc923136e1"),
}


def default_dest():
    return Path(env("SKIN_MODELS", "DNB_MODELS") or module_folder(data_root(), "skin") / "models")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, target, expected, opener=urlopen):
    """Fetch `url` to a temporary file beside `target`, check its SHA-256, then move it into place."""
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, opener(Request(url, headers={"User-Agent": "shp-portrait-tools-installer"}),
                                                timeout=60) as response:
            while block := response.read(1 << 20):
                out.write(block)
        actual = sha256(temporary)
        if actual != expected:
            raise ValueError(f"{target.name}: SHA-256 {actual} does not match the expected {expected}")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fetch_onnx(dest, opener=urlopen, log=print):
    dest.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in MODELS.items():
        target = dest / name
        if target.is_file() and sha256(target) == expected:
            log(f"{name}: present and verified")
            continue
        log(f"{name}: downloading")
        download(url, target, expected, opener)
        log(f"{name}: verified")


def fetch_pose(log=print):
    """Prefetch rtmlib's balanced Wholebody detector and pose weights into its cache (no inference session)."""
    from rtmlib import Wholebody
    from rtmlib.tools.file import download_checkpoint
    for role in ("det", "pose"):
        log(f"pose {role}: {download_checkpoint(Wholebody.MODE['balanced'][role])}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download and verify the Skin Tools models")
    parser.add_argument("--dest", type=Path, default=None, help="model folder (default: the Skin Tools models folder)")
    parser.add_argument("--skip-pose", action="store_true", help="do not prefetch the rtmlib pose weights")
    args = parser.parse_args(argv)
    dest = (args.dest or default_dest()).expanduser().resolve()
    try:
        fetch_onnx(dest)
        if not args.skip_pose:
            fetch_pose()
    except (OSError, ValueError) as error:
        print(f"Model download failed: {error}", file=sys.stderr)
        return 1
    print(f"Models ready in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
