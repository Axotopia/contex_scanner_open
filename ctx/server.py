"""FastAPI application server & workflow coordinator for the Contex Scanner App.

Implements the complete operational workflow:
  1. Initiate scan (live scanner, local ``.rec`` capture, or synthetic 5-cam demo)
  2. Collect stream & frame 16-bit 5-camera raster
  3. Reorder Cameras 1..5, apply per-camera color adjustments, and test for
     operating-temperature color shifts (``OK`` | ``WARN`` | ``DRIFT`` | ``INSUFFICIENT_WHITE``)
  4. Render side-by-side Default Preset vs. Auto-Adjusted previews
  5. Handle user decision (Ignore / Apply Auto-Adjust / Official Sheet Wizard)
  6. Stitch image, apply optional Generative AI / Photocraft cleanup, and
     autosave to TIFF / JPG / PDF (Color or Grayscale) + optional Debug artifacts.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Literal
import uuid

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from .ai import EnhancementOptions
from .calibration import (
    DEFAULT_GAPS,
    DEFAULT_SERIAL,
    CalibrationPreset,
    PresetStore,
    analyze_drift,
    calibrate_from_official_sheet,
    load_rec_concat,
    module_gaps,
    piece_matrix,
    promote_margin_calibration_to_preset,
    raw_camera_images,
    to_raster_16bit,
)
from .engine import DEFAULT_SCANNER_IP, ScannerEngine, check_network_preflight
from .pipeline import (
    PAPER_SIZE_PRESETS,
    SUPPORTED_RESOLUTIONS_DPI,
    StitchResult,
    WINDOW_PX_300DPI,
    export_scan_files,
    generate_synthetic_5cam_raster,
    process_scan_to_stitch,
    MAX_SCAN_WIDTH_IN,
    EXPORT_RENDER_SUFFIX,
    export_variant_exists,
    find_free_export_group,
    render_export_stem,
    resolve_next_scan_basename,
    resolve_paper_width,
    save_preview_image,
    save_seam_crops,
    stitch_cameras,
    u16_to_srgb8,
)

from .panel import PanelKey, PanelMonitor

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"


@dataclass
class AppSettings:
    scanner_ip: str = DEFAULT_SCANNER_IP
    scanner_serial: str = DEFAULT_SERIAL
    active_preset_id: str = "architectural-neutral"
    # Last-used scan form state.  Defaults seed a fresh settings file; after that
    # the values are held across scans and app restarts.
    scan_source: str = "live"
    scan_paper_size: str = "full_42"
    scan_orientation: str = "portrait"
    scan_width_inches: float = 42.0
    scan_dpi: int = 300
    scan_color_mode: str = "color"
    # "prompt" | "auto_adjust" | "disabled"
    drift_policy: str = "prompt"
    seam_color_blend_enabled: bool = True
    seam_blend_width_px: int = 128
    seam_blend_strength: float = 0.85
    seam_feather_mode: str = "classic"
    autosave_enabled: bool = True
    # Autosave always writes the job's standard (calibrated) render.  When this is
    # on, the workflow also runs post-processing automatically and autosave writes
    # the matching ``_AI`` file for the same number.
    auto_ai_enhance: bool = False
    output_dir: str = str(REPO_ROOT / "scans_output")
    filename_prefix: str = "scan"
    auto_number_enabled: bool = True
    next_sequence_number: int = 1
    export_formats: list[str] = field(default_factory=lambda: ["tiff", "jpg", "pdf"])
    color_mode: str = "color"            # "color" | "grayscale"
    tiff_16bit: bool = False
    jpg_quality: int = 95
    post_scan_eject: str = "hold"        # "hold" | "eject_back" | "eject_front"
    debug_mode: bool = False
    photocraft_url: str = ""
    photocraft_api_key: str = ""
    # Front-panel keys (see ctx/panel.py). Disabled by default: pressing a key
    # then starts a live scan, which is a physical action.  Only SCAN is wired
    # for now; COPY/EMAIL emit events but do nothing until we decide what they
    # should do.
    panel_keys_enabled: bool = False
    panel_scan_on_keys: list[str] = field(default_factory=lambda: ["scan"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppSettings":
        scan_source = str(d.get("scan_source", "live"))
        if scan_source not in ("live", "rec_file", "demo_architectural", "demo_artwork"):
            scan_source = "live"
        scan_paper_size = str(d.get("scan_paper_size", "full_42"))
        if scan_paper_size not in PAPER_SIZE_PRESETS:
            scan_paper_size = "full_42"
        scan_orientation = str(d.get("scan_orientation", "portrait"))
        if scan_orientation not in ("portrait", "landscape"):
            scan_orientation = "portrait"
        try:
            scan_width_inches = float(d.get("scan_width_inches", 42.0))
        except (TypeError, ValueError):
            scan_width_inches = 42.0
        scan_width_inches = min(MAX_SCAN_WIDTH_IN, max(4.0, scan_width_inches))
        try:
            scan_dpi = int(d.get("scan_dpi", 300))
        except (TypeError, ValueError):
            scan_dpi = 300
        if scan_dpi not in SUPPORTED_RESOLUTIONS_DPI:
            scan_dpi = 300
        scan_color_mode = str(d.get("scan_color_mode", "color"))
        if scan_color_mode not in ("color", "grayscale"):
            scan_color_mode = "color"
        seam_feather_mode = str(d.get("seam_feather_mode", "classic")).lower()
        if seam_feather_mode not in ("classic", "artwork"):
            seam_feather_mode = "classic"
        try:
            jpg_quality = int(d.get("jpg_quality", 95))
        except (TypeError, ValueError):
            jpg_quality = 95
        jpg_quality = int(np.clip(jpg_quality, 1, 100))
        return cls(
            scanner_ip=str(d.get("scanner_ip", DEFAULT_SCANNER_IP)),
            scanner_serial=str(d.get("scanner_serial", DEFAULT_SERIAL)),
            active_preset_id=str(d.get("active_preset_id", "architectural-neutral")),
            scan_source=scan_source,
            scan_paper_size=scan_paper_size,
            scan_orientation=scan_orientation,
            scan_width_inches=scan_width_inches,
            scan_dpi=scan_dpi,
            scan_color_mode=scan_color_mode,
            drift_policy=str(d.get("drift_policy", "prompt")),
            seam_color_blend_enabled=bool(d.get("seam_color_blend_enabled", True)),
            seam_blend_width_px=int(np.clip(int(d.get("seam_blend_width_px", 128)), 8, 4096)),
            seam_blend_strength=float(np.clip(float(d.get("seam_blend_strength", 0.85)), 0.0, 1.0)),
            seam_feather_mode=seam_feather_mode,
            autosave_enabled=bool(d.get("autosave_enabled", True)),
            auto_ai_enhance=bool(d.get("auto_ai_enhance", False)),
            output_dir=str(d.get("output_dir", REPO_ROOT / "scans_output")),
            filename_prefix=str(d.get("filename_prefix", "scan") or "scan"),
            auto_number_enabled=bool(d.get("auto_number_enabled", True)),
            next_sequence_number=max(1, int(d.get("next_sequence_number", 1))),
            export_formats=list(d.get("export_formats") or ["tiff", "jpg", "pdf"]),
            color_mode=str(d.get("color_mode", "color")),
            tiff_16bit=bool(d.get("tiff_16bit", False)),
            jpg_quality=jpg_quality,
            post_scan_eject=str(d.get("post_scan_eject", "hold")),
            debug_mode=bool(d.get("debug_mode", False)),
            photocraft_url=str(d.get("photocraft_url", "")),
            photocraft_api_key=str(d.get("photocraft_api_key", "")),
            panel_keys_enabled=bool(d.get("panel_keys_enabled", False)),
            panel_scan_on_keys=list(d.get("panel_scan_on_keys", ["scan"])),
        )


@dataclass
class ScanJob:
    scan_id: str
    created_at: str
    source: str
    width_inches: float
    dpi: int
    color_mode: str
    preset_id: str
    status: str  # "scanning" | "awaiting_drift_decision" | "ready" | "error"
    active_render_mode: str  # "preset" | "auto_adjust" | "ai_enhanced"
    rows: int = 0
    records_count: int = 0
    holes: int = 0
    duration_seconds: float = 0.0
    drift_report: dict[str, Any] = field(default_factory=dict)
    stitch_metadata: dict[str, Any] = field(default_factory=dict)
    previews: dict[str, str] = field(default_factory=dict)
    seam_crops: list[dict[str, Any]] = field(default_factory=list)
    exported: dict[str, Any] = field(default_factory=dict)
    applied_ai_filters: list[str] = field(default_factory=list)
    # Bookkeeping for "autosave writes each render once" and for the buffers that
    # can be released once a newer scan exists.
    _autosaved: dict[str, int] = field(default_factory=dict, repr=False)
    _buffers_released: bool = field(default=False, repr=False)
    # In-memory buffers for fast re-rendering & calibration promotion
    _raster: np.ndarray | None = field(default=None, repr=False)
    _raw_rec_bytes: bytes | None = field(default=None, repr=False)
    _transcript: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _stitch_preset: StitchResult | None = field(default=None, repr=False)
    _stitch_auto: StitchResult | None = field(default=None, repr=False)
    _stitch_ai: StitchResult | None = field(default=None, repr=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "created_at": self.created_at,
            "source": self.source,
            "width_inches": self.width_inches,
            "dpi": self.dpi,
            "color_mode": self.color_mode,
            "preset_id": self.preset_id,
            "status": self.status,
            "active_render_mode": self.active_render_mode,
            "rows": self.rows,
            "records_count": self.records_count,
            "holes": self.holes,
            "duration_seconds": self.duration_seconds,
            "drift_report": self.drift_report,
            "stitch_metadata": self.stitch_metadata,
            "previews": self.previews,
            "seam_crops": self.seam_crops,
            "exported": self.exported,
            "applied_ai_filters": self.applied_ai_filters,
            # Which render views this job can still show/save.  Drift check off ->
            # no auto-adjusted render; AI only after post-processing.
            "available_renders": [
                mode
                for mode, stitch in (
                    ("preset", self._stitch_preset),
                    ("auto_adjust", self._stitch_auto),
                    ("ai_enhanced", self._stitch_ai),
                )
                if stitch is not None
            ],
            "buffers_released": self._buffers_released,
        }


class ScanWorkspaceManager:
    """Central state & pipeline coordinator shared by the WebUI and MCP Server."""

    def __init__(self, config_dir: str | Path | None = None):
        if config_dir is None:
            self.config_dir = Path.home() / ".config" / "contex"
        else:
            self.config_dir = Path(config_dir)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.config_dir / "app_settings.json"
        self.settings = self._load_settings()
        self.preset_store = PresetStore(self.config_dir, serial=self.settings.scanner_serial)
        self.engine = ScannerEngine(host=self.settings.scanner_ip)
        self.preview_cache_dir = Path(self.settings.output_dir) / ".previews"
        self.preview_cache_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, ScanJob] = {}
        self._cancel_lock = threading.Lock()
        self._active_scan_id: str | None = None
        self._active_cancel: threading.Event | None = None
        self.event_listeners: list[Callable[[dict[str, Any]], None]] = []
        self.panel = PanelMonitor(
            self.settings.scanner_ip,
            on_key=self._on_panel_key,
            on_state=self._on_panel_state,
        )
        self._panel_scan_active = False
        if self.settings.panel_keys_enabled:
            self.panel.start()

    def _load_settings(self) -> AppSettings:
        if self.settings_path.exists():
            try:
                return AppSettings.from_dict(json.loads(self.settings_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        s = AppSettings()
        self._save_settings(s)
        return s

    def _save_settings(self, s: AppSettings) -> None:
        self.settings_path.write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")

    def update_settings(self, updates: dict[str, Any]) -> AppSettings:
        cur = self.settings.to_dict()
        cur.update(updates)
        if "export_formats" in updates:
            valid = {"tiff", "jpg", "jpeg", "pdf", "png"}
            requested = [str(f).strip().lower() for f in (updates.get("export_formats") or [])]
            formats = list(dict.fromkeys(f for f in requested if f in valid))
            # Autosave must write at least one real file; never persist an empty list.
            cur["export_formats"] = formats or list(cur.get("export_formats") or ["jpg"])
        self.settings = AppSettings.from_dict(cur)
        self.engine.host = self.settings.scanner_ip
        self.preview_cache_dir = Path(self.settings.output_dir) / ".previews"
        self.preview_cache_dir.mkdir(parents=True, exist_ok=True)
        self._save_settings(self.settings)
        self.panel.host = self.settings.scanner_ip
        return self.settings

    # -- front-panel keys --------------------------------------------------
    def _on_panel_state(self, state: dict[str, Any]) -> None:
        self.broadcast_event("panel_state", state)

    def _preflight_live_scan(self) -> None:
        """Fail fast when the scanner is offline or has no sheet.

        The full media recovery cycle can spend tens of seconds before it gives
        up; checking the status first keeps a missing sheet an immediate error.
        """
        status = self.engine.probe_status(timeout=2.0)
        if not status.get("connected"):
            host = status.get("host", self.settings.scanner_ip)
            reason = status.get("error") or "connection failed"
            raise RuntimeError(f"Scanner unreachable at {host}: {reason}")
        code = status.get("media_code")
        if code is None:
            raise RuntimeError(
                "Scanner did not report media status. Check the connection and try again."
            )
        if code == 0x00:
            raise RuntimeError(
                "No media detected. Load a sheet into the scanner and start the scan again."
            )
        if code == 0x24:
            raise RuntimeError(
                "Scanner is still scanning (0x24). Wait for it to finish, or use Recover (0x24), "
                "before starting a new scan."
            )

    def _settings_scan_kwargs(self) -> dict[str, Any]:
        """Live-scan parameters from the last-used scan form settings."""
        return {
            "source_mode": "live",
            "paper_size": self.settings.scan_paper_size,
            "orientation": self.settings.scan_orientation,
            "width_inches": self.settings.scan_width_inches,
            "dpi": self.settings.scan_dpi,
            "color_mode": self.settings.scan_color_mode,
            "seam_feather_mode": self.settings.seam_feather_mode,
        }

    def _on_panel_key(self, ev: PanelKey) -> None:
        """A front-panel key was pressed: announce it, and maybe start a scan."""
        self.broadcast_event("panel_key", ev.as_dict())
        if not self.settings.panel_keys_enabled:
            return
        if ev.key not in self.settings.panel_scan_on_keys:
            return
        if self._panel_scan_active:
            return          # one panel-triggered scan at a time
        self._panel_scan_active = True

        def _run() -> None:
            try:
                # Polling a second connection while the scan owns one is asking
                # for interference; pause the monitor for the duration.
                self.panel.suspend()
                # The physical SCAN key reuses the last scan form settings
                # (paper/orientation/width/DPI/colour) and the active preset, so
                # a key press matches what the UI would have sent.
                self.execute_scan_workflow(**self._settings_scan_kwargs())
            except Exception as exc:
                self.broadcast_event("panel_error", {"key": ev.key, "error": str(exc)})
            finally:
                self.panel.resume()
                self._panel_scan_active = False

        threading.Thread(
            target=_run, name=f"panel-scan-{ev.key}", daemon=True
        ).start()

    def broadcast_event(self, event_type: str, payload: dict[str, Any]) -> None:
        msg = {"type": event_type, "t": time.strftime("%H:%M:%S"), **payload}
        for cb in list(self.event_listeners):
            try:
                cb(msg)
            except Exception:
                pass

    def list_available_rec_files(self) -> list[dict[str, Any]]:
        """Find any ``.rec`` capture files under ``analysis/``, ``scans_output/``, or ``W:/contex_scanner``."""
        found: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        search_roots = [
            (Path(self.settings.output_dir), True),
            (REPO_ROOT / "scans_output", True),
        ]
        for root_dir, recursive in search_roots:
            try:
                if not root_dir.exists():
                    continue
                it = sorted(root_dir.rglob("*.rec")) if recursive else sorted(root_dir.glob("*.rec"))
            except Exception:
                continue
            for p in it:
                try:
                    norm = str(p.resolve())
                    if norm in seen_paths:
                        continue
                    sz = p.stat().st_size
                    if sz > 1000:
                        seen_paths.add(norm)
                        rel = str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else str(p)
                        found.append({
                            "name": p.name,
                            "path": str(p),
                            "display": rel,
                            "size_mb": round(sz / (1024 * 1024), 2),
                        })
                except Exception:
                    continue
        return found

    def _save_raw_uncalibrated_preview(
        self,
        raster: np.ndarray,
        gaps: list[tuple[int, int]],
        out_path: Path,
        window_px: int = WINDOW_PX_300DPI,
        max_width: int = 2400,
    ) -> None:
        """Render the raw uncalibrated camera-ordered composite so the user can see
        the raw sensor output (including Camera 4/5 blue cast) vs. calibrated."""
        pmeta = piece_matrix(gaps, raster.shape[1])
        raw_cams = [(name, im, {}) for name, im in raw_camera_images(raster, pmeta)]
        comp, _, _ = stitch_cameras(raw_cams, pmeta, order="bed")
        doc = comp[:, :window_px, :] if window_px and window_px < comp.shape[1] else comp
        v = np.clip(doc.astype(np.float32) / 50000.0, 0.0, 1.0) ** 0.45
        arr8 = (v * 255.0).astype(np.uint8)
        im = Image.fromarray(arr8)
        if max_width and im.width > max_width:
            new_h = max(1, int(round(im.height * max_width / im.width)))
            im = im.resize((max_width, new_h), Image.Resampling.LANCZOS)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        im.save(out_path, format="PNG")

    def allocate_next_scan_id(self, custom_prefix: str | None = None) -> str:
        """Allocate the next sequential auto-numbered scan basename (e.g. ``scan_0001``)
        guaranteed not to collide with any existing file in ``output_dir`` or in-memory job."""
        prefix = (custom_prefix or self.settings.filename_prefix or "scan").strip()
        if custom_prefix and custom_prefix.strip() != self.settings.filename_prefix:
            self.settings.filename_prefix = prefix

        if self.settings.auto_number_enabled:
            seq = max(1, int(self.settings.next_sequence_number))
            while True:
                # Reserve a number that is free for *every* render variant
                # (_Auto / _AI included) so the three views of a scan can share it.
                base, found_seq = find_free_export_group(
                    self.settings.output_dir,
                    prefix=prefix,
                    start_seq=seq,
                    digits=4,
                )
                if base not in self.jobs:
                    self.settings.next_sequence_number = found_seq + 1
                    self._save_settings(self.settings)
                    return base
                # Number is free on disk but already used by an in-memory job
                # (autosave off, nothing written yet): take the next one.
                seq = found_seq + 1
        return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

    def execute_scan_workflow(
        self,
        *,
        cancel_event: threading.Event | None = None,
        **kwargs: Any,
    ) -> ScanJob:
        """Run the scan workflow; ``cancel_event`` aborts it at the next safe point."""
        event = cancel_event or threading.Event()
        try:
            return self._execute_scan_workflow(cancel_event=event, **kwargs)
        finally:
            with self._cancel_lock:
                if self._active_cancel is event:
                    self._active_cancel = None
                    self._active_scan_id = None

    def cancel_active_scan(self) -> dict[str, Any]:
        """Ask the running scan to stop at the next safe point."""
        with self._cancel_lock:
            event = self._active_cancel
            scan_id = self._active_scan_id
        if event is None:
            return {"ok": False, "scan_id": None, "message": "No scan in progress"}
        event.set()
        return {
            "ok": True,
            "scan_id": scan_id,
            "message": "Cancel requested; the scan will stop at the next safe point",
        }

    def _build_cancelled_job(
        self,
        *,
        scan_id: str,
        source: str,
        width_inches: float,
        dpi: int,
        color_mode: str,
        preset: CalibrationPreset,
        rows: int,
        records_count: int,
        holes: int,
        t0: float,
        drift_report: dict[str, Any] | None = None,
    ) -> ScanJob:
        job = ScanJob(
            scan_id=scan_id,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            source=source,
            width_inches=round(float(width_inches), 2),
            dpi=int(dpi),
            color_mode=color_mode,
            preset_id=preset.id,
            status="cancelled",
            active_render_mode="preset",
            rows=int(rows),
            records_count=int(records_count),
            holes=int(holes),
            duration_seconds=round(time.time() - t0, 2),
            drift_report=drift_report or {},
        )
        self.broadcast_event("scan_cancelled", job.to_public_dict())
        return job

    def _execute_scan_workflow(
        self,
        *,
        source_mode: str = "live",
        rec_path: str | None = None,
        paper_size: str = "full_42",
        orientation: str = "portrait",
        width_inches: float = 42.0,
        dpi: int = 300,
        color_mode: str | None = None,
        preset_id: str | None = None,
        simulate_thermal_drift: bool = True,
        seam_color_blend: bool | None = None,
        seam_blend_width_px: int | None = None,
        seam_blend_strength: float | None = None,
        seam_feather_mode: str | None = None,
        seconds: float = 25.0,
        demo_rows: int = 960,
        demo_paper_width_inches: float = 36.0,
        filename_prefix: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ScanJob:
        """Run the full scan -> color adjust -> drift check -> stitch -> preview/autosave flow."""
        if source_mode == "live":
            self._preflight_live_scan()
        scan_id = self.allocate_next_scan_id(filename_prefix)
        cancel_event = cancel_event or threading.Event()
        with self._cancel_lock:
            self._active_scan_id = scan_id
            self._active_cancel = cancel_event
        chosen_color_mode = color_mode or self.settings.color_mode
        chosen_preset_id = preset_id or self.settings.active_preset_id
        preset = self.preset_store.get_preset(chosen_preset_id)
        use_seam_blend = (
            self.settings.seam_color_blend_enabled
            if seam_color_blend is None
            else bool(seam_color_blend)
        )
        blend_w_px = int(
            self.settings.seam_blend_width_px
            if seam_blend_width_px is None
            else seam_blend_width_px
        )
        blend_str = float(
            self.settings.seam_blend_strength
            if seam_blend_strength is None
            else seam_blend_strength
        )
        feather_mode = str(
            self.settings.seam_feather_mode if seam_feather_mode is None else seam_feather_mode
        ).lower()
        if feather_mode not in ("classic", "artwork"):
            feather_mode = "classic"

        # Resolve paper preset, orientation and custom width.  Portrait uses the
        # short edge, landscape the long edge (clamped to the 42 in bed).
        win = resolve_paper_width(paper_size, orientation, width_inches)
        p_key = str(win["paper_size"])
        orientation = str(win["orientation"])
        auto_detect_width = p_key == "auto"
        effective_width_in = float(win["effective_width_in"])

        # Native 5-camera stream window in 300-DPI bed pixels before output DPI scaling
        window_px_300 = int(round(effective_width_in * 300.0))

        t0 = time.time()
        rows = 0
        records_count = 0
        holes = 0
        src_label = source_mode
        drift_report: dict[str, Any] = {}
        self.broadcast_event(
            "scan_started",
            {
                "scan_id": scan_id,
                "source_mode": source_mode,
                "paper_size": p_key,
                "orientation": orientation,
                "width_inches": effective_width_in,
            },
        )

        if cancel_event.is_set():
            return self._build_cancelled_job(
                scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                rows=0, records_count=0, holes=0, t0=t0,
            )

        # Step 1: Collect stream from scanner (or offline .rec / synthetic generator)
        if source_mode == "live":
            cap = self.engine.run_live_scan(
                width_inches=effective_width_in,
                dpi=dpi,
                seconds=seconds,
                recover=True,
                post_scan_eject=self.settings.post_scan_eject,
                cancel_event=cancel_event,
                on_progress=lambda stg, info: self.broadcast_event(
                    "scan_progress", {"scan_id": scan_id, "stage": stg, **info}
                ),
            )
            src_label = cap.source
            if cap.cancelled or cancel_event.is_set():
                return self._build_cancelled_job(
                    scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                    dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                    rows=cap.rows, records_count=cap.records_count,
                    holes=cap.holes, t0=t0,
                )
            buf, _info = load_rec_concat_bytes(cap.raw_rec_bytes)
            raster, rows = to_raster_16bit(buf)
            raw_rec_bytes = cap.raw_rec_bytes
            transcript = cap.transcript
            records_count = cap.records_count
            holes = cap.holes
            src_label = cap.source

        elif source_mode in ("rec_file", "rec") and rec_path:
            cap = self.engine.load_rec_file(rec_path, width_inches=effective_width_in, dpi=dpi)
            src_label = f"file://{Path(rec_path).name}"
            if cancel_event.is_set():
                return self._build_cancelled_job(
                    scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                    dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                    rows=0, records_count=0, holes=0, t0=t0,
                )
            buf, _info = load_rec_concat_bytes(cap.raw_rec_bytes)
            raster, rows = to_raster_16bit(buf)
            raw_rec_bytes = cap.raw_rec_bytes
            transcript = cap.transcript
            records_count = cap.records_count
            holes = cap.holes
            src_label = f"file://{Path(rec_path).name}"

        else:
            subject = "full_bleed_artwork" if source_mode == "demo_artwork" else "architectural_and_it8"
            sim_sheet_w = float(demo_paper_width_inches) if auto_detect_width else effective_width_in
            self.broadcast_event("scan_progress", {"scan_id": scan_id, "stage": "generating_demo_stream"})
            raster, raw_rec_bytes = generate_synthetic_5cam_raster(
                rows=max(120, int(demo_rows)),
                preset=preset,
                simulate_thermal_drift=simulate_thermal_drift,
                subject=subject,
                paper_width_inches=sim_sheet_w,
            )
            rows = int(raster.shape[0])
            records_count = len(raw_rec_bytes) // 2942
            holes = 0
            transcript = [
                {
                    "t": 0.0,
                    "label": "DEMO_STREAM",
                    "subject": subject,
                    "thermal_drift": simulate_thermal_drift,
                    "paper_size": p_key,
                    "simulated_sheet_width_in": sim_sheet_w,
                }
            ]
            src_label = f"demo://{subject}"

        if cancel_event.is_set():
            return self._build_cancelled_job(
                scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                rows=rows, records_count=records_count, holes=holes, t0=t0,
            )

        # Step 2: Detect module gaps & run Thermal / Operating Temp Drift Check
        # With the check switched off (bulk scanning) both the analysis and the
        # second auto-adjusted stitch are skipped: measured 23.6 s + 5.8 s per
        # sheet on a 42" 300 dpi capture.
        self.broadcast_event("scan_progress", {"scan_id": scan_id, "stage": "analyzing_color_drift"})
        gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))
        drift_check_enabled = str(self.settings.drift_policy) != "disabled"
        if drift_check_enabled:
            drift_report = analyze_drift(raster, rows, preset, gaps=gaps)
        else:
            drift_report = {
                "verdict": "DISABLED",
                "recommendation": "Colour / temperature drift check is off — the stored preset is used as-is.",
                "disabled": True,
            }
        if cancel_event.is_set():
            return self._build_cancelled_job(
                scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                rows=rows, records_count=records_count, holes=holes, t0=t0,
                drift_report=drift_report,
            )

        # Step 3: Stitch BOTH Default Preset and Auto-Adjusted versions for instant comparison
        self.broadcast_event("scan_progress", {"scan_id": scan_id, "stage": "stitching_cameras"})
        stitch_preset = process_scan_to_stitch(
            raster,
            rows,
            preset,
            mode="preset",
            window_px=window_px_300,
            dpi=dpi,
            source_dpi=(dpi if source_mode == "live" else 300),
            paper_size=p_key,
            orientation=orientation,
            auto_detect_width=auto_detect_width,
            gaps=gaps,
            seam_color_blend=use_seam_blend,
            seam_blend_width_px=blend_w_px,
            seam_blend_strength=blend_str,
            seam_feather_mode=feather_mode,
            )
        if cancel_event.is_set():
            return self._build_cancelled_job(
                scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                rows=rows, records_count=records_count, holes=holes, t0=t0,
                drift_report=drift_report,
            )
        # The auto-adjusted variant exists only to compare/accept a drift
        # correction, so it is skipped entirely when the check is off.
        stitch_auto = None
        if drift_check_enabled:
            stitch_auto = process_scan_to_stitch(
                raster,
                rows,
                preset,
                mode="auto_adjust",
                window_px=window_px_300,
                dpi=dpi,
                source_dpi=(dpi if source_mode == "live" else 300),
                paper_size=p_key,
                orientation=orientation,
                auto_detect_width=auto_detect_width,
                gaps=gaps,
                seam_color_blend=use_seam_blend,
                seam_blend_width_px=blend_w_px,
                seam_blend_strength=blend_str,
                seam_feather_mode=feather_mode,
            )
        if cancel_event.is_set():
            return self._build_cancelled_job(
                scan_id=scan_id, source=src_label, width_inches=effective_width_in,
                dpi=dpi, color_mode=chosen_color_mode, preset=preset,
                rows=rows, records_count=records_count, holes=holes, t0=t0,
                drift_report=drift_report,
            )

        reference_stitch = stitch_auto if stitch_auto is not None else stitch_preset
        final_width_in = round(reference_stitch.document_width / float(reference_stitch.dpi), 2)

        # Step 4: Render web previews (Raw, Default Preset, Auto-Adjusted, and Seam Crops)
        job_prev_dir = self.preview_cache_dir / scan_id
        job_prev_dir.mkdir(parents=True, exist_ok=True)

        raw_prev_path = job_prev_dir / "preview_raw.png"
        preset_prev_path = job_prev_dir / "preview_preset.png"
        auto_prev_path = job_prev_dir / "preview_auto.png"

        raw_crop_300 = (
            int(reference_stitch.auto_width_info["detected_px"])
            if reference_stitch.auto_width_info
            else window_px_300
        )
        self._save_raw_uncalibrated_preview(raster, gaps, raw_prev_path, window_px=raw_crop_300)
        save_preview_image(stitch_preset, preset, preset_prev_path, color_mode=chosen_color_mode)
        if stitch_auto is not None:
            save_preview_image(stitch_auto, preset, auto_prev_path, color_mode=chosen_color_mode)
        seam_crops_list = save_seam_crops(reference_stitch, preset, job_prev_dir, "seam")

        previews_map = {
            "raw": f"/api/scans/{scan_id}/preview/raw",
            "preset": f"/api/scans/{scan_id}/preview/preset",
        }
        if stitch_auto is not None:
            previews_map["auto_adjust"] = f"/api/scans/{scan_id}/preview/auto_adjust"
        for sc in seam_crops_list:
            sc["url"] = f"/api/scans/{scan_id}/seam/{sc['seam_index']}"

        # Step 5: Decide active mode based on Drift Verdict & User Settings Policy
        verdict = drift_report.get("verdict", "OK")
        policy = self.settings.drift_policy

        if policy == "disabled" or verdict == "OK":
            active_mode = "preset"
            status = "ready"
        elif policy == "auto_adjust":
            active_mode = "auto_adjust" if verdict != "INSUFFICIENT_WHITE" else "preset"
            status = "ready"
        else:
            # policy == "prompt" and verdict in ("WARN", "DRIFT", "INSUFFICIENT_WHITE")
            active_mode = "auto_adjust" if verdict in ("WARN", "DRIFT") else "preset"
            status = "awaiting_drift_decision"

        active_stitch = stitch_auto if active_mode == "auto_adjust" else stitch_preset

        job = ScanJob(
            scan_id=scan_id,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            source=src_label,
            width_inches=final_width_in,
            dpi=reference_stitch.dpi,
            color_mode=chosen_color_mode,
            preset_id=preset.id,
            status=status,
            active_render_mode=active_mode,
            rows=reference_stitch.rows,
            records_count=records_count,
            holes=holes,
            duration_seconds=round(time.time() - t0, 2),
            drift_report=drift_report,
            stitch_metadata=active_stitch.to_metadata_dict(),
            previews=previews_map,
            seam_crops=seam_crops_list,
            _raster=raster,
            _raw_rec_bytes=raw_rec_bytes,
            _transcript=transcript,
            _stitch_preset=stitch_preset,
            _stitch_auto=stitch_auto,
        )
        self.jobs[scan_id] = job
        # Keep the newest two scans fully re-exportable (~3.2 GiB peak) and release
        # the buffers of anything older, so a long bulk run stays flat in RAM.
        self.prune_old_jobs(keep_latest=2)

        if cancel_event.is_set():
            job.status = "cancelled"
            self.broadcast_event("scan_cancelled", job.to_public_dict())
            return job

        # Step 6: Autosave immediately if status == "ready" and autosave_enabled
        if status == "ready" and self.settings.autosave_enabled:
            self._autosave_job(job, preset)

        self.broadcast_event("scan_completed", job.to_public_dict())
        return job

    def _release_job_buffers(self, job: ScanJob) -> None:
        """Drop a job's large in-memory buffers, keeping its metadata and files."""
        job._raster = None
        job._raw_rec_bytes = None
        job._transcript = []
        job._stitch_preset = None
        job._stitch_auto = None
        job._stitch_ai = None
        job._buffers_released = True

    def prune_old_jobs(self, keep_latest: int = 1) -> None:
        """Keep only the newest ``keep_latest`` scans fully in memory.

        Every finished job used to retain its raster (566 MiB) plus both stitched
        documents (~523 MiB each) forever, so a 30-sheet bulk run needed ~48 GiB.
        Older jobs keep their previews, exported files and metadata; only the
        buffers that allow re-export / re-calibration are released.
        """
        jobs = list(self.jobs.values())
        if keep_latest < 0:
            keep_latest = 0
        for job in jobs[: max(0, len(jobs) - keep_latest)] if keep_latest else jobs:
            if not job._buffers_released:
                self._release_job_buffers(job)

    def _note_export_sequence(self, used_seq: int) -> None:
        """Advance ``settings.next_sequence_number`` past a number we just wrote."""
        if used_seq >= self.settings.next_sequence_number:
            self.settings.next_sequence_number = used_seq + 1
            self._save_settings(self.settings)

    def _autosave_targets(self, job: ScanJob) -> list[str]:
        """Render variants autosave should write for this job.

        Autosave is a failsafe for bulk scanning, so it writes the job's *standard*
        calibrated render (stored preset, or the accepted auto-adjusted one) — never
        an AI render on its own, because post-processing can always be redone later.
        The one exception is when the workflow runs AI automatically: then the ``_AI``
        file is written too, otherwise that unattended work would be discarded.
        """
        standard = job.active_render_mode if job.active_render_mode in ("preset", "auto_adjust") else "preset"
        targets = [standard] if self._stitch_for_mode_available(job, standard) else []
        if self.settings.auto_ai_enhance and job._stitch_ai is not None:
            targets.append("ai_enhanced")
        return targets

    @staticmethod
    def _stitch_for_mode_available(job: ScanJob, mode: str) -> bool:
        return {
            "preset": job._stitch_preset,
            "auto_adjust": job._stitch_auto,
            "ai_enhanced": job._stitch_ai,
        }.get(mode) is not None

    def _autosave_job(self, job: ScanJob, preset: CalibrationPreset) -> dict[str, Any] | None:
        """Write the autosave target renders once per render instance."""
        last: dict[str, Any] | None = None
        for mode in self._autosave_targets(job):
            stitch_res = self._stitch_for_mode(job, mode)
            token = id(stitch_res)
            if job._autosaved.get(mode) == token:
                continue  # already on disk for this exact render
            base, used_seq = self._render_export_basename(job, mode)
            exported = export_scan_files(
                stitch_res=stitch_res,
                preset=preset,
                output_dir=self.settings.output_dir,
                base_name=base,
                formats=self.settings.export_formats,
                color_mode=job.color_mode,
                tiff_16bit=self.settings.tiff_16bit,
                jpg_quality=self.settings.jpg_quality,
                debug_mode=self.settings.debug_mode,
                raw_records_bytes=job._raw_rec_bytes if mode == "preset" else None,
                transcript=job._transcript if mode == "preset" else None,
                drift_report=job.drift_report,
                avoid_overwrite=False,
            )
            exported["render_mode"] = mode
            job._autosaved[mode] = token
            self._note_export_sequence(used_seq)
            job.exported = exported
            last = exported
        return last

    def _get_active_stitch(self, job: ScanJob) -> StitchResult:
        return self._stitch_for_mode(job, job.active_render_mode)

    def _stitch_for_mode(self, job: ScanJob, render_mode: str | None) -> StitchResult:
        """Return the stitch for one render variant, or explain why it is unavailable."""
        mode = str(render_mode or job.active_render_mode or "preset")
        if mode == "ai_enhanced":
            if job._stitch_ai is None:
                raise ValueError("AI Enhanced render is not available for this scan — run post-processing first")
            return job._stitch_ai
        if mode == "auto_adjust":
            if job._stitch_auto is None:
                raise ValueError(
                    "Auto-Adjusted render is not available for this scan — the colour / "
                    "temperature drift check was off"
                )
            return job._stitch_auto
        if job._stitch_preset is None:
            raise ValueError(
                "This scan's pixel buffers were released to keep memory flat; "
                "only the newest scan can be re-exported"
            )
        return job._stitch_preset

    @staticmethod
    def _split_numbered_stem(stem: str) -> tuple[str, int, int]:
        """``'CU-Kee_0087' -> ('CU-Kee', 87, 4)``; unnumbered names return seq 0."""
        import re as _re

        m = _re.match(r"^(.*?)[_-](\d+)$", stem)
        if not m:
            return stem, 0, 4
        return m.group(1), int(m.group(2)), len(m.group(2))

    def _render_export_basename(self, job: ScanJob, render_mode: str, prefix_override: str | None = None) -> tuple[str, int]:
        """Filename base for one render variant of ``job``.

        All variants of a scan share its allocated number (``CU-Kee_0087``,
        ``CU-Kee_0087_Auto``, ``CU-Kee_0087_AI``); a variant is only moved to the
        next number when that exact file already exists, so nothing is overwritten.
        """
        stem_prefix, seq, digits = self._split_numbered_stem(str(job.scan_id))
        prefix = (prefix_override or "").strip() or stem_prefix
        if digits < 1:
            digits = 4
        if seq <= 0:
            candidate_stem, seq = find_free_export_group(
                self.settings.output_dir,
                prefix=prefix,
                start_seq=self.settings.next_sequence_number,
                digits=digits,
            )
            return f"{candidate_stem}{EXPORT_RENDER_SUFFIX.get(render_mode, '')}", seq
        while True:
            candidate_stem = render_export_stem(prefix, seq, digits)
            if not export_variant_exists(self.settings.output_dir, candidate_stem, render_mode):
                return f"{candidate_stem}{EXPORT_RENDER_SUFFIX.get(render_mode, '')}", seq
            # That exact view is already on disk: take the next number that is free
            # for every variant so the group stays aligned.
            _next_stem, seq = find_free_export_group(
                self.settings.output_dir, prefix=prefix, start_seq=seq + 1, digits=digits
            )

    def resolve_drift_decision(
        self,
        scan_id: str,
        *,
        choice: str = "auto_adjust",  # "preset" | "auto_adjust"
        promote_to_preset: bool = False,
        save_as_new_preset_name: str | None = None,
    ) -> ScanJob:
        """Apply the user's choice from the Drift Comparison Modal and trigger autosave."""
        job = self.get_job(scan_id)
        preset = self.preset_store.get_preset(job.preset_id)

        if choice == "auto_adjust":
            job.active_render_mode = "auto_adjust"
            if (promote_to_preset or save_as_new_preset_name) and job._raster is not None:
                new_id = None
                if save_as_new_preset_name:
                    slug = "".join(
                        c if c.isalnum() else "-" for c in save_as_new_preset_name.lower()
                    ).strip("-")
                    new_id = f"custom-{slug or 'preset'}-{int(time.time()) % 10000}"
                updated_preset = promote_margin_calibration_to_preset(
                    job._raster,
                    job.rows,
                    preset,
                    new_id=new_id,
                    new_name=save_as_new_preset_name or preset.name,
                    source_label=f"promoted-from-{scan_id}",
                )
                self.preset_store.save_preset(updated_preset)
                preset = updated_preset
                job.preset_id = updated_preset.id
                self.settings.active_preset_id = updated_preset.id
                self._save_settings(self.settings)
        else:
            job.active_render_mode = "preset"

        job.status = "ready"
        active_stitch = self._get_active_stitch(job)
        job.stitch_metadata = active_stitch.to_metadata_dict()

        if self.settings.autosave_enabled:
            self._autosave_job(job, preset)

        self.broadcast_event("drift_resolved", job.to_public_dict())
        return job

    def calibrate_with_official_sheet(
        self,
        scan_id: str,
        *,
        sheet_mode: str = "it8_full",  # "white_flatfield" | "it8_full"
        save_as_preset_id: str | None = None,
        save_as_preset_name: str | None = None,
        it8_dx_300px: int = 0,
        it8_dy_300px: int = 0,
        auto_align: bool = True,
    ) -> dict[str, Any]:
        """Run the Contex Official Calibration Sheet fit on ``scan_id``'s raster,
        save the resulting preset, and re-stitch ``scan_id``."""
        job = self.get_job(scan_id)
        if job._raster is None:
            raise ValueError("Raw raster is no longer in memory for this scan")
        base_preset = self.preset_store.get_preset(job.preset_id)
        new_preset, report = calibrate_from_official_sheet(
            job._raster,
            job.rows,
            base_preset,
            sheet_mode=sheet_mode,
            new_id=save_as_preset_id,
            new_name=save_as_preset_name,
            dpi=(job.dpi if str(job.source).startswith("live://") else 300),
            it8_dx_300px=int(it8_dx_300px),
            it8_dy_300px=int(it8_dy_300px),
            auto_align=bool(auto_align),
        )
        self.preset_store.save_preset(new_preset)
        self.settings.active_preset_id = new_preset.id
        self._save_settings(self.settings)

        # Re-stitch the scan with the newly calibrated preset
        # window_px is expressed in 300 dpi sensor columns; process_scan_to_stitch
        # scales it to the target dpi.  Passing job.dpi here cropped twice as wide
        # at 600/1200 dpi (a letter came out 17.0 in wide instead of 8.5).
        window_px = int(round(job.width_inches * 300.0))
        gaps = module_gaps(job._raster.mean(axis=2, dtype=np.float32))
        use_seam_blend = bool(self.settings.seam_color_blend_enabled)
        blend_w_px = int(self.settings.seam_blend_width_px)
        blend_str = float(self.settings.seam_blend_strength)
        re_feather = str(job.stitch_metadata.get("seam_blend_info", {}).get("mode", "classic"))
        if re_feather not in ("classic", "artwork"):
            re_feather = "classic"
        job._stitch_preset = process_scan_to_stitch(
            job._raster,
            job.rows,
            new_preset,
            mode="preset",
            window_px=window_px,
            dpi=job.dpi,
            orientation=str(job.stitch_metadata.get("orientation", "portrait")),
            source_dpi=(job.dpi if str(job.source).startswith("live://") else 300),
            gaps=gaps,
            seam_color_blend=use_seam_blend,
            seam_blend_width_px=blend_w_px,
            seam_blend_strength=blend_str,
            seam_feather_mode=re_feather,
        )
        job._stitch_auto = process_scan_to_stitch(
            job._raster,
            job.rows,
            new_preset,
            mode="auto_adjust",
            window_px=window_px,
            dpi=job.dpi,
            orientation=str(job.stitch_metadata.get("orientation", "portrait")),
            source_dpi=(job.dpi if str(job.source).startswith("live://") else 300),
            gaps=gaps,
            seam_color_blend=use_seam_blend,
            seam_blend_width_px=blend_w_px,
            seam_blend_strength=blend_str,
            seam_feather_mode=re_feather,
        )
        job.preset_id = new_preset.id
        job.active_render_mode = "preset"
        job.status = "ready"
        job.drift_report = analyze_drift(job._raster, job.rows, new_preset, gaps=gaps)
        job.stitch_metadata = job._stitch_preset.to_metadata_dict()

        job_prev_dir = self.preview_cache_dir / job.scan_id
        save_preview_image(job._stitch_preset, new_preset, job_prev_dir / "preview_preset.png", color_mode=job.color_mode)
        save_preview_image(job._stitch_auto, new_preset, job_prev_dir / "preview_auto.png", color_mode=job.color_mode)

        if self.settings.autosave_enabled:
            self._autosave_job(job, new_preset)

        self.broadcast_event("scan_completed", job.to_public_dict())

        return {
            "preset": new_preset.to_dict(),
            "calibration_report": report,
            "scan": job.to_public_dict(),
        }

    def run_ai_enhancement(
        self,
        scan_id: str,
        options: EnhancementOptions,
    ) -> ScanJob:
        """Run local or external Generative AI / Photocraft post-processing on ``scan_id``."""
        job = self.get_job(scan_id)
        preset = self.preset_store.get_preset(job.preset_id)
        base_stitch = (
            job._stitch_auto
            if job.active_render_mode == "auto_adjust" and job._stitch_auto is not None
            else job._stitch_preset
        )
        if base_stitch is None:
            raise ValueError("Scan stitch result not available")

        from .ai import measure_seam_step_u16, run_enhancement_pipeline

        before_seam_stats = measure_seam_step_u16(base_stitch.doc_u16, base_stitch.joins)
        enhanced_u16, applied = run_enhancement_pipeline(
            base_stitch.doc_u16, base_stitch.joins, options
        )
        after_seam_stats = measure_seam_step_u16(enhanced_u16, base_stitch.joins)
        seam_blend_info = {
            "enabled": bool(options.blend_seam_colors),
            "mode": str(options.seam_feather_mode or "classic"),
            "blend_width_px": int(options.seam_blend_width_px),
            "strength": round(float(options.seam_blend_strength), 3),
            "before_rgb_step_mean": before_seam_stats["mean_rgb_step"],
            "after_rgb_step_mean": after_seam_stats["mean_rgb_step"],
            "seams_before": before_seam_stats["seams"],
            "seams_after": after_seam_stats["seams"],
        }
        ai_stitch = StitchResult(
            mode="ai_enhanced",
            preset_id=base_stitch.preset_id,
            doc_u16=enhanced_u16,
            composite_width=base_stitch.composite_width,
            document_width=base_stitch.document_width,
            rows=base_stitch.rows,
            dpi=base_stitch.dpi,
            K=base_stitch.K,
            joins=base_stitch.joins,
            gaps=base_stitch.gaps,
            pieces=base_stitch.pieces,
            camera_diagnostics=base_stitch.camera_diagnostics,
            zero_columns=base_stitch.zero_columns,
            dark_columns=base_stitch.dark_columns,
            applied_filters=applied,
            paper_size=base_stitch.paper_size,
            orientation=base_stitch.orientation,
            auto_width_info=base_stitch.auto_width_info,
            seam_blend_info=seam_blend_info,
        )
        job._stitch_ai = ai_stitch
        job.active_render_mode = "ai_enhanced"
        job.applied_ai_filters = applied
        job.stitch_metadata = ai_stitch.to_metadata_dict()

        job_prev_dir = self.preview_cache_dir / job.scan_id
        ai_prev_path = job_prev_dir / "preview_ai.png"
        save_preview_image(ai_stitch, preset, ai_prev_path, color_mode=job.color_mode)
        seam_crops_list = save_seam_crops(ai_stitch, preset, job_prev_dir, "seam")
        for sc in seam_crops_list:
            sc["url"] = f"/api/scans/{scan_id}/seam/{sc['seam_index']}"
        job.seam_crops = seam_crops_list
        job.previews["ai_enhanced"] = f"/api/scans/{scan_id}/preview/ai_enhanced?t={int(time.time())}"

        if self.settings.autosave_enabled:
            self._autosave_job(job, preset)

        self.broadcast_event("ai_enhanced", job.to_public_dict())
        return job

    def export_job(
        self,
        scan_id: str,
        *,
        formats: list[str] | None = None,
        color_mode: str | None = None,
        tiff_16bit: bool | None = None,
        debug_mode: bool | None = None,
        filename_prefix: str | None = None,
        render_mode: str | None = None,
    ) -> dict[str, Any]:
        job = self.get_job(scan_id)
        preset = self.preset_store.get_preset(job.preset_id)
        if color_mode and color_mode != job.color_mode:
            job.color_mode = color_mode
            job_prev_dir = self.preview_cache_dir / job.scan_id
            if job._stitch_preset:
                save_preview_image(job._stitch_preset, preset, job_prev_dir / "preview_preset.png", color_mode=color_mode)
            if job._stitch_auto:
                save_preview_image(job._stitch_auto, preset, job_prev_dir / "preview_auto.png", color_mode=color_mode)
            if job._stitch_ai:
                save_preview_image(job._stitch_ai, preset, job_prev_dir / "preview_ai.png", color_mode=color_mode)

        if filename_prefix and filename_prefix.strip():
            self.settings.filename_prefix = filename_prefix.strip()

        mode = str(render_mode or job.active_render_mode or "preset")
        stitch_res = self._stitch_for_mode(job, mode)
        desired_base, used_seq = self._render_export_basename(
            job, mode, filename_prefix if (filename_prefix and filename_prefix.strip()) else None
        )
        exported = export_scan_files(
            stitch_res=stitch_res,
            preset=preset,
            output_dir=self.settings.output_dir,
            base_name=desired_base,
            formats=formats or self.settings.export_formats,
            color_mode=job.color_mode,
            tiff_16bit=self.settings.tiff_16bit if tiff_16bit is None else tiff_16bit,
            jpg_quality=self.settings.jpg_quality,
            debug_mode=self.settings.debug_mode if debug_mode is None else debug_mode,
            raw_records_bytes=job._raw_rec_bytes,
            transcript=job._transcript,
            drift_report=job.drift_report,
            avoid_overwrite=False,
        )
        self._note_export_sequence(used_seq)
        exported["render_mode"] = mode
        job.exported = exported
        return exported

    def save_debug_capture(self, scan_id: str, formats: list[str] | None = None) -> dict[str, Any]:
        """Persist the reusable raw stream and visual copies of a completed scan.

        ``rec`` is the authoritative reusable capture (the app can reload it via
        the .rec source).  ``npy`` is the lossless decoded 5-camera raster for
        offline analysis.  ``tiff``/``png`` are 8-bit visual copies.
        """
        job = self.get_job(scan_id)
        if job._raster is None:
            raise ValueError("Raw raster is no longer in memory for this scan")
        chosen = [str(f).lower() for f in (formats or ["rec", "tiff", "png"])]
        base_dir = Path(self.settings.output_dir) / "debug" / f"{job.scan_id}-capture"
        base_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}

        if "rec" in chosen:
            if not job._raw_rec_bytes:
                raise ValueError("Raw .rec bytes are not retained for this scan")
            p = base_dir / f"{job.scan_id}.rec"
            p.write_bytes(job._raw_rec_bytes)
            files["rec"] = str(p)

        if "npy" in chosen:
            p = base_dir / f"{job.scan_id}-raw.npy"
            np.save(p, job._raster)
            files["npy"] = str(p)

        if "tiff" in chosen or "png" in chosen:
            preset = self.preset_store.get_preset(job.preset_id)
            arr8 = u16_to_srgb8(job._raster, preset, already_it8_srgb=False)
            im = Image.fromarray(arr8)
            if "tiff" in chosen:
                p = base_dir / f"{job.scan_id}-raw.tiff"
                im.save(p, format="TIFF", compression="tiff_lzw", dpi=(int(job.dpi), int(job.dpi)))
                files["tiff"] = str(p)
            if "png" in chosen:
                p = base_dir / f"{job.scan_id}-raw.png"
                im.save(p, format="PNG")
                files["png"] = str(p)

        manifest = {
            "scan_id": job.scan_id,
            "dpi": int(job.dpi),
            "rows": int(job.rows),
            "source": job.source,
            "preset_id": job.preset_id,
            "formats": chosen,
            "files": files,
            "sizes": {k: (Path(v).stat().st_size if Path(v).exists() else None) for k, v in files.items()},
            "notes": (
                "The .rec file is the reusable raw capture. The .npy is the decoded "
                "5-camera 16-bit raster. TIFF/PNG are 8-bit visual copies."
            ),
        }
        mp = base_dir / f"{job.scan_id}-capture.json"
        mp.write_text(json.dumps(manifest, indent=2))
        files["manifest"] = str(mp)
        return {"scan_id": job.scan_id, "output_dir": str(base_dir), "files": files, "sizes": manifest["sizes"]}

    def get_job(self, scan_id: str) -> ScanJob:
        if scan_id not in self.jobs:
            raise KeyError(f"Scan job {scan_id!r} not found")
        return self.jobs[scan_id]


