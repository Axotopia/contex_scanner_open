"""Tests for CIS channel registration (red/green/blue sensor line offset).

The scanner's three colour channels are captured on separate sensor lines, so
they arrive vertically displaced.  ``ctx.registration`` measures that shift
from horizontal edge detail and realigns the channels before calibration.

These tests use synthetic data with a *known* imposed offset, because the
synthetic demo raster is perfectly aligned (0 px) and therefore cannot exercise
the correction -- only real hardware or an imposed shift can.
"""
from __future__ import annotations

import unittest

import numpy as np

from ctx.registration import (
    MAX_LAG_PX,
    MIN_QUALITY,
    apply_offsets,
    estimate_offsets,
    register_cameras,
)


def _edge_image(rows: int = 600, cols: int = 400, seed: int = 7) -> np.ndarray:
    """Aperiodic blocks of random height: strong, non-repeating horizontal edges.

    Deliberately not a sine/square wave -- periodic content correlates with
    itself at multiples of its period and would make the estimator's job
    artificially ambiguous.
    """
    rng = np.random.default_rng(seed)
    levels = (9000.0, 26000.0, 48000.0)
    vals: list[float] = []
    while len(vals) < rows:
        vals.extend([float(rng.choice(levels))] * int(rng.integers(4, 37)))
    wave = np.asarray(vals[:rows], dtype=np.float32)[:, None]
    base = np.repeat(wave, cols, axis=1)
    base = base + rng.normal(0, 120.0, size=base.shape).astype(np.float32)
    img = np.repeat(base[:, :, None], 3, axis=2)
    return np.clip(img, 0, 65535).astype(np.uint16)


def _impose_offset(img: np.ndarray, dy_red: float, dy_green: float) -> np.ndarray:
    """Make red/green content sit ``dy`` rows *below* blue."""
    return apply_offsets(img, -dy_red, -dy_green)


class TestEstimateOffsets(unittest.TestCase):
    def test_aligned_image_reports_zero(self):
        img = _edge_image()
        est = estimate_offsets(img)
        self.assertAlmostEqual(est["red"][0], 0.0, delta=0.25)
        self.assertAlmostEqual(est["green"][0], 0.0, delta=0.25)
        self.assertGreater(est["red"][1], 0.5)

    def test_recovers_known_integer_offset(self):
        img = _impose_offset(_edge_image(), 6.0, 3.0)
        est = estimate_offsets(img)
        self.assertAlmostEqual(est["red"][0], 6.0, delta=0.4)
        self.assertAlmostEqual(est["green"][0], 3.0, delta=0.4)

    def test_recovers_known_subpixel_offset(self):
        img = _impose_offset(_edge_image(seed=11), 4.4, 2.3)
        est = estimate_offsets(img)
        self.assertAlmostEqual(est["red"][0], 4.4, delta=0.4)
        self.assertAlmostEqual(est["green"][0], 2.3, delta=0.4)

    def test_recovers_negative_offset(self):
        img = _impose_offset(_edge_image(seed=5), -2.5, -1.0)
        est = estimate_offsets(img)
        self.assertAlmostEqual(est["red"][0], -2.5, delta=0.4)
        self.assertAlmostEqual(est["green"][0], -1.0, delta=0.4)

    def test_recovers_high_dpi_offset(self):
        """1200 dpi shifts are ~4x the 300 dpi value; the window must clear them."""
        img = _impose_offset(_edge_image(rows=1600, seed=13), 24.0, 12.0)
        est = estimate_offsets(img)
        self.assertAlmostEqual(est["red"][0], 24.0, delta=0.5)
        self.assertAlmostEqual(est["green"][0], 12.0, delta=0.5)

    def test_rejects_non_image_shape(self):
        with self.assertRaises(ValueError):
            estimate_offsets(np.zeros((10, 10), dtype=np.uint16))


class TestApplyOffsets(unittest.TestCase):
    def test_moves_content_back_up(self):
        img = _edge_image()
        shifted = _impose_offset(img, 5.0, 0.0)
        restored = apply_offsets(shifted, 5.0, 0.0)
        # interior rows only: edges are clamped
        interior = slice(20, -20)
        np.testing.assert_allclose(
            restored[interior, :, 0].astype(np.int32),
            img[interior, :, 0].astype(np.int32),
            atol=3000,
        )

    def test_blue_is_never_touched(self):
        img = _edge_image()
        out = apply_offsets(img, 3.0, 2.0)
        np.testing.assert_array_equal(out[:, :, 2], img[:, :, 2])

    def test_zero_shift_is_identity(self):
        img = _edge_image()
        out = apply_offsets(img, 0.0, 0.0)
        np.testing.assert_array_equal(out, img)


