"""ICC device-link encoding of Skin Tools' numeric RGB Hue lookup table."""

import struct

import numpy as np

from . import skintone as ST


def _mluc(text):
    encoded = text.encode("utf-16-be")
    return b"mluc" + bytes(4) + struct.pack(">II", 1, 12) + b"enUS" + struct.pack(">II", len(encoded), 28) + encoded


def device_link(table):
    """Encode the RGB grid directly, with identity curves and no color conversion."""
    if (not isinstance(table, np.ndarray) or table.shape != (ST.LUT_SIZE,) * 3 + (3,)
            or not np.isfinite(table).all() or np.any(table < 0) or np.any(table > 1)):
        raise ValueError("Hue LUT must contain finite RGB samples between 0 and 1")
    # .cube has red varying fastest; ICC CLUTs have the first input varying slowest.
    clut = np.rint(table.transpose(2, 1, 0, 3) * 65535).astype(">u2").tobytes()
    matrix = struct.pack(">9i", 65536, 0, 0, 0, 65536, 0, 0, 0, 65536)
    identity = struct.pack(">6H", 0, 65535, 0, 65535, 0, 65535)
    lut = (b"mft2" + bytes(4) + bytes((3, 3, ST.LUT_SIZE, 0)) + matrix +
           struct.pack(">HH", 2, 2) + identity + clut + identity)
    description = _mluc("SHP Skin Tools Hue")
    copyright_tag = _mluc("SHP Skin Tools generated numeric transform")
    sequence = (b"pseq" + bytes(4) + struct.pack(">I", 1) + bytes(20) +
                _mluc("SHP Skin Tools") + _mluc("Numeric Hue LUT"))
    tags = [(b"desc", description), (b"cprt", copyright_tag), (b"pseq", sequence), (b"A2B0", lut)]
    offset = 128 + 4 + 12 * len(tags)
    entries, data = [], bytearray()
    for signature, content in tags:
        entries.append(struct.pack(">4sII", signature, offset + len(data), len(content)))
        data.extend(content)
        data.extend(bytes(-len(data) % 4))
    header = bytearray(128)
    struct.pack_into(">I", header, 0, offset + len(data))
    header[8:24] = b"\x04\x30\x00\x00linkRGB RGB "
    struct.pack_into(">6H", header, 24, 2026, 1, 1, 0, 0, 0)
    header[36:40] = b"acsp"
    struct.pack_into(">3i", header, 68, 0x0000F6D6, 0x00010000, 0x0000D32D)
    header[80:84] = b"SHPT"
    return bytes(header) + struct.pack(">I", len(tags)) + b"".join(entries) + data
