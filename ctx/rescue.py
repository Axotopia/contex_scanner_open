"""Per-block channel-registration rescue for already-stitched documents.

Context (2026-11-02)
--------------------
The banded long-sheet path estimated the CIS red/green-vs-blue *row* offset per
4,000-row band.  Sparse, mostly-white bands disagreed with each other: one band
was 5 rows off in red while later bands were aligned, which reads as rainbow
fringing on some bands and not others (see ``ctx/registration.py``, where the
estimate is now made once and forced on every band).

A scan produced by that build cannot be fixed with a single global shift,
because the required shift differs per band.  This module repairs such a file
best-effort: it measures the offset in row blocks across the *stitched 8-bit
document*, fills low-confidence blocks from their neighbours, and shifts each
block's red and green channels onto blue.

It is interpolation on pixels that are already misregistered -- a stopgap to
avoid refeeding a long sheet, not a substitute for rescanning with the fixed
pipeline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np

#: Rows per correction block.  2,000 rows = 10 in at 200 dpi: small enough to
#: follow real drift, large enough to measure reliably on a sparse drawing.
BLOCK_ROWS = 2000
#: Search radius, in pixels, for the channel offset.
MAX_SHIFT = 8
#: Peak correlation a block needs before its own estimate is trusted.
MIN_QUALITY = 0.45
#: libjpeg's row limit, kept clear.
JPEG_MAX_ROWS = 60_000


def _best_shift(chan: np.ndarray, ref: np.ndarray, r: int = MAX_SHIFT) -> tuple[int, int, float]:
    """Integer (dx, dy) that best maps ``chan`` onto ``ref``, plus its score."""
    h, w = ref.shape
    ref_c = ref[r : h - r, r : w - r]
    ref_n = (ref_c - ref_c.mean()) / (ref_c.std() + 1e-6)
    best = (0, 0, -2.0)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            c = chan[r + dy : h - r + dy, r + dx : w - r + dx]
            cn = (c - c.mean()) / (c.std() + 1e-6)
            score = float((ref_n * cn).mean())
            if score > best[2]:
                best = (dx, dy, score)
    return best


def measure_offsets(
    src: np.memmap,
    *,
    block_rows: int = BLOCK_ROWS,
    x0: int = 200,
    x1: int | None = None,
    max_shift: int = MAX_SHIFT,
) -> list[dict[str, Any]]:
    """Measure the per-block red/green offset relative to blue."""
    total, width = src.shape[0], src.shape[1]
    x1 = min(x1 or width - 200, width - max_shift - 1)
    out: list[dict[str, Any]] = []
    for y0 in range(0, total, block_rows):
        y1 = min(y0 + block_rows, total)
        # Central slice of the block, full resolution: downsampling would
        # quantise the very offsets we are measuring.
        cy0 = min(y0 + max(0, (y1 - y0 - 600) // 2), max(0, y1 - 600))
        block = np.array(src[cy0 : min(cy0 + 600, total), x0:x1], dtype=np.float32)
        if block.shape[0] < 2 * max_shift + 8:
            continue
        blue = block[..., 2]
        rdx, rdy, rq = _best_shift(block[..., 0], blue, max_shift)
        gdx, gdy, gq = _best_shift(block[..., 1], blue, max_shift)
        out.append({
            "row_start": y0,
            "row_end": y1,
            "red": [rdx, rdy, round(rq, 3)],
            "green": [gdx, gdy, round(gq, 3)],
            "confident": bool(min(rq, gq) >= MIN_QUALITY),
        })
    return out


def _fill_and_smooth(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interpolate low-confidence blocks from confident neighbours, then smooth."""
    confident = [i for i, b in enumerate(blocks) if b["confident"]]
    if not confident:
        return blocks

    def interp(idx: int, axis: int, comp: int) -> float:
        before = [i for i in confident if i < idx]
        after = [i for i in confident if i > idx]
        if not before:
            return float(blocks[after[0]][axis][comp])
        if not after:
            return float(blocks[before[-1]][axis][comp])
        i0, i1 = before[-1], after[0]
        t = (idx - i0) / float(i1 - i0)
        v0 = float(blocks[i0][axis][comp])
        v1 = float(blocks[i1][axis][comp])
        return v0 * (1.0 - t) + v1 * t

    for i, b in enumerate(blocks):
        if b["confident"]:
            continue
        b["red"] = [int(round(interp(i, "red", 0))), int(round(interp(i, "red", 1))), b["red"][2]]
        b["green"] = [int(round(interp(i, "green", 0))), int(round(interp(i, "green", 1))), b["green"][2]]
        b["interpolated"] = True

    # A median of three kills single-block outliers without blurring real drift.
    for axis in ("red", "green"):
        seq = [b[axis][:2] for b in blocks]
        for i in range(len(blocks)):
            window = seq[max(0, i - 1) : min(len(seq), i + 2)]
            dxs = sorted(w[0] for w in window)
            dys = sorted(w[1] for w in window)
            blocks[i][axis] = [dxs[len(dxs) // 2], dys[len(dys) // 2], blocks[i][axis][2]]
    return blocks


def _apply_block(block: np.ndarray, dx: int, dy: int, channel: int) -> None:
    """In-place shift of one channel by whole pixels, with edge clamping."""
    if dx == 0 and dy == 0:
        return
    h, w = block.shape[:2]
    ys = np.clip(np.arange(h) + dy, 0, h - 1)
    xs = np.clip(np.arange(w) + dx, 0, w - 1)
    block[:, :, channel] = block[ys][:, xs][:, :, channel]


def rescue(
    raw_path: str | Path,
    output_dir: str | Path,
    *,
    rows: int,
    width: int,
    base_name: str = "corrected",
    jpg_quality: int = 95,
    block_rows: int = BLOCK_ROWS,
    progress: Any = None,
) -> dict[str, Any]:
    """Correct per-block channel registration and write JPG parts + a manifest."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    t0 = time.time()
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    src = np.memmap(raw_path, dtype=np.uint8, mode="r", shape=(rows, width, 3))

    blocks = _fill_and_smooth(measure_offsets(src, block_rows=block_rows, x1=min(width, 3600)))
    fixed_raw = out_dir / f"{base_name}-8bit.raw"
    dst = np.memmap(fixed_raw, dtype=np.uint8, mode="w+", shape=(rows, width, 3))

    for b in blocks:
        y0, y1 = b["row_start"], b["row_end"]
        block = np.array(src[y0:y1], dtype=np.uint8)
        _apply_block(block, b["red"][0], b["red"][1], 0)
        _apply_block(block, b["green"][0], b["green"][1], 1)
        dst[y0:y1] = block
        if progress is not None:
            progress({"row_end": y1, "rows": rows})
    dst.flush()

    parts: list[dict[str, Any]] = []
    for idx, y0 in enumerate(range(0, rows, JPEG_MAX_ROWS), start=1):
        y1 = min(y0 + JPEG_MAX_ROWS, rows)
        im = Image.fromarray(np.asarray(dst[y0:y1]))
        p = out_dir / f"{base_name}_part{idx}.jpg"
        im.save(p, format="JPEG", quality=int(jpg_quality))
        parts.append({"part": idx, "path": str(p), "row_start": y0, "row_end": y1,
                      "size_bytes": p.stat().st_size})
        del im

    # Residual, re-measured on the corrected document.
    residual = []
    for b in measure_offsets(dst, block_rows=block_rows, x1=min(width, 3600)):
        residual.append({"row_start": b["row_start"], "red": b["red"][:2],
                         "green": b["green"][:2], "confident": b["confident"]})

    manifest = {
        "base_name": base_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_raw": str(raw_path),
        "corrected_raw": str(fixed_raw),
        "rows": rows,
        "width": width,
        "block_rows": block_rows,
        "blocks": [{k: b[k] for k in ("row_start", "row_end", "red", "green")} for b in blocks],
        "residual_after": residual,
        "parts": parts,
        "duration_seconds": round(time.time() - t0, 1),
        "note": (
            "Best-effort repair of per-band channel misregistration in an already "
            "stitched document. Rescan with the fixed pipeline for full quality."
        ),
    }
    meta = out_dir / f"{base_name}.meta.json"
    meta.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["metadata_path"] = str(meta)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", required=True, help="8-bit document memmap (rows*width*3 bytes)")
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--out", default="~/Pictures")
    ap.add_argument("--prefix", default="corrected")
    ap.add_argument("--block-rows", type=int, default=BLOCK_ROWS)
    args = ap.parse_args(argv)
    man = rescue(args.raw, args.out, rows=args.rows, width=args.width,
                 base_name=args.prefix, block_rows=args.block_rows,
                 progress=lambda m: print(f"  corrected to row {m['row_end']:,}", flush=True))
    print(f"{man['rows']:,} rows, {len(man['blocks'])} blocks, {man['duration_seconds']}s")
    for p in man["parts"]:
        print(f"  part {p['part']}: {p['path']} ({p['size_bytes']/1e6:.1f} MB)")
    print(f"  manifest: {man['metadata_path']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
