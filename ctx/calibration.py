"""Camera-first calibration, multi-preset management, and thermal drift analysis.

Implements the unified camera-order pipeline established in:
  - ``DESIGN.md`` §5, §6, §11
  - ``analysis/color/COLOR-REPORT.md``
  - ``analysis/stitch/STITCH-REPORT.md`` (§1-§2, §6c)

Key design rules:
  1. The 13,640-px raw raster is a circular readout where 23-px zero bands sit
     at ``490 + 2728*k``, splitting Camera 5 across the row boundaries:
     ``[cam5_right (p0)][gap][cam1 (p1)][gap][cam2 (p2)][gap][cam3 (p3)][gap][cam4 (p4)][gap][cam5_left (p5)]``.
  2. Camera strips 1..5 (``cam1..cam5``, where ``cam5 = p5 + p0``) are assembled
     BEFORE flat-field correction, drift analysis, or stitching.
  3. Optical black pedestals ``B_i(ch)`` are measured per scan from the masked
     dark ramp columns immediately preceding each piece.
  4. Multiple named presets are stored per scanner serial under
     ``~/.config/contex/<serial>/presets/`` with automatic ``.bak`` backups.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import struct
import time
from typing import Any

import numpy as np

from .registration import register_cameras
from .gpu import is_cuda_available, apply_it8_transform_cuda

logger = logging.getLogger(__name__)

#: First CUDA failure is reported; later ones stay quiet so a scan loop cannot
#: flood the log with the same message.
_cuda_fallback_warned = False


def _warn_cuda_fallback(exc: BaseException) -> None:
    global _cuda_fallback_warned
    if _cuda_fallback_warned:
        return
    _cuda_fallback_warned = True
    logger.warning(
        "CUDA IT8 transform failed (%s: %s); using multithreaded CPU instead",
        type(exc).__name__,
        exc,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERIAL = "UNKNOWN"

STRIDE_16BIT_5CAM = 81_840       # 5 strips * 16,368 B (2,728 px * 3 ch * u16 LE)
STRIP_B = 16_368
STRIP_PX = 2_728
RAW_WIDTH_PX = 13_640            # 5 * 2,728 px
WINDOW_PX_300DPI = 12_600        # 42.000 in @ 300 dpi
GROUP_PX = 32                    # spatial white profile group width
DEFAULT_K = 41_000.0

# Default canonical gaps measured across live-5cam, live-5cam-2nd, live-5cam-3rd
DEFAULT_GAPS: list[tuple[int, int]] = [
    (468, 538),
    (3196, 3262),
    (5924, 5990),
    (8652, 8718),
    (11380, 11446),
]

CAMERAS: list[tuple[str, list[int]]] = [
    ("cam1", [1]),
    ("cam2", [2]),
    ("cam3", [3]),
    ("cam4", [4]),
    ("cam5", [5, 0]),
]

# Drift thresholds (DESIGN.md §11.5)
SPREAD_OK = 0.006
SPREAD_WARN = 0.015
LEVEL_OK = 0.02
LEVEL_WARN = 0.06
MIN_WHITE_GROUPS_PER_CAM = 5

# CIE XYZ D50 -> linear sRGB matrix (colourcal.py)
XN, YN, ZN = 0.9642, 1.0, 0.8249
MSRGB = np.array([
    [3.1338561, -1.6168667, -0.4906146],
    [-0.9787684, 1.9161415, 0.0334540],
    [0.0719453, -0.2289914, 1.4052427],
], dtype=np.float32)

# Official Contex Calibration Sheet (p/n 6779G014, Charge R170829) geometry @ 300 dpi
SHEET_BOX = (378, 2384, 838, 2300)
# Full 22x12 IT8 patch area.  x1=2264 is the right edge of the last patch
# column; the A-L labels are outside it.  A 21-box crop (x1=2190) made the
# template one colour box narrower than the scan and biased the fit.
SHEET_MOSAIC = (581, 2264, 1011, 1942)
SHEET_WHITE_BAND = (2795, 3545)
SHEET_BLACK_BAND = (3861, 4766)
IT8_NX, IT8_NY, IT8_NGREY = 22, 12, 24

# Measured per-seam optical overlap (px) and vertical row offset (dy) across cam1..cam5 @ 300 dpi
DEFAULT_SEAM_OVERLAPS_PX: tuple[int, int, int, int] = (144, 136, 121, 151)
DEFAULT_SEAM_DY_PX: tuple[int, int, int, int] = (0, 0, 1, 0)

# Reference Camera 1 raw endpoints on which colourcal.json (IT8.7/2 R170829) was fitted
IT8_REF_BLACK = np.array([4622.0, 3599.0, 3603.0], dtype=np.float32)
IT8_REF_WHITE = np.array([41911.0, 46014.0, 48807.0], dtype=np.float32)


# ---------------------------------------------------------------------------
# .rec container & raster framing
# ---------------------------------------------------------------------------

def read_file_chunked(path: str | Path, chunk_bytes: int = 16 * 1024 * 1024) -> bytes:
    """Read a potentially large file in chunks to avoid Windows SMB ``EINVAL`` on >500 MB reads."""
    chunks: list[bytes] = []
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk_bytes)
            if not b:
                break
            chunks.append(b)
    return b"".join(chunks)


def load_rec_concat(path: str | Path) -> tuple[bytes, dict[str, Any]]:
    """Load a ``.rec`` capture file (``[u32 seq][u16 dlen][payload]``) and
    concatenate payloads in sequence order."""
    raw = read_file_chunked(path)
    off = 0
    n = len(raw)
    records_by_seq: dict[int, bytes] = {}
    dlens: list[int] = []
    while off + 6 <= n:
        seq, dlen = struct.unpack_from("<IH", raw, off)
        o = off + 6
        payload = raw[o : o + dlen]
        if seq not in records_by_seq:
            records_by_seq[seq] = payload
            dlens.append(dlen)
        off = o + dlen

    if not records_by_seq:
        return b"", {
            "records": 0,
            "holes": 0,
            "maxseq": 0,
            "strictly_increasing": True,
            "n_short": 0,
            "payload_bytes": 0,
        }

    sorted_seqs = np.asarray(sorted(records_by_seq.keys()), dtype=np.int64)
    dlens_arr = np.asarray(dlens, dtype=np.int64)
    span = int(sorted_seqs[-1] - sorted_seqs[0] + 1)
    holes = int(span - len(sorted_seqs))
    buf = b"".join(records_by_seq[int(s)] for s in sorted_seqs)
    info = {
        "records": int(len(sorted_seqs)),
        "holes": holes,
        "maxseq": int(sorted_seqs[-1]),
        "strictly_increasing": bool(np.all(np.diff(sorted_seqs) == 1)),
        "n_short": int((dlens_arr != 2936).sum()),
        "payload_bytes": int(len(buf)),
    }
    return buf, info


def to_raster_16bit(buf: bytes) -> tuple[np.ndarray, int]:
    """Convert concatenated wire stream into ``(rows, 13640, 3)`` uint16 array."""
    rows = len(buf) // STRIDE_16BIT_5CAM
    if rows <= 0:
        raise ValueError(
            f"Stream too short ({len(buf)} B) for 16-bit 5-camera stride ({STRIDE_16BIT_5CAM} B)"
        )
    raster = np.frombuffer(buf[: rows * STRIDE_16BIT_5CAM], dtype="<u2").reshape(
        rows, RAW_WIDTH_PX, 3
    )
    return raster, rows


# ---------------------------------------------------------------------------
# Camera-first geometry (STITCH-REPORT.md §1)
# ---------------------------------------------------------------------------

def module_gaps(
    lum: np.ndarray,
    ramp_frac: float = 0.70,
    max_ramp: int = 150,
    pre: int = 22,
) -> list[tuple[int, int]]:
    """Detect unusable column bands ``(start, end_exclusive)`` at the 5 module junctions."""
    prof = lum.mean(axis=0, dtype=np.float32) if lum.ndim == 2 else lum.astype(np.float32)
    zero = prof <= 0.0
    runs: list[tuple[int, int]] = []
    i, n = 0, len(zero)
    while i < n:
        if zero[i]:
            j = i
            while j + 1 < n and zero[j + 1]:
                j += 1
            runs.append((i, j + 1))
            i = j + 1
        else:
            i += 1

    if len(runs) != 5:
        return list(DEFAULT_GAPS)

    out: list[tuple[int, int]] = []
    for idx, (a, b) in enumerate(runs):
        ref = float(np.median(prof[b + 80 : b + 280])) if b + 280 < n else 1.0
        ref = ref or 1.0
        e = max(b, DEFAULT_GAPS[idx][1])
        while e < min(n, b + max_ramp) and prof[e] < ramp_frac * ref:
            e += 1
        out.append((int(max(0, a - pre)), int(e)))
    return out


def piece_matrix(gaps: list[tuple[int, int]], width: int = RAW_WIDTH_PX) -> list[dict[str, Any]]:
    """Return the 6 pieces between the 5 gaps, each annotated with strip segments."""
    spans: list[tuple[int, int]] = []
    prev = 0
    for a, b in gaps:
        spans.append((prev, a))
        prev = b
    spans.append((prev, width))
    out: list[dict[str, Any]] = []
    for idx, (a, b) in enumerate(spans):
        segs: list[dict[str, int]] = []
        x = a
        while x < b:
            strip = x // STRIP_PX
            end = min(b, (strip + 1) * STRIP_PX)
            c0 = x - strip * STRIP_PX
            segs.append(
                {
                    "strip": int(strip),
                    "start": int(x),
                    "end": int(end),
                    "col0": int(c0),
                    "col1": int(c0 + (end - x)),
                }
            )
            x = end
        out.append({"index": idx, "start": int(a), "end": int(b), "width": int(b - a), "segs": segs})
    return out


def raw_camera_images(
    raster: np.ndarray,
    pmeta: list[dict[str, Any]],
    cameras: list[tuple[str, list[int]]] = CAMERAS,
) -> list[tuple[str, np.ndarray]]:
    """Reassemble each camera image (``cam1..cam5``) in bed order from the raw raster."""
    out: list[tuple[str, np.ndarray]] = []
    for name, pids in cameras:
        parts = [raster[:, sg["start"] : sg["end"], :] for pid in pids for sg in pmeta[pid]["segs"]]
        out.append((name, np.concatenate(parts, axis=1)))
    return out


def extract_black_levels(
    raster: np.ndarray,
    pmeta: list[dict[str, Any]],
    cameras: list[tuple[str, list[int]]] = CAMERAS,
) -> dict[str, list[float]]:
    """Measure per-camera optical dark pedestal ``[R, G, B]`` from the masked ramp
    columns immediately preceding each camera piece (excluding the 23-px digital zero core)."""
    out: dict[str, list[float]] = {}
    for name, pids in cameras:
        vals = []
        for pid in pids:
            s0 = pmeta[pid]["start"]
            if s0 < 28:
                continue
            patch = raster[:, s0 - 25 : s0, :].reshape(-1, 3).astype(np.float64)
            nz = patch[patch.sum(axis=1) > 0]
            if len(nz) > 0:
                vals.append(nz)
        if not vals:
            out[name] = [0.0, 0.0, 0.0]
        else:
            v = np.concatenate(vals, axis=0)
            med = np.median(v, axis=0)
            out[name] = [float(med[0]), float(med[1]), float(med[2])]
    return out


# ---------------------------------------------------------------------------
# White-profile estimation & polynomial gain fitting
# ---------------------------------------------------------------------------

def camera_white_profile(img: np.ndarray, rows: int, G: int = GROUP_PX) -> np.ndarray:
    """Compute per-channel paper white level in ``G``-px column groups: shape ``(ng, 3)``,
    with ``NaN`` in groups lacking low-variance bright paper blocks."""
    w = img.shape[1]
    ng = max(1, (w + G - 1) // G)
    out = np.full((ng, 3), np.nan, dtype=np.float64)
    h8 = (rows // 8) * 8
    if h8 < 8:
        return out
    group_p95 = np.full(ng, np.nan, dtype=np.float64)
    group_candidates: list[tuple[int, np.ndarray]] = []
    for g in range(ng):
        a = g * G
        bw = ((min(w, a + G) - a) // 8) * 8
        if bw < 8:
            continue
        sb = img[:h8, a : a + bw, :].reshape(h8 // 8, 8, bw // 8, 8, 3)
        sm = sb.mean(axis=(1, 3)).reshape(-1, 3)
        sd = sb.std(axis=(1, 3)).mean(axis=2).reshape(-1)
        lum = sm.mean(axis=1)
        p95 = float(np.percentile(lum, 95))
        if p95 < 15000:  # guard against dark/midtone regions lacking white paper
            continue
        sel = (sd < 1600) & (lum > 0.88 * p95)
        min_blocks = min(40, max(4, int(len(lum) * 0.08)))
        if int(sel.sum()) < min_blocks:
            continue
        med_rgb = np.median(sm[sel], axis=0)
        group_p95[g] = p95
        group_candidates.append((g, med_rgb))

    valid_p95 = group_p95[~np.isnan(group_p95)]
    if len(valid_p95) == 0:
        return out
    cam_white_ref = float(np.percentile(valid_p95, 85))
    min_group_p95 = max(15000.0, 0.62 * cam_white_ref)
    for g, med_rgb in group_candidates:
        if group_p95[g] >= min_group_p95:
            out[g] = med_rgb
    return out


def fill_profile_columns(prof: np.ndarray, width: int, G: int = GROUP_PX) -> np.ndarray:
    """Interpolate a ``(ng, 3)`` group profile to every column ``(width, 3)``."""
    ng = prof.shape[0]
    xg = np.arange(ng, dtype=np.float64) * G + (G - 1) / 2.0
    cols = np.arange(width, dtype=np.float64)
    out = np.empty((width, 3), dtype=np.float64)
    for ch in range(3):
        v = np.asarray(prof[:, ch], dtype=np.float64)
        ok = ~np.isnan(v)
        if int(ok.sum()) >= 2:
            out[:, ch] = np.interp(cols, xg[ok], v[ok])
        elif bool(ok.any()):
            out[:, ch] = float(v[ok][0])
        else:
            out[:, ch] = DEFAULT_K
    return out.astype(np.float32)


def smooth_group_profile(prof: np.ndarray) -> np.ndarray:
    """Fill NaNs by linear interpolation and apply a 3-group boxcar smooth."""
    ng = prof.shape[0]
    gx = np.arange(ng, dtype=np.float64)
    out = np.empty((ng, 3), dtype=np.float64)
    for ch in range(3):
        v = np.asarray(prof[:, ch], dtype=np.float64)
        ok = ~np.isnan(v)
        if int(ok.sum()) >= 2:
            filled = np.interp(gx, gx[ok], v[ok])
        elif bool(ok.any()):
            filled = np.full(ng, float(v[ok][0]), dtype=np.float64)
        else:
            filled = np.full(ng, DEFAULT_K, dtype=np.float64)
        pad = np.pad(filled, 1, mode="edge")
        out[:, ch] = np.convolve(pad, np.ones(3) / 3.0, mode="valid")
    return out


def fit_gain_profile(
    shape_w: np.ndarray,
    shape_t: np.ndarray,
    deg: int = 5,
    iters: int = 4,
) -> np.ndarray:
    """Fit a smooth degree-``deg`` polynomial gain profile ``g(col, ch)`` such that
    ``shape_t ~ g * shape_w`` on valid white groups (STITCH-REPORT.md §2)."""
    n = shape_w.shape[0]
    if shape_t.shape[0] != n:
        # Resample shape_w to match shape_t length if needed
        x_old = np.linspace(-1.0, 1.0, n)
        x_new = np.linspace(-1.0, 1.0, shape_t.shape[0])
        sw_resampled = np.empty_like(shape_t)
        for ch in range(3):
            v = shape_w[:, ch]
            ok_w = ~np.isnan(v)
            sw_resampled[:, ch] = np.interp(x_new, x_old[ok_w], v[ok_w]) if ok_w.any() else DEFAULT_K
        shape_w = sw_resampled
        n = shape_t.shape[0]

    ok = ~np.isnan(shape_w[:, 0]) & ~np.isnan(shape_t[:, 0]) & (shape_w[:, 0] > 100) & (shape_t[:, 0] > 100)
    x = np.linspace(-1.0, 1.0, n)
    out = np.ones((n, 3), dtype=np.float64)
    if int(ok.sum()) < deg + 2:
        if int(ok.sum()) >= 1:
            for ch in range(3):
                ratio = np.median(shape_t[ok, ch] / np.maximum(shape_w[ok, ch], 1.0))
                out[:, ch] = float(np.clip(ratio, 0.3, 3.5))
        return out.astype(np.float32)

    for ch in range(3):
        y = np.log(np.maximum(shape_t[ok, ch], 1.0) / np.maximum(shape_w[ok, ch], 1.0))
        xx = x[ok]
        coef = np.polyfit(xx, y, min(deg, len(xx) - 1))
        for _ in range(iters):
            r = y - np.polyval(coef, xx)
            med = np.median(r)
            mad = 1.4826 * np.median(np.abs(r - med)) + 1e-9
            w = np.abs(r - med) < 3.0 * mad
            if int(w.sum()) < deg + 2:
                break
            coef = np.polyfit(xx[w], y[w], min(deg, int(w.sum()) - 1))
        out[:, ch] = np.clip(np.exp(np.polyval(coef, x)), 0.25, 4.0)
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# Colorimetric Transform (IT8.7/2 - colourcal.py)
# ---------------------------------------------------------------------------

def xyz_to_lab(X: np.ndarray) -> np.ndarray:
    t = np.clip(X, 0.0, None) / np.array([XN, YN, ZN], dtype=np.float64)
    d = (6.0 / 29.0) ** 3
    f = np.where(t > d, np.cbrt(t), t / (3.0 * d ** (2.0 / 3.0)) + 4.0 / 29.0)
    return np.stack(
        [116.0 * f[..., 1] - 16.0, 500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2])],
        axis=-1,
    )


def _apply_it8_transform_chunk(
    chunk_u16: np.ndarray,
    g: float,
    M: np.ndarray,
    b: np.ndarray,
    k_val: float,
    xyz_corr0: np.ndarray,
) -> np.ndarray:
    frac = np.clip(chunk_u16.astype(np.float32) / np.float32(k_val), 0.0, 1.45)

    # Equalize deep-shadow quantization noise floor across cameras 4 & 5 (where ch0 W-B is ~6,500
    # vs ch2 W-B ~39,200) by locking deep-shadow chromaticity to the high-SNR ch2 reference when <7%
    r_ref = frac[:, :, 2:3]
    shadow_lock = np.square(np.clip(1.0 - r_ref / np.float32(0.075), 0.0, 1.0))
    frac = frac * (np.float32(1.0) - shadow_lock) + r_ref * shadow_lock

    v_ref = np.clip(
        frac * (IT8_REF_WHITE - IT8_REF_BLACK)[None, None, :] + IT8_REF_BLACK[None, None, :],
        0.0,
        65535.0,
    )
    v = np.power(v_ref / np.float32(65535.0), g)
    xyz = v @ M.T + b[None, None, :]

    shadow_w = np.power(1.0 - np.minimum(1.0, frac.mean(axis=2, keepdims=True)), 4.0)
    xyz = xyz - shadow_w * xyz_corr0[None, None, :]

    lin_srgb = np.clip(xyz @ MSRGB.T, 0.0, 1.0)
    srgb = np.where(
        lin_srgb <= 0.0031308,
        12.92 * lin_srgb,
        1.055 * np.power(np.maximum(lin_srgb, 1e-12), 1.0 / 2.4) - 0.055,
    )
    return np.clip(np.rint(srgb * 65535.0), 0, 65535).astype(np.uint16)


def apply_it8_transform_u16(
    comp_u16: np.ndarray,
    gamma: float,
    matrix: list[list[float]] | np.ndarray,
    offset: list[float] | np.ndarray,
    K: float = DEFAULT_K,
) -> np.ndarray:
    """Apply the fitted IT8 colorimetric transform (gamma -> 3x3 XYZ D50 -> sRGB)
    and return a uint16 sRGB image ``(H, W, 3)``.

    Maps the normalized ``[0, K]`` two-point calibrated reflectance onto the
    Camera 1 reference endpoints ``[IT8_REF_BLACK, IT8_REF_WHITE]`` on which
    ``colourcal.json`` was fitted, and neutralizes the residual shadow pedestal
    at ``frac -> 0`` so dark/black bands match across all 5 cameras.
    """
    M = np.asarray(matrix, dtype=np.float32)
    b = np.asarray(offset, dtype=np.float32)
    g = float(gamma)
    k_val = max(float(K or DEFAULT_K), 1.0)

    # Neutralize residual XYZ offset at black endpoint (v_ref == IT8_REF_BLACK)
    xyz0 = (np.power(IT8_REF_BLACK / np.float32(65535.0), g) @ M.T) + b
    xyz_target0 = np.array([0.00293, 0.00304, 0.00250], dtype=np.float32)
    xyz_corr0 = np.maximum(xyz0 - xyz_target0, 0.0)

    # 1. NVIDIA CUDA acceleration with automatic fallback
    if is_cuda_available() and comp_u16.shape[0] >= 64:
        try:
            return apply_it8_transform_cuda(
                comp_u16,
                gamma=g,
                matrix=M,
                offset=b,
                K=k_val,
                xyz_corr0=xyz_corr0,
                IT8_REF_BLACK=IT8_REF_BLACK,
                IT8_REF_WHITE=IT8_REF_WHITE,
                MSRGB=MSRGB,
            )
        except Exception as exc:
            _warn_cuda_fallback(exc)

    # 2. CPU Multithreaded execution (fallback for systems without NVIDIA GPU)
    h = comp_u16.shape[0]
    num_workers = min(os.cpu_count() or 4, 32)
    if h < 256 or num_workers <= 1:
        return _apply_it8_transform_chunk(comp_u16, g, M, b, k_val, xyz_corr0)

    chunk_size = (h + num_workers - 1) // num_workers
    slices = [(i * chunk_size, min(h, (i + 1) * chunk_size)) for i in range(num_workers) if i * chunk_size < h]
    out = np.empty_like(comp_u16)

    def _worker(sl):
        sr, er = sl
        out[sr:er] = _apply_it8_transform_chunk(comp_u16[sr:er], g, M, b, k_val, xyz_corr0)

    with ThreadPoolExecutor(max_workers=len(slices)) as ex:
        list(ex.map(_worker, slices))
    return out


# ---------------------------------------------------------------------------
# Calibration Preset Dataclass & Store
# ---------------------------------------------------------------------------

@dataclass
class It8Transform:
    enabled: bool = True
    gamma: float = 0.95
    matrix: list[list[float]] = field(default_factory=list)
    offset: list[float] = field(default_factory=list)
    sheet_pn: str = "6779G014"
    sheet_charge: str = "R170829"
    dE76_mean: float = 3.64
    dE76_median: float = 2.30


@dataclass
class CalibrationPreset:
    id: str
    name: str
    description: str
    serial: str = DEFAULT_SERIAL
    schema: str = "scanner-color/camera-preset-v2"
    K: float = DEFAULT_K
    # W maps "cam1".."cam5" -> {"R": [...], "G": [...], "B": [...]} (84 groups each)
    W: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    # B maps "cam1".."cam5" -> [R, G, B] dark pedestal
    B: dict[str, list[float]] = field(default_factory=dict)
    # Optional per-group black profile B_profile maps "cam1".."cam5" -> {"R": [...], "G": [...], "B": [...]}
    B_profile: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    it8: It8Transform = field(default_factory=It8Transform)
    created: str = ""
    updated: str = ""
    source: str = ""
    is_builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalibrationPreset":
        it8_raw = d.get("it8") or {}
        it8 = It8Transform(
            enabled=bool(it8_raw.get("enabled", True)),
            gamma=float(it8_raw.get("gamma", 0.95)),
            matrix=list(it8_raw.get("matrix", [])),
            offset=list(it8_raw.get("offset", [])),
            sheet_pn=str(it8_raw.get("sheet_pn", "6779G014")),
            sheet_charge=str(it8_raw.get("sheet_charge", "R170829")),
            dE76_mean=float(it8_raw.get("dE76_mean", 3.64)),
            dE76_median=float(it8_raw.get("dE76_median", 2.30)),
        )
        k_val = d.get("K", DEFAULT_K)
        if isinstance(k_val, dict):
            k_val = float(np.mean([k_val.get("R", DEFAULT_K), k_val.get("G", DEFAULT_K), k_val.get("B", DEFAULT_K)]))
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            serial=str(d.get("serial", DEFAULT_SERIAL)),
            schema=str(d.get("schema", "scanner-color/camera-preset-v2")),
            K=float(k_val),
            W=dict(d.get("W", {})),
            B=dict(d.get("B", {})),
            B_profile=dict(d.get("B_profile", {})),
            it8=it8,
            created=str(d.get("created", "")),
            updated=str(d.get("updated", "")),
            source=str(d.get("source", "")),
            is_builtin=bool(d.get("is_builtin", False)),
        )

    def get_camera_W_array(self, cam_name: str, expected_groups: int = 84) -> np.ndarray:
        """Return ``(ng, 3)`` float64 white profile for ``cam_name``."""
        cam_w = self.W.get(cam_name)
        if not cam_w:
            return np.full((expected_groups, 3), self.K, dtype=np.float64)
        r = np.asarray(cam_w.get("R", [self.K] * expected_groups), dtype=np.float64)
        g = np.asarray(cam_w.get("G", [self.K] * expected_groups), dtype=np.float64)
        b = np.asarray(cam_w.get("B", [self.K] * expected_groups), dtype=np.float64)
        return np.stack([r, g, b], axis=1)

    def get_camera_B_array(self, cam_name: str, expected_groups: int = 84) -> np.ndarray:
        """Return ``(ng, 3)`` float64 dark reference profile for ``cam_name``."""
        cam_bp = self.B_profile.get(cam_name)
        b_scalar = self.B.get(cam_name, [4600.0, 3600.0, 3600.0])
        if cam_bp and cam_bp.get("R"):
            r = np.asarray(cam_bp.get("R", [b_scalar[0]] * expected_groups), dtype=np.float64)
            g = np.asarray(cam_bp.get("G", [b_scalar[1]] * expected_groups), dtype=np.float64)
            b = np.asarray(cam_bp.get("B", [b_scalar[2]] * expected_groups), dtype=np.float64)
            arr = np.stack([r, g, b], axis=1)
            if arr.shape[0] == expected_groups:
                return arr
        return np.tile(np.asarray(b_scalar, dtype=np.float64)[None, :], (expected_groups, 1))


def _build_factory_camera_profiles() -> tuple[
    dict[str, dict[str, list[float]]],
    dict[str, list[float]],
    dict[str, dict[str, list[float]]],
    float,
    It8Transform,
]:
    """Construct the initial camera-ordered ``W``, ``B``, ``B_profile``, ``K``, and ``It8Transform``
    from the repository's measured calibration artifacts (``analysis/stitch/sheet-cal-official1.json``,
    ``analysis/color/calib.json``, and ``analysis/stitch/colourcal.json``)."""
    calib_path = REPO_ROOT / "ctx" / "data" / "calib.json"
    sheet_path = REPO_ROOT / "ctx" / "data" / "sheet-cal-official1.json"
    it8_path = REPO_ROOT / "ctx" / "data" / "colourcal.json"

    # Load IT8 colorimetric matrix
    it8 = It8Transform()
    if it8_path.exists():
        it8_data = json.loads(it8_path.read_text(encoding="utf-8"))
        err = it8_data.get("error", {})
        it8 = It8Transform(
            enabled=True,
            gamma=float(it8_data.get("gamma", 0.95)),
            matrix=list(it8_data.get("matrix", [])),
            offset=list(it8_data.get("offset", [])),
            dE76_mean=float(err.get("dE76_mean", 3.64)),
            dE76_median=float(err.get("dE76_median", 2.30)),
        )

    # Load black levels, white refs, and 84-group W/B profiles from sheet-cal-official1.json
    B_cams: dict[str, list[float]] = {
        f"cam{i + 1}": [4600.0, 3600.0, 3600.0] for i in range(5)
    }
    B_profiles: dict[str, dict[str, list[float]]] = {}
    W_from_sheet: dict[str, dict[str, list[float]]] = {}
    K_val = DEFAULT_K
    if sheet_path.exists():
        sdata = json.loads(sheet_path.read_text(encoding="utf-8"))
        if sdata.get("K"):
            K_val = float(sdata["K"])
        mb_black = sdata.get("measured_before", {}).get("black", [])
        for i, row in enumerate(mb_black[:5]):
            B_cams[f"cam{i + 1}"] = [float(row[0]), float(row[1]), float(row[2])]
        if isinstance(sdata.get("W_profiles"), dict):
            W_from_sheet = dict(sdata["W_profiles"])
        if isinstance(sdata.get("B_profiles"), dict):
            B_profiles = dict(sdata["B_profiles"])

    # Prefer direct 84-group W_profiles from official-cal-1.rec if available
    W_cams: dict[str, dict[str, list[float]]] = {}
    if len(W_from_sheet) == 5:
        W_cams = W_from_sheet
    elif calib_path.exists():
        cdata = json.loads(calib_path.read_text(encoding="utf-8"))
        k_dict = cdata.get("K", {})
        if isinstance(k_dict, dict) and k_dict:
            K_val = float(np.mean([k_dict.get("R", DEFAULT_K), k_dict.get("G", DEFAULT_K), k_dict.get("B", DEFAULT_K)]))
        w_strips = cdata.get("W", {})
        full_row = np.zeros((1, RAW_WIDTH_PX, 3), dtype=np.float32)
        gx = np.arange(85, dtype=np.float64) * GROUP_PX + GROUP_PX / 2.0
        cols_strip = np.arange(STRIP_PX, dtype=np.float64)
        for s_idx in range(5):
            sc = w_strips.get(str(s_idx), {})
            for ch_i, ch_name in enumerate("RGB"):
                arr = np.asarray(sc.get(ch_name, [K_val] * 85), dtype=np.float64)
                interp_col = np.interp(cols_strip, gx, arr)
                full_row[0, s_idx * STRIP_PX : (s_idx + 1) * STRIP_PX, ch_i] = interp_col

        pmeta = piece_matrix(DEFAULT_GAPS, RAW_WIDTH_PX)
        p5_end = full_row[0, pmeta[5][" end" if " end" in pmeta[5] else "end"] - 64 : pmeta[5]["end"], :].mean(axis=0)
        full_row[0, : pmeta[0]["end"], :] = p5_end[None, :]

        cams_raw = raw_camera_images(full_row, pmeta, CAMERAS)
        for cam_name, cam_arr in cams_raw:
            w = cam_arr.shape[1]
            ng = max(1, (w + GROUP_PX - 1) // GROUP_PX)
            cam_dict: dict[str, list[float]] = {"R": [], "G": [], "B": []}
            for g in range(ng):
                a = g * GROUP_PX
                b = min(w, a + GROUP_PX)
                med = np.median(cam_arr[0, a:b, :], axis=0)
                cam_dict["R"].append(round(float(med[0]), 2))
                cam_dict["G"].append(round(float(med[1]), 2))
                cam_dict["B"].append(round(float(med[2]), 2))
            W_cams[cam_name] = cam_dict
    else:
        for i in range(5):
            W_cams[f"cam{i + 1}"] = {
                "R": [K_val] * 84,
                "G": [K_val] * 84,
                "B": [K_val] * 84,
            }

    return W_cams, B_cams, B_profiles, K_val, it8


class PresetStore:
    """Manages multiple named calibration presets on disk per scanner serial."""

    def __init__(self, base_dir: str | Path | None = None, serial: str = DEFAULT_SERIAL):
        self.serial = serial
        if base_dir is None:
            self.base_dir = Path.home() / ".config" / "contex" / serial / "presets"
        else:
            self.base_dir = Path(base_dir) / serial / "presets"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_builtins()

    def _ensure_builtins(self) -> None:
        W_cams, B_cams, B_profiles, K_val, it8 = _build_factory_camera_profiles()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        builtins = [
            CalibrationPreset(
                id="default-it8",
                name="Official Contex IT8 + Flat-Field",
                description="Full two-point flat-field + Official R170829 IT8.7/2 3x3 matrix & gamma (0.95). Ideal for accurate color reproduction.",
                serial=self.serial,
                K=K_val,
                W=W_cams,
                B=B_cams,
                B_profile=B_profiles,
                it8=it8,
                created=now,
                updated=now,
                source="ctx/data/sheet-cal-official1.json + ctx/data/colourcal.json",
                is_builtin=True,
            ),
            CalibrationPreset(
                id="architectural-neutral",
                name="3D Architectural & CAD Bond",
                description="Neutral per-column flat-field + dark pedestal subtraction with clean paper white point (K=41,000) for crisp 3D architectural drawings and linework.",
                serial=self.serial,
                K=K_val,
                W=W_cams,
                B=B_cams,
                B_profile=B_profiles,
                it8=It8Transform(
                    enabled=False,
                    gamma=it8.gamma,
                    matrix=it8.matrix,
                    offset=it8.offset,
                ),
                created=now,
                updated=now,
                source="ctx/data/sheet-cal-official1.json",
                is_builtin=True,
            ),
            CalibrationPreset(
                id="artwork-photo",
                name="Fine Art & Large Photo",
                description="Two-point per-column calibration + IT8 colorimetry tuned for continuous-tone artwork and photography with smooth shadow gradation.",
                serial=self.serial,
                K=43_500.0,
                W=W_cams,
                B=B_cams,
                B_profile=B_profiles,
                it8=It8Transform(
                    enabled=True,
                    gamma=0.92,
                    matrix=it8.matrix,
                    offset=it8.offset,
                    dE76_mean=it8.dE76_mean,
                    dE76_median=it8.dE76_median,
                ),
                created=now,
                updated=now,
                source="ctx/data/sheet-cal-official1.json + ctx/data/colourcal.json",
                is_builtin=True,
            ),
        ]
        for preset in builtins:
            p = self.base_dir / f"{preset.id}.json"
            needs_write = not p.exists()
            if not needs_write:
                try:
                    existing = json.loads(p.read_text(encoding="utf-8"))
                    if existing.get("is_builtin") and not existing.get("B_profile"):
                        needs_write = True
                except Exception:
                    needs_write = True
            if needs_write:
                p.write_text(json.dumps(preset.to_dict(), indent=2), encoding="utf-8")

    def list_presets(self) -> list[CalibrationPreset]:
        out: list[CalibrationPreset] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(CalibrationPreset.from_dict(data))
            except Exception:
                continue
        return out

    def get_preset(self, preset_id: str) -> CalibrationPreset:
        path = self.base_dir / f"{preset_id}.json"
        if not path.exists():
            presets = self.list_presets()
            if presets:
                return presets[0]
            raise KeyError(f"Calibration preset {preset_id!r} not found")
        return CalibrationPreset.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_preset(self, preset: CalibrationPreset, backup: bool = True) -> Path:
        path = self.base_dir / f"{preset.id}.json"
        if backup and path.exists():
            bak = path.with_name(f"{preset.id}.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, bak)
        preset.updated = time.strftime("%Y-%m-%dT%H:%M:%S")
        if not preset.created:
            preset.created = preset.updated
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(preset.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def rollback_preset(self, preset_id: str) -> CalibrationPreset | None:
        backups = sorted(self.base_dir.glob(f"{preset_id}.json.bak-*"), reverse=True)
        if not backups:
            return None
        latest_bak = backups[0]
        target = self.base_dir / f"{preset_id}.json"
        shutil.copy2(latest_bak, target)
        latest_bak.unlink()
        return self.get_preset(preset_id)

    def delete_preset(self, preset_id: str) -> bool:
        path = self.base_dir / f"{preset_id}.json"
        if not path.exists():
            return False
        preset = self.get_preset(preset_id)
        if preset.is_builtin:
            raise ValueError("Cannot delete a built-in preset")
        path.unlink()
        return True


# ---------------------------------------------------------------------------
# Camera Correction & Thermal Drift Analyzer
# ---------------------------------------------------------------------------

def apply_camera_calibration(
    raster: np.ndarray,
    rows: int,
    preset: CalibrationPreset,
    *,
    auto_adjust_margins: bool = False,
    gaps: list[tuple[int, int]] | None = None,
    registration_out: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, np.ndarray, dict[str, Any]]], float, list[dict[str, Any]]]:
    """Apply per-camera flat-field + dark pedestal correction in camera order ``cam1..cam5``.

    When ``auto_adjust_margins=False`` ("Default Preset" mode):
      Uses the stored ``preset.W`` and optical black ``B`` directly.
    When ``auto_adjust_margins=True`` ("Auto-Adjusted" mode):
      Fits a smooth degree-5 polynomial gain ``s(cam, col, ch)`` on the scan's own
      white margins to absorb lamp operating temperature drift and paper differences.

    Before calibration, the CIS red/green lines are shifted onto blue to remove the
    sensor's fixed row offset (see :mod:`ctx.registration`).  ``registration_out``,
    when supplied, receives that measurement for scan metadata.
    """
    if gaps is None:
        gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))
    pmeta = piece_matrix(gaps, raster.shape[1])
    cams = raw_camera_images(raster, pmeta, CAMERAS)
    cams, channel_reg = register_cameras(cams)
    if registration_out is not None:
        registration_out.update(channel_reg)
    live_blacks = extract_black_levels(raster, pmeta, CAMERAS)
    with ThreadPoolExecutor(max_workers=min(len(cams), 8)) as ex:
        scan_profs = list(ex.map(lambda c: camera_white_profile(c[1], rows), cams))

    K = float(preset.K or DEFAULT_K)

    def _calibrate_single_cam(pair):
        (name, img), scan_prof = pair
        ng = scan_prof.shape[0]
        stored_w = preset.get_camera_W_array(name, expected_groups=ng)
        stored_b = preset.get_camera_B_array(name, expected_groups=ng)
        preset_b_scalar = preset.B.get(name)
        if preset_b_scalar and any(float(x) > 0.0 for x in preset_b_scalar):
            B_vec = np.asarray(preset_b_scalar, dtype=np.float32)
        else:
            B_vec = np.asarray(live_blacks.get(name, [0.0, 0.0, 0.0]), dtype=np.float32)
            stored_b = np.tile(B_vec.astype(np.float64)[None, :], (ng, 1))

        if auto_adjust_margins:
            gain_prof = fit_gain_profile(stored_w, scan_prof)
        else:
            gain_prof = np.ones_like(stored_w, dtype=np.float32)

        predicted_white = fill_profile_columns(stored_w * gain_prof, img.shape[1])
        B_col = fill_profile_columns(stored_b, img.shape[1])
        den = np.maximum(predicted_white - B_col, 1.0)
        v = (img.astype(np.float32) - B_col[None, :, :]) / den[None, :, :] * np.float32(K)
        corr_u16 = np.clip(np.rint(v), 0, 65535).astype(np.uint16)

        diag = {
            "width": int(img.shape[1]),
            "black_rgb": [round(float(x), 1) for x in B_vec],
            "gain_r_range": [
                round(float(gain_prof[:, 0].min()), 4),
                round(float(gain_prof[:, 0].mean()), 4),
                round(float(gain_prof[:, 0].max()), 4),
            ],
            "gain_g_mean": round(float(gain_prof[:, 1].mean()), 4),
            "gain_b_mean": round(float(gain_prof[:, 2].mean()), 4),
            "white_groups_found": int((~np.isnan(scan_prof[:, 0])).sum()),
            "channel_registration": {
                "red_px": channel_reg.get("dy_red_px"),
                "green_px": channel_reg.get("dy_green_px"),
                "applied": bool(channel_reg.get("applied")),
            },
            "_gain_prof": gain_prof,
            "_stored_w": stored_w,
            "_scan_prof": scan_prof,
        }
        return (name, corr_u16, diag)

    with ThreadPoolExecutor(max_workers=min(len(cams), 8)) as ex:
        corrected_cams = list(ex.map(_calibrate_single_cam, zip(cams, scan_profs)))

    return corrected_cams, K, pmeta


def _measure_corrected_white_stats(
    corrected_cams: list[tuple[str, np.ndarray, dict[str, Any]]],
    rows: int,
    K: float,
) -> tuple[dict[str, list[float] | None], dict[str, list[float] | None], dict[str, int]]:
    """Measure per-camera white chromaticity ``[r, g, b]`` and level ratio on corrected camera images."""
    chroma: dict[str, list[float] | None] = {}
    levels: dict[str, list[float] | None] = {}
    ngood: dict[str, int] = {}

    for name, img, _diag in corrected_cams:
        prof = camera_white_profile(img, rows)
        ok = ~np.isnan(prof[:, 0])
        ngood[name] = int(ok.sum())
        if int(ok.sum()) >= 1:
            mean_rgb = np.median(prof[ok], axis=0)
            s = float(mean_rgb.sum()) or 1.0
            chroma[name] = [
                round(float(mean_rgb[0] / s), 5),
                round(float(mean_rgb[1] / s), 5),
                round(float(mean_rgb[2] / s), 5),
            ]
            levels[name] = [
                round(float(mean_rgb[0] / K), 4),
                round(float(mean_rgb[1] / K), 4),
                round(float(mean_rgb[2] / K), 4),
            ]
        else:
            chroma[name] = None
            levels[name] = None
    return chroma, levels, ngood


def analyze_drift(
    raster: np.ndarray,
    rows: int,
    preset: CalibrationPreset,
    gaps: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Compare stored preset calibration vs. per-scan margin self-calibration across
    all 5 cameras to detect thermal/lamp color shifts."""
    if gaps is None:
        gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))

    preset_cams, k_preset, _ = apply_camera_calibration(
        raster, rows, preset, auto_adjust_margins=False, gaps=gaps
    )
    auto_cams, k_auto, _ = apply_camera_calibration(
        raster, rows, preset, auto_adjust_margins=True, gaps=gaps
    )

    stored_chroma, stored_levels, ngood = _measure_corrected_white_stats(preset_cams, rows, k_preset)
    self_chroma, self_levels, _ = _measure_corrected_white_stats(auto_cams, rows, k_auto)

    cams_with_white = sum(1 for v in ngood.values() if v >= MIN_WHITE_GROUPS_PER_CAM)
    if cams_with_white < 4:
        return {
            "verdict": "INSUFFICIENT_WHITE",
            "preset_id": preset.id,
            "preset_name": preset.name,
            "white_groups": ngood,
            "stored_spread": None,
            "self_spread": None,
            "stored_level_dev_max": None,
            "self_level_dev_max": None,
            "stored_chroma": stored_chroma,
            "self_chroma": self_chroma,
            "camera_gains": {name: d["gain_r_range"] for name, _, d in auto_cams},
            "thresholds": {
                "spread_ok": SPREAD_OK,
                "spread_warn": SPREAD_WARN,
                "level_ok": LEVEL_OK,
                "level_warn": LEVEL_WARN,
            },
            "recommendation": (
                "Full-bleed artwork/photo detected (insufficient white margins to auto-fit). "
                "Use the active preset or calibrate with the Official Contex Calibration Sheet."
            ),
        }

    def _calc_spread(ch_map: dict[str, list[float] | None]) -> float | None:
        r_vals = [v[0] for v in ch_map.values() if v is not None]
        return round(float(max(r_vals) - min(r_vals)), 5) if len(r_vals) >= 3 else None

    def _calc_level_dev(lv_map: dict[str, list[float] | None]) -> float | None:
        devs = [abs(x - 1.0) for v in lv_map.values() if v is not None for x in v]
        return round(float(max(devs)), 4) if devs else None

    s_stored = _calc_spread(stored_chroma)
    s_self = _calc_spread(self_chroma)
    l_stored = _calc_level_dev(stored_levels)
    l_self = _calc_level_dev(self_levels)

    def _rank(val: float | None, ok_thr: float, warn_thr: float) -> int:
        if val is None or val < ok_thr:
            return 0
        if val < warn_thr:
            return 1
        return 2

    r_worst = max(_rank(s_stored, SPREAD_OK, SPREAD_WARN), _rank(l_stored, LEVEL_OK, LEVEL_WARN))
    verdict = ("OK", "WARN", "DRIFT")[r_worst]

    recommendations = {
        "OK": "Camera operating temperatures and white balance match the active preset.",
        "WARN": (
            "Moderate camera color/level shift detected (operating temperature / paper difference). "
            "Review the Default vs. Auto-Adjusted preview or recalibrate."
        ),
        "DRIFT": (
            "Major camera color shift detected across the 5 CIS modules! "
            "Recommended: apply Auto-Adjust from margins or run the Official Calibration Sheet Wizard."
        ),
    }

    return {
        "verdict": verdict,
        "preset_id": preset.id,
        "preset_name": preset.name,
        "white_groups": ngood,
        "stored_spread": s_stored,
        "self_spread": s_self,
        "stored_level_dev_max": l_stored,
        "self_level_dev_max": l_self,
        "stored_chroma": stored_chroma,
        "self_chroma": self_chroma,
        "camera_gains": {name: d["gain_r_range"] for name, _, d in auto_cams},
        "thresholds": {
            "spread_ok": SPREAD_OK,
            "spread_warn": SPREAD_WARN,
            "level_ok": LEVEL_OK,
            "level_warn": LEVEL_WARN,
        },
        "recommendation": recommendations[verdict],
    }


