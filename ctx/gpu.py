"""Hardware-accelerated CUDA backend with seamless CPU fallback.

Compatible with NVIDIA GPUs across Windows and Ubuntu.
Gracefully detects CUDA availability at runtime:
  - If an NVIDIA GPU + CuPy is present and operational, uses CUDA acceleration.
  - If not (e.g. Intel/AMD GPU, CPU-only system, missing drivers), seamlessly
    falls back to the multithreaded CPU implementation without errors.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger("ctx.gpu")

# Ensure CuPy generates PTX for forward compatibility with all GPU architectures
# (including Blackwell, Hopper, Ada Lovelace, Ampere, Turing, etc.)
os.environ.setdefault("CUPY_COMPILE_WITH_PTX", "1")

#: Live float32 temporaries per band of the IT8 transform; sizes the VRAM band.
_BAND_TEMPORARIES = 10

#: Soft VRAM budget per transform in GiB, overridable for small cards or huge scans.
_VRAM_BUDGET_GIB = float(os.environ.get("CTX_CUDA_VRAM_BUDGET_GIB", "2.0"))


def _format_cuda_version(value: int) -> str:
    """Render an NVIDIA version integer (e.g. 12090) as ``"12.9"``."""
    return f"{value // 1000}.{(value % 1000) // 10}"


_CUDA_AVAILABLE: bool = False
_CUDA_DEVICE_INFO: dict[str, Any] = {
    "available": False,
    "device_name": None,
    "vram_gb": 0.0,
    "driver_version": None,
    "cuda_version": None,
    "reason": "Not initialized",
}

try:
    import cupy as cp
    # Probe CUDA driver and device availability
    dev_count = cp.cuda.runtime.getDeviceCount()
    if dev_count > 0:
        props = cp.cuda.runtime.getDeviceProperties(0)
        dev_name = props["name"].decode() if isinstance(props["name"], bytes) else str(props["name"])
        total_mem = float(props["totalGlobalMem"]) / (1024 ** 3)
        drv_ver = cp.cuda.runtime.driverGetVersion()
        rt_ver = cp.cuda.runtime.runtimeGetVersion()

        # Run a micro test kernel to confirm JIT compilation & execution succeed
        _test_arr = cp.array([1.0, 2.0, 3.0], dtype=cp.float32)
        _ = (_test_arr * 2.0).get()

        _CUDA_AVAILABLE = True
        _CUDA_DEVICE_INFO = {
            "available": True,
            "device_name": dev_name,
            "vram_gb": round(total_mem, 2),
            "driver_version": _format_cuda_version(drv_ver),
            "cuda_version": _format_cuda_version(rt_ver),
            "reason": "CUDA operational",
        }
        logger.info(f"NVIDIA CUDA acceleration active: {dev_name} ({total_mem:.1f} GB VRAM)")
    else:
        _CUDA_DEVICE_INFO["reason"] = "No CUDA devices detected"
except Exception as exc:
    _CUDA_AVAILABLE = False
    _CUDA_DEVICE_INFO = {
        "available": False,
        "device_name": None,
        "vram_gb": 0.0,
        "driver_version": None,
        "cuda_version": None,
        "reason": f"CUDA unavailable ({exc})",
    }
    logger.info(f"NVIDIA CUDA unavailable, defaulting to multicore CPU: {exc}")


def is_cuda_available() -> bool:
    """Return True if NVIDIA CUDA hardware acceleration is operational."""
    return _CUDA_AVAILABLE


def get_cuda_info() -> dict[str, Any]:
    """Return dictionary of CUDA hardware and driver information."""
    return dict(_CUDA_DEVICE_INFO)


def _transform_band(
    comp,
    g: float,
    d_M,
    d_b,
    d_msrgb,
    d_black,
    d_white,
    d_corr0,
    k_val: float,
):
    """Run the IT8 colour math on one band that is already resident in VRAM."""
    import cupy as cp

    # 1. Normalize and lock shadow chromaticity
    frac = cp.clip(comp.astype(cp.float32) / cp.float32(k_val), 0.0, 1.45)
    r_ref = frac[:, :, 2:3]
    shadow_lock = cp.square(cp.clip(1.0 - r_ref / cp.float32(0.075), 0.0, 1.0))
    frac = frac * (cp.float32(1.0) - shadow_lock) + r_ref * shadow_lock

    # 2. Map endpoints and compute XYZ space
    v_ref = cp.clip(
        frac * (d_white - d_black)[None, None, :] + d_black[None, None, :],
        0.0,
        65535.0,
    )
    v = cp.power(v_ref / cp.float32(65535.0), g)
    xyz = v @ d_M.T + d_b[None, None, :]

    shadow_w = cp.power(1.0 - cp.minimum(1.0, frac.mean(axis=2, keepdims=True)), 4.0)
    xyz = xyz - shadow_w * d_corr0[None, None, :]

    # 3. XYZ -> linear sRGB -> gamma corrected sRGB
    lin_srgb = cp.clip(xyz @ d_msrgb.T, 0.0, 1.0)
    srgb = cp.where(
        lin_srgb <= 0.0031308,
        12.92 * lin_srgb,
        1.055 * cp.power(cp.maximum(lin_srgb, 1e-12), 1.0 / 2.4) - 0.055,
    )
    return cp.clip(cp.rint(srgb * 65535.0), 0, 65535).astype(cp.uint16)


def apply_it8_transform_cuda(
    comp_u16: np.ndarray,
    gamma: float,
    matrix: np.ndarray,
    offset: np.ndarray,
    K: float,
    xyz_corr0: np.ndarray,
    IT8_REF_BLACK: np.ndarray,
    IT8_REF_WHITE: np.ndarray,
    MSRGB: np.ndarray,
) -> np.ndarray:
    """Apply the fitted IT8 colorimetric transform via NVIDIA CUDA.

    The document streams through VRAM in row bands sized to a soft budget
    (``CTX_CUDA_VRAM_BUDGET_GIB``, default 2 GiB) instead of being staged whole:
    one 600 dpi sheet needs ~13 GiB in a single pass, which leaves no headroom
    on a 16 GiB card that is also driving the display, and fails outright on
    smaller cards.  The transform is row-local, so banding does not change the
    result -- it only bounds peak VRAM.
    """
    import cupy as cp

    h = comp_u16.shape[0]
    width = comp_u16.shape[1]

    d_M = cp.asarray(matrix, dtype=cp.float32)
    d_b = cp.asarray(offset, dtype=cp.float32)
    d_msrgb = cp.asarray(MSRGB, dtype=cp.float32)
    d_black = cp.asarray(IT8_REF_BLACK, dtype=cp.float32)
    d_white = cp.asarray(IT8_REF_WHITE, dtype=cp.float32)
    d_corr0 = cp.asarray(xyz_corr0, dtype=cp.float32)
    k_val = max(float(K), 1.0)
    g = float(gamma)

    bytes_per_row = max(1, width * 3 * 4 * _BAND_TEMPORARIES)
    band_rows = max(64, min(h, int(_VRAM_BUDGET_GIB * (1024 ** 3)) // bytes_per_row))

    out = np.empty_like(comp_u16)
    for y0 in range(0, h, band_rows):
        y1 = min(h, y0 + band_rows)
        out[y0:y1] = cp.asnumpy(
            _transform_band(
                cp.asarray(comp_u16[y0:y1]),
                g,
                d_M,
                d_b,
                d_msrgb,
                d_black,
                d_white,
                d_corr0,
                k_val,
            )
        )
    return out

