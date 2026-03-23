import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import make_slideshow2 as ms


class SlideshowSortingTests(unittest.TestCase):
    def _info(self, name: str, dt=None, gps=None):
        return ms.PhotoInfo(
            path=Path(name),
            width=1200,
            height=800,
            aspect_ratio=1.5,
            is_landscape=True,
            orientation="",
            datetime_taken=dt,
            gps_coords=gps,
        )

    def test_time_sort_keeps_all_images_and_orders_dated_first(self):
        images = [Path("a.png"), Path("b.png"), Path("c.png"), Path("d.png")]
        infos = [
            self._info("a.png", dt=datetime(2024, 1, 3, 10, 0, 0)),
            None,
            self._info("c.png", dt=datetime(2024, 1, 1, 10, 0, 0)),
            self._info("d.png", dt=None),
        ]

        out = ms.sort_photos(images, infos, sort_by="time")

        self.assertEqual(len(out), len(images))
        self.assertEqual(out, [Path("c.png"), Path("a.png"), Path("b.png"), Path("d.png")])

    def test_location_sort_keeps_all_images_and_orders_geotagged_first(self):
        images = [Path("a.png"), Path("b.png"), Path("c.png"), Path("d.png")]
        infos = [
            self._info("a.png", gps=(37.0, -122.0)),
            None,
            self._info("c.png", gps=(35.0, -120.0)),
            self._info("d.png", gps=None),
        ]

        out = ms.sort_photos(images, infos, sort_by="location")

        self.assertEqual(len(out), len(images))
        self.assertEqual(out, [Path("c.png"), Path("a.png"), Path("b.png"), Path("d.png")])

    def test_random_sort_does_not_mutate_input_list(self):
        images = [Path("1.png"), Path("2.png"), Path("3.png"), Path("4.png")]
        before = images.copy()

        _ = ms.sort_photos(images, [None] * len(images), sort_by="random", seed=42)

        self.assertEqual(images, before)

    def test_orientation_stats_handles_all_missing_metadata(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ms.print_orientation_stats([None, None], show_camera_sources=True)
        text = buf.getvalue()
        self.assertIn("Metadata unavailable", text)


if __name__ == "__main__":
    unittest.main()
