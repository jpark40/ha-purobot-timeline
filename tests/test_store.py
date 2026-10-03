"""Run: python3 -m unittest discover -s tests -v (Pillow required)."""
from datetime import datetime, timedelta, timezone
import importlib.util
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image

spec = importlib.util.spec_from_file_location(
    "purobot_store",
    Path(__file__).resolve().parents[1] / "custom_components/purobot_timeline/store.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def image_bytes(color="navy", fmt="JPEG"):
    output = BytesIO()
    Image.new("RGB", (640, 360), color).save(output, format=fmt)
    return output.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = mod.EventStore(self.tmp.name)
        self.store.initialize()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def save(self, category="visits", entity="image.purobot_visit", stamp=None,
             content=None, content_type="image/jpeg"):
        return self.store.ingest(
            entity, category, (stamp or self.now).isoformat(),
            content or image_bytes(), content_type, "America/Los_Angeles",
        )

    def test_two_tabs_and_dedupe_same_update(self):
        self.assertEqual(self.save(), "saved")
        self.assertEqual(self.save(), "duplicate")
        self.assertEqual(self.save(
            "toilet_used", "image.purobot_toilet", self.now + timedelta(seconds=1), image_bytes("green")
        ), "saved")
        visits = self.store.list_events("visits")
        toilet = self.store.list_events("toilet_used")
        self.assertEqual(visits["counts"], {"visits": 1, "toilet_used": 1})
        self.assertEqual(visits["images"][0]["camera_name"], "Visits")
        self.assertEqual(toilet["images"][0]["camera_name"], "Toilet Used")
        self.assertEqual(visits["total_count"], 2)

    def test_identical_picture_on_new_update_is_saved(self):
        self.assertEqual(self.save(), "saved")
        self.assertEqual(self.save(stamp=self.now + timedelta(seconds=1)), "saved")
        self.assertEqual(self.store.list_events("visits")["total_count"], 2)

    def test_png_is_archived_as_jpeg_with_thumbnail(self):
        self.save(content=image_bytes("purple", "PNG"), content_type="image/png")
        item = self.store.list_events("visits")["images"][0]
        with Image.open(self.store.image_path(item["id"])) as full:
            self.assertEqual(full.format, "JPEG")
        with Image.open(self.store.image_path(item["id"], True)) as thumb:
            self.assertLessEqual(thumb.width, 400)

    def test_timezone_date_and_pagination(self):
        base = self.now - timedelta(hours=1)
        local = base.astimezone(ZoneInfo("America/Los_Angeles"))
        for i in range(5):
            self.save(stamp=base + timedelta(seconds=i), content=image_bytes((i * 30, 0, 80)))
        first = self.store.list_events("visits", day=local.date().isoformat(), limit=2)
        second = self.store.list_events(
            "visits", day=local.date().isoformat(), before=first["next_before"], limit=2
        )
        self.assertEqual(
            first["images"][-1]["time"], (local + timedelta(seconds=4)).strftime("%H:%M:%S")
        )
        self.assertEqual(len(set(x["id"] for x in first["images"] + second["images"])), 4)

    def test_bad_input_and_path_traversal_are_rejected(self):
        for args in [
            ("other", "image.purobot", self.now.isoformat(), image_bytes(), "image/jpeg"),
            ("visits", "sensor.not_image", self.now.isoformat(), image_bytes(), "image/jpeg"),
            ("visits", "image.purobot", "bad", image_bytes(), "image/jpeg"),
            ("visits", "image.purobot", self.now.isoformat(), b"bad", "image/jpeg"),
        ]:
            with self.subTest(args=args[:3]), self.assertRaises(mod.RejectedImage):
                self.store.ingest(*args, "UTC")
        self.assertIsNone(self.store.image_path("../../etc/passwd"))
        with self.assertRaises(ValueError):
            self.store.list_events("visits; DROP TABLE events")

    def test_retention_cleanup_preserves_unrelated_files(self):
        unrelated = Path(self.tmp.name) / "images" / "family.jpg"
        unrelated.write_bytes(b"keep")
        self.save()
        with patch.object(mod.time, "time", return_value=mod.time.time() + 31 * 86400):
            self.assertEqual(self.store.cleanup(), 1)
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
