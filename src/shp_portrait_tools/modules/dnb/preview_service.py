"""Foreground preview requests and their background preparation lifecycle."""

from . import progress

STOPPED = "Preparation stopped by another action; run a preview to prepare again."


def run(req, *, state, lock, render, parse_params, matches_preparation, layer_precache,
        layer_active, select_mask, queue_mask, queue_layers, attach_status):
    keep_preparation = matches_preparation(req)
    if not keep_preparation:
        layer_precache.reset(STOPPED)
    token = req.pop("_progress", None)
    with progress.operation(token), lock():
        if keep_preparation:
            keep_preparation = matches_preparation(req)
        if not keep_preparation:
            # Another preview may have queued work while this request waited.
            with layer_precache.condition:
                if layer_precache.signature is not None:
                    layer_precache.reset(STOPPED)
        if state["img"] is None:
            return render(req)

        precache_mask = req.get("precache_mask", True)
        precache_layers = req.get("precache_layers", False)
        stamp = req.get("layer_cache_revision")
        if type(precache_mask) is not bool:
            raise ValueError("precache_mask must be a boolean")
        if type(precache_layers) is not bool:
            raise ValueError("precache_layers must be a boolean")
        if stamp is not None and (not isinstance(stamp, str) or not 1 <= len(stamp) <= 128):
            raise ValueError("layer_cache_revision must be a short string")
        state["mask_precache_enabled"] = precache_mask
        params = parse_params(req)
        select_mask(params, enabled=precache_mask)

        result = render(req)
        if precache_mask:
            queue_mask(params)
        if precache_layers and not keep_preparation:
            queue_layers(params, stamp, req)
        elif not precache_layers:
            with layer_precache.condition:
                layer_active.update(mode=params["mode"], stamp=stamp,
                                    generation=state.get("landmark_generation"), tabs={}, budget_note=None)
            layer_precache.skip(None, "Background layer preparation is disabled in Settings & Info.")
        attach_status(result)
        return result
