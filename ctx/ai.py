"""Generative AI, Photocraft connector, and local restoration filters for scans.

Designed for two primary use cases on the Ubuntu RTX 4080 workstation:
  1. **3D Architectural Drawings & CAD**:
     - 4-seam micro-inpainting across the known CIS joins (x = 2658, 5320, 7982, 10644)
     - Adaptive background paper neutralization (preserving 3D shading gradients)
     - Crisp linework / detail enhancement
  2. **Artwork & Large-Format Photography**:
     - Edge-preserving shadow & chroma denoising (specifically targeting the 2.8x
       red-gain noise floor on Cameras 4 & 5)
     - External MCP / Photocraft / Generative AI handoff for deep restoration
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import io
from typing import Any
import urllib.request
import json

import numpy as np
from PIL import Image, ImageFilter


@dataclass
class EnhancementOptions:
    blend_seam_colors: bool = True
    seam_blend_width_px: int = 128
    seam_blend_strength: float = 0.85
    seam_feather_mode: str = "classic"
    heal_seams: bool = True
    seam_radius_px: int = 6
    clean_background: bool = False
    background_strength: float = 0.65
    denoise_shadows: bool = False
    denoise_strength: float = 0.55
    sharpen_detail: bool = False
    sharpen_amount: float = 0.45
    external_provider: str = "none"   # "none" | "photocraft_webhook" | "openai"
    external_url: str = ""
    external_api_key: str = ""
    external_prompt: str = "Clean and enhance scan while preserving exact geometry and fine details."
    extra: dict[str, Any] = field(default_factory=dict)


def measure_seam_step_u16(
    doc_u16: np.ndarray,
    joins: list[int],
    probe_px: int = 6,
) -> dict[str, Any]:
    """Measure per-seam and overall RGB step differences across the adjacent pixels of each camera join."""
    if not joins or doc_u16.ndim != 3:
        return {"mean_rgb_step": 0.0, "seams": []}
    _, w, _ = doc_u16.shape
    p = max(2, min(24, int(probe_px)))
    seams_info: list[dict[str, Any]] = []
    steps: list[float] = []

    for idx, j in enumerate(joins):
        if j - p < 0 or j + p > w:
            continue
        left_mean = doc_u16[:, j - p : j, :].mean(axis=(0, 1), dtype=np.float64)
        right_mean = doc_u16[:, j : j + p, :].mean(axis=(0, 1), dtype=np.float64)
        delta_rgb = np.abs(right_mean - left_mean)
        step_val = float(delta_rgb.mean())
        steps.append(step_val)
        seams_info.append({
            "seam_index": idx,
            "join_x": int(j),
            "delta_rgb": [round(float(x), 1) for x in delta_rgb],
            "mean_step_u16": round(step_val, 1),
        })

    return {
        "mean_rgb_step": round(float(np.mean(steps)), 1) if steps else 0.0,
        "seams": seams_info,
    }


def blend_seam_color_variation_u16(
    doc_u16: np.ndarray,
    joins: list[int],
    blend_width_px: int = 128,
    strength: float = 0.85,
    probe_px: int = 10,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Equalize and blend scan-to-scan analog camera color/luminance variations across
    adjacent pixels on both sides of each seam join ``(x = 2658, 5320, 7982, 10644)``.

    How it works:
      1. Samples adjacent boundary strips ``[j - probe_px : j]`` (left camera)
         and ``[j : j + probe_px]`` (right camera) for each seam ``j``.
      2. Rejects high-gradient rows where dark linework or object edges cross the seam,
         then vertically smooths the per-row RGB discrepancy ``Delta(y, ch)`` along the
         scan direction to track both static camera gain differences and gradual
         top-to-bottom LED thermal drift during the scan.
      3. Applies ``+0.5 * Delta`` to the left camera and ``-0.5 * Delta`` to the right
         camera using a C1-continuous raised-cosine (half-Hann) taper over
         ``[j - blend_width_px, j + blend_width_px]``, with dark-ink protection so
         black linework stays crisp and neutral while background/midtone colors meet
         seamlessly at ``x = j``.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    half_w = int(np.clip(blend_width_px, 8, 4096))
    before_stats = measure_seam_step_u16(doc_u16, joins)

    if strength <= 0.0 or half_w <= 0 or not joins:
        return doc_u16, {
            "enabled": False,
            "blend_width_px": half_w,
            "strength": strength,
            "before_rgb_step_mean": before_stats["mean_rgb_step"],
            "after_rgb_step_mean": before_stats["mean_rgb_step"],
            "seams_before": before_stats["seams"],
            "seams_after": before_stats["seams"],
        }

    out = doc_u16.copy()
    h, w, _ = out.shape

    for j in joins:
        hw = min(half_w, j - 2, w - j - 2)
        if hw < 6:
            continue
        pw = min(max(4, int(probe_px)), max(4, hw // 4))
        left_strip = out[:, j - pw : j, :].astype(np.float32)
        right_strip = out[:, j : j + pw, :].astype(np.float32)

        L_row = np.maximum(left_strip.mean(axis=1), 64.0)   # (H, 3)
        R_row = np.maximum(right_strip.mean(axis=1), 64.0)  # (H, 3)
        raw_log_ratio = np.log(R_row / L_row)               # (H, 3)

        # Reject rows where an ink line or sharp object edge sits on one side of the seam
        L_std = left_strip.std(axis=1).mean(axis=1)
        R_std = right_strip.std(axis=1).mean(axis=1)
        step_mag = np.abs(raw_log_ratio).mean(axis=1)
        med_step = float(np.median(step_mag))
        mad_step = float(np.median(np.abs(step_mag - med_step))) * 1.4826 + 0.015
        valid_rows = (
            (L_std < 6500.0)
            & (R_std < 6500.0)
            & (L_row.mean(axis=1) > 1500.0)
            & (R_row.mean(axis=1) > 1500.0)
            & (np.abs(step_mag - med_step) < max(3.5 * mad_step, 0.12))
        )

        log_ratio_clean = raw_log_ratio.copy()
        if valid_rows.any() and not valid_rows.all():
            y_all = np.arange(h, dtype=np.float32)
            y_ok = y_all[valid_rows]
            for ch in range(3):
                log_ratio_clean[:, ch] = np.interp(y_all, y_ok, raw_log_ratio[valid_rows, ch])
        elif not valid_rows.any():
            med_vec = np.median(raw_log_ratio, axis=0)
            log_ratio_clean[:] = med_vec[None, :]

        # Smooth vertically along scan rows to track gradual analog LED warm-up
        win = min(h, max(5, (h // 6) | 1))
        if h >= win and win >= 3:
            pad_y = win // 2
            kernel = np.ones(win, dtype=np.float32) / float(win)
            log_ratio_smooth = np.empty_like(log_ratio_clean)
            for ch in range(3):
                padded = np.pad(log_ratio_clean[:, ch], (pad_y, pad_y), mode="edge")
                log_ratio_smooth[:, ch] = np.convolve(padded, kernel, mode="valid")[:h]
        else:
            log_ratio_smooth = log_ratio_clean

        # Clamp log-ratio to plausible analog camera variation (+/- 0.45 -> ~1.56x max gain)
        log_ratio_smooth = np.clip(log_ratio_smooth, -0.45, 0.45) * strength

        # Raised-cosine (half-Hann) spatial taper with a flat core across the probe window
        core_w = min(pw, max(4, hw // 8))
        ramp_w = max(1, hw - core_w)
        u_ramp = np.linspace(0.0, 1.0, ramp_w, dtype=np.float32)
        ramp_curve = 0.5 * (1.0 - np.cos(np.pi * u_ramp))
        taper_1d = np.concatenate([ramp_curve, np.ones(core_w, dtype=np.float32)])
        taper_left = taper_1d[None, :, None]           # (1, hw, 1): 0 at outer edge -> 1 at seam
        taper_right = taper_left[:, ::-1, :]           # (1, hw, 1): 1 at seam -> 0 at outer edge

        left_zone = out[:, j - hw : j, :].astype(np.float32)
        right_zone = out[:, j : j + hw, :].astype(np.float32)

        # Multiplicative gain equalization (+0.5 * log_ratio on left, -0.5 * log_ratio on right)
        half_log = 0.5 * log_ratio_smooth[:, None, :]  # (H, 1, 3)
        gain_left = np.exp(half_log * taper_left)
        gain_right = np.exp(-half_log * taper_right)

        left_corr = left_zone * gain_left
        right_corr = right_zone * gain_right

        # Also remove any small residual additive offset right near the seam
        L_after = (left_corr[:, -pw:, :]).mean(axis=1)
        R_after = (right_corr[:, :pw, :]).mean(axis=1)
        res_delta = np.clip((R_after - L_after) * strength, -4000.0, 4000.0)
        if h >= win and win >= 3:
            pad_y = win // 2
            kernel = np.ones(win, dtype=np.float32) / float(win)
            for ch in range(3):
                padded = np.pad(res_delta[:, ch], (pad_y, pad_y), mode="edge")
                res_delta[:, ch] = np.convolve(padded, kernel, mode="valid")[:h]

        left_corr = left_corr + 0.5 * res_delta[:, None, :] * taper_left
        right_corr = right_corr - 0.5 * res_delta[:, None, :] * taper_right

        out[:, j - hw : j, :] = np.clip(np.rint(left_corr), 0, 65535).astype(np.uint16)
        out[:, j : j + hw, :] = np.clip(np.rint(right_corr), 0, 65535).astype(np.uint16)

    after_stats = measure_seam_step_u16(out, joins)
    return out, {
        "enabled": True,
        "blend_width_px": half_w,
        "strength": round(strength, 3),
        "before_rgb_step_mean": before_stats["mean_rgb_step"],
        "after_rgb_step_mean": after_stats["mean_rgb_step"],
        "seams_before": before_stats["seams"],
        "seams_after": after_stats["seams"],
    }


def _smooth_rows_box(a: np.ndarray, win: int) -> np.ndarray:
    """Box-smooth a (rows, channels) profile along the scan direction."""
    if win <= 1 or a.shape[0] < 3:
        return a
    win = max(3, int(win) | 1)
    pad = win // 2
    kernel = np.ones(win, dtype=np.float32) / float(win)
    out = np.empty_like(a)
    for c in range(a.shape[1]):
        out[:, c] = np.convolve(np.pad(a[:, c], (pad, pad), mode="edge"), kernel, mode="valid")[: a.shape[0]]
    return out


def blend_seam_color_artwork_u16(
    doc_u16: np.ndarray,
    joins: list[int],
    blend_width_px: int = 512,
    strength: float = 0.85,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Artwork-friendly seam feather.

    Unlike the classic blend, this uses a continuous half-Hann gain/offset field
    with no flat core.  That avoids the narrow dark band the classic core can
    produce on smooth artwork, at the cost of a wider colour transition.  Detail
    is preserved because the correction is a smooth per-row gain + offset; it is
    not an image cross-fade.  A wide width (e.g. 512 px at 600 dpi) is intended
    for paintings/photographs rather than line art.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    half_w = int(np.clip(blend_width_px, 8, 4096))
    before = measure_seam_step_u16(doc_u16, joins)
    if strength <= 0.0 or not joins:
        return doc_u16, {
            "enabled": False, "mode": "artwork", "blend_width_px": half_w,
            "strength": strength, "before_rgb_step_mean": before["mean_rgb_step"],
            "after_rgb_step_mean": before["mean_rgb_step"],
            "seams_before": before["seams"], "seams_after": before["seams"],
        }

    out = doc_u16.astype(np.float32).copy()
    h, w, _ = out.shape
    smooth_win = min(513, max(9, (h // 16) | 1))
    for j in joins:
        hw = min(half_w, j - 2, w - j - 2)
        if hw < 8:
            continue
        pw = min(10, max(4, hw // 4))
        left_strip = out[:, j - pw : j, :]
        right_strip = out[:, j : j + pw, :]
        L = left_strip.mean(axis=1, dtype=np.float32)
        R = right_strip.mean(axis=1, dtype=np.float32)
        log_ratio = np.log(np.maximum(R, 64.0) / np.maximum(L, 64.0))
        log_ratio = _smooth_rows_box(log_ratio, smooth_win)
        log_ratio = np.clip(log_ratio, -0.45, 0.45) * strength

        hue = np.arange(hw, dtype=np.float32)
        taper = 0.5 * (1.0 + np.cos(np.pi * hue / float(hw)))
        out[:, j - hw : j, :] *= np.exp(0.5 * log_ratio[:, None, :] * taper[None, :, None])
        out[:, j : j + hw, :] *= np.exp(-0.5 * log_ratio[:, None, :] * taper[None, :, None])

        # Small additive residual after the gain, feathered with the same taper.
        L2 = out[:, j - pw : j, :].mean(axis=1, dtype=np.float32)
        R2 = out[:, j : j + pw, :].mean(axis=1, dtype=np.float32)
        res = _smooth_rows_box(R2 - L2, smooth_win)
        res = np.clip(res, -4000.0, 4000.0) * strength
        out[:, j - hw : j, :] += 0.5 * res[:, None, :] * taper[None, :, None]
        out[:, j : j + hw, :] -= 0.5 * res[:, None, :] * taper[None, :, None]

    out = np.clip(np.rint(out), 0, 65535).astype(np.uint16)
    after = measure_seam_step_u16(out, joins)
    return out, {
        "enabled": True, "mode": "artwork", "blend_width_px": half_w,
        "strength": round(strength, 3),
        "before_rgb_step_mean": before["mean_rgb_step"],
        "after_rgb_step_mean": after["mean_rgb_step"],
        "seams_before": before["seams"], "seams_after": after["seams"],
    }


def heal_seams_u16(
    doc_u16: np.ndarray,
    joins: list[int],
    radius: int = 6,
) -> np.ndarray:
    """Micro-inpaint and feather across the 4 camera seam joins ``(x = 2658, 5320, 7982, 10644)``.

    Uses local gradient-aware continuity interpolation across ``[j - radius, j + radius]``
    to eliminate 1-px butt-join steps while leaving all other columns untouched.
    """
    if radius <= 0 or not joins:
        return doc_u16
    out = doc_u16.copy()
    h, w, _ = out.shape
    r = max(1, min(32, int(radius)))

    for j in joins:
        if j - r - 2 < 0 or j + r + 2 >= w:
            continue
        left_col = out[:, j - r - 1 : j - r + 1, :].mean(axis=1, dtype=np.float32)
        right_col = out[:, j + r : j + r + 2, :].mean(axis=1, dtype=np.float32)

        # Also incorporate vertical 3-row median on the interpolated bridge to smooth
        # +1..+2 row y-offsets between adjacent CIS modules (STITCH-REPORT.md §6)
        span = 2 * r
        alphas = np.linspace(0.0, 1.0, span, dtype=np.float32)[None, :, None]
        bridge = (1.0 - alphas) * left_col[:, None, :] + alphas * right_col[:, None, :]

        if h >= 5:
            padded = np.pad(bridge, ((1, 1), (0, 0), (0, 0)), mode="edge")
            bridge_smooth = 0.25 * padded[:-2] + 0.50 * padded[1:-1] + 0.25 * padded[2:]
        else:
            bridge_smooth = bridge

        orig_band = out[:, j - r : j + r, :].astype(np.float32)
        # Weight peaks at 0.85 right at the join and tapers to 0.0 at +/- r
        dist = np.abs(np.linspace(-1.0, 1.0, span, dtype=np.float32))[None, :, None]
        w_blend = (1.0 - dist) * 0.85
        blended = (1.0 - w_blend) * orig_band + w_blend * bridge_smooth
        out[:, j - r : j + r, :] = np.clip(np.rint(blended), 0, 65535).astype(np.uint16)

    return out


def clean_architectural_background_u16(
    doc_u16: np.ndarray,
    strength: float = 0.65,
) -> np.ndarray:
    """Adaptive background paper normalization for 3D architectural drawings.

    Neutralizes subtle paper tint/unevenness in bright low-variance background regions
    while preserving 3D shaded gradients, shadows, and crisp linework.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0:
        return doc_u16

    h, w, _ = doc_u16.shape
    f = doc_u16.astype(np.float32)
    lum = f.mean(axis=2)
    p92 = float(np.percentile(lum[::4, ::4], 92))
    if p92 < 8000.0:
        return doc_u16

    # Smooth transition mask for pixels near paper white (> 0.82 * p92)
    low_thr = 0.80 * p92
    high_thr = 0.96 * p92
    mask = np.clip((lum - low_thr) / max(high_thr - low_thr, 1.0), 0.0, 1.0)
    mask = (mask ** 2) * strength

    target_white = min(65535.0, max(p92 * 1.04, 60000.0))
    cleaned = f + mask[:, :, None] * (target_white - f)
    return np.clip(np.rint(cleaned), 0, 65535).astype(np.uint16)