def promote_margin_calibration_to_preset(
    raster: np.ndarray,
    rows: int,
    base_preset: CalibrationPreset,
    *,
    new_id: str | None = None,
    new_name: str | None = None,
    source_label: str = "margin-self-calibration",
) -> CalibrationPreset:
    """Create or update a preset by promoting the current scan's self-fitted ``W`` and ``B``."""
    gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))
    auto_cams, k_auto, _ = apply_camera_calibration(
        raster, rows, base_preset, auto_adjust_margins=True, gaps=gaps
    )
    W_new: dict[str, dict[str, list[float]]] = {}
    B_new: dict[str, list[float]] = {}
    for name, _img, diag in auto_cams:
        eff_w = smooth_group_profile(diag["_stored_w"] * diag["_gain_prof"])
        W_new[name] = {
            "R": [round(float(x), 2) for x in eff_w[:, 0]],
            "G": [round(float(x), 2) for x in eff_w[:, 1]],
            "B": [round(float(x), 2) for x in eff_w[:, 2]],
        }
        B_new[name] = list(diag["black_rgb"])

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    return CalibrationPreset(
        id=new_id or base_preset.id,
        name=new_name or base_preset.name,
        description=base_preset.description,
        serial=base_preset.serial,
        K=round(float(k_auto), 1),
        W=W_new,
        B=B_new,
        B_profile=dict(base_preset.B_profile),
        it8=base_preset.it8,
        created=base_preset.created or now,
        updated=now,
        source=source_label,
        is_builtin=False,
    )