def load_rec_concat_bytes(raw: bytes) -> tuple[bytes, dict[str, Any]]:
    """In-memory version of ``load_rec_concat``."""
    import struct as _struct

    off = 0
    n = len(raw)
    records_by_seq: dict[int, bytes] = {}
    while off + 6 <= n:
        seq, dlen = _struct.unpack_from("<IH", raw, off)
        o = off + 6
        if seq not in records_by_seq:
            records_by_seq[seq] = raw[o : o + dlen]
        off = o + dlen
    sorted_seqs = sorted(records_by_seq.keys())
    buf = b"".join(records_by_seq[s] for s in sorted_seqs)
    return buf, {"records": len(sorted_seqs), "payload_bytes": len(buf)}


# ---------------------------------------------------------------------------
# FastAPI Request Schemas & Route Definitions
# ---------------------------------------------------------------------------

class ScanStartRequest(BaseModel):
    source_mode: str = "live"                 # "live" | "rec_file" | "demo_architectural" | "demo_artwork"
    rec_path: str | None = None
    paper_size: str = "full_42"               # "auto" | "full_42" | "arch_e" | "arch_d" | "iso_a0" | ... | "custom"
    orientation: Literal["portrait", "landscape"] = "portrait"
    width_inches: float = Field(default=42.0, gt=0.0, le=42.0)
    dpi: int = 300                            # 100 | 200 | 300 | 400 | 600 | 1200
    color_mode: str = "color"                 # "color" | "grayscale"
    preset_id: str | None = None
    simulate_thermal_drift: bool = True
    seam_color_blend: bool | None = None
    seam_blend_width_px: int | None = None
    seam_blend_strength: float | None = None
    seam_feather_mode: str = "classic"
    seconds: float = 25.0
    demo_rows: int = 960
    demo_paper_width_inches: float = 36.0
    filename_prefix: str | None = None


