"""CUDA backend: availability reporting and band-size invariance.

The colour maths itself is covered by the CPU tests.  These pin the two
properties the GPU path must keep: streaming the document through VRAM in
bands cannot change the result, and the GPU result stays within rounding
distance of the CPU reference.  Everything device-specific is skipped when no
NVIDIA GPU is present, so the suite still runs on CPU-only machines.
"""
from __future__ import annotations

import unittest

import numpy as np

from ctx import gpu
from ctx.calibration import apply_it8_transform_u16
import ctx.calibration as calibration

GAMMA = 0.95
MATRIX = [[0.72, 0.31, 0.07], [0.21, 0.81, 0.05], [0.02, 0.11, 0.94]]
OFFSET = [-0.05, -0.04, -0.07]


def _image(rows: int = 300, cols: int = 96) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 65535, (rows, cols, 3), dtype=np.uint16)


class CudaInfoTest(unittest.TestCase):
    def test_info_contract(self) -> None:
        info = gpu.get_cuda_info()
        for key in ("available", "device_name", "vram_gb", "driver_version", "cuda_version", "reason"):
            self.assertIn(key, info)
        self.assertIsInstance(info["available"], bool)
        if info["available"]:
            # Version fields are rendered for humans (e.g. "12.9"), not raw ints.
            self.assertRegex(info["cuda_version"], r"^\d+\.\d+$")
            self.assertRegex(info["driver_version"], r"^\d+\.\d+$")
            self.assertGreater(info["vram_gb"], 0.0)


@unittest.skipUnless(gpu.is_cuda_available(), "no NVIDIA CUDA device available")
class CudaBandingTest(unittest.TestCase):
    def test_band_size_does_not_change_output(self) -> None:
        img = _image()
        old_budget = gpu._VRAM_BUDGET_GIB
        try:
            gpu._VRAM_BUDGET_GIB = 0.0005  # forces several 64-row bands
            banded = apply_it8_transform_u16(img, GAMMA, MATRIX, OFFSET)
            gpu._VRAM_BUDGET_GIB = 1024.0  # whole image in one band
            whole = apply_it8_transform_u16(img, GAMMA, MATRIX, OFFSET)
        finally:
            gpu._VRAM_BUDGET_GIB = old_budget
        np.testing.assert_array_equal(banded, whole)

    def test_gpu_matches_cpu_within_one_lsb(self) -> None:
        img = _image()
        gpu_out = apply_it8_transform_u16(img, GAMMA, MATRIX, OFFSET)

        original = calibration.is_cuda_available
        calibration.is_cuda_available = lambda: False
        try:
            cpu_out = apply_it8_transform_u16(img, GAMMA, MATRIX, OFFSET)
        finally:
            calibration.is_cuda_available = original

        delta = np.abs(gpu_out.astype(np.int16) - cpu_out.astype(np.int16))
        self.assertLessEqual(int(delta.max()), 1)


if __name__ == "__main__":
    unittest.main()
