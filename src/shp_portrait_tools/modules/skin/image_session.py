"""Source publication and generation-owned snapshots for Portrait Tools.

Callers hold the existing foreground lock for publication and snapshot capture.
Background callers use their scheduler's commit guard when checking a snapshot.
"""

_SOURCE_REFERENCES = ("img", "source_img", "masks")
_SNAPSHOT_FIELDS = (*_SOURCE_REFERENCES, "path", "landmark_generation", "icc", "source_icc",
                    "source_kind", "source_opaque", "skintone_error")


def new_session():
    return dict(img=None, path=None, S=None, centre=None, imgs={}, version=0,
                region_key=None, region=None, masks=None, icc=None, source_img=None,
                source_icc=None, source_opaque=True, skintone_error=None, seen=None,
                pbox=None, last=None, struct=None, avers=0, region_off=(0, 0),
                region_box=None, subject=None, source_kind=None)


def publish_source(session, prepared, *, kind="local"):
    """Replace source-owned state without discarding session-wide counters."""
    generation = session.get("landmark_generation", 0) + 1
    session.update(prepared, source_kind=kind, landmark_generation=generation,
                   imgs={}, region_key=None, region=None, last=None, struct=None, avers=0,
                   preview_cache={}, full_mask_cache=None)
    return generation


def full_resolution_snapshot(session, *, depth, colour_mode):
    # Load/Get replaces the arrays and mask mapping rather than editing them.
    return {**{key: session.get(key) for key in _SNAPSHOT_FIELDS},
            "depth": depth, "colour_mode": colour_mode}


def matches_source(session, snapshot):
    return (session.get("landmark_generation") == snapshot["landmark_generation"]
            and all(session.get(key) is snapshot[key] for key in _SOURCE_REFERENCES))
