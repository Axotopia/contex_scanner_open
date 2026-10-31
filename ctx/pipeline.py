"""End-to-end 16-bit 5-camera stitching, dual-mode preview rendering, and export.

Connects:
  - Camera reordering & flat-field / IT8 calibration (``ctx.calibration``)
  - Seam stitching & 42.000-in window cropping (``STITCH-REPORT.md`` §2-§5)
  - Generative AI & local restoration filters (``ctx.ai``)
  - Multi-format export (TIFF, JPG, PDF in Color or Grayscale) & Debug artifact saving
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import struct
import time
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from .ai import (
    EnhancementOptions,
    blend_seam_color_artwork_u16,
    blend_seam_color_variation_u16,
    heal_seams_u16,
    measure_seam_step_u16,
    run_enhancement_pipeline,
)
from .calibration import (
    CAMERAS,
    DEFAULT_GAPS,
    DEFAULT_K,
    DEFAULT_SEAM_DY_PX,
    DEFAULT_SEAM_OVERLAPS_PX,
    GROUP_PX,
    IT8_NX,
    IT8_NY,
    RAW_WIDTH_PX,
    SHEET_BLACK_BAND,
    SHEET_MOSAIC,
    SHEET_WHITE_BAND,
    STRIDE_16BIT_5CAM,
    WINDOW_PX_300DPI,
    CalibrationPreset,
    analyze_drift,
    apply_camera_calibration,
    apply_it8_transform_u16,
    fill_profile_columns,
    load_it8_reference,
    module_gaps,
    piece_matrix,
)

Image.MAX_IMAGE_PIXELS = None

#: Resolutions advertised by the Contex HD Ultra i4250s on VPD page 0xC3 (SPECS.md §2.6).
SUPPORTED_RESOLUTIONS_DPI: tuple[int, ...] = (100, 200, 300, 400, 600, 1200)

#: Cross-feed width of the scan bed, used to clamp landscape requests.
MAX_SCAN_WIDTH_IN = WINDOW_PX_300DPI / 300.0

#: Standard large-format paper size presets (ARCH, ISO, ANSI) + Auto-Detect & Custom.
#: ``width_in`` is the short edge (portrait cross-feed width); ``long_in`` is the
#: long edge, used when the user selects landscape orientation.
PAPER_SIZE_PRESETS: dict[str, dict[str, Any]] = {
    "auto": {"id": "auto", "label": "Auto-Detect Width (Nextimage DetectSize)", "width_in": 42.0, "series": "Auto"},
    "full_42": {"id": "full_42", "label": "Full Sensor Width (42.0\" / 1067 mm)", "width_in": 42.0, "long_in": 42.0, "series": "Max"},
    "arch_e": {"id": "arch_e", "label": "ARCH E (36.0\" × 48.0\")", "width_in": 36.0, "long_in": 48.0, "series": "ARCH"},
    "arch_e1": {"id": "arch_e1", "label": "ARCH E1 (30.0\" × 42.0\")", "width_in": 30.0, "long_in": 42.0, "series": "ARCH"},
    "arch_d": {"id": "arch_d", "label": "ARCH D (24.0\" × 36.0\")", "width_in": 24.0, "long_in": 36.0, "series": "ARCH"},
    "arch_c": {"id": "arch_c", "label": "ARCH C (18.0\" × 24.0\")", "width_in": 18.0, "long_in": 24.0, "series": "ARCH"},
    "iso_a0": {"id": "iso_a0", "label": "ISO A0 (33.11\" × 46.81\" / 841 × 1189 mm)", "width_in": 33.11, "long_in": 46.81, "series": "ISO"},
    "iso_a1": {"id": "iso_a1", "label": "ISO A1 (23.39\" × 33.11\" / 594 × 841 mm)", "width_in": 23.39, "long_in": 33.11, "series": "ISO"},
    "iso_a2": {"id": "iso_a2", "label": "ISO A2 (16.54\" × 23.39\" / 420 × 594 mm)", "width_in": 16.54, "long_in": 23.39, "series": "ISO"},
    "ansi_e": {"id": "ansi_e", "label": "ANSI E (34.0\" × 44.0\")", "width_in": 34.0, "long_in": 44.0, "series": "ANSI"},
    "ansi_d": {"id": "ansi_d", "label": "ANSI D (22.0\" × 34.0\")", "width_in": 22.0, "long_in": 34.0, "series": "ANSI"},
    "ansi_c": {"id": "ansi_c", "label": "ANSI C (17.0\" × 22.0\")", "width_in": 17.0, "long_in": 22.0, "series": "ANSI"},
    "ansi_b": {"id": "ansi_b", "label": "ANSI B / Ledger (11.0\" × 17.0\")", "width_in": 11.0, "long_in": 17.0, "series": "ANSI"},
    "ansi_a": {"id": "ansi_a", "label": "ANSI A / Letter (8.5\" × 11.0\")", "width_in": 8.5, "long_in": 11.0, "series": "ANSI"},
    "custom": {"id": "custom", "label": "Custom Width (Inches)", "width_in": 42.0, "series": "Custom"},
}


def resolve_paper_width(
    paper_size: str,
    orientation: str = "portrait",
    width_inches: float = 42.0,
) -> dict[str, Any]:
    """Resolve the cross-feed scan width for a paper preset and orientation.

    Portrait uses the short edge, landscape the long edge.  The bed is 42 in, so
    a landscape request wider than that (ISO A0, ANSI E, ARCH E) is clamped and
    flagged in ``clamped`` rather than rejected.  ``auto`` and ``custom`` do not
    use the long edge: auto detects the width after the scan, and custom takes
    the operator's width as the cross-feed width.
    """
    p_key = (paper_size or "full_42").lower().strip()
    orient = (orientation or "portrait").lower().strip()
    if orient not in ("portrait", "landscape"):
        raise ValueError(f"Unknown orientation {orientation!r}; expected 'portrait' or 'landscape'")

    out: dict[str, Any] = {
        "paper_size": p_key,
        "orientation": orient,
        "orientation_applied": False,
        "clamped": False,
        "effective_width_in": MAX_SCAN_WIDTH_IN,
    }
    if p_key == "auto":
        return out
    info = PAPER_SIZE_PRESETS.get(p_key)
    if info is None or p_key == "custom":
        out["paper_size"] = "custom"
        out["effective_width_in"] = min(MAX_SCAN_WIDTH_IN, max(4.0, float(width_inches)))
        return out

    short_in = float(info["width_in"])
    long_in = float(info.get("long_in") or short_in)
    width = short_in
    if orient == "landscape" and long_in > short_in:
        width = long_in
        out["orientation_applied"] = True
    if width > MAX_SCAN_WIDTH_IN:
        width = MAX_SCAN_WIDTH_IN
        out["clamped"] = True
    out["effective_width_in"] = width
    return out


def detect_auto_paper_width_u16(
    comp_u16: np.ndarray,
    base_dpi: int = 300,
    max_window_px: int = WINDOW_PX_300DPI,
    snap_tolerance_in: float = 0.45,
) -> dict[str, Any]:
    """Replicate Nextimage's ``DetectSize`` / ``AutoSizeDetect Filter`` on a stitched
    16-bit composite scan.

    How Nextimage computes Auto Width on the HD Ultra i4250s:
      1. VPD page ``0xC1`` byte 11 has ``centerLoad = 0`` (side/right-aligned feed at Cam 1).
      2. Hardware optical paper sensors (``READ 0x92`` -> ``propPaperSensorInfo``) report
         coarse left/right presence across the 5 camera modules before ``SET WINDOW``.
      3. The scanner streams full camera strips (``2,728 px`` per active camera), and
         Nextimage's ``DetectSize`` (``StateAnalysingScan``) profiles horizontal luminance
         and vertical edge contrast against the dark backing roller/platen to find the
         exact paper edge and snap to standard ISO/ARCH/ANSI widths.
    """
    usable_w = min(int(comp_u16.shape[1]), max_window_px)
    sub = comp_u16[:, :usable_w, :]
    # 90th percentile luminance per column + vertical gradient energy
    lum = sub.mean(axis=2, dtype=np.float32)
    col_p90 = np.percentile(lum, 90, axis=0)
    if lum.shape[0] > 4:
        col_grad = np.abs(np.diff(lum[::2, :], axis=0)).mean(axis=0)
    else:
        col_grad = np.zeros(usable_w, dtype=np.float32)

    # Smooth over 16 columns (~1.3 mm) to ignore isolated dust specks on the platen
    k = 16
    if usable_w > k:
        kernel = np.ones(k, dtype=np.float32) / k
        col_p90_s = np.convolve(col_p90, kernel, mode="same")
        col_grad_s = np.convolve(col_grad, kernel, mode="same")
    else:
        col_p90_s = col_p90
        col_grad_s = col_grad

    # Media is present where 16-bit black-subtracted luminance exceeds dark platen (~4500)
    # or where vertical texture gradient exceeds platen noise (~350).
    is_media = (col_p90_s > 4500.0) | (col_grad_s > 350.0)
    media_cols = np.where(is_media)[0]

    if len(media_cols) < int(base_dpi * 2.0):
        # Fallback to full 42.0 in if media cannot be distinguished
        return {
            "detected_px": usable_w,
            "detected_width_inches": round(usable_w / float(base_dpi), 3),
            "snapped_width_inches": round(usable_w / float(base_dpi), 2),
            "matched_preset_id": "full_42",
            "matched_label": PAPER_SIZE_PRESETS["full_42"]["label"],
        }

    right_edge_px = int(media_cols[-1]) + 1
    raw_in = right_edge_px / float(base_dpi)

    # Check if raw_in is within snap_tolerance_in of a standard ARCH / ISO / ANSI width
    best_id = "custom"
    best_label = f"Auto-Detected ({raw_in:.2f}\")"
    snapped_in = round(raw_in, 2)
    best_diff = snap_tolerance_in

    for pid, pinfo in PAPER_SIZE_PRESETS.items():
        if pid in ("auto", "custom"):
            continue
        w_std = float(pinfo["width_in"])
        diff = abs(raw_in - w_std)
        if diff <= best_diff:
            best_diff = diff
            best_id = pid
            best_label = pinfo["label"]
            snapped_in = w_std

    final_px = min(usable_w, max(int(base_dpi * 4.0), int(round(snapped_in * base_dpi))))
    return {
        "detected_px": final_px,
        "raw_detected_px": right_edge_px,
        "detected_width_inches": round(raw_in, 3),
        "snapped_width_inches": snapped_in,
        "matched_preset_id": best_id,
        "matched_label": best_label,
    }


@dataclass
class StitchResult:
    """Holds the stitched 16-bit document image, seam joins, and quality metrics."""

    mode: str                         # "preset" | "auto_adjust" | "ai_enhanced"
    preset_id: str
    doc_u16: np.ndarray               # (rows, doc_width, 3) uint16
    composite_width: int              # full 5-camera stitched width (~13,306 px)
    document_width: int               # cropped window width (12,600 px @ 42.000 in)
    rows: int
    dpi: int
    K: float
    joins: list[int]                  # [2658, 5320, 7982, 10644]
    gaps: list[tuple[int, int]]
    pieces: list[int]
    camera_diagnostics: dict[str, dict[str, Any]]
    zero_columns: int
    dark_columns: int
    applied_filters: list[str] = field(default_factory=list)
    paper_size: str = "full_42"
    orientation: str = "portrait"
    auto_width_info: dict[str, Any] | None = None
    seam_blend_info: dict[str, Any] | None = None
    channel_registration: dict[str, Any] = field(default_factory=dict)

    def to_metadata_dict(self) -> dict[str, Any]:
        clean_cam_diag = {}
        for k, v in self.camera_diagnostics.items():
            clean_cam_diag[k] = {
                ck: cv for ck, cv in v.items() if not ck.startswith("_")
            }
        return {
            "mode": self.mode,
            "preset_id": self.preset_id,
            "rows": self.rows,
            "composite_width": self.composite_width,
            "document_width": self.document_width,
            "width_inches": round(self.document_width / float(self.dpi), 3),
            "height_inches": round(self.rows / float(self.dpi), 3),
            "dpi": self.dpi,
            "paper_size": self.paper_size,
            "orientation": self.orientation,
            "auto_width_info": self.auto_width_info,
            "seam_blend_info": self.seam_blend_info,
            "channel_registration": self.channel_registration,
            "K": round(self.K, 1),
            "joins": self.joins,
            "gaps": [list(g) for g in self.gaps],
            "pieces": self.pieces,
            "zero_columns": self.zero_columns,
            "dark_columns": self.dark_columns,
            "applied_filters": self.applied_filters,
            "cameras": clean_cam_diag,
        }


def _shift_rows_edge(arr: np.ndarray, dy: int) -> np.ndarray:
    """Shift a 3D image vertically so output row ``y`` samples input row ``y + dy``."""
    if dy == 0 or arr.shape[0] <= abs(dy):
        return arr
    if dy > 0:
        return np.pad(arr[dy:, :, :], ((0, dy), (0, 0), (0, 0)), mode="edge")
    pad_top = -dy
    return np.pad(arr[: arr.shape[0] - pad_top, :, :], ((pad_top, 0), (0, 0), (0, 0)), mode="edge")


def stitch_cameras(
    cams: list[tuple[str, np.ndarray, dict[str, Any]]],
    pmeta: list[dict[str, Any]],
    order: str = "bed",
    seam_overlaps_px: tuple[int, ...] | list[int] | None = DEFAULT_SEAM_OVERLAPS_PX,
    seam_dy_px: tuple[int, ...] | list[int] | None = DEFAULT_SEAM_DY_PX,
) -> tuple[np.ndarray, list[str], list[int]]:
    """Stitch corrected camera images in bed order ``[cam1..cam5]`` (applying per-seam
    optical overlap trimming and vertical row alignment ``dy``) or raster order."""
    by = {n: im for n, im, _ in cams}
    left_w = pmeta[5]["width"]
    if order == "bed":
        raw_seq = [
            ("cam1", by["cam1"]),
            ("cam2", by["cam2"]),
            ("cam3", by["cam3"]),
            ("cam4", by["cam4"]),
            ("cam5", by["cam5"]),
        ]
        overlaps = list(seam_overlaps_px) if seam_overlaps_px is not None else [0, 0, 0, 0]
        dys = list(seam_dy_px) if seam_dy_px is not None else [0, 0, 0, 0]
        cum_dy = 0
        seq: list[tuple[str, np.ndarray]] = []
        for idx, (cam_name, im) in enumerate(raw_seq):
            if idx > 0 and idx - 1 < len(dys):
                cum_dy += int(dys[idx - 1])
            aligned = _shift_rows_edge(im, cum_dy)
            left_trim = 0
            right_trim = 0
            if idx > 0 and idx - 1 < len(overlaps):
                ov_prev = max(0, int(overlaps[idx - 1]))
                left_trim = ov_prev - (ov_prev // 2)
            if idx < len(raw_seq) - 1 and idx < len(overlaps):
                ov_next = max(0, int(overlaps[idx]))
                right_trim = ov_next // 2
            end_col = max(left_trim + 1, aligned.shape[1] - right_trim)
            seq.append((cam_name, aligned[:, left_trim:end_col, :]))
    elif order == "raster":
        c5 = by["cam5"]
        seq = [
            ("cam5R", c5[:, left_w:]),
            ("cam1", by["cam1"]),
            ("cam2", by["cam2"]),
            ("cam3", by["cam3"]),
            ("cam4", by["cam4"]),
            ("cam5L", c5[:, :left_w]),
        ]
    else:
        raise ValueError(f"Unknown stitch order {order!r}")

    parts = [im for _, im in seq]
    comp = np.concatenate(parts, axis=1)
    joins = [int(j) for j in np.cumsum([p.shape[1] for p in parts])[:-1]]
    return comp, [n for n, _ in seq], joins


def count_zero_columns(comp: np.ndarray, prof: np.ndarray | None = None) -> int:
    if prof is None:
        prof = comp.mean(axis=(0, 2), dtype=np.float64)
    return int((prof <= 0.0).sum())


def count_dark_columns(comp: np.ndarray, frac: float = 0.25, prof: np.ndarray | None = None) -> int:
    if prof is None:
        prof = comp.mean(axis=(0, 2), dtype=np.float64)
    k = min(401, max(3, (len(prof) // 4) * 2 + 1))
    bg = np.convolve(np.pad(prof, (k // 2, k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    return int((prof < frac * bg[: len(prof)]).sum())


def u16_to_srgb8(
    doc_u16: np.ndarray,
    preset: CalibrationPreset,
    K: float | None = None,
    already_it8_srgb: bool = False,
    gamma: float = 0.45,
) -> np.ndarray:
    """Convert a 16-bit stitched array to 8-bit sRGB for preview or 8-bit export."""
    if already_it8_srgb:
        return np.clip(np.rint(doc_u16.astype(np.float32) / 257.0), 0, 255).astype(np.uint8)
    k_norm = float(K or preset.K or DEFAULT_K)
    v = np.clip(doc_u16.astype(np.float32) / max(k_norm * 1.05, 1.0), 0.0, 1.0)
    return np.clip(np.rint((v ** gamma) * 255.0), 0, 255).astype(np.uint8)


def to_grayscale(arr: np.ndarray) -> np.ndarray:
    """Convert a 3-channel RGB array (uint8 or uint16) to single-channel grayscale."""
    if arr.ndim == 2:
        return arr
    lum = (
        0.2126 * arr[:, :, 0].astype(np.float32)
        + 0.7152 * arr[:, :, 1].astype(np.float32)
        + 0.0722 * arr[:, :, 2].astype(np.float32)
    )
    max_val = 65535 if arr.dtype == np.uint16 else 255
    return np.clip(np.rint(lum), 0, max_val).astype(arr.dtype)


def process_scan_to_stitch(
    raster: np.ndarray,
    rows: int,
    preset: CalibrationPreset,
    *,
    mode: str = "preset",
    window_px: int = WINDOW_PX_300DPI,
    dpi: int = 300,
    paper_size: str = "full_42",
    orientation: str = "portrait",
    auto_detect_width: bool = False,
    order: str = "bed",
    gaps: list[tuple[int, int]] | None = None,
    source_dpi: int = 300,
    seam_color_blend: bool = False,
    seam_blend_width_px: int = 128,
    seam_blend_strength: float = 0.85,
    seam_feather_mode: str = "classic",
    seam_micro_feather_px: int = 6,
    enhancement: EnhancementOptions | None = None,
) -> StitchResult:
    """Run the full camera-first calibration and stitching pipeline.

    ``mode``:
      - ``"preset"``: Apply ``preset`` directly without per-scan polynomial gain fit.
      - ``"auto_adjust"``: Fit per-scan degree-5 polynomial gain ``s(cam, col, ch)`` on
        white margins to compensate for operating temperature / paper drift.
    ``seam_color_blend``:
      - When ``True``, equalizes and blends analog camera-to-camera color/luminance
        variations over ``+/- seam_blend_width_px`` adjacent pixels around each of the
        4 camera joins plus a fine micro-feather at the butt join.
    ``source_dpi``:
      - Resolution the rows were actually captured at.  A live scan honours the
        requested dpi (600 dpi gives ~601 rows/inch), so pass ``dpi``; offline
        captures are recorded at 300 dpi, so the default is correct there.
    """
    if gaps is None:
        gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))

    auto_adjust = mode == "auto_adjust"
    channel_registration: dict[str, Any] = {}
    cams, K, pmeta = apply_camera_calibration(
        raster,
        rows,
        preset,
        auto_adjust_margins=auto_adjust,
        gaps=gaps,
        registration_out=channel_registration,
    )
    comp, _names, joins = stitch_cameras(cams, pmeta, order=order)
    comp_width = int(comp.shape[1])

    auto_width_info: dict[str, Any] | None = None
    if auto_detect_width or paper_size == "auto":
        auto_width_info = detect_auto_paper_width_u16(comp, base_dpi=300, max_window_px=WINDOW_PX_300DPI)
        crop_px_300 = int(auto_width_info["detected_px"])
    else:
        crop_px_300 = window_px

    doc = comp[:, :crop_px_300, :] if (crop_px_300 and crop_px_300 < comp_width) else comp
    doc_joins = [j for j in joins if j < doc.shape[1]]

    if preset.it8.enabled and preset.it8.matrix:
        doc = apply_it8_transform_u16(
            doc,
            gamma=preset.it8.gamma,
            matrix=preset.it8.matrix,
            offset=preset.it8.offset,
            K=K,
        )
        # Scale K to 16-bit sRGB paper white after IT8 transform
        K_effective = 62000.0
    else:
        K_effective = K

    applied_filters: list[str] = []
    if seam_color_blend and doc_joins:
        feather_mode = str(seam_feather_mode or "classic").lower()
        if feather_mode == "artwork":
            doc, seam_blend_info = blend_seam_color_artwork_u16(
                doc,
                doc_joins,
                blend_width_px=int(seam_blend_width_px),
                strength=float(seam_blend_strength),
            )
        else:
            doc, seam_blend_info = blend_seam_color_variation_u16(
                doc,
                doc_joins,
                blend_width_px=int(seam_blend_width_px),
                strength=float(seam_blend_strength),
            )
            seam_blend_info["mode"] = "classic"
        if seam_micro_feather_px > 0:
            doc = heal_seams_u16(doc, doc_joins, radius=int(seam_micro_feather_px))
            post_feather = measure_seam_step_u16(doc, doc_joins)
            seam_blend_info["after_rgb_step_mean"] = post_feather["mean_rgb_step"]
            seam_blend_info["seams_after"] = post_feather["seams"]
        applied_filters.append(
            f"seam_color_blend({feather_mode},width={int(seam_blend_width_px)}px,"
            f"strength={float(seam_blend_strength):.2f})"
        )
    else:
        unblended_stats = measure_seam_step_u16(doc, doc_joins)
        seam_blend_info = {
            "enabled": False,
            "blend_width_px": int(seam_blend_width_px),
            "strength": round(float(seam_blend_strength), 3),
            "before_rgb_step_mean": unblended_stats["mean_rgb_step"],
            "after_rgb_step_mean": unblended_stats["mean_rgb_step"],
            "seams_before": unblended_stats["seams"],
            "seams_after": unblended_stats["seams"],
        }

    if enhancement is not None:
        doc, ai_filters = run_enhancement_pipeline(doc, doc_joins, enhancement)
        applied_filters.extend(ai_filters)
        if ai_filters:
            mode = "ai_enhanced"
            after_ai_stats = measure_seam_step_u16(doc, doc_joins)
            seam_blend_info["after_rgb_step_mean"] = after_ai_stats["mean_rgb_step"]
            seam_blend_info["seams_after"] = after_ai_stats["seams"]

    # Scale from the native 300 DPI 5-camera stream to requested DPI if different
    # Scale to the requested DPI.  The two axes are not symmetric:
    #   * columns always come from the fixed CIS sensor, ~300 dpi regardless of
    #     the requested resolution, so they scale by target/300;
    #   * rows are sampled by the scanner at the requested line rate -- a live
    #     600 dpi capture already has 600 rows/inch (measured 601) -- so they
    #     must only be scaled from the resolution the raster was captured at.
    # Scaling both axes by target/300 inflated the row count a second time and
    # stretched every 600/1200 dpi scan vertically.
    target_dpi = int(dpi) if int(dpi) in SUPPORTED_RESOLUTIONS_DPI else 300
    capture_dpi = int(source_dpi) if int(source_dpi) > 0 else 300
    scale_x = target_dpi / 300.0
    scale_y = target_dpi / float(capture_dpi)
    new_w = max(1, int(round(doc.shape[1] * scale_x)))
    new_h = max(1, int(round(doc.shape[0] * scale_y)))
    if (new_w, new_h) != (doc.shape[1], doc.shape[0]):
        y_idx = np.clip(np.rint(np.linspace(0, doc.shape[0] - 1, new_h)).astype(np.int32), 0, doc.shape[0] - 1)
        x_idx = np.clip(np.rint(np.linspace(0, doc.shape[1] - 1, new_w)).astype(np.int32), 0, doc.shape[1] - 1)
        doc = doc[y_idx][:, x_idx]
        doc_joins = [int(round(j * scale_x)) for j in doc_joins if int(round(j * scale_x)) < new_w]

    cam_diag = {name: diag for name, _im, diag in cams}
    doc_col_prof = doc.mean(axis=(0, 2), dtype=np.float64)
    return StitchResult(
        mode=mode,
        preset_id=preset.id,
        doc_u16=doc,
        composite_width=comp_width,
        document_width=int(doc.shape[1]),
        rows=int(doc.shape[0]),
        dpi=target_dpi,
        K=K_effective,
        joins=doc_joins,
        gaps=gaps,
        pieces=[int(m["width"]) for m in pmeta],
        camera_diagnostics=cam_diag,
        zero_columns=count_zero_columns(doc, prof=doc_col_prof),
        dark_columns=count_dark_columns(doc, prof=doc_col_prof),
        applied_filters=applied_filters,
        paper_size=paper_size,
        orientation=orientation,
        auto_width_info=auto_width_info,
        seam_blend_info=seam_blend_info,
        channel_registration=channel_registration,
    )


def save_preview_image(
    stitch_res: StitchResult,
    preset: CalibrationPreset,
    out_path: str | Path,
    *,
    max_width: int = 2400,
    color_mode: str = "color",
) -> Path:
    """Save a high-resolution web preview PNG for the deep-zoom / A/B viewer."""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    already_srgb = bool(preset.it8.enabled and preset.it8.matrix)
    arr8 = u16_to_srgb8(stitch_res.doc_u16, preset, K=stitch_res.K, already_it8_srgb=already_srgb)
    if color_mode == "grayscale":
        arr8 = to_grayscale(arr8)
    im = Image.fromarray(arr8)
    if max_width and im.width > max_width:
        new_h = max(1, int(round(im.height * max_width / im.width)))
        im = im.resize((max_width, new_h), Image.Resampling.LANCZOS)
    im.save(p, format="PNG")
    return p


def save_seam_crops(
    stitch_res: StitchResult,
    preset: CalibrationPreset,
    out_dir: str | Path,
    stem: str,
    half_width: int = 150,
) -> list[dict[str, Any]]:
    """Save 1:1 pixel-resolution crops around each of the 4 camera joins."""
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    already_srgb = bool(preset.it8.enabled and preset.it8.matrix)
    arr8 = u16_to_srgb8(stitch_res.doc_u16, preset, K=stitch_res.K, already_it8_srgb=already_srgb)
    crops: list[dict[str, Any]] = []
    for idx, j in enumerate(stitch_res.joins):
        a = max(0, j - half_width)
        b = min(arr8.shape[1], j + half_width)
        crop_path = od / f"{stem}-join{idx}-x{j}.png"
        Image.fromarray(arr8[:, a:b]).save(crop_path, format="PNG")
        crops.append({
            "seam_index": idx,
            "join_x": int(j),
            "x_range": [int(a), int(b)],
            "filename": crop_path.name,
            "path": str(crop_path),
        })
    return crops


def resolve_next_scan_basename(
    output_dir: str | Path,
    prefix: str = "scan",
    start_seq: int = 1,
    digits: int = 4,
) -> tuple[str, int]:
    """Return ``(base_name, next_seq_after)`` such as ``('scan_0001', 2)``,
    scanning ``output_dir`` so existing files are never overwritten."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_prefix = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (prefix or "scan").strip()).strip("_") or "scan"
    seq = max(1, int(start_seq))
    pad = max(1, int(digits))

    while True:
        candidate = f"{clean_prefix}_{seq:0{pad}d}"
        # Check if any file with this candidate stem already exists in out_dir
        collision = any(
            (out_dir / f"{candidate}.{ext}").exists()
            for ext in ("tiff", "tif", "jpg", "jpeg", "pdf", "png", "meta.json")
        )
        if not collision:
            return candidate, seq + 1
        seq += 1


