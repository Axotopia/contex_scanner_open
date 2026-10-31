"""Model Context Protocol (MCP) Server for the Contex HD Ultra i4250s Scanner.

Exposes the scanner hardware, multi-preset calibration engine, thermal drift
analyzer, 16-bit 5-camera stitcher, and AI / Photocraft post-processing pipeline
as standard MCP tools and resources.

Run via:
    python -m ctx mcp               # stdio transport (for Claude / Antigravity / IDEs)
    python -m ctx mcp --sse         # SSE HTTP transport
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .ai import EnhancementOptions
from .server import ScanWorkspaceManager

mcp = FastMCP(
    "Contex-HD-Ultra-i4250s",
    instructions=(
        "Controls the Contex HD Ultra i4250s 42-inch 5-camera large-format scanner. "
        "Workflow: scanner_status -> scanner_initiate_scan -> scanner_resolve_drift "
        "(or scanner_calibrate_official_sheet) -> scanner_ai_enhance -> scanner_export."
    ),
)

_workspace: ScanWorkspaceManager | None = None


def get_workspace() -> ScanWorkspaceManager:
    global _workspace
    if _workspace is None:
        _workspace = ScanWorkspaceManager()
    return _workspace


@mcp.tool()
def scanner_status() -> dict[str, Any]:
    """Probe the Contex HD Ultra i4250s hardware status, Gigabit Ethernet preflight,
    media status (0x30 READY / 0x24 SCANNING), active calibration preset, and recent scans."""
    ws = get_workspace()
    hw = ws.engine.probe_status(timeout=1.0)
    return {
        "hardware": hw,
        "settings": ws.settings.to_dict(),
        "presets": [
            {"id": p.id, "name": p.name, "description": p.description, "updated": p.updated}
            for p in ws.preset_store.list_presets()
        ],
        "available_rec_files": ws.list_available_rec_files(),
    }


@mcp.tool()
def scanner_paper_control(action: str = "load") -> dict[str, Any]:
    """Move or recover media in the scanner feeder.
    Valid actions: 'load', 'unload', 'eject_front', 'eject_back', 'recover'."""
    ws = get_workspace()
    try:
        return ws.engine.paper_control(action=action)
    except Exception as exc:
        return {
            "action": action,
            "ok": False,
            "simulated": True,
            "message": f"Hardware unreachable ({exc}); validated '{action}' command.",
        }


@mcp.tool()
def scanner_cancel_scan() -> dict[str, Any]:
    """Request cancellation of the active scan at its next safe point (capture or
    post-processing).  The scanner may finish feeding the sheet; use paper_recover
    or an eject action to clear the media afterwards."""
    return get_workspace().cancel_active_scan()


@mcp.tool()
def scanner_initiate_scan(
    source_mode: str = "live",
    rec_path: str | None = None,
    paper_size: str = "full_42",
    orientation: str = "portrait",
    width_inches: float = 42.0,
    dpi: int = 300,
    color_mode: str = "color",
    preset_id: str | None = None,
    simulate_thermal_drift: bool = True,
    seam_color_blend: bool | None = None,
    seam_blend_width_px: int | None = None,
    seam_blend_strength: float | None = None,
    seam_feather_mode: str = "classic",
) -> dict[str, Any]:
    """Initiate a scan, collect the 5-camera 16-bit stream, apply per-camera color
    adjustments, check for operating-temperature color drift, stitch the 5 cameras,
    and autosave if no major drift requires user confirmation.

    source_mode:
      - 'live': Scan from physical Contex HD Ultra i4250s over Ethernet
      - 'rec_file': Process an existing .rec capture file at rec_path
      - 'demo_architectural': Generate a 5-camera 3D architectural + IT8 test scan
      - 'demo_artwork': Generate a 5-camera full-bleed artwork test scan
    paper_size:
      - 'auto': Nextimage-style Auto-Detect Width (crops to detected paper edge & snaps to ISO/ARCH/ANSI)
      - 'full_42' | 'arch_e' | 'arch_e1' | 'arch_d' | 'arch_c' | 'iso_a0' | 'iso_a1' | 'iso_a2' | 'ansi_e' | 'ansi_d' | 'ansi_c' | 'ansi_b' | 'ansi_a' | 'custom'
    orientation: 'portrait' (short edge cross-feed) | 'landscape' (long edge, clamped to the 42 in bed)
    dpi: 100 | 200 | 300 | 400 | 600 | 1200
    seam_color_blend: Enable/disable analog seam color variation blending across adjacent pixels
    seam_blend_width_px: Half-width in pixels on each side of the seam (8..4096, default 128)
    seam_blend_strength: Blend strength (0.0..1.0, default 0.85)
    seam_feather_mode: 'classic' (crisp) or 'artwork' (soft, wide colour feather)
    """
    ws = get_workspace()
    job = ws.execute_scan_workflow(
        source_mode=source_mode,
        rec_path=rec_path,
        paper_size=paper_size,
        orientation=orientation,
        width_inches=width_inches,
        dpi=dpi,
        color_mode=color_mode,
        preset_id=preset_id,
        simulate_thermal_drift=simulate_thermal_drift,
        seam_color_blend=seam_color_blend,
        seam_blend_width_px=seam_blend_width_px,
        seam_blend_strength=seam_blend_strength,
        seam_feather_mode=seam_feather_mode,
    )
    return job.to_public_dict()


@mcp.tool()
def scanner_resolve_drift(
    scan_id: str,
    choice: str = "auto_adjust",
    promote_to_preset: bool = False,
    save_as_new_preset_name: str | None = None,
) -> dict[str, Any]:
    """Resolve a thermal/lamp color drift warning on a scan by choosing between
    the stored preset ('preset') or the margin self-calibrated render ('auto_adjust'),
    optionally updating or saving a new preset, then autosaving."""
    ws = get_workspace()
    job = ws.resolve_drift_decision(
        scan_id,
        choice=choice,
        promote_to_preset=promote_to_preset,
        save_as_new_preset_name=save_as_new_preset_name,
    )
    return job.to_public_dict()


@mcp.tool()
def scanner_calibrate_official_sheet(
    scan_id: str,
    sheet_mode: str = "it8_full",
    save_as_preset_id: str | None = None,
    save_as_preset_name: str | None = None,
    auto_align: bool = True,
) -> dict[str, Any]:
    """Calibrate the 5 cameras from a scan of the Contex Official Calibration Sheet
    (p/n 6779G014, Charge R170829).
    sheet_mode: 'white_flatfield' or 'it8_full' (fits 264 IT8 patches + white/black bands).
    auto_align: sweep the 22x12 sampling window and use the offset with the lowest dE76."""
    ws = get_workspace()
    return ws.calibrate_with_official_sheet(
        scan_id,
        sheet_mode=sheet_mode,
        save_as_preset_id=save_as_preset_id,
        save_as_preset_name=save_as_preset_name,
        auto_align=auto_align,
    )


@mcp.tool()
def scanner_ai_enhance(
    scan_id: str,
    blend_seam_colors: bool = True,
    seam_blend_width_px: int = 128,
    seam_blend_strength: float = 0.85,
    seam_feather_mode: str = "classic",
    heal_seams: bool = True,
    seam_radius_px: int = 6,
    clean_background: bool = True,
    denoise_shadows: bool = True,
    sharpen_detail: bool = True,
    external_provider: str = "none",
    external_url: str = "",
    external_prompt: str = "Clean and enhance scan while preserving exact geometry and fine details.",
) -> dict[str, Any]:
    """Post-process a stitched scan with local restoration filters (analog seam color variation
    blending, 4-seam micro-inpainting, architectural background cleanup, Camera 4/5 shadow
    denoising, detail sharpening) and/or an external Generative AI / Photocraft endpoint."""
    ws = get_workspace()
    opts = EnhancementOptions(
        blend_seam_colors=blend_seam_colors,
        seam_blend_width_px=seam_blend_width_px,
        seam_blend_strength=seam_blend_strength,
        seam_feather_mode=seam_feather_mode,
        heal_seams=heal_seams,
        seam_radius_px=seam_radius_px,
        clean_background=clean_background,
        denoise_shadows=denoise_shadows,
        sharpen_detail=sharpen_detail,
        external_provider=external_provider,
        external_url=external_url,
        external_prompt=external_prompt,
    )
    job = ws.run_ai_enhancement(scan_id, opts)
    return job.to_public_dict()


@mcp.tool()
def scanner_export(
    scan_id: str,
    formats: list[str] | None = None,
    color_mode: str | None = None,
    tiff_16bit: bool = False,
    debug_mode: bool | None = None,
    render_mode: str | None = None,
) -> dict[str, Any]:
    """Export a scan to TIFF, JPG, and/or PDF in Color ('color') or Grayscale ('grayscale').

    ``render_mode`` picks which calibrated render to write: ``"preset"`` (default
    stored preset), ``"auto_adjust"`` (margin self-calibrated) or ``"ai_enhanced"``
    (after post-processing). All variants of a scan share its number, e.g.
    ``scan_0087``, ``scan_0087_Auto``, ``scan_0087_AI``.
    """
    ws = get_workspace()
    return ws.export_job(
        scan_id,
        formats=formats,
        color_mode=color_mode,
        tiff_16bit=tiff_16bit,
        debug_mode=debug_mode,
        render_mode=render_mode,
    )


@mcp.resource("contex://status")
def resource_status() -> str:
    """Current scanner status, settings, and calibration presets as JSON."""
    return json.dumps(scanner_status(), indent=2)


def run_mcp_server(transport: str = "stdio") -> None:
    mcp.run(transport=transport)