class TestRegisterCameras(unittest.TestCase):
    def test_consensus_offset_is_applied(self):
        base = _edge_image()
        cams = [(f"cam{i}", _impose_offset(base, 6.0, 3.0)) for i in range(1, 6)]
        out, info = register_cameras(cams)
        self.assertTrue(info["applied"])
        self.assertAlmostEqual(info["dy_red_px"], 6.0, delta=0.4)
        self.assertAlmostEqual(info["dy_green_px"], 3.0, delta=0.4)
        self.assertEqual(len(out), 5)

    def test_narrow_original_uses_the_camera_that_saw_paper(self):
        """Blank cameras must not outvote the one camera covering the original.

        A letter spans cam1 only; cams 2-5 see blank backing and report a noisy
        ~0 offset.  A plain median over all five picks one of those zeros and the
        correction silently does nothing.
        """
        content = _impose_offset(_edge_image(), 6.0, 3.0)
        rng = np.random.default_rng(21)
        ramp = np.linspace(8000, 30000, 600, dtype=np.float32)[:, None]
        blanks = []
        for _ in range(4):
            b = np.repeat(ramp, 400, axis=1)[:, :, None]
            b = np.clip(b + rng.normal(0, 2500, size=(600, 400, 3)), 0, 65535)
            blanks.append(b.astype(np.uint16))
        cams = [("cam1", content)] + [(f"cam{i}", b) for i, b in zip(range(2, 6), blanks)]

        out, info = register_cameras(cams)
        self.assertEqual(info["voters"], ["cam1"])
        self.assertTrue(info["applied"])
        self.assertAlmostEqual(info["dy_red_px"], 6.0, delta=0.5)
        self.assertAlmostEqual(info["dy_green_px"], 3.0, delta=0.5)

    def test_blank_backing_cannot_outvote_even_with_high_correlation(self):
        """At 600 dpi blank backing is not featureless.

        Line interpolation leaves a 2-row periodicity that correlates strongly
        across channels at lag 0, so it looks like a confident "no offset"
        measurement.  Content energy must keep it out of the vote.
        """
        content = _impose_offset(_edge_image(), 12.0, 6.0)
        rows = 600
        period = (np.arange(rows) % 2).astype(np.float32) * 300.0
        blank = np.repeat((12000.0 + period)[:, None], 400, axis=1)[:, :, None]
        blank = np.repeat(blank, 3, axis=2).astype(np.uint16)

        cams = [("cam1", content)] + [(f"cam{i}", blank) for i in range(2, 6)]
        _out, info = register_cameras(cams)
        self.assertEqual(info["voters"], ["cam1"])
        self.assertAlmostEqual(info["dy_red_px"], 12.0, delta=0.6)
        self.assertAlmostEqual(info["dy_green_px"], 6.0, delta=0.6)

    def test_flat_image_is_not_applied(self):
        flat = np.full((400, 300, 3), 30000, dtype=np.uint16)
        rng = np.random.default_rng(3)
        flat = flat + rng.integers(0, 40, size=flat.shape, dtype=np.uint16)
        out, info = register_cameras([("cam1", flat)])
        self.assertFalse(info["applied"])
        self.assertTrue(info.get("reason"))
        self.assertLess(info["quality"], MIN_QUALITY)

    def test_disabled_returns_input_unchanged(self):
        img = _impose_offset(_edge_image(), 6.0, 3.0)
        out, info = register_cameras([("cam1", img)], enabled=False)
        self.assertFalse(info["applied"])
        self.assertIs(out[0][1], img)

    def test_offset_beyond_search_window_is_rejected(self):
        """An offset outside the window must be refused, not partially applied."""
        img = _impose_offset(_edge_image(rows=900), MAX_LAG_PX + 8.0, 0.0)
        _out, info = register_cameras([("cam1", img)])
        self.assertFalse(info["applied"])
        self.assertTrue(info.get("reason"))


if __name__ == "__main__":
    unittest.main()