class PanelEnableRequest(BaseModel):
    enabled: bool = True


class PanelLedRequest(BaseModel):
    on: bool = True


class DriftDecisionRequest(BaseModel):
    choice: str = "auto_adjust"               # "preset" | "auto_adjust"
    promote_to_preset: bool = False
    save_as_new_preset_name: str | None = None


class DebugCaptureRequest(BaseModel):
    formats: list[str] = ["rec", "tiff", "png"]


class SheetCalibrationRequest(BaseModel):
    sheet_mode: str = "it8_full"              # "white_flatfield" | "it8_full"
    save_as_preset_id: str | None = None
    save_as_preset_name: str | None = None
    it8_dx_300px: int = 0                     # user fine-tuning in 300-dpi pixels
    it8_dy_300px: int = 0
    auto_align: bool = True                   # sweep the IT8 window and keep the best fit


class EnhanceRequest(BaseModel):
    blend_seam_colors: bool = True
    seam_blend_width_px: int = 128
    seam_blend_strength: float = 0.85
    seam_feather_mode: str = ""
    heal_seams: bool = True
    seam_radius_px: int = 6
    clean_background: bool = False
    background_strength: float = 0.65
    denoise_shadows: bool = False
    denoise_strength: float = 0.55
    sharpen_detail: bool = False
    sharpen_amount: float = 0.45
    external_provider: str = "none"
    external_url: str = ""
    external_api_key: str = ""
    external_prompt: str = "Clean and enhance scan while preserving exact geometry and fine details."