#: Filename suffix per render variant. All variants of one scan share the number
#: (``scan_0087``, ``scan_0087_Auto``, ``scan_0087_AI``); the next number is only
#: used once *every* variant of that number is taken, so the three views of a scan
#: stay grouped and nothing is ever overwritten.
EXPORT_RENDER_SUFFIX: dict[str, str] = {
    "preset": "",
    "auto_adjust": "_Auto",
    "ai_enhanced": "_AI",
}

_EXPORT_FILE_EXTS = ("tiff", "tif", "jpg", "jpeg", "pdf", "png", "meta.json")


def _clean_export_prefix(prefix: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in (prefix or "scan").strip()).strip("_") or "scan"


def render_export_stem(prefix: str, seq: int, digits: int = 4) -> str:
    """``('scan', 87, 4) -> 'scan_0087'`` (no render suffix)."""
    return f"{_clean_export_prefix(prefix)}_{max(1, int(seq)):0{max(1, int(digits))}d}"


def export_variant_exists(output_dir: str | Path, stem: str, render_mode: str) -> bool:
    """True when this exact render variant already has a file on disk."""
    out_dir = Path(output_dir)
    name = f"{stem}{EXPORT_RENDER_SUFFIX.get(render_mode, '')}"
    return any((out_dir / f"{name}.{ext}").exists() for ext in _EXPORT_FILE_EXTS)


