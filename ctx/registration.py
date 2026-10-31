"""Sub-pixel registration of the CIS red/green/blue sensor lines.

The Contex HD Ultra CIS reads red, green and blue on physically separate
sensor lines, so the three channels of a capture are displaced along the paper
travel direction.  Paper motion turns that optical spacing into a constant row
offset between channels, which is *not* a stitching or seam defect: it is
present in every capture, on all five cameras, before any colour processing.

Measured on this machine (native raster, ``analysis/fresh/*.rec``):

    R - B = +6 px,  G - B = +3 px      at 300 dpi, identical for cam1..cam5

Because the offset is a fixed time delay converted to pixels by paper speed,
it grows with resolution (a 400 dpi scan measures roughly 1.5 - 2x the 300 dpi
value), so it is estimated per scan rather than hardcoded.

Left uncorrected, the colour matrix's negative off-diagonal terms turn the
few-pixel displacement into saturated fringing on every horizontal edge --
blue/red/yellow bands on a black line.  Large flat areas (such as IT8 patches)
hide it completely, which is why it only becomes obvious on linework.

Public API
----------
``estimate_offsets``     measure ``(dy_red, dy_green)`` for one camera image
``apply_offsets``        shift the red/green rows onto blue (reference)
``register_cameras``     estimate across cameras, apply one consensus shift
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor

import numpy as np

#: Largest believable row offset between channels, in pixels.  Anything beyond
#: this is a mis-estimate (no horizontal detail, or a torn stream) and is
#: rejected rather than smearing the image.
#:
#: The offset is a fixed optical distance converted to rows by the line rate,
#: so it grows with DPI: ~6 px at 300 dpi, and roughly 24-30 px at 1200 dpi.
#: The search window has to clear the highest supported resolution, otherwise
#: the peak saturates at the boundary and reports a plausible-looking lie.
MAX_LAG_PX = 64

#: Minimum normalised correlation for an offset estimate to be trusted.
MIN_QUALITY = 0.30

#: Correlation a camera must reach to vote on the consensus offset.
STRONG_QUALITY = 0.85

#: A camera must also carry at least this fraction of the strongest camera's
#: row-gradient energy to vote.  Without it, blank backing would be counted as
#: a confident "no offset" measurement.
CONTENT_FRACTION = 0.5

#: Sub-pixel interpolation needs a clean peak; below this the curve is flat.
_MIN_PEAK_GAP = 1e-9

#: Strength of the small-displacement prior applied before peak selection.
_LAG_PRIOR = 0.15


def _row_profiles(img: np.ndarray, x_frac: tuple[float, float] = (0.25, 0.75)) -> np.ndarray:
    """Vertical-gradient profile per channel: ``(rows-1, 3)``, mean-centred.

    Averaging across a central band of columns suppresses sensor noise while
    keeping horizontal edges (the only signal that reveals a row offset).
    """
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"expected (rows, cols, 3) image, got {img.shape}")
    h, w, _ = img.shape
    x0, x1 = int(w * x_frac[0]), int(w * x_frac[1])
    if x1 - x0 < 8:                      # very narrow camera: use everything
        x0, x1 = 0, w
    prof = img[:, x0:x1, :].astype(np.float32).mean(axis=1)
    d = np.diff(prof, axis=0)
    return d - d.mean(axis=0, keepdims=True)


def _corr(a: np.ndarray, b: np.ndarray, lag: int) -> float:
    """Pearson correlation of ``a`` shifted by ``lag`` rows against ``b``.

    ``lag > 0`` compares ``a[lag:]`` with ``b[:-lag]``, so a peak at ``lag``
    means the feature in ``a`` sits ``lag`` rows *below* the same feature in
    ``b``.
    """
    if lag > 0:
        x, y = a[lag:], b[:-lag]
    elif lag < 0:
        x, y = a[:lag], b[-lag:]
    else:
        x, y = a, b
    if len(x) < 16:
        return -1.0
    xs, ys = float(x.std()), float(y.std())
    if xs < 1e-9 or ys < 1e-9:
        return -1.0
    return float(((x - x.mean()) * (y - y.mean())).mean() / (xs * ys))


def _subpixel_peak(scores: np.ndarray, k: int) -> float:
    """Parabolic interpolation of the correlation peak at index ``k``."""
    if k <= 0 or k >= len(scores) - 1:
        return 0.0
    left, mid, right = float(scores[k - 1]), float(scores[k]), float(scores[k + 1])
    if not (np.isfinite(left) and np.isfinite(mid) and np.isfinite(right)):
        return 0.0
    denom = left - 2.0 * mid + right
    if abs(denom) < _MIN_PEAK_GAP:
        return 0.0
    return float(np.clip(0.5 * (left - right) / denom, -1.0, 1.0))


def estimate_offsets(
    img: np.ndarray, max_lag: int = MAX_LAG_PX
) -> dict[str, tuple[float, float]]:
    """Estimate ``{channel: (dy_px, quality)}`` relative to blue.

    ``dy_px`` is how far that channel sits *below* blue; positive values pass
    straight to :func:`apply_offsets`.  ``quality`` is the normalised
    correlation at the peak, in ``[-1, 1]``.
    """
    d = _row_profiles(img)
    lags = np.arange(-max_lag, max_lag + 1)
    # Mild prior toward small displacements.  Periodic content (halftone, graph
    # paper, the IT8 grid) can correlate just as well at a multiple of its period
    # as at the true optical offset, which is always small; this breaks those
    # ties without overriding a genuinely stronger peak.
    prior = 1.0 - _LAG_PRIOR * np.abs(lags) / max(1, max_lag)
    out: dict[str, tuple[float, float]] = {}
    for name, idx in (("red", 0), ("green", 1)):
        scores = np.array([_corr(d[:, idx], d[:, 2], int(lag)) for lag in lags])
        k = int(np.argmax(scores * prior))
        # quality reports the raw correlation, so the guard stays honest
        out[name] = (float(lags[k]) + _subpixel_peak(scores, k), float(scores[k]))
    return out


def apply_offsets(img: np.ndarray, dy_red: float, dy_green: float) -> np.ndarray:
    """Shift red/green rows so they align with blue.

    Output row ``y`` samples input row ``y + dy``, so a channel that sits low
    (positive ``dy``) is moved back up.  Sub-pixel shifts use linear
    interpolation; blue is the reference and is left untouched.
    """
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError(f"expected (rows, cols, 3) image, got {img.shape}")
    h = img.shape[0]
    out = img.copy()
    for ch, dy in ((0, float(dy_red)), (1, float(dy_green))):
        if abs(dy) < 1e-3 or h < 2:
            continue
        src = np.arange(h, dtype=np.float32) + np.float32(dy)
        lo = np.clip(np.floor(src).astype(np.int32), 0, h - 1)
        hi = np.clip(lo + 1, 0, h - 1)
        t = np.clip(src - lo, 0.0, 1.0)[:, None].astype(np.float32)
        col = (
            img[lo, :, ch].astype(np.float32) * (1.0 - t)
            + img[hi, :, ch].astype(np.float32) * t
        )
        out[:, :, ch] = np.clip(np.rint(col), 0, 65535).astype(np.uint16)
    return out


def register_cameras(
    cams: Sequence[tuple[str, np.ndarray]],
    *,
    enabled: bool = True,
    max_lag: int = MAX_LAG_PX,
    min_quality: float = MIN_QUALITY,
    strong_quality: float = STRONG_QUALITY,
) -> tuple[list[tuple[str, np.ndarray]], dict[str, Any]]:
    """Estimate and apply one consensus channel shift across all cameras.

    A single consensual shift is used deliberately: per-camera shifts would break
    the continuity of horizontal lines across the four camera joins, which is a
    worse artifact than the fringe.

    Concentration matters. A narrow original (a letter, a photo) covers only one
    or two cameras; the rest see blank backing and cannot measure anything, so
    their estimates are noise near zero. A plain median over all five cameras is
    then outvoted by the blank cameras and returns ~0 -- which is exactly how the
    correction silently did nothing on letter-sized scans. Only cameras with a
    confidently strong correlation are allowed to vote; if none reach that bar,
    the single best estimate is used, and only if it clears ``min_quality``.

    Returns the (possibly corrected) camera list plus an info dict suitable for
    scan metadata.
    """
    cams = list(cams)
    if not enabled or not cams:
        return cams, {"applied": False, "reason": "disabled"}

    # Multithreaded offset estimation
    names = [c[0] for c in cams]
    raw_ims = [c[1] for c in cams]
    with ThreadPoolExecutor(max_workers=min(len(cams), 8)) as ex:
        est_list = list(ex.map(lambda im: estimate_offsets(im, max_lag=max_lag), raw_ims))
    estimates = dict(zip(names, est_list))
    cam_quality = {
        name: min(e["red"][1], e["green"][1]) for name, e in estimates.items()
    }
    # A camera can only measure the offset if it actually saw the original.
    # Blank backing is not featureless at high dpi: line interpolation leaves a
    # 2-row periodicity that correlates strongly across channels at lag 0, so
    # quality alone marks it as a confident voter for "no offset".  Horizontal
    # gradient energy separates paper from backing cleanly (cam1 ~1000 vs ~80 at
    # 300 dpi; ~550 vs ~170 at 600 dpi), so voters are the cameras with real
    # row-direction content.
    with ThreadPoolExecutor(max_workers=min(len(cams), 8)) as ex:
        grad_list = list(ex.map(lambda im: float(_row_profiles(im).std()), raw_ims))
    grad_energy = dict(zip(names, grad_list))
    peak_energy = max(grad_energy.values()) if grad_energy else 0.0
    with_content = [n for n, g in grad_energy.items() if g >= CONTENT_FRACTION * peak_energy]

    strong = [n for n in with_content if cam_quality[n] >= strong_quality]
    if strong:
        voters = strong
    elif with_content:
        voters = [max(with_content, key=lambda n: cam_quality[n])]
    else:
        voters = []

    if voters:
        dy_red = float(np.median([estimates[n]["red"][0] for n in voters]))
        dy_green = float(np.median([estimates[n]["green"][0] for n in voters]))
        quality = float(min(cam_quality[n] for n in voters))
    else:
        dy_red = dy_green = quality = 0.0

    info: dict[str, Any] = {
        "applied": False,
        "dy_red_px": round(dy_red, 3),
        "dy_green_px": round(dy_green, 3),
        "quality": round(quality, 4),
        "voters": voters,
        "cam_grad_energy": {n: round(g, 1) for n, g in grad_energy.items()},
        "per_camera": {
            name: {
                "red": [round(e["red"][0], 3), round(e["red"][1], 3)],
                "green": [round(e["green"][0], 3), round(e["green"][1], 3)],
            }
            for name, e in estimates.items()
        },
    }

    if not voters:
        info["reason"] = f"no camera reached quality {min_quality}"
        return cams, info
    if quality < min_quality:
        info["reason"] = f"weak correlation ({quality:.2f} < {min_quality})"
        return cams, info
    # A peak sitting on the search boundary means the true offset is outside the
    # window; the number is a truncated artefact, not a measurement.
    if max(abs(dy_red), abs(dy_green)) >= max_lag - 0.5:
        info["reason"] = "estimate at search boundary (offset exceeds max_lag)"
        return cams, info
    if abs(dy_red) < 0.05 and abs(dy_green) < 0.05:
        info["applied"] = True
        info["reason"] = "already aligned"
        return cams, info

    with ThreadPoolExecutor(max_workers=min(len(cams), 8)) as ex:
        corrected = list(ex.map(lambda c: (c[0], apply_offsets(c[1], dy_red, dy_green)), cams))
    info["applied"] = True
    return corrected, info