class ExportRequest(BaseModel):
    formats: list[str] | None = None
    color_mode: str | None = None
    tiff_16bit: bool | None = None
    debug_mode: bool | None = None
    filename_prefix: str | None = None
    # "preset" | "auto_adjust" | "ai_enhanced"; defaults to the job's active render.
    render_mode: str | None = None


def create_app(workspace: ScanWorkspaceManager | None = None) -> FastAPI:
    ws_mgr = workspace or ScanWorkspaceManager()
    app = FastAPI(
        title="Contex HD Ultra i4250s — Ubuntu Scanner Studio & MCP Server",
        version="1.0.0",
    )
    app.state.workspace = ws_mgr

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_Methods=["*"],
        allow_headers=["*"],
    ) if False else app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        hw = ws_mgr.engine.probe_status(timeout=0.6)
        presets = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "K": p.K,
                "it8_enabled": p.it8.enabled,
                "dE76_mean": p.it8.dE76_mean,
                "updated": p.updated,
                "is_builtin": p.is_builtin,
            }
            for p in ws_mgr.preset_store.list_presets()
        ]
        return {
            "hardware": hw,
            "settings": ws_mgr.settings.to_dict(),
            "presets": presets,
            "paper_sizes": list(PAPER_SIZE_PRESETS.values()),
            "supported_resolutions_dpi": list(SUPPORTED_RESOLUTIONS_DPI),
            "available_rec_files": ws_mgr.list_available_rec_files(),
            "recent_scans": [
                j.to_public_dict()
                for j in sorted(ws_mgr.jobs.values(), key=lambda x: x.created_at, reverse=True)[:10]
            ],
        }

    @app.post("/api/discover")
    def api_discover() -> dict[str, Any]:
        scanners = ws_mgr.engine.discover()
        return {"scanners": scanners, "active_host": ws_mgr.engine.host}

    @app.post("/api/paper/{action}")
    def api_paper(action: str) -> dict[str, Any]:
        try:
            return ws_mgr.engine.paper_control(action)
        except Exception as exc:
            return {
                "action": action,
                "ok": False,
                "simulated": True,
                "message": f"Scanner offline ({exc}); command '{action}' validated in dry-run.",
            }

    @app.get("/api/settings")
    def api_get_settings() -> dict[str, Any]:
        return ws_mgr.settings.to_dict()

    @app.put("/api/settings")
    def api_put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        return ws_mgr.update_settings(payload).to_dict()

    @app.get("/api/fs/dirs")
    def api_list_dirs(path: str | None = None) -> dict[str, Any]:
        """List sub-directories for the output-folder picker (directories only)."""
        raw = (path or "").strip() or ws_mgr.settings.output_dir or str(Path.home())
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid path: {exc}") from exc
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Directory not found: {target}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {target}")

        directories: list[dict[str, str]] = []
        truncated = False
        try:
            with os.scandir(target) as entries:
                for entry in entries:
                    if len(directories) >= 500:
                        truncated = True
                        break
                    try:
                        if not entry.is_dir():
                            continue
                    except OSError:
                        continue
                    directories.append({"name": entry.name, "path": entry.path})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"Permission denied: {target}") from exc
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        directories.sort(key=lambda d: d["name"].lower())
        return {
            "path": str(target),
            "parent": str(target.parent) if target.parent != target else None,
            "home": str(Path.home()),
            "directories": directories,
            "truncated": truncated,
        }

    @app.get("/api/presets")
    def api_list_presets() -> dict[str, Any]:
        return {
            "presets": [p.to_dict() for p in ws_mgr.preset_store.list_presets()],
            "active_preset_id": ws_mgr.settings.active_preset_id,
        }

    # -- front panel -------------------------------------------------------
    @app.get("/api/panel/status")
    def api_panel_status() -> dict[str, Any]:
        return ws_mgr.panel.snapshot()

    @app.post("/api/panel/enable")
    def api_panel_enable(req: PanelEnableRequest) -> dict[str, Any]:
        ws_mgr.settings.panel_keys_enabled = bool(req.enabled)
        ws_mgr._save_settings(ws_mgr.settings)
        if req.enabled:
            ws_mgr.panel.start()
        else:
            ws_mgr.panel.stop()
        return {"enabled": bool(req.enabled), "panel": ws_mgr.panel.snapshot()}

    @app.post("/api/panel/led")
    def api_panel_led(req: PanelLedRequest) -> dict[str, Any]:
        """Drive the scan-ready LED. This WRITES to the scanner."""
        return ws_mgr.panel.set_ready_led(bool(req.on))

    @app.post("/api/presets/{preset_id}/rollback")
    def api_rollback_preset(preset_id: str) -> dict[str, Any]:
        restored = ws_mgr.preset_store.rollback_preset(preset_id)
        if restored is None:
            raise HTTPException(status_code=404, detail="No backup found for this preset")
        return {"preset": restored.to_dict()}

    @app.post("/api/scan/cancel")
    def api_cancel_scan() -> dict[str, Any]:
        return ws_mgr.cancel_active_scan()

    @app.post("/api/scan/start")
    def api_start_scan(req: ScanStartRequest) -> dict[str, Any]:
        ws_mgr.panel.suspend()
        try:
            job = ws_mgr.execute_scan_workflow(
                source_mode=req.source_mode,
                rec_path=req.rec_path,
                paper_size=req.paper_size,
                orientation=req.orientation,
                width_inches=req.width_inches,
                dpi=req.dpi,
                color_mode=req.color_mode,
                preset_id=req.preset_id,
                simulate_thermal_drift=req.simulate_thermal_drift,
                seam_color_blend=req.seam_color_blend,
                seam_blend_width_px=req.seam_blend_width_px,
                seam_blend_strength=req.seam_blend_strength,
                seam_feather_mode=req.seam_feather_mode,
                seconds=req.seconds,
                demo_rows=req.demo_rows,
                demo_paper_width_inches=req.demo_paper_width_inches,
                filename_prefix=req.filename_prefix,
            )
            res = job.to_public_dict()
            res["next_sequence_number"] = ws_mgr.settings.next_sequence_number
            res["filename_prefix"] = ws_mgr.settings.filename_prefix
            return res
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            ws_mgr.panel.resume()

    @app.get("/api/scans/{scan_id}")
    def api_get_scan(scan_id: str) -> dict[str, Any]:
        try:
            return ws_mgr.get_job(scan_id).to_public_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/scans/{scan_id}/drift-decision")
    def api_drift_decision(scan_id: str, req: DriftDecisionRequest) -> dict[str, Any]:
        try:
            job = ws_mgr.resolve_drift_decision(
                scan_id,
                choice=req.choice,
                promote_to_preset=req.promote_to_preset,
                save_as_new_preset_name=req.save_as_new_preset_name,
            )
            return job.to_public_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/scans/{scan_id}/save-debug-capture")
    def api_save_debug_capture(scan_id: str, req: DebugCaptureRequest) -> dict[str, Any]:
        try:
            return ws_mgr.save_debug_capture(scan_id, formats=req.formats)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scans/{scan_id}/calibrate-sheet")
    def api_calibrate_sheet(scan_id: str, req: SheetCalibrationRequest) -> dict[str, Any]:
        try:
            return ws_mgr.calibrate_with_official_sheet(
                scan_id,
                sheet_mode=req.sheet_mode,
                save_as_preset_id=req.save_as_preset_id,
                save_as_preset_name=req.save_as_preset_name,
                it8_dx_300px=req.it8_dx_300px,
                it8_dy_300px=req.it8_dy_300px,
                auto_align=req.auto_align,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scans/{scan_id}/enhance")
    def api_enhance_scan(scan_id: str, req: EnhanceRequest) -> dict[str, Any]:
        try:
            opts = EnhancementOptions(
                blend_seam_colors=req.blend_seam_colors,
                seam_blend_width_px=req.seam_blend_width_px,
                seam_blend_strength=req.seam_blend_strength,
                seam_feather_mode=req.seam_feather_mode or ws_mgr.settings.seam_feather_mode,
                heal_seams=req.heal_seams,
                seam_radius_px=req.seam_radius_px,
                clean_background=req.clean_background,
                background_strength=req.background_strength,
                denoise_shadows=req.denoise_shadows,
                denoise_strength=req.denoise_strength,
                sharpen_detail=req.sharpen_detail,
                sharpen_amount=req.sharpen_amount,
                external_provider=req.external_provider,
                external_url=req.external_url or ws_mgr.settings.photocraft_url,
                external_api_key=req.external_api_key or ws_mgr.settings.photocraft_api_key,
                external_prompt=req.external_prompt,
            )
            job = ws_mgr.run_ai_enhancement(scan_id, opts)
            return job.to_public_dict()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scans/{scan_id}/export")
    def api_export_scan(scan_id: str, req: ExportRequest) -> dict[str, Any]:
        try:
            exported = ws_mgr.export_job(
                scan_id,
                formats=req.formats,
                color_mode=req.color_mode,
                tiff_16bit=req.tiff_16bit,
                debug_mode=req.debug_mode,
                filename_prefix=req.filename_prefix,
                render_mode=req.render_mode,
            )
            return {
                "exported": exported,
                "scan": ws_mgr.get_job(scan_id).to_public_dict(),
                "next_sequence_number": ws_mgr.settings.next_sequence_number,
                "filename_prefix": ws_mgr.settings.filename_prefix,
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/scans/{scan_id}/preview/{kind}")
    def api_get_preview(scan_id: str, kind: str) -> FileResponse:
        file_map = {
            "raw": "preview_raw.png",
            "preset": "preview_preset.png",
            "auto_adjust": "preview_auto.png",
            "ai_enhanced": "preview_ai.png",
        }
        fname = file_map.get(kind, "preview_auto.png")
        p = ws_mgr.preview_cache_dir / scan_id / fname
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Preview {kind!r} not found")
        return FileResponse(p, media_type="image/png")

    @app.get("/api/scans/{scan_id}/seam/{seam_idx}")
    def api_get_seam_crop(scan_id: str, seam_idx: int) -> FileResponse:
        try:
            job = ws_mgr.get_job(scan_id)
            matches = [c for c in job.seam_crops if c["seam_index"] == seam_idx]
            if not matches:
                raise HTTPException(status_code=404, detail="Seam crop not found")
            return FileResponse(matches[0]["path"], media_type="image/png")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket) -> None:
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _listener(msg: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, msg)

        ws_mgr.event_listeners.append(_listener)
        try:
            while True:
                msg = await queue.get()
                await websocket.send_json(msg)
        except WebSocketDisconnect:
            pass
        finally:
            if _listener in ws_mgr.event_listeners:
                ws_mgr.event_listeners.remove(_listener)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index_page() -> HTMLResponse:
        idx = WEB_DIR / "index.html"
        if idx.exists():
            return HTMLResponse(idx.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Contex Scanner Server Running</h1>")

    return app


def run_app_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    scanner_ip: str = DEFAULT_SCANNER_IP,
    scanner_port: int = 58170,
) -> None:
    import uvicorn

    ws_mgr = ScanWorkspaceManager()
    if scanner_ip:
        ws_mgr.update_settings({"scanner_ip": scanner_ip})
        ws_mgr.engine.port = scanner_port
    app = create_app(ws_mgr)
    uvicorn.run(app, host=host, port=port)

