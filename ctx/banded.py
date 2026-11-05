"""Band-wise (memory-bounded) capture processing for very long sheets.

Why this exists
---------------
``pipeline.process_scan_to_stitch`` stages the whole sheet in RAM at every
step: the wire stream, a second copy for the record join, five parallel
float32 camera calibrations, the full-width composite, the IT8 pass and the
8-bit export.  Measured on this machine (2026-11-02) that costs 7-10x the wire
stream, so a 30 ft sheet at 200 dpi -- 5.9 GB on the wire -- needs 40-60 GB.
A 32 GB box cannot do it: the 30 ft attempt reached 30.5 GB RSS and the kernel
OOM-killed the process, taking the desktop session with it.

This module keeps the same per-pixel maths but bounds memory by:

* spooling the UDP stream straight to disk while capturing
  (``ScannerEngine.run_live_scan(spool_path=...)``), and
* processing the spool in row bands, cross-fading each band join, writing an
  8-bit memmap that is encoded to JPEG only at the end.

Peak RAM becomes one band (~2-3 GB at the defaults) regardless of sheet
length, so a long sheet costs disk and time instead of memory.

JPEG cannot encode an image taller than 65,535 px, so a sheet longer than that
is written as numbered ``_partN.jpg`` files (60,000 rows each) plus a sidecar
manifest describing how the parts map back to sheet coordinates.
"""

from __future__ import annotations

import gc
import json
import os
import struct
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .calibration import (
    DEFAULT_SERIAL,
    RAW_WIDTH_PX,
    STRIDE_16BIT_5CAM,
    CalibrationPreset,
    PresetStore,
    module_gaps,
)
from .pipeline import process_scan_to_stitch, u16_to_srgb8

#: libjpeg's hard limit is 65,535 px on either axis; stay clear of it and keep
#: each part a sane file size.
JPEG_MAX_ROWS = 60_000

#: Defaults: 4,000 rows = 20 in at 200 dpi, 200 rows of cross-fade (~1 in).
DEFAULT_BAND_ROWS = 4_000
DEFAULT_OVERLAP_ROWS = 200


# ---------------------------------------------------------------------------
# Capture spools
# ---------------------------------------------------------------------------
def rec_to_spool(rec_path: str | Path, spool_path: str | Path) -> dict[str, Any]:
    """Convert a record-framed ``.rec`` capture into a raw payload spool.

    ``.rec`` records are ``<u32 seq><u16 length><payload>`` (little-endian);
    the spool is just the payloads concatenated in file order, which is the
    layout :meth:`ScannerEngine.run_live_scan` writes directly.
    """
    src, dst = Path(rec_path), Path(spool_path)
    records = payload = 0
    first_seq: int | None = None
    last_seq: int | None = None
    buf = b""
    with src.open("rb") as f, dst.open("wb") as out:
        while True:
            block = f.read(1 << 24)
            if not block:
                break
            buf += block
            pos = 0
            while pos + 6 <= len(buf):
                seq, ln = struct.unpack_from("<IH", buf, pos)
                end = pos + 6 + ln
                if end > len(buf):
                    break
                out.write(buf[pos + 6 : end])
                payload += ln
                records += 1
                if first_seq is None:
                    first_seq = seq
                last_seq = seq
                pos = end
            buf = buf[pos:]
    return {
        "records": records,
        "payload_bytes": payload,
        "rows": payload // STRIDE_16BIT_5CAM,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "spool_path": str(dst),
    }


