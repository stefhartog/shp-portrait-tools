import unittest

from shp_photocraft_tools.modules.dnb import image_session as session


class ImageSessionTests(unittest.TestCase):
    def test_publication_replaces_source_and_invalidates_only_source_owned_state(self):
        state = session.new_session()
        first = dict(img=object(), source_img=object(), masks={"A": object()},
                     source_icc=b"native-icc", icc=b"working-icc", path="portrait.png")
        self.assertEqual(session.publish_source(state, first), 1)
        snapshot = session.full_resolution_snapshot(state, depth=16, colour_mode="native")
        for field in first:
            self.assertIs(snapshot[field], first[field])
        self.assertEqual((snapshot["depth"], snapshot["colour_mode"]), (16, "native"))
        self.assertTrue(session.matches_source(state, snapshot))

        state.update(version=7, preview_cache={"old": object()}, full_mask_cache=object())
        second = dict(first, img=object(), source_img=object(), masks=None, source_icc=b"second-icc")
        self.assertEqual(session.publish_source(state, second, kind="photocraft"), 2)
        self.assertEqual((state["source_kind"], state["source_icc"]), ("photocraft", b"second-icc"))
        self.assertIs(state["img"], second["img"])
        self.assertIs(state["source_img"], second["source_img"])
        self.assertIsNone(state["masks"])
        self.assertIsNone(state["full_mask_cache"])
        self.assertFalse(state["preview_cache"])
        self.assertEqual(state["version"], 7)
        self.assertIs(snapshot["masks"], first["masks"])
        self.assertFalse(session.matches_source(state, snapshot))

    def test_snapshot_rejects_replaced_masks_without_generation_change(self):
        state = session.new_session()
        session.publish_source(state, {"masks": {"A": object()}})
        snapshot = session.full_resolution_snapshot(state, depth=None, colour_mode="native")
        state["masks"] = dict(snapshot["masks"])
        self.assertFalse(session.matches_source(state, snapshot))