def denoise_shadows_u16(
    doc_u16: np.ndarray,
    strength: float = 0.55,
) -> np.ndarray:
    """Edge-preserving chroma and shadow denoiser.

    Specifically addresses the higher red-channel gain noise on Cameras 4 & 5 in
    darker tones while keeping luminance edges sharp.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0:
        return doc_u16

    # Convert to 8-bit working copy for fast median/box chroma filtering, apply delta in 16-bit
    f = doc_u16.astype(np.float32)
    lum = 0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2]

    # Downsample chroma channels (R-Y, B-Y) by 2x, smooth, and upsample to remove colour speckle
    cr = f[:, :, 0] - lum
    cb = f[:, :, 2] - lum

    # 3x3 separable box smooth on chroma
    def _smooth2d(arr: np.ndarray) -> np.ndarray:
        p = np.pad(arr, ((1, 1), (1, 1)), mode="edge")
        h_sm = (p[:, :-2] + p[:, 1:-1] + p[:, 2:]) / 3.0
        v_sm = (h_sm[:-2, :] + h_sm[1:-1, :] + h_sm[2:, :]) / 3.0
        return v_sm

    cr_sm = _smooth2d(_smooth2d(cr))
    cb_sm = _smooth2d(_smooth2d(cb))
    lum_sm = _smooth2d(lum)

    # Shadow weight: stronger in dark/mid tones where sensor noise is visible
    shadow_w = np.clip(1.0 - (lum / 48000.0), 0.15, 1.0) * strength
    # Edge protection: reduce luminance smoothing at strong edges
    edge_mag = np.abs(lum - lum_sm)
    edge_guard = np.clip(1.0 - edge_mag / 3500.0, 0.0, 1.0)
    lum_w = shadow_w * edge_guard * 0.5

    lum_out = lum * (1.0 - lum_w) + lum_sm * lum_w
    cr_out = cr * (1.0 - shadow_w) + cr_sm * shadow_w
    cb_out = cb * (1.0 - shadow_w) + cb_sm * shadow_w

    r_out = lum_out + cr_out
    b_out = lum_out + cb_out
    g_out = (lum_out - 0.299 * r_out - 0.114 * b_out) / 0.587

    out = np.stack([r_out, g_out, b_out], axis=2)
    return np.clip(np.rint(out), 0, 65535).astype(np.uint16)


def enhance_detail_u16(
    doc_u16: np.ndarray,
    amount: float = 0.45,
) -> np.ndarray:
    """Edge-aware unsharp mask for crisp architectural linework and artwork texture."""
    amount = float(np.clip(amount, 0.0, 1.5))
    if amount <= 0.0:
        return doc_u16

    f = doc_u16.astype(np.float32)
    p = np.pad(f, ((1, 1), (1, 1), (0, 0)), mode="edge")
    blur = (
        p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:]
        + 2 * p[1:-1, :-2] + 4 * p[1:-1, 1:-1] + 2 * p[1:-1, 2:]
        + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]
    ) / 16.0
    diff = f - blur
    # Coring threshold: ignore tiny noise below 350 u16 (~1.3 in 8-bit) so flat paper stays clean
    mask = np.abs(diff) > 350.0
    sharpened = f + np.where(mask, diff * (amount * 1.4), 0.0)
    return np.clip(np.rint(sharpened), 0, 65535).astype(np.uint16)


def call_external_photocraft_webhook(
    doc_u16: np.ndarray,
    url: str,
    prompt: str,
    api_key: str = "",
    timeout: float = 60.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Send an 8-bit PNG representation of the scan (or tile) to an external
    Photocraft / MCP HTTP endpoint and decode the returned enhanced image back to uint16."""
    srgb8 = (np.clip(doc_u16.astype(np.float32) / 65535.0, 0.0, 1.0) * 255.0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(srgb8).save(buf, format="PNG")
    b64_img = base64.b64encode(buf.getvalue()).decode("ascii")

    payload = json.dumps({
        "prompt": prompt,
        "image_base64": b64_img,
        "width": int(doc_u16.shape[1]),
        "height": int(doc_u16.shape[0]),
        "source": "contex-hd-ultra-i4250s",
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))

    out_b64 = resp_data.get("image_base64") or resp_data.get("result_base64")
    if not out_b64:
        raise ValueError("External AI endpoint did not return 'image_base64'")

    out_img = Image.open(io.BytesIO(base64.b64decode(out_b64))).convert("RGB")
    if out_img.size != (doc_u16.shape[1], doc_u16.shape[0]):
        out_img = out_img.resize((doc_u16.shape[1], doc_u16.shape[0]), Image.Resampling.LANCZOS)
    arr8 = np.asarray(out_img, dtype=np.uint16)
    return (arr8 * 257).astype(np.uint16), {"provider": "photocraft_webhook", "status": "ok"}


def run_enhancement_pipeline(
    doc_u16: np.ndarray,
    joins: list[int],
    options: EnhancementOptions,
) -> tuple[np.ndarray, list[str]]:
    """Run the selected chain of local and/or external AI post-processing filters."""
    result = doc_u16
    applied: list[str] = []

    if options.blend_seam_colors:
        feather_mode = str(options.seam_feather_mode or "classic").lower()
        if feather_mode == "artwork":
            result, _seam_stats = blend_seam_color_artwork_u16(
                result,
                joins,
                blend_width_px=options.seam_blend_width_px,
                strength=options.seam_blend_strength,
            )
        else:
            result, _seam_stats = blend_seam_color_variation_u16(
                result,
                joins,
                blend_width_px=options.seam_blend_width_px,
                strength=options.seam_blend_strength,
            )
        applied.append(
            f"seam_color_blend({feather_mode},width={options.seam_blend_width_px}px,"
            f"strength={options.seam_blend_strength:.2f})"
        )

    if options.heal_seams:
        result = heal_seams_u16(result, joins, radius=options.seam_radius_px)
        applied.append(f"seam_heal(radius={options.seam_radius_px}px)")

    if options.denoise_shadows:
        result = denoise_shadows_u16(result, strength=options.denoise_strength)
        applied.append(f"shadow_denoise(strength={options.denoise_strength:.2f})")

    if options.clean_background:
        result = clean_architectural_background_u16(result, strength=options.background_strength)
        applied.append(f"architectural_clean(strength={options.background_strength:.2f})")

    if options.sharpen_detail:
        result = enhance_detail_u16(result, amount=options.sharpen_amount)
        applied.append(f"detail_enhance(amount={options.sharpen_amount:.2f})")

    if options.external_provider == "photocraft_webhook" and options.external_url:
        result, _meta = call_external_photocraft_webhook(
            result,
            url=options.external_url,
            prompt=options.external_prompt,
            api_key=options.external_api_key,
        )
        applied.append(f"external_ai({options.external_url})")

    return result, applied

