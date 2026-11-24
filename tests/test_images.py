"""Exact output geometry, EXIF, alpha handling and explicit fallback behavior."""

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from backend.app.services.images import crop_image, placeholder_image, prepare_image, resize_image
from backend.app.services.style_lock import DEFAULT_STYLE_LOCK, ILLUSTRATION_RULES, StyleLock, build_prompt


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.source = Image.new("RGB", (400, 200), "red")

    def test_resize_exact_size_padding_and_no_input_mutation(self):
        output = resize_image(self.source, (300, 300), background="#123456")
        self.assertEqual(output.size, (300, 300))
        self.assertEqual(output.getpixel((150, 0)), (18, 52, 86))
        self.assertEqual(output.getpixel((150, 150)), (255, 0, 0))
        self.assertEqual(self.source.size, (400, 200))

    def test_crop_exact_size_without_letterbox(self):
        output = crop_image(self.source, (300, 300))
        self.assertEqual(output.size, (300, 300))
        self.assertEqual(output.getextrema(), ((255, 255), (0, 0), (0, 0)))

    def test_crop_anchor_and_explicit_region(self):
        source = Image.new("RGB", (400, 200), "red")
        source.paste("blue", (200, 0, 400, 200))
        left = crop_image(source, (100, 100), centering=(0, 0.5))
        right = crop_image(source, (100, 100), centering=(1, 0.5))
        region = crop_image(source, (75, 50), box=(250, 0, 400, 200))
        self.assertEqual(left.getpixel((50, 50)), (255, 0, 0))
        self.assertEqual(right.getpixel((50, 50)), (0, 0, 255))
        self.assertEqual(region.size, (75, 50))
        self.assertEqual(region.getpixel((20, 20)), (0, 0, 255))

    def test_placeholder_is_solid_requested_color(self):
        output = placeholder_image((640, 360), "#F3EBDD")
        self.assertEqual(output.size, (640, 360))
        self.assertEqual(output.mode, "RGB")
        self.assertEqual(output.getextrema(), ((243, 243), (235, 235), (221, 221)))

    def test_missing_and_corrupt_sources_explicitly_mark_placeholder(self):
        for source in (None, b"not an image", Image.new("RGB", (0, 0))):
            with self.subTest(source=source):
                result = prepare_image(source, (320, 180))
                self.assertEqual(result.image.size, (320, 180))
                self.assertTrue(result.is_placeholder)
                self.assertIsNotNone(result.warning)
        with self.assertRaises(ValueError):
            prepare_image(None, (100, 100), fallback=False)

    def test_all_modes_and_tiny_extreme_aspect_ratios(self):
        extreme = Image.new("RGB", (1000, 1), "blue")
        for source in (self.source, extreme):
            for mode in ("contain", "cover", "stretch"):
                with self.subTest(mode=mode, source_size=source.size):
                    result = prepare_image(source, (1, 5), mode=mode)
                    self.assertEqual(result.image.size, (1, 5))
                    self.assertFalse(result.is_placeholder)

    def test_alpha_is_composited_on_requested_background(self):
        source = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
        result = crop_image(source, (30, 20), background="blue")
        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.getpixel((15, 10)), (0, 0, 255))
        self.assertEqual(source.mode, "RGBA")

    def test_exif_orientation_is_applied_before_crop(self):
        source = Image.new("RGB", (20, 10), "red")
        source.getexif()[274] = 6  # Rotate 90 degrees clockwise: oriented size is 10x20.
        result = crop_image(source, (100, 200), box=(0, 0, 10, 20))
        self.assertEqual(result.size, (100, 200))
        self.assertEqual(source.size, (20, 10))
        self.assertEqual(source.getexif()[274], 6)

    def test_path_bytes_and_detached_output_can_be_saved(self):
        stream = BytesIO()
        self.source.save(stream, format="PNG")
        self.assertEqual(resize_image(stream.getvalue(), (123, 77)).size, (123, 77))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            self.source.save(path)
            result = crop_image(path, (123, 77))
            renamed = path.with_name("renamed.png")
            path.rename(renamed)  # No lingering file handle on Windows.
            result.save(path)
            with Image.open(path) as saved:
                self.assertEqual(saved.size, (123, 77))

    def test_invalid_parameters_do_not_silently_fallback(self):
        for size in ((0, 10), (-1, 10), (True, 10), (10.5, 10), (10000, 10000)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                prepare_image(None, size)
        with self.assertRaises(ValueError):
            prepare_image(None, (100, 100), mode="invalid")
        with self.assertRaises(ValueError):
            prepare_image(None, (100, 100), background="invalid-color")
        with self.assertRaises(ValueError):
            crop_image(self.source, (100, 100), box=(-1, 0, 200, 100))
        with self.assertRaises(ValueError):
            crop_image(self.source, (100, 100), centering=(float("nan"), 0))


class StyleLockTests(unittest.TestCase):
    def test_same_style_rules_and_signature_across_prompts(self):
        lock = StyleLock()
        for text in ("Cover artwork", "Day 1 artwork"):
            prompt = build_prompt(text, lock)
            self.assertIn(DEFAULT_STYLE_LOCK, prompt)
            self.assertIn(ILLUSTRATION_RULES, prompt)
            self.assertEqual(lock.apply(prompt), prompt)
        self.assertEqual(lock.signature, StyleLock().signature)
        self.assertEqual(len(lock.signature), 64)
        with self.assertRaises(FrozenInstanceError):
            lock.description = "changed"

    def test_settings_override_changes_style_but_keeps_rules(self):
        lock = StyleLock.from_settings(SimpleNamespace(image_style_lock="  Flat vector travel art  "))
        self.assertEqual(lock.description, "Flat vector travel art")
        self.assertIn(ILLUSTRATION_RULES, lock.apply("Cover"))
        self.assertNotEqual(lock.signature, StyleLock().signature)
        default = StyleLock.from_settings(SimpleNamespace(image_style_lock=""))
        self.assertEqual(default.description, DEFAULT_STYLE_LOCK)

    def test_blank_style_and_prompt_are_rejected(self):
        with self.assertRaises(ValueError):
            StyleLock("   ")
        with self.assertRaises(ValueError):
            StyleLock().apply("   ")


if __name__ == "__main__":
    unittest.main()
