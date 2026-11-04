"""Read the camera's autofocus point from Sony maker notes using an optional external ExifTool.

Returns the AF point in image pixels, or None if the file has no usable value."""
import os
import shutil
import subprocess
from pathlib import Path

def af_point(path, width, height):
    configured = os.environ.get("DNB_EXIFTOOL")
    executable = configured or shutil.which("exiftool")
    if not executable:
        return None
    tool = Path(executable).expanduser()
    if configured and not tool.is_file():
        raise FileNotFoundError(f"DNB_EXIFTOOL does not exist: {tool}")
    try:
        out = subprocess.run(["perl", str(tool), "-s3", "-Sony:FocusLocation", str(path)],
                             capture_output=True, text=True, timeout=30).stdout.split()
        sw, sh, x, y = (float(v) for v in out[:4])
    except Exception:
        return None
    if sw <= 0 or sh <= 0 or (x == 0 and y == 0):
        return None
    return x * width / sw, y * height / sh