def spool_to_rec(
    spool_path: str | Path,
    rec_path: str | Path,
    *,
    payload: int = 2960,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Reframe a payload spool as the app's ``.rec`` capture format.

    ``.rec`` records are ``<u32 seq><u16 length><payload>`` (little-endian); a
    spool is the same bytes without framing, so this only re-chunks it.  The
    result reloads with ``source_mode="rec_file"`` or ``ctx longscan
    --from-rec`` and can be reprocessed -- crop, DPI, seam equalisation, larger
    bands, a fixed bug -- without touching the scanner again.
    """
    src, dst = Path(spool_path), Path(rec_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = src.stat().st_size
    seq = 0
    written = 0
    with src.open("rb") as f, dst.open("wb") as out:
        while True:
            chunk = f.read(payload * 4096)  # multiple of payload keeps alignment
            if not chunk:
                break
            for off in range(0, len(chunk), payload):
                piece = chunk[off : off + payload]
                out.write(struct.pack("<IH", seq, len(piece)))
                out.write(piece)
                seq += 1
                written += len(piece)
            if progress is not None:
                progress({"bytes_done": written, "bytes_total": total})
    return {
        "records": seq,
        "payload_bytes": written,
        "rows": written // STRIDE_16BIT_5CAM,
        "rec_path": str(dst),
        "rec_bytes": dst.stat().st_size,
    }


def spool_rows(spool_path: str | Path) -> int:
    """Rows held in a payload spool (no sizes to guess: stride is fixed)."""
    return int(os.path.getsize(spool_path) // STRIDE_16BIT_5CAM)


def _sample_gaps(src: np.memmap, total_rows: int) -> list[tuple[int, int]] | None:
    """Detect the five module gaps once, from a bounded sample of the spool.

    Held constant for every band so the camera geometry cannot shift between
    them (a per-band detection would put the joins in slightly different
    places and produce vertical stepping).
    """
    n = int(min(2048, total_rows))
    if n < 8:
        return None
    block = np.asarray(src[: n * STRIDE_16BIT_5CAM]).copy()
    raster = block.view("<u2").reshape(n, RAW_WIDTH_PX, 3)
    try:
        return module_gaps(raster.mean(axis=2, dtype=np.float32))
    except Exception:
        return None
    finally:
        del block, raster


def _estimate_registration(
    src: np.memmap,
    total_rows: int,
    gaps: list[tuple[int, int]] | None,
    sample_rows: int = 12_000,
) -> tuple[float, float] | None:
    """Measure the CIS channel line offset once, over a large sample.

    Channel registration must be one global answer for the whole sheet.  When it
    is estimated per band, sparse mostly-blank strips disagree and the bands end
    up misregistered by different amounts -- measured on a 229 in chart: band 1
    was 5 rows off in red while the later bands were aligned, which reads as
    rainbow fringing in some bands and not others.
    """
    from .calibration import CAMERAS, piece_matrix, raw_camera_images
    from .registration import register_cameras

    n = int(min(sample_rows, total_rows))
    if n < 512:
        return None
    start = max(0, (total_rows - n) // 2)
    block = np.asarray(src[start * STRIDE_16BIT_5CAM : (start + n) * STRIDE_16BIT_5CAM]).copy()
    try:
        raster = block.view("<u2").reshape(n, RAW_WIDTH_PX, 3)
        pmeta = piece_matrix(gaps, RAW_WIDTH_PX)
        cams = raw_camera_images(raster, pmeta, CAMERAS)
        _, info = register_cameras(cams)
    except Exception:
        return None
    finally:
        del block
    if not info.get("applied"):
        return None
    return float(info.get("dy_red_px", 0.0)), float(info.get("dy_green_px", 0.0))


def equalize_seam_columns(
    doc: np.memmap,
    joins: list[int],
    *,
    window_px: int = 256,
    measure_px: int = 32,
    block_rows: int = 2000,
    max_offset: float = 24.0,
    dead_offset: float = 0.5,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Remove per-camera colour steps at the joins, measured per row block.

    A per-band blend would give every band its own daytime gains and show up as
    horizontal steps, which is why banded stitching runs with
    ``seam_color_blend=False`` -- but that leaves the camera-to-camera tone
    differences in place (a 42" sheet measured +14 blue at the cam2/3 join and
    +9 red at cam3/4).

    Two things make the naive global version wrong, both measured on a real
    42" x 19 ft capture:

    * **Means lie when the seam crosses content.**  A +/-256 px *mean* beside the
      8.6" join reported a 5-level step that a per-row *median* shows is zero;
      correcting it made the seam worse.  Medians ignore ink, so the estimate is
      paper tone rather than paper-plus-drawing.
    * **The step is not constant with height.**  The 17" join measured 3-14
      levels depending on where you look, so one global number over-corrects
      some rows and under-corrects others.

    So each join is estimated with per-row medians aggregated per block, the
    block estimates are interpolated along the height, and the symmetric +/-half
    ramp is applied with the value that belongs to that row.  Only columns within
    ``window_px`` of a join are touched and the far-field tone is never modified.
    """
    if not joins:
        return []
    rows, width = doc.shape[0], doc.shape[1]
    w = max(8, min(int(window_px), width // 8))
    # Measure close to the join: a wide window averages the step away (a +/-256
    # px median read 11 where the visible jump is 15).
    m = max(8, min(int(measure_px), w))
    block_rows = max(64, int(block_rows))
    results: list[dict[str, Any]] = []

    for j in joins:
        j = int(j)
        if j - w - 1 < 0 or j + w + 1 >= width:
            continue
        centres: list[float] = []
        deltas: list[np.ndarray] = []
        for b0 in range(0, rows, block_rows):
            b1 = min(b0 + block_rows, rows)
            step = max(1, (b1 - b0) // 40)
            idx = np.arange(b0, b1, step)
            if idx.size < 3:
                continue
            left = np.median(doc[idx][:, j - m : j - 1, :].astype(np.float32), axis=1)
            right = np.median(doc[idx][:, j + 1 : j + m + 1, :].astype(np.float32), axis=1)
            d = np.clip(np.median(left - right, axis=0), -max_offset, max_offset)
            centres.append(0.5 * (b0 + b1))
            deltas.append(d)
        if not deltas:
            continue
        centres_a = np.asarray(centres, dtype=np.float32)
        deltas_a = np.asarray(deltas, dtype=np.float32)          # (blocks, 3)
        overall = np.median(deltas_a, axis=0)
        entry = {
            "join_x": j,
            "delta_rgb": [round(float(v), 2) for v in overall],
            "block_delta_rgb": [[round(float(v), 2) for v in row] for row in deltas_a[:: max(1, len(deltas_a) // 8)]],
            "window_px": w,
            "measure_px": m,
            "corrected": bool(np.any(np.abs(overall) > dead_offset)),
        }
        results.append(entry)
        if not entry["corrected"]:
            continue

        cols = np.arange(j - w, j + w)
        dist = np.abs(cols - j) / float(w)                        # 1 at join, 0 at edge
        side = np.where(cols < j, -0.5, 0.5).astype(np.float32)   # close it symmetrically
        shape = (side * (1.0 - dist)).astype(np.float32)          # (2w,)
        for b0 in range(0, rows, 4096):
            b1 = min(b0 + 4096, rows)
            ys = np.arange(b0, b1, dtype=np.float32)
            drow = np.stack(
                [np.interp(ys, centres_a, deltas_a[:, c]) for c in range(3)], axis=1
            )                                                     # (h, 3)
            block = np.array(doc[b0:b1, j - w : j + w, :], dtype=np.float32)
            block += shape[None, :, None] * drow[:, None, :]
            doc[b0:b1, j - w : j + w, :] = np.clip(np.rint(block), 0, 255).astype(np.uint8)
        if progress is not None:
            progress({"join_x": j, "delta_rgb": entry["delta_rgb"], "window_px": w})
    doc.flush()
    return results


# ---------------------------------------------------------------------------
# Banded processing
# ---------------------------------------------------------------------------
def process_spool_banded(
    spool_path: str | Path,
    output_dir: str | Path,
    *,
    preset: CalibrationPreset,
    width_inches: float = 42.0,
    dpi: int = 200,
    capture_dpi: int | None = None,
    band_rows: int = DEFAULT_BAND_ROWS,
    overlap_rows: int = DEFAULT_OVERLAP_ROWS,
    jpg_quality: int = 95,
    base_name: str = "longscan",
    part_rows: int = JPEG_MAX_ROWS,
    out_format: str = "jpg",
    single_file: bool = False,
    seam_equalize: bool = True,
    seam_window_px: int = 256,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Stitch a spooled capture band by band and write JPEG parts.

    Returns a manifest describing the parts, their row ranges and the sheet
    geometry, which is also written beside them as ``<base_name>.meta.json``.
    """
    from PIL import Image  # imported late: the capture path does not need it

    Image.MAX_IMAGE_PIXELS = None  # a 30 ft sheet legitimately exceeds PIL's bomb guard

    t0 = time.time()
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    band_rows = max(64, int(band_rows))
    overlap_rows = max(0, min(int(overlap_rows), band_rows // 2))

    src = np.memmap(spool_path, dtype=np.uint8, mode="r")
    total_rows = int(src.size // STRIDE_16BIT_5CAM)
    if total_rows < 8:
        raise ValueError(
            f"spool holds {int(src.size)} bytes = {total_rows} rows; need at least 8"
        )

    cap_dpi = int(capture_dpi or dpi)
    scale_x = dpi / 300.0
    scale_y = dpi / float(cap_dpi)
    window_px = int(round(float(width_inches) * 300.0))
    doc_w = int(round(window_px * scale_x))
    out_rows = int(round(total_rows * scale_y))

    gaps = _sample_gaps(src, total_rows)
    # One global channel registration for every band (see _estimate_registration).
    registration_dy = _estimate_registration(src, total_rows, gaps)
    raw_out = out_dir / f"{base_name}-8bit.raw"
    out = np.memmap(raw_out, dtype=np.uint8, mode="w+", shape=(out_rows, doc_w, 3))

    step = max(1, band_rows - overlap_rows)
    written = 0
    bands = 0
    seams: list[int] = []
    joins_doc: list[int] = []
    try:
        for start in range(0, total_rows, step):
            end = int(min(start + band_rows, total_rows))
            if end - start < 8:
                break
            block = np.asarray(src[start * STRIDE_16BIT_5CAM : end * STRIDE_16BIT_5CAM]).copy()
            raster = block.view("<u2").reshape(end - start, RAW_WIDTH_PX, 3)
            res = process_scan_to_stitch(
                raster,
                end - start,
                preset,
                mode="preset",
                window_px=window_px,
                dpi=dpi,
                paper_size="full_42",
                orientation="landscape",
                auto_detect_width=False,
                order="bed",
                gaps=gaps,
                source_dpi=cap_dpi,
                registration_dy=registration_dy,
                # Per-band colour equalisation would give each band its own seam
                # gains and show up as horizontal steps; the stored preset
                # already carries the camera calibration.
                seam_color_blend=False,
            )
            if not joins_doc:
                joins_doc = [int(j) for j in getattr(res, "joins", [])]
            arr8 = u16_to_srgb8(
                res.doc_u16,
                preset,
                K=res.K,
                already_it8_srgb=bool(preset.it8.enabled and preset.it8.matrix),
            )
            h = int(arr8.shape[0])
            if written == 0:
                out[0:h] = arr8
                written = h
            else:
                ov = int(min(overlap_rows, h, written))
                if ov > 0:
                    prev = np.array(out[written - ov : written])
                    ramp = np.linspace(0.0, 1.0, ov, dtype=np.float32)[:, None, None]
                    blended = prev.astype(np.float32) * (1.0 - ramp) + arr8[:ov].astype(np.float32) * ramp
                    out[written - ov : written] = np.clip(blended, 0, 255).astype(np.uint8)
                out[written : written + (h - ov)] = arr8[ov:]
                written = written + (h - ov)
                seams.append(written)
            bands += 1
            if progress is not None:
                try:
                    progress({
                        "band": bands,
                        "rows_done": written,
                        "rows_total": out_rows,
                        "inches_done": round(written / float(dpi), 1),
                        "elapsed_s": round(time.time() - t0, 1),
                    })
                except Exception:
                    pass
            del block, raster, res, arr8
            gc.collect()
    finally:
        out.flush()

    # Global seam equalisation: one measurement, per-column correction, so the
    # bands cannot disagree with each other.
    seam_info: list[dict[str, Any]] = []
    if seam_equalize and joins_doc:
        seam_info = equalize_seam_columns(
            out, joins_doc, window_px=seam_window_px, progress=progress
        )

    # ---- encode JPEG parts ------------------------------------------------
    parts: list[dict[str, Any]] = []
    fmt = str(out_format).lower()
    if fmt == "auto":
        # Single file in the best format that can hold the sheet: JPEG while it
        # fits under libjpeg's 65,535-row ceiling, TIFF beyond it.
        fmt = "jpg" if written <= JPEG_MAX_ROWS else "tiff"
        single_file = True
    parts: list[dict[str, Any]] = []
    if fmt in ("tif", "tiff"):
        # TIFF has no 65,535-row ceiling, so the whole sheet can be one file.
        path = out_dir / f"{base_name}.tiff"
        image = Image.fromarray(np.asarray(out[:written]))
        image.save(path, format="TIFF", compression="tiff_lzw", dpi=(float(dpi), float(dpi)))
        parts.append({
            "part": 1, "format": "tiff", "path": str(path), "size_bytes": int(path.stat().st_size),
            "row_start": 0, "row_end": int(written),
            "inches_start": 0.0, "inches_end": round(written / float(dpi), 2),
            "px": [int(image.size[0]), int(image.size[1])],
        })
        del image
        written_rows = written
    else:
        if single_file and written > JPEG_MAX_ROWS:
            raise ValueError(
                f"a single JPEG needs at most {JPEG_MAX_ROWS:,} rows; this document has "
                f"{written:,} rows ({written / float(dpi):.0f} in at {dpi} dpi). "
                "Use --format tiff for one file at full resolution, --part-rows to split, "
                "or a lower dpi (200 dpi fits ~327 in in one JPEG)."
            )
        part_rows = written if single_file else max(1000, min(int(part_rows), JPEG_MAX_ROWS))
        written_rows = written
    for idx, y0 in enumerate(range(0, written_rows, part_rows), start=1) if fmt not in ("tif", "tiff") else ():
        y1 = int(min(y0 + part_rows, written_rows))
        image = Image.fromarray(np.asarray(out[y0:y1]))
        path = out_dir / (f"{base_name}.jpg" if single_file else f"{base_name}_part{idx}.jpg")
        image.save(path, format="JPEG", quality=int(jpg_quality), dpi=(float(dpi), float(dpi)))
        parts.append({
            "part": idx,
            "format": "jpg",
            "path": str(path),
            "size_bytes": int(path.stat().st_size),
            "row_start": y0,
            "row_end": y1,
            "inches_start": round(y0 / float(dpi), 2),
            "inches_end": round(y1 / float(dpi), 2),
            "px": [int(image.size[0]), int(image.size[1])],
        })
        del image

    manifest = {
        "base_name": base_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "spool_path": str(spool_path),
        "raw_8bit_path": str(raw_out),
        "dpi": int(dpi),
        "capture_dpi": cap_dpi,
        "width_inches": float(width_inches),
        "rows": int(written),
        "inches": round(written / float(dpi), 2),
        "document_width_px": int(doc_w),
        "source_rows": total_rows,
        "bands": bands,
        "band_rows": band_rows,
        "overlap_rows": overlap_rows,
        "preset_id": preset.id,
        "gaps": [list(g) for g in (gaps or [])],
        "registration_dy": list(registration_dy) if registration_dy else None,
        "joins_px": joins_doc,
        "seam_equalization": seam_info,
        "parts": parts,
        "duration_seconds": round(time.time() - t0, 1),
        "jpeg_row_limit": JPEG_MAX_ROWS,
        "part_rows": part_rows,
        "out_format": fmt,
        "single_file": bool(single_file),
    }
    meta_path = out_dir / f"{base_name}.meta.json"
    meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["metadata_path"] = str(meta_path)
    return manifest


def load_preset(preset_id: str | None, *, serial: str = DEFAULT_SERIAL) -> CalibrationPreset:
    """Resolve a preset by id, defaulting to whatever the app has active."""
    store = PresetStore(serial=serial)
    if preset_id:
        return store.get_preset(preset_id)
    active = None
    settings = Path.home() / ".config" / "contex" / "app_settings.json"
    try:
        active = json.loads(settings.read_text(encoding="utf-8")).get("active_preset_id")
    except Exception:
        active = None
    if active:
        try:
            return store.get_preset(active)
        except Exception:
            pass
    presets = store.list_presets()
    if not presets:
        raise RuntimeError("no calibration presets found")
    return presets[0]
