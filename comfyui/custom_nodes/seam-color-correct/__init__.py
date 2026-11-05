import torch
import torch.nn.functional as F

ANALYSIS_SIDE = 256
CELL = 8


def _seam_offsets(original, generated, new_area, band, max_offset, min_offset, max_spread):
    """Smooth per-channel offset field (1,3,H,W in 0..1) that makes `generated` meet
    `original` near the edge of the new area, or None when nothing needs fixing.
    Port of OpenFill's quality.seam_field for Generative Expand."""
    H, W = original.shape[1], original.shape[2]
    scale = ANALYSIS_SIDE / max(H, W)
    sh, sw = max(1, round(H * scale)), max(1, round(W * scale))

    def small(img):
        return F.interpolate(img.permute(0, 3, 1, 2), size=(sh, sw), mode="area")

    o, g = small(original[..., :3]), small(generated[..., :3])
    m = F.interpolate(new_area[:, None], size=(sh, sw), mode="area")[0, 0]
    new = m >= 0.5
    known = ~new

    reach = max(1, round(band * scale))
    grown = F.max_pool2d(new[None, None].float(), 2 * reach + 1, stride=1, padding=reach)[0, 0] > 0
    measure = known & grown
    if measure.sum() < 4:
        return None

    diff = (o - g)[0] * 255.0
    gh, gw = -(-sh // CELL), -(-sw // CELL)
    pad_h, pad_w = gh * CELL - sh, gw * CELL - sw

    def cells(t):
        t = F.pad(t, (0, pad_w, 0, pad_h))
        return t.unfold(-2, CELL, CELL).unfold(-2, CELL, CELL).reshape(*t.shape[:-2], gh, gw, CELL * CELL)

    w = cells(measure[None].float())[0]
    count = w.sum(-1)
    cd = cells(diff)
    mean = (cd * w).sum(-1) / count.clamp(min=1)
    spread = ((cd - mean[..., None]).abs() * w).sum(-1) / count.clamp(min=1)
    valid = (count >= 4) & (spread.max(0).values <= max_spread) & (mean.abs().max(0).values <= max_offset)
    if not valid.any() or mean[:, valid].abs().max() < min_offset:
        return None

    ys, xs = torch.nonzero(valid, as_tuple=True)
    ky, kx = ys.float() + 0.5, xs.float() + 0.5
    kv = mean[:, valid]
    gy, gx = torch.meshgrid(torch.arange(gh, device=original.device) + 0.5,
                            torch.arange(gw, device=original.device) + 0.5, indexing="ij")
    d2 = (gy.reshape(-1, 1) - ky) ** 2 + (gx.reshape(-1, 1) - kx) ** 2
    weights = 1.0 / d2.clamp(min=1e-6)
    field = (weights @ kv.T) / weights.sum(-1, keepdim=True)
    field = field.T.reshape(1, 3, gh, gw) / 255.0
    return F.interpolate(field, size=(H, W), mode="bilinear", align_corners=False)


class SeamColorCorrect:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "original": ("IMAGE", {"tooltip": "Padded original: real pixels in the known area."}),
            "generated": ("IMAGE", {"tooltip": "Generated result at the same size."}),
            "new_area": ("MASK", {"tooltip": "1 where the image is new (e.g. ImagePadForOutpaint mask)."}),
            "band": ("INT", {"default": 128, "min": 8, "max": 2048, "step": 8,
                             "tooltip": "How far (px) into the known area the difference is measured."}),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05}),
            "max_offset": ("INT", {"default": 24, "min": 1, "max": 96,
                                   "tooltip": "Larger differences count as content, not drift."}),
            "min_offset": ("FLOAT", {"default": 1.5, "min": 0.0, "max": 16.0, "step": 0.1}),
            "max_spread": ("INT", {"default": 10, "min": 1, "max": 64,
                                   "tooltip": "Cells whose difference varies more than this are texture."}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"
    CATEGORY = "image/color"

    def run(self, original, generated, new_area, band, strength, max_offset, min_offset, max_spread):
        if strength <= 0:
            return (generated,)
        if original.shape[1:3] != generated.shape[1:3]:
            raise ValueError("original and generated must have the same size")
        if new_area.dim() == 2:
            new_area = new_area[None]
        new_area = new_area.to(generated.device)
        if new_area.shape[1:3] != generated.shape[1:3]:
            new_area = F.interpolate(new_area[:, None], size=generated.shape[1:3], mode="bilinear")[:, 0]
        out = generated.clone()
        for b in range(generated.shape[0]):
            field = _seam_offsets(original[b:b + 1], generated[b:b + 1], new_area[b:b + 1],
                                  band, max_offset, min_offset, max_spread)
            if field is None:
                continue
            fix = field.permute(0, 2, 3, 1)[0] * strength
            out[b, ..., :3] = (generated[b, ..., :3] + fix).clamp(0, 1)
        return (out,)


NODE_CLASS_MAPPINGS = {"SeamColorCorrect": SeamColorCorrect}
NODE_DISPLAY_NAME_MAPPINGS = {"SeamColorCorrect": "Seam Color Correct (OpenFill-style)"}