# ---------------------------------------------------------------------------
# Official Contex Calibration Sheet Wizard (White Flat-Field & IT8 264-Patch Fit)
# ---------------------------------------------------------------------------

def it8_reference_source(path: str | Path | None = None) -> tuple[str, bool]:
    """Return ``(description, is_exact)`` for the IT8 reference ``load_it8_reference`` uses.

    The exact ``R170829.it8`` table ships only inside the git-ignored vendor installer
    extraction; a checkout without it falls back to an approximate sRGB-derived table,
    which fits noticeably worse and can exceed the dE76 acceptance gate on its own.
    """
    if path is None:
        path = REPO_ROOT / "extracted" / "wide_payload" / "TargetDir_-_IT8" / "R170829.it8"
    p = Path(path)
    if p.exists():
        return f"vendor:{p.name}", True
    csv_path = REPO_ROOT / "ctx" / "data" / "colour-patches.csv"
    if csv_path.exists():
        return "approximate:ctx/data/colour-patches.csv", False
    return f"missing:{p}", False


def load_it8_reference(path: str | Path | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Load ``(XYZ[288,3] in 0..1, Lab[288,3])`` from ``R170829.it8`` or fallback to
    ``docs/colour-patches.csv`` if the extracted installer folder is absent."""
    if path is None:
        path = REPO_ROOT / "extracted" / "wide_payload" / "TargetDir_-_IT8" / "R170829.it8"
    p = Path(path)
    if p.exists():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        fmt: list[str] = []
        on = False
        for ln in lines:
            if ln.strip() == "BEGIN_DATA_FORMAT":
                on = True
                continue
            if ln.strip() == "END_DATA_FORMAT":
                on = False
                continue
            if on:
                fmt = ln.split()
        rows_data: list[list[float]] = []
        on = False
        for ln in lines:
            if ln.strip() == "BEGIN_DATA":
                on = True
                continue
            if ln.strip() == "END_DATA":
                on = False
                continue
            if on and ln.strip() and not ln.strip().startswith("#"):
                parts = ln.split()
                if len(parts) == len(fmt):
                    try:
                        rows_data.append([float(x) for x in parts[1:]])
                    except ValueError:
                        pass
        ref = np.asarray(rows_data, dtype=np.float64)
        return ref[:, 0:3] / 100.0, ref[:, 3:6]

    # Fallback: derive approximate XYZ/Lab from docs/colour-patches.csv vendor TIFF sRGB
    csv_path = REPO_ROOT / "ctx" / "data" / "colour-patches.csv"
    if csv_path.exists():
        lines = csv_path.read_text(encoding="utf-8").splitlines()[1:]
        srgb_list = []
        for ln in lines:
            parts = ln.split(",")
            if len(parts) >= 7:
                srgb_list.append([float(parts[4]), float(parts[5]), float(parts[6])])
        srgb = np.asarray(srgb_list, dtype=np.float64) / 255.0
        lin = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
        inv_msrgb = np.linalg.inv(MSRGB.astype(np.float64))
        xyz = lin @ inv_msrgb.T
        lab = xyz_to_lab(xyz)
        return xyz, lab

    raise FileNotFoundError(f"IT8 reference file not found at {p}")


def sheet_geometry_for_dpi(dpi: int) -> dict[str, tuple[int, int] | int]:
    """Return the official-sheet geometry scaled from its 300-dpi reference."""
    scale = max(0.5, float(dpi) / 300.0)
    def s(v: float) -> int:
        return int(round(float(v) * scale))
    return {
        "dpi": int(dpi),
        "scale": scale,
        "white_band": (s(SHEET_WHITE_BAND[0]), s(SHEET_WHITE_BAND[1])),
        "black_band": (s(SHEET_BLACK_BAND[0]), s(SHEET_BLACK_BAND[1])),
        "mosaic": (s(SHEET_MOSAIC[0]), s(SHEET_MOSAIC[1]), s(SHEET_MOSAIC[2]), s(SHEET_MOSAIC[3])),
        "min_rows": s(2200),
    }


#: Mean dE76 above which an IT8 fit is treated as mis-registered and not applied.
IT8_ACCEPT_MAX_DE76 = 8.0
#: IT8 sample windows narrower than this are rejected rather than fitted.
IT8_MIN_WINDOW_PX = 100
#: Median RGB chroma spread below which the sampled area is not the IT8 mosaic.
IT8_MIN_CHROMA_SPREAD = 800.0
#: Interior padding of each sampled patch cell (fraction of a cell per side).
IT8_SAMPLE_PAD = 0.30


def it8_sample_patches(
    comp: np.ndarray,
    geom: dict[str, Any],
    dx: int = 0,
    dy: int = 0,
) -> np.ndarray | None:
    """Return the ``264 x 3`` median RGB of the 22x12 IT8 cells for one window offset.

    ``dx``/``dy`` are pixel offsets in the composite's own resolution; the caller is
    responsible for converting 300-dpi offsets (see ``sheet_geometry_for_dpi``).
    ``None`` means the offset pushed the sampling window off the composite.
    """
    x0, x1, y0, y1 = geom["mosaic"]
    x0 = max(0, int(x0) + int(dx))
    x1 = min(comp.shape[1] - 1, int(x1) + int(dx))
    y0 = max(0, int(y0) + int(dy))
    y1 = min(comp.shape[0] - 1, int(y1) + int(dy))
    if x1 - x0 < IT8_MIN_WINDOW_PX or y1 - y0 < IT8_MIN_WINDOW_PX:
        return None
    out = np.empty((IT8_NY * IT8_NX, 3), dtype=np.float64)
    k = 0
    for r in range(IT8_NY):
        ya = int(y0 + (r + IT8_SAMPLE_PAD) * (y1 - y0 + 1) / IT8_NY)
        yb = int(y0 + (r + 1 - IT8_SAMPLE_PAD) * (y1 - y0 + 1) / IT8_NY)
        yb = max(yb, ya + 1)
        for c in range(IT8_NX):
            xa = int(x0 + (c + IT8_SAMPLE_PAD) * (x1 - x0 + 1) / IT8_NX)
            xb = int(x0 + (c + 1 - IT8_SAMPLE_PAD) * (x1 - x0 + 1) / IT8_NX)
            xb = max(xb, xa + 1)
            out[k] = np.median(comp[ya:yb, xa:xb, :].reshape(-1, 3), axis=0)
            k += 1
    return out


def it8_fit_patches(
    rgb_patches: np.ndarray,
    xyz_ref: np.ndarray,
    lab_ref: np.ndarray,
) -> dict[str, Any]:
    """Fit ``gamma`` + 3x3 matrix + offset to the IT8 reference and return the error stats."""
    n_patches = IT8_NX * IT8_NY
    best = None
    for g_cand in np.arange(0.75, 2.25, 0.05):
        v = (rgb_patches / 65535.0) ** float(g_cand)
        a = np.hstack([v, np.ones((len(v), 1))])
        sol, *_ = np.linalg.lstsq(a, xyz_ref[:n_patches], rcond=None)
        lab_fit = xyz_to_lab(a @ sol)
        err_lab = np.abs(lab_fit - lab_ref[:n_patches]).mean(axis=0)
        if best is None or err_lab.mean() < best[0].mean():
            best = (err_lab, float(g_cand), sol, lab_fit)
    assert best is not None
    _err_lab, gamma, sol, lab_fit = best
    d_lab = lab_fit - lab_ref[:n_patches]
    d_e = np.sqrt((d_lab ** 2).sum(axis=1))
    chroma = rgb_patches.max(axis=1) - rgb_patches.min(axis=1)
    return {
        "gamma": gamma,
        "sol": sol,
        "dE76_mean": float(d_e.mean()),
        "dE76_median": float(np.median(d_e)),
        "patches_under_3": int((d_e < 3.0).sum()),
        "patches_total": int(len(d_e)),
        "chroma_spread": float(np.median(chroma)),
    }


def search_it8_offset(
    comp: np.ndarray,
    geom: dict[str, Any],
    xyz_ref: np.ndarray,
    lab_ref: np.ndarray,
    *,
    span: int = 280,
    step: int = 40,
    dy_span: int = 60,
    dy_step: int = 30,
    refine_passes: tuple[tuple[int, int], ...] = ((20, 8), (6, 2)),
) -> dict[str, Any] | None:
    """Find the window offset whose 22x12 IT8 fit has the lowest mean dE76.

    A hand-placed calibration sheet lands anywhere within a few tenths of an inch
    laterally, so a single hard-coded X origin (``SHEET_MOSAIC``) cannot centre every
    scan.  Sweeping the window and scoring the real fit is cheap next to the composite
    build and cannot be fooled by the periodic patch grid the way an image-only grid
    detector can: a one-cell shift mismatches the reference patch colours outright.

    Returns offsets in composite pixels, or ``None`` when no window produced a usable
    IT8 mosaic.
    """
    cache: dict[tuple[int, int], dict[str, Any] | None] = {}

    def evaluate(dx: int, dy: int) -> dict[str, Any] | None:
        key = (int(dx), int(dy))
        if key not in cache:
            rgb = it8_sample_patches(comp, geom, dx, dy)
            if rgb is None or float(np.median(rgb.max(axis=1) - rgb.min(axis=1))) < IT8_MIN_CHROMA_SPREAD:
                cache[key] = None
            else:
                cache[key] = it8_fit_patches(rgb, xyz_ref, lab_ref)
        return cache[key]

    def scan(center: tuple[int, int] | None, radius: int, radius_y: int, stride: int) -> tuple[int, int] | None:
        if center is None:
            # The nominal (0, 0) window must always be a candidate: it is the most
            # likely answer, and a stride that skips it can leave the search stuck
            # in a neighbouring local minimum.
            dxs = sorted(set(range(-span, span + 1, max(1, stride))) | {0})
            dys = sorted(set(range(-dy_span, dy_span + 1, max(1, stride))) | {0})
        else:
            dxs = range(center[0] - radius, center[0] + radius + 1, max(1, stride))
            dys = range(center[1] - radius_y, center[1] + radius_y + 1, max(1, stride))
        best: tuple[int, int] | None = None
        best_de = float("inf")
        for dx in dxs:
            for dy in dys:
                fit = evaluate(dx, dy)
                if fit is not None and fit["dE76_mean"] < best_de:
                    best_de = float(fit["dE76_mean"])
                    best = (dx, dy)
        return best

    best = scan(None, 0, 0, step)
    if best is None:
        return None
    for radius, stride in refine_passes:
        refined = scan(best, radius, radius, stride)
        if refined is not None:
            best = refined
    final = evaluate(*best)
    assert final is not None
    unity = evaluate(0, 0)
    return {
        "dx": int(best[0]),
        "dy": int(best[1]),
        "dE76_mean": round(float(final["dE76_mean"]), 2),
        "dE76_median": round(float(final["dE76_median"]), 2),
        "gamma": final["gamma"],
        "patches_under_3": final["patches_under_3"],
        "patches_total": final["patches_total"],
        "dE76_mean_at_zero": None if unity is None else round(float(unity["dE76_mean"]), 2),
        "evaluated_offsets": len(cache),
    }


def calibrate_from_official_sheet(
    raster: np.ndarray,
    rows: int,
    base_preset: CalibrationPreset,
    *,
    sheet_mode: str = "it8_full",
    new_id: str | None = None,
    new_name: str | None = None,
    it8_path: str | Path | None = None,
    dpi: int = 300,
    it8_dx_300px: int = 0,
    it8_dy_300px: int = 0,
    auto_align: bool = False,
) -> tuple[CalibrationPreset, dict[str, Any]]:
    """Calibrate from a scan of the Contex Official Calibration Sheet (``p/n 6779G014``).

    Supports two modes:
      - ``"white_flatfield"``: Fits full-width ``W`` and optical black ``B`` across
        all 5 cameras from a white reference scan.
      - ``"it8_full"``: Matches Cameras 1..5 using the sheet's white & black reference
        bands and fits ``gamma``, 3x3 ``matrix``, and ``offset`` against the 264
        patches of the ``R170829`` IT8.7/2 target (``colourcal.py``).
    """
    gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))
    pmeta = piece_matrix(gaps, raster.shape[1])
    raw_cams = raw_camera_images(raster, pmeta, CAMERAS)
    live_blacks = extract_black_levels(raster, pmeta, CAMERAS)

    W_new: dict[str, dict[str, list[float]]] = {}
    B_new: dict[str, list[float]] = {}
    B_profile_new: dict[str, dict[str, list[float]]] = dict(base_preset.B_profile)
    k_vals: list[float] = []

    geom = sheet_geometry_for_dpi(dpi)
    wb0, wb1 = geom["white_band"]  # type: ignore[misc]
    bb0, bb1 = geom["black_band"]  # type: ignore[misc]
    has_sheet_bands = sheet_mode == "it8_full" and rows > bb1

    if not has_sheet_bands:
        for name, img in raw_cams:
            prof = smooth_group_profile(camera_white_profile(img, rows))
            W_new[name] = {
                "R": [round(float(x), 2) for x in prof[:, 0]],
                "G": [round(float(x), 2) for x in prof[:, 1]],
                "B": [round(float(x), 2) for x in prof[:, 2]],
            }
            B_new[name] = live_blacks.get(name, [0.0, 0.0, 0.0])
            k_vals.append(float(np.median(prof)))

    K_new = float(np.median(k_vals)) if k_vals else float(base_preset.K or DEFAULT_K)
    it8_new = It8Transform(
        enabled=base_preset.it8.enabled,
        gamma=base_preset.it8.gamma,
        matrix=list(base_preset.it8.matrix),
        offset=list(base_preset.it8.offset),
        dE76_mean=base_preset.it8.dE76_mean,
        dE76_median=base_preset.it8.dE76_median,
    )

    report: dict[str, Any] = {
        "sheet_mode": sheet_mode,
        "rows": int(rows),
        "dpi": int(dpi),
        "sheet_scale": round(float(geom["scale"]), 3),
        "it8_dx_300px": int(it8_dx_300px),
        "it8_dy_300px": int(it8_dy_300px),
        "K": round(K_new, 1),
        "cameras_fitted": 5,
        "warnings": [],
    }
    if has_sheet_bands and raw_cams:
        try:
            white_med = float(np.median([
                np.median(img[wb0:wb1, :, :]) for _, img in raw_cams
            ]))
            black_med = float(np.median([
                np.median(img[bb0:bb1, :, :]) for _, img in raw_cams
            ]))
            report["white_band_median"] = round(white_med, 1)
            report["black_band_median"] = round(black_med, 1)
            bands_ok = bool(
                white_med > 1000.0
                and white_med < 65535.0
                and black_med < 0.8 * white_med
            )
            report["sheet_bands_detected"] = bands_ok
            if not bands_ok:
                report["warnings"].append(
                    "White/black reference bands were not detected at the expected "
                    f"{dpi}-dpi positions; check sheet placement, orientation and scan height."
                )
        except Exception:
            report["sheet_bands_detected"] = False
            report["warnings"].append("Could not validate the sheet reference bands.")
    else:
        report["sheet_bands_detected"] = False
        if sheet_mode == "it8_full":
            report["warnings"].append(
                f"Scan is shorter than the full sheet ({rows} rows); need > {bb1} rows at {dpi} dpi. "
                "The IT8 matrix was retained rather than re-fitted."
            )

    if sheet_mode == "it8_full" and rows >= int(geom["min_rows"]):
        if rows > bb1:
            for idx, (name, img) in enumerate(raw_cams):
                w_band = np.median(img[wb0:wb1, :, :].astype(np.float64), axis=0)
                b_band = np.median(img[bb0:bb1, :, :].astype(np.float64), axis=0)
                ng = max(1, (img.shape[1] + GROUP_PX - 1) // GROUP_PX)
                w_grp = np.full((ng, 3), np.nan, dtype=np.float64)
                b_grp = np.full((ng, 3), np.nan, dtype=np.float64)
                for g in range(ng):
                    a = g * GROUP_PX
                    b = min(img.shape[1], a + GROUP_PX)
                    if idx == 0 and a < 416:
                        continue
                    if idx == 4 and b > 2208:
                        continue
                    w_grp[g] = np.median(w_band[a:b], axis=0)
                    b_grp[g] = np.median(b_band[a:b], axis=0)
                w_grp = smooth_group_profile(w_grp)
                b_grp = smooth_group_profile(b_grp)
                W_new[name] = {
                    "R": [round(float(x), 2) for x in w_grp[:, 0]],
                    "G": [round(float(x), 2) for x in w_grp[:, 1]],
                    "B": [round(float(x), 2) for x in w_grp[:, 2]],
                }
                B_profile_new[name] = {
                    "R": [round(float(x), 2) for x in b_grp[:, 0]],
                    "G": [round(float(x), 2) for x in b_grp[:, 1]],
                    "B": [round(float(x), 2) for x in b_grp[:, 2]],
                }
                B_new[name] = [round(float(x), 1) for x in np.median(b_grp, axis=0)]
            K_new = float(base_preset.K or DEFAULT_K)
            report["K"] = round(K_new, 1)

        # Apply the new flat-field to assemble the composite and sample the 264 IT8 patches
        temp_preset = CalibrationPreset(
            id="temp",
            name="temp",
            description="",
            serial=base_preset.serial,
            K=K_new,
            W=W_new,
            B=B_new,
            B_profile=B_profile_new,
            it8=it8_new,
        )
        corr_cams, _, _ = apply_camera_calibration(
            raster, rows, temp_preset, auto_adjust_margins=False, gaps=gaps
        )
        comp = np.concatenate([im for _, im, _ in corr_cams], axis=1)
        xyz_ref, lab_ref = load_it8_reference(it8_path)
        ref_source, ref_exact = it8_reference_source(it8_path)
        report["it8_reference"] = ref_source
        scale = float(geom["scale"])
        if auto_align:
            found = search_it8_offset(comp, geom, xyz_ref, lab_ref)
            if found is not None:
                it8_dx_300px = int(round(found["dx"] / scale))
                it8_dy_300px = int(round(found["dy"] / scale))
                report["it8_auto_align"] = {**found, "auto": True}
                report["it8_dx_300px"] = it8_dx_300px
                report["it8_dy_300px"] = it8_dy_300px
                report["warnings"].append(
                    f"IT8 grid auto-aligned to X {it8_dx_300px} / Y {it8_dy_300px} px (300 dpi): "
                    f"mean dE76 {found['dE76_mean']} at that offset vs "
                    f"{found['dE76_mean_at_zero']} at the nominal one."
                )
            else:
                report["it8_auto_align"] = {"auto": True, "found": False}
                report["warnings"].append(
                    "IT8 auto-align could not find the patch mosaic; used the X/Y offsets as given."
                )
        dx = int(round(float(it8_dx_300px) * scale))
        dy = int(round(float(it8_dy_300px) * scale))
        rgb_patches = it8_sample_patches(comp, geom, dx, dy)
        if rgb_patches is None and (dx or dy):
            report["warnings"].append(
                "IT8 sample area too small after the X/Y offset; offsets ignored."
            )
            rgb_patches = it8_sample_patches(comp, geom, 0, 0)
        if rgb_patches is not None:
            fit = it8_fit_patches(rgb_patches, xyz_ref, lab_ref)
            chroma_spread = fit["chroma_spread"]
            if chroma_spread >= IT8_MIN_CHROMA_SPREAD:
                g_best = fit["gamma"]
                sol_best = fit["sol"]
                dE_mean = fit["dE76_mean"]
                dE_median = fit["dE76_median"]
                report["dE76_mean"] = round(dE_mean, 2)
                report["dE76_median"] = round(dE_median, 2)
                if dE_mean > IT8_ACCEPT_MAX_DE76 or not np.isfinite(dE_mean):
                    # A mis-oriented/rotated IT8 mosaic can still produce a
                    # plausible-looking fit with huge dE.  Keep the known-good
                    # matrix from the base preset instead of applying garbage.
                    report["it8_fitted"] = False
                    report["it8_note"] = (
                        f"IT8 fit rejected: mean dE76 {dE_mean:.2f} > {IT8_ACCEPT_MAX_DE76:.1f}. "
                        "Retained the base preset's IT8 matrix; the sheet's W/B "
                        "bands were still applied."
                    )
                    if not ref_exact:
                        report["it8_note"] += (
                            " The exact R170829.it8 reference table was not found; the "
                            "approximate ctx/data/colour-patches.csv fallback is in use, "
                            "which fits worse."
                        )
                    report["warnings"].append(report["it8_note"])
                else:
                    it8_new = It8Transform(
                        enabled=True,
                        gamma=round(g_best, 3),
                        matrix=sol_best[:3].T.tolist(),
                        offset=sol_best[3].tolist(),
                        dE76_mean=round(dE_mean, 2),
                        dE76_median=round(dE_median, 2),
                    )
                    report["it8_fitted"] = True
                    report["gamma"] = it8_new.gamma
                    report["patches_under_3"] = fit["patches_under_3"]
                    report["patches_total"] = fit["patches_total"]
            else:
                report["it8_fitted"] = False
                report["it8_note"] = (
                    "IT8 patch mosaic not detected at expected coordinates; "
                    "updated per-camera flat-field W and black pedestal B while retaining stored IT8 matrix."
                )
        else:
            report["it8_fitted"] = False
            report["it8_note"] = (
                "Scan height shorter than full IT8 sheet; updated per-camera flat-field W and B."
            )

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not new_id:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        new_id = f"official-sheet-{stamp}"
        new_name = new_name or f"Official Sheet {stamp}"
    new_preset = CalibrationPreset(
        id=new_id,
        name=new_name or new_id,
        description=base_preset.description,
        serial=base_preset.serial,
        K=round(K_new, 1),
        W=W_new,
        B=B_new,
        B_profile=B_profile_new,
        it8=it8_new,
        created=base_preset.created or now,
        updated=now,
        source=f"official-sheet-{sheet_mode}",
        is_builtin=base_preset.is_builtin if not new_id else False,
    )
    return new_preset, report