def export_group_taken(output_dir: str | Path, stem: str) -> bool:
    """True when *any* render variant of ``stem`` exists (the number is in use)."""
    out_dir = Path(output_dir)
    return any(
        (out_dir / f"{stem}{suffix}.{ext}").exists()
        for suffix in set(EXPORT_RENDER_SUFFIX.values())
        for ext in _EXPORT_FILE_EXTS
    )


def find_free_export_group(
    output_dir: str | Path,
    prefix: str = "scan",
    start_seq: int = 1,
    digits: int = 4,
) -> tuple[str, int]:
    """Return ``(stem, seq)`` whose number is free for every render variant."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seq = max(1, int(start_seq))
    while True:
        stem = render_export_stem(prefix, seq, digits)
        if not export_group_taken(out_dir, stem):
            return stem, seq
        seq += 1


def ensure_unique_basename_in_dir(out_dir: Path, base_name: str) -> str:
    """Guarantee that ``base_name`` does not collide with any existing exported file in ``out_dir``."""
    exts = ("tiff", "tif", "jpg", "jpeg", "pdf", "png", "meta.json")
    if not any((out_dir / f"{base_name}.{ext}").exists() for ext in exts):
        return base_name

    # If base_name already ends with _NNNN, increment NNNN; otherwise append _0002, _0003...
    import re
    m = re.match(r"^(.*?)_(\d+)$", base_name)
    if m:
        stem, num_str = m.group(1), m.group(2)
        pad = len(num_str)
        seq = int(num_str) + 1
        while any((out_dir / f"{stem}_{seq:0{pad}d}.{ext}").exists() for ext in exts):
            seq += 1
        return f"{stem}_{seq:0{pad}d}"

    seq = 2
    while any((out_dir / f"{base_name}_{seq:04d}.{ext}").exists() for ext in exts):
        seq += 1
    return f"{base_name}_{seq:04d}"


def export_scan_files(
    stitch_res: StitchResult,
    preset: CalibrationPreset,
    output_dir: str | Path,
    base_name: str,
    *,
    formats: list[str] | tuple[str, ...] = ("tiff", "jpg", "pdf"),
    color_mode: str = "color",
    tiff_16bit: bool = False,
    jpg_quality: int = 95,
    debug_mode: bool = False,
    avoid_overwrite: bool = True,
    raw_records_bytes: bytes | None = None,
    transcript: list[dict[str, Any]] | None = None,
    drift_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export the stitched scan to TIFF, JPG, and/or PDF in Color or Grayscale,
    auto-incrementing the filename if ``avoid_overwrite=True`` so existing files
    are never overwritten."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if avoid_overwrite:
        base_name = ensure_unique_basename_in_dir(out_dir, base_name)
    already_srgb = bool(preset.it8.enabled and preset.it8.matrix)
    dpi_tuple = (float(stitch_res.dpi), float(stitch_res.dpi))

    arr8 = u16_to_srgb8(stitch_res.doc_u16, preset, K=stitch_res.K, already_it8_srgb=already_srgb)
    if color_mode == "grayscale":
        arr8_export = to_grayscale(arr8)
    else:
        arr8_export = arr8

    saved_files: list[dict[str, Any]] = []
    norm_formats = [f.lower().strip() for f in formats]

    for fmt in norm_formats:
        if fmt in ("tif", "tiff"):
            tif_path = out_dir / f"{base_name}.tiff"
            if tiff_16bit and color_mode == "grayscale":
                gray16 = to_grayscale(stitch_res.doc_u16)
                im16 = Image.fromarray(gray16)
                im16.save(tif_path, format="TIFF", dpi=dpi_tuple)
            else:
                im_tif = Image.fromarray(arr8_export)
                im_tif.save(tif_path, format="TIFF", compression="tiff_lzw", dpi=dpi_tuple)
            saved_files.append({
                "format": "tiff",
                "color_mode": color_mode,
                "path": str(tif_path),
                "size_bytes": tif_path.stat().st_size,
            })

        elif fmt in ("jpg", "jpeg"):
            jpg_path = out_dir / f"{base_name}.jpg"
            im_jpg = Image.fromarray(arr8_export)
            im_jpg.save(jpg_path, format="JPEG", quality=int(jpg_quality), dpi=dpi_tuple)
            saved_files.append({
                "format": "jpg",
                "color_mode": color_mode,
                "path": str(jpg_path),
                "size_bytes": jpg_path.stat().st_size,
            })

        elif fmt == "pdf":
            pdf_path = out_dir / f"{base_name}.pdf"
            im_pdf = Image.fromarray(arr8_export)
            im_pdf.save(pdf_path, format="PDF", resolution=float(stitch_res.dpi))
            saved_files.append({
                "format": "pdf",
                "color_mode": color_mode,
                "path": str(pdf_path),
                "size_bytes": pdf_path.stat().st_size,
            })

        elif fmt == "png":
            png_path = out_dir / f"{base_name}.png"
            im_png = Image.fromarray(arr8_export)
            im_png.save(png_path, format="PNG", dpi=dpi_tuple)
            saved_files.append({
                "format": "png",
                "color_mode": color_mode,
                "path": str(png_path),
                "size_bytes": png_path.stat().st_size,
            })

    # Always write a compact metadata sidecar next to the exported scan
    meta_path = out_dir / f"{base_name}.meta.json"
    sidecar = {
        "base_name": base_name,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "color_mode": color_mode,
        "files": saved_files,
        "stitch": stitch_res.to_metadata_dict(),
        "drift": drift_report,
    }
    meta_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    debug_files: list[str] = []
    if debug_mode:
        dbg_dir = out_dir / "debug"
        dbg_dir.mkdir(parents=True, exist_ok=True)
        if raw_records_bytes:
            rec_path = dbg_dir / f"{base_name}.rec"
            rec_path.write_bytes(raw_records_bytes)
            debug_files.append(str(rec_path))
        if transcript:
            jsonl_path = dbg_dir / f"{base_name}.jsonl"
            with open(jsonl_path, "w", encoding="utf-8") as fh:
                for entry in transcript:
                    fh.write(json.dumps(entry) + "\n")
            debug_files.append(str(jsonl_path))
        crops = save_seam_crops(stitch_res, preset, dbg_dir, f"{base_name}-seam")
        debug_files.extend(c["path"] for c in crops)
        diag_path = dbg_dir / f"{base_name}-diagnostics.json"
        diag_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        debug_files.append(str(diag_path))

    return {
        "output_dir": str(out_dir),
        "base_name": base_name,
        "files": saved_files,
        "metadata_path": str(meta_path),
        "debug_files": debug_files,
    }


# ---------------------------------------------------------------------------
# Synthetic 5-Camera 16-bit Scan Generator (for Offline Testing & Demo UI)
# ---------------------------------------------------------------------------

def generate_synthetic_5cam_raster(
    rows: int = 960,
    preset: CalibrationPreset | None = None,
    *,
    simulate_thermal_drift: bool = True,
    subject: str = "architectural_and_it8",
    paper_width_inches: float = 42.0,
) -> tuple[np.ndarray, bytes]:
    """Generate a realistic 16-bit 5-camera raw raster ``(rows, 13640, 3)`` and its
    ``.rec`` byte stream for offline testing, calibration verification, and UI demos.

    Accurately models:
      - The 13,306-px 5-camera bed image with 3D architectural perspective linework,
        rulers, and an IT8 color patch chart
      - Inverse camera flat-field response ``W_i(col, ch)`` and optical black ``B_i``
        (including Cameras 4 & 5's 2.6x red deficiency)
      - Optional thermal drift on Cameras 3..5 (+14% R/G shift) to trigger DRIFT alerts
      - Circular readout framing ``[cam5R][gap][cam1][gap][cam2][gap][cam3][gap][cam4][gap][cam5L]``
        with 23-px hard-zero cores and optical black ramps at ``468, 3196, 5924, 8652, 11380``
    """
    from .calibration import PresetStore

    if preset is None:
        store = PresetStore()
        preset = store.get_preset("default-it8")

    pmeta = piece_matrix(DEFAULT_GAPS, RAW_WIDTH_PX)
    cam_widths = {
        "cam1": pmeta[1]["width"],
        "cam2": pmeta[2]["width"],
        "cam3": pmeta[3]["width"],
        "cam4": pmeta[4]["width"],
        "cam5": pmeta[5]["width"] + pmeta[0]["width"],
    }
    comp_w = sum(cam_widths.values())  # 13,306 px

    # Draw a crisp 8-bit ground-truth bed canvas at 1/4 scale then upsample or draw directly
    scale = 4
    sh = max(60, rows // scale)
    sw = comp_w // scale
    canvas = Image.new("RGB", (sw, sh), (248, 248, 248))
    draw = ImageDraw.Draw(canvas)

    if subject == "full_bleed_artwork":
        # Rich continuous-tone textured artwork with no white margins (triggers INSUFFICIENT_WHITE)
        y_grid, x_grid = np.mgrid[0:sh, 0:sw].astype(np.float32)
        yn = y_grid / max(sh, 1)
        xn = x_grid / max(sw, 1)
        # Fine-art brushwork & canvas weave texture so 8x8 blocks have high local variance
        weave = 38.0 * np.sin(x_grid * 1.9) * np.cos(y_grid * 1.9)
        r_art = np.clip(45 + 75 * np.sin(xn * 3.14) + 25 * np.cos(yn * 6.28) + weave, 12, 165)
        g_art = np.clip(40 + 70 * (1.0 - yn) + 30 * np.sin(xn * 9.42) - weave, 12, 160)
        b_art = np.clip(55 + 80 * yn + 25 * np.cos(xn * 4.71) + weave * 0.8, 15, 175)
        art_arr = np.stack([r_art, g_art, b_art], axis=2).astype(np.uint8)
        canvas = Image.fromarray(art_arr)
        draw = ImageDraw.Draw(canvas)
        for rad in range(20, min(sw, sh) // 2, 18):
            draw.ellipse(
                [sw // 2 - rad, sh // 2 - rad // 2, sw // 2 + rad, sh // 2 + rad // 2],
                outline=(180, 145, 80),
                width=2,
            )
    else:
        # 5mm architectural grid + 3D perspective building elevation + colour swatch chart
        by0 = max(2, int(sh * 0.04))
        by1 = sh - by0
        step_grid = max(8, int(round(96.0 / scale)))
        for gx in range(24, sw - 24, step_grid):
            draw.line([(gx, by0 + 4), (gx, by1 - 4)], fill=(235, 238, 242), width=1)

        # Outer architectural border & title block leaving clean white margins at top/bottom
        draw.rectangle([16, by0, sw - 16, by1], outline=(35, 45, 65), width=1)
        # 3D Architectural perspective trusses / towers across all 5 camera seams
        for tower_i in range(4):
            tx0 = int(sw * (0.12 + tower_i * 0.21))
            tx1 = tx0 + int(sw * 0.14)
            ty0 = int(sh * 0.30)
            ty1 = int(sh * 0.72)
            # Shaded facade
            draw.rectangle([tx0, ty0, tx1, ty1], fill=(212, 222, 235), outline=(25, 35, 55), width=2)
            # Perspective roof lines crossing seams
            vanish_x = sw // 2
            vanish_y = int(sh * 0.14)
            draw.line([(tx0, ty0), (vanish_x, vanish_y)], fill=(40, 60, 95), width=2)
            draw.line([(tx1, ty0), (vanish_x, vanish_y)], fill=(40, 60, 95), width=2)
            # Floor cross-beams
            for fy in range(ty0 + 12, ty1, 16):
                draw.line([(tx0, fy), (tx1, fy)], fill=(70, 90, 125), width=1)
                draw.line([(tx0, fy), (tx1, min(ty1, fy + 16))], fill=(110, 130, 160), width=1)

        # Color calibration swatch strip in Camera 1
        palette = [
            (210, 45, 45), (45, 165, 75), (40, 90, 205), (230, 185, 35),
            (175, 55, 165), (45, 175, 185), (225, 115, 40), (65, 65, 70),
        ]
        for pi, col in enumerate(palette):
            px0 = 40 + pi * 36
            py0 = int(sh * 0.35)
            py1 = min(int(sh * 0.65), py0 + 28)
            draw.rectangle([px0, py0, px0 + 30, py1], fill=col, outline=(30, 30, 30), width=1)

    full_rgb8 = np.asarray(
        canvas.resize((comp_w, rows), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )
    if subject == "full_bleed_artwork":
        # Add fine pixel-level canvas weave so 8x8 blocks have realistic brushwork variance
        yg, xg = np.mgrid[0:rows, 0:comp_w].astype(np.float32)
        tex = 28.0 * np.sin(xg * 1.57) * np.cos(yg * 1.57)
        full_rgb8 = np.clip(full_rgb8 * 0.55 + tex[:, :, None], 8.0, 125.0)
    # Normalize reflectance in [0.02, 1.0]
    refl = np.clip(full_rgb8 / 248.0, 0.02, 1.0)
    # If physical sheet is narrower than 42.0 in, columns past the right edge of the sheet
    # see the dark matte backing roller/platen (reflectance ~0.015), enabling Auto-Width detection.
    sheet_px = int(round(min(42.0, max(4.0, float(paper_width_inches))) * 300.0))
    if sheet_px < comp_w:
        refl[:, sheet_px:, :] = 0.015

    # Slice reflectance into cam1..cam5 and apply inverse physical sensor response
    drift_factors = {
        "cam1": (1.00, 1.00, 1.00),
        "cam2": (1.01, 0.99, 1.00) if simulate_thermal_drift else (1.00, 1.00, 1.00),
        "cam3": (0.95, 0.98, 1.02) if simulate_thermal_drift else (1.00, 1.00, 1.00),
        "cam4": (0.84, 0.94, 1.04) if simulate_thermal_drift else (1.00, 1.00, 1.00),
        "cam5": (0.81, 0.93, 1.05) if simulate_thermal_drift else (1.00, 1.00, 1.00),
    }

    raw_cam_arrays: dict[str, np.ndarray] = {}
    x_cursor = 0
    cum_dy = 0
    cam_names_order = ("cam1", "cam2", "cam3", "cam4", "cam5")
    for idx, cam_name in enumerate(cam_names_order):
        cw = cam_widths[cam_name]
        if idx > 0 and idx - 1 < len(DEFAULT_SEAM_DY_PX):
            cum_dy += int(DEFAULT_SEAM_DY_PX[idx - 1])
        cam_refl = _shift_rows_edge(refl[:, x_cursor : x_cursor + cw, :], -cum_dy)
        ov_next = int(DEFAULT_SEAM_OVERLAPS_PX[idx]) if idx < len(DEFAULT_SEAM_OVERLAPS_PX) else 0
        x_cursor += cw - ov_next

        ng_cam = (cw + GROUP_PX - 1) // GROUP_PX
        w_grp = preset.get_camera_W_array(cam_name, expected_groups=ng_cam)
        w_col = fill_profile_columns(w_grp, cw)
        b_grp = preset.get_camera_B_array(cam_name, expected_groups=ng_cam)
        b_col = fill_profile_columns(b_grp, cw)
        df = np.asarray(drift_factors[cam_name], dtype=np.float32)[None, None, :]

        # Forward sensor model: raw = B + refl * (W * drift - B)
        raw_f = b_col[None, :, :] + cam_refl * np.maximum(w_col[None, :, :] * df - b_col[None, :, :], 100.0)
        raw_cam_arrays[cam_name] = np.clip(np.rint(raw_f), 1, 65535).astype(np.uint16)

    # Pack into the circular 13,640-px raw raster with 23-px zero bands & optical black ramps
    raster = np.zeros((rows, RAW_WIDTH_PX, 3), dtype=np.uint16)
    p5_w = pmeta[5]["width"]
    p0_w = pmeta[0]["width"]
    cam5_full = raw_cam_arrays["cam5"]

    piece_payloads = {
        0: cam5_full[:, p5_w : p5_w + p0_w, :],
        1: raw_cam_arrays["cam1"],
        2: raw_cam_arrays["cam2"],
        3: raw_cam_arrays["cam3"],
        4: raw_cam_arrays["cam4"],
        5: cam5_full[:, :p5_w, :],
    }

    for pid, pm in enumerate(pmeta):
        s0, s1 = pm["start"], pm["end"]
        raster[:, s0:s1, :] = piece_payloads[pid]

    # Populate the optical black ramp columns inside each gap (leaving the 23-px hard-zero core at 490+2728*k)
    for k_idx, (ga, gb) in enumerate(DEFAULT_GAPS):
        zero_start = 490 + 2728 * k_idx
        zero_end = zero_start + 23
        cam_for_gap = f"cam{min(5, k_idx + 1)}"
        b_val = np.asarray(preset.B.get(cam_for_gap, [4600.0, 3600.0, 3600.0]), dtype=np.uint16)
        if ga < zero_start:
            raster[:, ga:zero_start, :] = b_val[None, None, :]
        if zero_end < gb:
            raster[:, zero_end:gb, :] = b_val[None, None, :]
        raster[:, zero_start:zero_end, :] = 0

    # Serialize as a valid .rec stream ([u32 seq][u16 dlen][payload] in 2,936-byte chunks)
    raw_bytes = raster.tobytes()
    rec_chunks: list[bytes] = []
    chunk_size = 2936
    seq = 0
    for off in range(0, len(raw_bytes), chunk_size):
        slice_b = raw_bytes[off : off + chunk_size]
        rec_chunks.append(struct.pack("<IH", seq, len(slice_b)) + slice_b)
        seq += 1

    return raster, b"".join(rec_chunks)
