"""One IMAGE inference, invoked only inside the parent's network/filesystem namespace."""
import json
import resource
import socket
import sys
from importlib.metadata import version


def main():
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError("The isolated Google pose runtime requires Python 3.13")
    if socket.if_nameindex() != [(1, "lo")]:
        raise RuntimeError("Google pose worker requires an isolated network namespace")
    probe = socket.socket()
    try:
        if probe.connect_ex(("192.0.2.1", 443)) != 101:
            raise RuntimeError("Google pose worker has an external route")
    finally:
        probe.close()
    if version("mediapipe") != "1.2.2":
        raise RuntimeError("Google pose requires mediapipe==1.2.2 in its separate runtime")
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    import numpy as np
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import PoseLandmarker, PoseLandmarkerOptions, RunningMode

    header = json.loads(sys.stdin.buffer.readline(256))
    h, w = header["height"], header["width"]
    if type(h) is not int or type(w) is not int or not (1 <= h <= 1600 and 1 <= w <= 1600):
        raise ValueError("Invalid pose image dimensions")
    raw = sys.stdin.buffer.read(h * w * 3 + 1)
    if len(raw) != h * w * 3:
        raise ValueError("Invalid pose image payload")
    image = mp.Image(image_format=mp.ImageFormat.SRGB,
                     data=np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3).copy())
    options = PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path="/model.task", delegate=BaseOptions.Delegate.CPU),
        running_mode=RunningMode.IMAGE, num_poses=2, output_segmentation_masks=False)
    with PoseLandmarker.create_from_options(options) as detector:
        result = detector.detect(image)
        poses = [[[p.x, p.y, p.visibility, p.presence] for p in pose]
                 for pose in result.pose_landmarks]
    with open("/result.json", "w") as stream:
        json.dump({"poses": poses}, stream, allow_nan=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Google pose worker failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
