"""End-to-end tests for the Contex Studio application workflow, calibration,
thermal drift comparison, Official Contex Sheet wizard, 5-camera stitching,
AI restoration, multi-format export (TIFF/JPG/PDF, Color/Grayscale), FastAPI
REST server, and MCP server tools.
"""
from __future__ import annotations

import pathlib
import tempfile
import threading
from unittest import mock
import unittest

import numpy as np
from fastapi.testclient import TestClient

from ctx.ai import EnhancementOptions, blend_seam_color_artwork_u16, run_enhancement_pipeline
from ctx.calibration import (
    IT8_NX,
    IT8_NY,
    PresetStore,
    WINDOW_PX_300DPI,
    analyze_drift,
    calibrate_from_official_sheet,
    extract_black_levels,
    it8_fit_patches,
    it8_sample_patches,
    load_it8_reference,
    module_gaps,
    piece_matrix,
    promote_margin_calibration_to_preset,
    raw_camera_images,
    search_it8_offset,
    sheet_geometry_for_dpi,
    xyz_to_lab,
)
from ctx.cli import build_parser
from ctx.engine import ScannerEngine, check_network_preflight
from ctx.pipeline import (
    export_scan_files,
    generate_synthetic_5cam_raster,
    process_scan_to_stitch,
    resolve_paper_width,
)
from ctx.proto import cdb as proto_cdb
from ctx.server import (
    AppSettings,
    ScanStartRequest,
    ScanWorkspaceManager,
    create_app,
)


class CalibrationAndStitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = pathlib.Path(self.tmp_dir.name)
        self.store = PresetStore(base_dir=self.base_dir)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_preset_store_seeds_and_rollbacks(self) -> None:
        presets = self.store.list_presets()
        self.assertGreaterEqual(len(presets), 3)
        default_preset = self.store.get_preset("default-it8")
        self.assertEqual(len(default_preset.W), 5)
        self.assertEqual(len(default_preset.B), 5)

        # Save an update and verify .bak rollback works
        orig_desc = default_preset.description
        default_preset.description = "updated description for test"
        self.store.save_preset(default_preset, backup=True)
        reloaded = self.store.get_preset(default_preset.id)
        self.assertEqual(reloaded.description, "updated description for test")

        rolled = self.store.rollback_preset(default_preset.id)
        self.assertIsNotNone(rolled)
        self.assertEqual(rolled.description, orig_desc)

    def test_synthetic_5cam_gaps_and_drift_detection(self) -> None:
        preset = self.store.get_preset("default-it8")

        # 1. With simulated thermal drift on 3D architectural drawing -> DRIFT
        raster_drift, _ = generate_synthetic_5cam_raster(
            rows=180,
            preset=preset,
            subject="architectural_and_it8",
            simulate_thermal_drift=True,
        )
        gaps = module_gaps(raster_drift.mean(axis=2, dtype=np.float32))
        self.assertEqual(len(gaps), 5)
        pmeta = piece_matrix(gaps, raster_drift.shape[1])
        cams_drift = raw_camera_images(raster_drift, pmeta)
        self.assertEqual(len(cams_drift), 5)

        report_drift = analyze_drift(raster_drift, 180, preset, gaps=gaps)
        self.assertEqual(report_drift["verdict"], "DRIFT")
        self.assertGreater(report_drift["stored_spread"], 0.015)

        # 2. Without thermal drift -> OK
        raster_ok, _ = generate_synthetic_5cam_raster(
            rows=180,
            preset=preset,
            subject="architectural_and_it8",
            simulate_thermal_drift=False,
        )
        report_ok = analyze_drift(raster_ok, 180, preset, gaps=gaps)
        self.assertEqual(report_ok["verdict"], "OK")

        # 3. Full-bleed dark artwork -> INSUFFICIENT_WHITE
        raster_art, _ = generate_synthetic_5cam_raster(
            rows=180,
            preset=preset,
            subject="full_bleed_artwork",
            simulate_thermal_drift=False,
        )
        report_art = analyze_drift(raster_art, 180, preset, gaps=gaps)
        self.assertEqual(report_art["verdict"], "INSUFFICIENT_WHITE")

    def test_stitch_5_cameras_and_margin_promotion(self) -> None:
        preset = self.store.get_preset("default-it8")
        raster, _ = generate_synthetic_5cam_raster(
            rows=160,
            preset=preset,
            subject="architectural_and_it8",
            simulate_thermal_drift=True,
        )
        gaps = module_gaps(raster.mean(axis=2, dtype=np.float32))
        pmeta = piece_matrix(gaps, raster.shape[1])
        blacks = extract_black_levels(raster, pmeta)
        self.assertEqual(len(blacks), 5)

        # Stitch with default preset
        stitch_def = process_scan_to_stitch(raster, 160, preset, mode="preset", gaps=gaps)
        self.assertEqual(stitch_def.doc_u16.shape, (160, WINDOW_PX_300DPI, 3))
        self.assertEqual(stitch_def.zero_columns, 0)
        self.assertEqual(stitch_def.joins, [2586, 5108, 7642, 10168])

        # Promote margin self-calibration to a preset and stitch
        auto_preset = promote_margin_calibration_to_preset(
            raster,
            160,
            preset,
            new_id="test_auto",
            new_name="Test Auto Preset",
        )
        stitch_auto = process_scan_to_stitch(raster, 160, auto_preset, mode="preset", gaps=gaps)
        self.assertEqual(stitch_auto.doc_u16.shape, (160, WINDOW_PX_300DPI, 3))
        self.assertEqual(stitch_auto.zero_columns, 0)

    def test_auto_width_detection_and_dpi_scaling(self) -> None:
        preset = self.store.get_preset("default-it8")
        # Simulate a 24.0" ARCH D sheet placed on the 42.0" scanner bed
        raster_24in, _ = generate_synthetic_5cam_raster(
            rows=150,
            preset=preset,
            subject="architectural_and_it8",
            simulate_thermal_drift=False,
            paper_width_inches=24.0,
        )
        # 1. Auto-detect width at 300 DPI -> should snap to ARCH D (24.0" = 7,200 px)
        stitch_300 = process_scan_to_stitch(
            raster_24in,
            150,
            preset,
            dpi=300,
            paper_size="auto",
            auto_detect_width=True,
        )
        self.assertIsNotNone(stitch_300.auto_width_info)
        self.assertEqual(stitch_300.auto_width_info["matched_preset_id"], "arch_d")
        self.assertEqual(stitch_300.document_width, 7200)

        # 2. Same Auto-Detect scan requested at 200 DPI -> 24.0" * 200 DPI = 4,800 px wide, 100 rows
        stitch_200 = process_scan_to_stitch(
            raster_24in,
            150,
            preset,
            dpi=200,
            paper_size="auto",
            auto_detect_width=True,
        )
        self.assertEqual(stitch_200.dpi, 200)
        self.assertEqual(stitch_200.document_width, 4800)
        self.assertEqual(stitch_200.rows, 100)

    def test_sheet_geometry_scales_with_dpi(self) -> None:
        g300 = sheet_geometry_for_dpi(300)
        g600 = sheet_geometry_for_dpi(600)
        self.assertEqual(g300["white_band"], (2795, 3545))
        self.assertEqual(g300["black_band"], (3861, 4766))
        self.assertEqual(g600["white_band"], (5590, 7090))
        self.assertEqual(g600["black_band"], (7722, 9532))
        self.assertEqual(g600["min_rows"], 4400)
        self.assertAlmostEqual(g600["scale"], 2.0)

    def test_official_calibration_sheet_wizard(self) -> None:
        preset = self.store.get_preset("default-it8")
        sheet_raster, _ = generate_synthetic_5cam_raster(
            rows=160,
            preset=preset,
            subject="architectural_and_it8",
            simulate_thermal_drift=True,
        )
        # Test both white_flatfield and it8_full modes
        cal_white, rep_w = calibrate_from_official_sheet(
            sheet_raster,
            160,
            preset,
            sheet_mode="white_flatfield",
            new_id="cal_white_test",
            new_name="Cal White Test",
        )
        self.assertEqual(len(cal_white.W), 5)
        self.assertEqual(rep_w["cameras_fitted"], 5)

        cal_it8, rep_it8 = calibrate_from_official_sheet(
            sheet_raster,
            160,
            preset,
            sheet_mode="it8_full",
            new_id="cal_it8_test",
            new_name="Cal IT8 Test",
        )
        self.assertEqual(len(cal_it8.W), 5)
        self.assertEqual(len(cal_it8.it8.matrix), 3)

        # A blank preset name must not overwrite the active preset.
        cal_default, rep_default = calibrate_from_official_sheet(
            sheet_raster, 160, preset, sheet_mode="it8_full",
        )
        self.assertNotEqual(cal_default.id, preset.id)
        self.assertTrue(cal_default.id.startswith("official-sheet-"))
        self.assertIn("sheet_bands_detected", rep_default)


class SheetIt8AlignmentTests(unittest.TestCase):
    """The 22x12 IT8 sampling window and the auto-align search that centres it."""

    CELL = 30
    X0 = 900
    Y0 = 200
    SHIFT = 100

    def _synthetic_sheet(self, shift_px: int) -> tuple[np.ndarray, dict, np.ndarray, np.ndarray]:
        """Mosaic of distinct, hue-separated patches drawn ``shift_px`` right of nominal.

        Every cell gets its own colour triple so a mis-window cannot be absorbed by
        the gamma/linear fit: only the offset the mosaic was drawn at reproduces the
        reference table.
        """
        n = IT8_NX * IT8_NY
        levels = (0.15, 0.55, 0.9)
        xyz = np.empty((n, 3), dtype=np.float64)
        for k in range(n):
            idx = k * 7 + 3          # a cheap low-discrepancy walk over the 27 colours
            xyz[k] = [levels[(idx) % 3], levels[(idx // 3) % 3], levels[(idx // 9) % 3]]
        lab = xyz_to_lab(xyz)
        comp = np.full((self.Y0 + IT8_NY * self.CELL + 200,
                        self.X0 + IT8_NX * self.CELL + 400, 3), 0.05 * 65535.0, dtype=np.float64)
        for r in range(IT8_NY):
            for c in range(IT8_NX):
                colour = xyz[r * IT8_NX + c]
                comp[self.Y0 + r * self.CELL + 2:self.Y0 + (r + 1) * self.CELL - 2,
                     self.X0 + shift_px + c * self.CELL + 2:self.X0 + shift_px + (c + 1) * self.CELL - 2,
                     :] = colour * 65535.0
        geom = {
            "mosaic": (self.X0, self.X0 + IT8_NX * self.CELL - 1,
                       self.Y0, self.Y0 + IT8_NY * self.CELL - 1),
            "scale": 1.0,
        }
        return comp, geom, xyz, lab

    def test_sample_patches_reads_cell_interiors(self) -> None:
        comp, geom, xyz, _lab = self._synthetic_sheet(0)
        rgb = it8_sample_patches(comp, geom, 0, 0)
        assert rgb is not None
        self.assertEqual(rgb.shape, (IT8_NX * IT8_NY, 3))
        # Cell medians land on the drawn colours, so the window geometry is right.
        self.assertLess(float(np.abs(rgb - xyz * 65535.0).max()), 1.0)

    def test_sample_patches_rejects_offscreen_window(self) -> None:
        comp, geom, _xyz, _lab = self._synthetic_sheet(0)
        self.assertIsNone(it8_sample_patches(comp, geom, 10_000, 0))
        self.assertIsNone(it8_sample_patches(comp, geom, 0, 10_000))

    def test_auto_align_reports_a_fitting_window(self) -> None:
        """The search must return a window that actually fits, inside the searched span.

        A flat mock mosaic is periodic, so several windows can fit equally well and
        this test deliberately does not pin the exact offset.  Recovery of a
        *mis-placed* sheet is covered by the real capture used during development
        (mean dE76 34.65 at the nominal window vs 4.06 at the offset the search
        picked), which cannot be synthesised here; this guards the search plumbing:
        candidate enumeration, the dE objective, the reported fields and the span.
        """
        comp, geom, xyz, lab = self._synthetic_sheet(0)
        found = search_it8_offset(comp, geom, xyz, lab)
        assert found is not None
        self.assertLess(found["dE76_mean"], 1.0)
        self.assertLessEqual(abs(found["dx"]), 280)
        self.assertLessEqual(abs(found["dy"]), 60)
        self.assertEqual(found["patches_total"], IT8_NX * IT8_NY)
        self.assertIsNotNone(found["dE76_mean_at_zero"])
        self.assertGreater(found["evaluated_offsets"], 1)

    def test_search_returns_none_when_no_mosaic_is_present(self) -> None:
        rng = np.random.default_rng(7)
        xyz = rng.uniform(0.1, 0.8, size=(IT8_NX * IT8_NY, 3))
        lab = xyz_to_lab(xyz)
        comp = np.full((900, 2600, 3), 1000.0)
        geom = {"mosaic": (900, 1559, 200, 559), "scale": 1.0}
        self.assertIsNone(search_it8_offset(comp, geom, xyz, lab, span=120, step=40, dy_span=40))


class AIEnhancementAndExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.out_dir = pathlib.Path(self.tmp_dir.name)
        self.store = PresetStore(base_dir=self.out_dir)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_ai_enhancement_pipeline(self) -> None:
        preset = self.store.get_preset("default-it8")
        raster, _ = generate_synthetic_5cam_raster(rows=120, preset=preset, simulate_thermal_drift=False)
        stitch_res = process_scan_to_stitch(raster, 120, preset)

        opts = EnhancementOptions(
            blend_seam_colors=True,
            seam_blend_width_px=128,
            seam_blend_strength=0.85,
            heal_seams=True,
            clean_background=True,
            denoise_shadows=True,
            sharpen_detail=True,
        )
        enhanced, applied = run_enhancement_pipeline(stitch_res.doc_u16, stitch_res.joins, opts)
        self.assertEqual(enhanced.shape, stitch_res.doc_u16.shape)
        self.assertEqual(enhanced.dtype, np.uint16)
        self.assertEqual(len(applied), 5)

        opts_art = EnhancementOptions(
            blend_seam_colors=True, seam_blend_width_px=256,
            seam_blend_strength=0.85, seam_feather_mode="artwork",
            heal_seams=False, clean_background=False,
            denoise_shadows=False, sharpen_detail=False,
        )
        _, applied_art = run_enhancement_pipeline(stitch_res.doc_u16, stitch_res.joins, opts_art)
        self.assertEqual(len(applied_art), 1)
        self.assertIn("artwork", applied_art[0])

    def test_artwork_seam_feather_mode(self) -> None:
        # A solid two-tone seam: the artwork feather must keep the shape and
        # report its mode, and it must not increase the narrow seam step.
        doc = np.full((300, 600, 3), 30000, dtype=np.uint16)
        doc[:, 300:, 0] = 36000
        doc[:, 300:, 1] = 33000
        doc[:, 300:, 2] = 28000
        out, info = blend_seam_color_artwork_u16(doc, [300], blend_width_px=128, strength=0.85)
        self.assertEqual(out.shape, doc.shape)
        self.assertEqual(out.dtype, np.uint16)
        self.assertEqual(info["mode"], "artwork")
        self.assertLessEqual(info["after_rgb_step_mean"], info["before_rgb_step_mean"])

        preset = self.store.get_preset("artwork-photo")
        raster, _ = generate_synthetic_5cam_raster(rows=120, preset=preset, simulate_thermal_drift=False)
        res = process_scan_to_stitch(
            raster, 120, preset, seam_color_blend=True,
            seam_blend_width_px=256, seam_feather_mode="artwork",
        )
        self.assertEqual(res.seam_blend_info["mode"], "artwork")
        self.assertEqual(res.seam_blend_info["blend_width_px"], 256)
        self.assertTrue(any("artwork" in f for f in res.applied_filters))

    def test_seam_color_variation_blending_enable_disable_and_adjust(self) -> None:
        preset = self.store.get_preset("default-it8")
        # Generate a scan with strong camera-to-camera analog variation
        raster, _ = generate_synthetic_5cam_raster(
            rows=120,
            preset=preset,
            simulate_thermal_drift=True,
        )

        # 1. Stitch with seam_color_blend disabled
        unblended = process_scan_to_stitch(
            raster,
            120,
            preset,
            mode="preset",
            seam_color_blend=False,
        )
        self.assertIsNotNone(unblended.seam_blend_info)
        self.assertFalse(unblended.seam_blend_info["enabled"])
        step_before = unblended.seam_blend_info["before_rgb_step_mean"]
        self.assertGreater(step_before, 400.0)

        # 2. Stitch with seam_color_blend enabled & custom blend width / strength
        blended = process_scan_to_stitch(
            raster,
            120,
            preset,
            mode="preset",
            seam_color_blend=True,
            seam_blend_width_px=160,
            seam_blend_strength=0.90,
        )
        self.assertTrue(blended.seam_blend_info["enabled"])
        self.assertEqual(blended.seam_blend_info["blend_width_px"], 160)
        self.assertAlmostEqual(blended.seam_blend_info["strength"], 0.90, places=2)
        step_after = blended.seam_blend_info["after_rgb_step_mean"]
        self.assertLess(step_after, step_before * 0.35)

    def test_export_tiff_jpg_pdf_color_and_grayscale_with_debug(self) -> None:
        preset = self.store.get_preset("default-it8")
        raster, raw_rec = generate_synthetic_5cam_raster(rows=120, preset=preset, simulate_thermal_drift=False)
        stitch_res = process_scan_to_stitch(raster, 120, preset)

        # Export Color TIFF + JPG + PDF + Debug artifacts
        res_color = export_scan_files(
            stitch_res=stitch_res,
            preset=preset,
            output_dir=self.out_dir / "color_out",
            base_name="scan_color",
            formats=["tiff", "jpg", "pdf"],
            color_mode="color",
            tiff_16bit=False,
            debug_mode=True,
            raw_records_bytes=raw_rec,
            transcript=[{"step": "test_cmd"}],
        )
        self.assertEqual(len(res_color["files"]), 3)
        self.assertGreaterEqual(len(res_color["debug_files"]), 3)

        # Export Grayscale TIFF + JPG + PDF
        res_gray = export_scan_files(
            stitch_res=stitch_res,
            preset=preset,
            output_dir=self.out_dir / "gray_out",
            base_name="scan_gray",
            formats=["tiff", "jpg", "pdf"],
            color_mode="grayscale",
            tiff_16bit=True,
            debug_mode=False,
        )
        self.assertEqual(len(res_gray["files"]), 3)
        for f in res_gray["files"]:
            self.assertTrue(pathlib.Path(f["path"]).exists())


class EngineSafetyAndServerAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = pathlib.Path(self.tmp_dir.name)
        self.manager = ScanWorkspaceManager(config_dir=self.base_dir / "config")
        self.manager.update_settings({
            "output_dir": str(self.base_dir / "scans_output"),
            "scanner_ip": "127.0.0.1",
        })
        self.manager.engine.port = 59999
        self.app = create_app(self.manager)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_oem_arming_sequence_never_writes_flash_0xa0(self) -> None:
        engine = ScannerEngine()
        seq = engine.build_oem_arming_sequence(width_inches=42.0)
        self.assertGreaterEqual(len(seq), 25)
        for name, cdb_bytes, _ in seq:
            # Must pass hard safety assertion across the entire OEM sequence (never WRITE BUFFER 0xA0 or mode 0x05)
            if cdb_bytes[0] == proto_cdb.Opcode.WRITE_BUFFER:
                self.assertNotEqual(cdb_bytes[2], 0xA0, f"Step {name} attempted WRITE BUFFER 0xA0!")
                self.assertIn(cdb_bytes[2], proto_cdb.SAFE_WRITE_BUFFER_IDS)

    def test_network_preflight(self) -> None:
        pf = check_network_preflight("10.0.0.40")
        self.assertEqual(pf["scanner_ip"], "10.0.0.40")

    def test_defaults_and_paper_orientation(self) -> None:
        # Product defaults: live hardware, architectural-neutral preset.
        self.assertEqual(AppSettings().active_preset_id, "architectural-neutral")
        self.assertEqual(ScanStartRequest().source_mode, "live")
        self.assertEqual(ScanStartRequest().orientation, "portrait")
        status = self.client.get("/api/status").json()
        self.assertEqual(status["settings"]["active_preset_id"], "architectural-neutral")
        self.assertEqual(
            AppSettings.from_dict({"export_formats": []}).export_formats,
            ["tiff", "jpg", "pdf"],
        )
        # Scan-form defaults seed a fresh settings file, then the last-used form
        # state is held (and validated on load).
        sd = AppSettings()
        self.assertEqual(
            (sd.scan_source, sd.scan_paper_size, sd.scan_orientation, sd.scan_width_inches,
             sd.scan_dpi, sd.scan_color_mode),
            ("live", "full_42", "portrait", 42.0, 300, "color"),
        )
        bad = AppSettings.from_dict({
            "scan_source": "nope",
            "scan_paper_size": "nope",
            "scan_orientation": "sideways",
            "scan_width_inches": 999,
            "scan_dpi": 123,
            "scan_color_mode": "sepia",
        })
        self.assertEqual(
            (bad.scan_source, bad.scan_paper_size, bad.scan_orientation, bad.scan_width_inches,
             bad.scan_dpi, bad.scan_color_mode),
            ("live", "full_42", "portrait", 42.0, 300, "color"),
        )
        self.assertEqual(AppSettings.from_dict({"scan_width_inches": -5}).scan_width_inches, 4.0)

        def width(paper, orientation="portrait", inches=42.0):
            return resolve_paper_width(paper, orientation, inches)

        self.assertEqual(width("ansi_a")["effective_width_in"], 8.5)
        land = width("ansi_a", "landscape")
        self.assertEqual(land["effective_width_in"], 11.0)
        self.assertTrue(land["orientation_applied"])
        self.assertFalse(land["clamped"])

        # Landscape long edges beyond the 42 in bed are clamped, not rejected.
        self.assertEqual(width("arch_e1", "landscape")["effective_width_in"], 42.0)
        self.assertFalse(width("arch_e1", "landscape")["clamped"])
        big = width("arch_e", "landscape")
        self.assertEqual(big["effective_width_in"], 42.0)
        self.assertTrue(big["clamped"])

        # Auto detects later; custom keeps the operator's cross-feed width.
        self.assertFalse(width("auto", "landscape")["orientation_applied"])
        self.assertEqual(width("custom", "portrait", 12.0)["effective_width_in"], 12.0)
        with self.assertRaises(ValueError):
            width("ansi_a", "sideways")

    def test_jpeg_quality_setting_persists_and_clamps(self) -> None:
        r = self.client.put("/api/settings", json={"jpg_quality": 82})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["jpg_quality"], 82)
        self.assertEqual(self.client.get("/api/settings").json()["jpg_quality"], 82)

        self.assertEqual(
            self.client.put("/api/settings", json={"jpg_quality": 0}).json()["jpg_quality"],
            1,
        )
        self.assertEqual(
            self.client.put("/api/settings", json={"jpg_quality": 250}).json()["jpg_quality"],
            100,
        )
        self.assertEqual(
            self.client.put("/api/settings", json={"jpg_quality": "not-a-number"}).json()["jpg_quality"],
            95,
        )

    def test_auto_number_counter_can_be_reset(self) -> None:
        """The Output card's Reset button puts the counter back to 1."""
        self.client.put("/api/settings", json={"next_sequence_number": 120})
        self.assertEqual(self.client.get("/api/settings").json()["next_sequence_number"], 120)

        reset = self.client.put("/api/settings", json={"next_sequence_number": 1})
        self.assertEqual(reset.json()["next_sequence_number"], 1)
        # Zero or negative must clamp, not produce an unusable start.
        self.assertEqual(
            self.client.put("/api/settings", json={"next_sequence_number": 0})
            .json()["next_sequence_number"],
            1,
        )

    def test_status_reports_the_running_build(self) -> None:
        """A long-running process serving older code is a recurring confusion."""
        payload = self.client.get("/api/status").json()
        self.assertIn("build", payload)
        self.assertTrue(payload["build"].get("commit"))
        self.assertTrue(payload["build"].get("started_at"))

    def test_output_dock_owns_auto_number_and_seam_group(self) -> None:
        """The rewired dock owns auto-numbering and the whole seam group.

        Both used to be duplicated into the Settings modal, which left the copy
        in the dock unwired.  Now the dock is the single owner, so every key it
        sends has to round-trip unchanged.
        """
        payload = {
            "auto_number_enabled": False,
            "filename_prefix_enabled": False,
            "seam_color_blend_enabled": False,
            "seam_blend_width_px": 256,
            "seam_blend_strength": 0.55,
            "seam_feather_mode": "artwork",
        }
        r = self.client.put("/api/settings", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        saved = self.client.get("/api/settings").json()
        for key, value in payload.items():
            if isinstance(value, float):
                self.assertAlmostEqual(saved[key], value, msg=key)
            else:
                self.assertEqual(saved[key], value, key)

        # A save that omits the OCR apply/cleanup keys must leave them alone:
        # Single Final File is policy now, and the dock no longer sends them, so
        # silently rewriting the persisted values would be a regression.
        before = self.client.get("/api/settings").json()
        self.client.put("/api/settings", json={"filename_prefix": "sheet"})
        after = self.client.get("/api/settings").json()
        self.assertEqual(after["ocr_cleanup"], before["ocr_cleanup"])
        self.assertEqual(after["ocr_apply"], before["ocr_apply"])

    def test_vision_prompt_setting_round_trips_and_feeds_the_naming_pass(self) -> None:
        """Settings owns the description prompt; an empty value means default."""
        r = self.client.put(
            "/api/settings",
            json={"ocr_vision_prompt": "  Describe only the room layout.  "},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.get("/api/settings").json()["ocr_vision_prompt"],
            "Describe only the room layout.",
        )
        self.assertEqual(
            self.manager._ocr_options()["prompt"],
            "Describe only the room layout.",
        )

        # Clearing it must remove the argument entirely, so the module's own
        # default (not an empty string) is what reaches the vision model.
        self.client.put("/api/settings", json={"ocr_vision_prompt": ""})
        self.assertNotIn("prompt", self.manager._ocr_options())

    def test_scan_id_is_prefix_timestamp_and_optional_number(self) -> None:
        """``[prefix_]YYMMDDHHMM[_NNNN]``: the timestamp is never optional."""
        self.manager.update_settings({
            "filename_prefix": "scan",
            "filename_prefix_enabled": True,
            "auto_number_enabled": True,
            "next_sequence_number": 1,
        })
        self.assertRegex(self.manager.allocate_next_scan_id(), r"^scan_\d{10}_0001$")

        self.manager.update_settings({"filename_prefix_enabled": False})
        self.assertRegex(self.manager.allocate_next_scan_id(), r"^\d{10}_0002$")

        self.manager.update_settings({"auto_number_enabled": False})
        self.assertRegex(self.manager.allocate_next_scan_id(), r"^\d{10}$")

    def test_output_directory_browser_lists_directories_only(self) -> None:
        root = self.base_dir / "browse_root"
        (root / "alpha").mkdir(parents=True)
        (root / "Beta").mkdir()
        (root / "notes.txt").write_text("not a directory", encoding="utf-8")

        r = self.client.get("/api/fs/dirs", params={"path": str(root)})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(
            [d["name"] for d in data["directories"]],
            ["alpha", "Beta"],
        )
        self.assertEqual(data["path"], str(root.resolve()))
        self.assertEqual(data["parent"], str(root.resolve().parent))
        self.assertFalse(data["truncated"])

        missing = self.client.get("/api/fs/dirs", params={"path": str(root / "missing")})
        self.assertEqual(missing.status_code, 404)
        not_a_dir = self.client.get("/api/fs/dirs", params={"path": str(root / "notes.txt")})
        self.assertEqual(not_a_dir.status_code, 400)

        # Defaults to the configured output directory when no path is supplied.
        default = self.client.get("/api/fs/dirs")
        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual(default.json()["path"], str((self.base_dir / "scans_output").resolve()))

    def test_web_ui_moves_output_settings_out_of_export_tab(self) -> None:
        html = self.client.get("/").text
        self.assertNotIn('id="sbtn-export"', html)
        self.assertNotIn('id="tab-content-export"', html)
        self.assertNotIn('id="exported-files-list"', html)
        for expected in (
            'id="autosave-enabled"',
            'id="export-prefix"',
            'id="export-next-seq"',
            'id="export-output-dir"',
            'id="btn-browse-output"',
            'id="browse-modal"',
            'id="set-jpg-quality"',
            'id="set-tiff-16bit"',
            # scan action + output dock now live in the right rail
            'class="sidebar sidebar-right"',
            'id="scan-drift-enabled"',
            'id="scan-drift-action"',
            'id="ai-auto-enhance"',
            # OCR card is Auto describe plus a status line
            'id="ocr-auto"',
            'id="ocr-alert"',
            # Settings owns the vision-description prompt
            'id="set-ocr-vision-prompt"',
            'id="set-ocr-vision-prompt-state"',
            'onclick="resetVisionPromptToDefault()"',
            'onclick="resetAutoNumber()"',
            # auto-numbering is owned by the output dock, not the settings modal
            'id="auto-number-enabled"',
            'Save current view',
        ):
            self.assertIn(expected, html)
        # the removed five-step stepper and the old "Active View" tab stay gone
        self.assertNotIn('id="step-1"', html)
        self.assertNotIn("Active View", html)
        # controls consolidated away by the profile/ownership refactor: each of
        # these had a single owner elsewhere and a duplicate here was dead.
        for gone in (
            'id="ocr-naming"',
            'id="ocr-vision"',
            'id="ocr-image-only"',
            'id="ocr-architectural"',
            'id="dummy-auto-number"',
            'id="ocr-profile"',
            'id="ocr-timestamp-prefix"',
            'id="ocr-status-line"',  # the whole OCR card is gone
            'id="set-seam-blend-enabled"',
            'id="set-auto-number"',
        ):
            self.assertNotIn(gone, html)

    def _demo_scan(self, prefix: str = "render", drift: bool = False) -> dict:
        r = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": drift,
            "filename_prefix": prefix,
            "demo_rows": 120,
        })
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_render_variants_share_one_number(self) -> None:
        job = self._demo_scan("var")
        sid = job["scan_id"]
        self.assertRegex(sid, r"^var_\d{10}_0001$")
        stamp = sid.split("_")[1]
        out = self.base_dir / "scans_output"
        # Autosave wrote the standard render on the scan's number.
        self.assertTrue((out / f"{sid}.jpg").exists())

        # Saving the auto-adjusted view keeps the number and appends _Auto.
        r = self.client.post(f"/api/scans/{sid}/export", json={"formats": ["jpg"], "render_mode": "auto_adjust"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["exported"]["base_name"], f"{sid}_Auto")
        self.assertTrue((out / f"{sid}_Auto.jpg").exists())
        self.assertEqual(r.json()["next_sequence_number"], job["next_sequence_number"])

        # Saving the same view twice must not overwrite: the whole group moves on.
        r2 = self.client.post(f"/api/scans/{sid}/export", json={"formats": ["jpg"], "render_mode": "auto_adjust"})
        self.assertEqual(r2.status_code, 200, r2.text)
        bumped = r2.json()["exported"]["base_name"]
        self.assertEqual(bumped, f"var_{stamp}_0002_Auto")
        self.assertTrue((out / f"var_{stamp}_0002_Auto.jpg").exists())
        # ...and the other variants of that new number are still free for the same scan
        self.assertEqual(r2.json()["next_sequence_number"], 3)

    def test_ai_render_is_rejected_until_post_processing_ran(self) -> None:
        job = self._demo_scan("aiv")
        r = self.client.post(f"/api/scans/{job['scan_id']}/export", json={"formats": ["jpg"], "render_mode": "ai_enhanced"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("AI Enhanced render is not available", r.json()["detail"])
        self.assertNotIn("ai_enhanced", job["available_renders"])

    def test_autosave_skips_ai_unless_the_workflow_runs_it(self) -> None:
        job = self._demo_scan("auto")
        sid = job["scan_id"]
        out = self.base_dir / "scans_output"
        ai = self.client.post(f"/api/scans/{sid}/enhance", json={"sharpen_detail": False})
        self.assertEqual(ai.status_code, 200, ai.text)
        self.assertEqual(ai.json()["active_render_mode"], "ai_enhanced")
        # AI is never autosaved on its own.
        self.assertFalse((out / f"{sid}_AI.jpg").exists())

        self.manager.update_settings({"auto_ai_enhance": True})
        job2 = self._demo_scan("autoai")
        sid2 = job2["scan_id"]
        ai2 = self.client.post(f"/api/scans/{sid2}/enhance", json={"sharpen_detail": False})
        self.assertEqual(ai2.status_code, 200, ai2.text)
        self.assertTrue((out / f"{sid2}.jpg").exists(), "standard render autosaved")
        self.assertTrue((out / f"{sid2}_AI.jpg").exists(), "auto-AI output autosaved on the same number")

    def test_drift_check_off_skips_analysis_and_auto_render(self) -> None:
        self.manager.update_settings({"drift_policy": "disabled"})
        job = self._demo_scan("nodrift", drift=True)
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["drift_report"]["verdict"], "DISABLED")
        self.assertEqual(job["available_renders"], ["preset"])
        self.assertNotIn("auto_adjust", job["previews"])
        r = self.client.post(f"/api/scans/{job['scan_id']}/export", json={"formats": ["jpg"], "render_mode": "auto_adjust"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("drift check was off", r.json()["detail"])

    def test_old_scan_buffers_are_released_for_bulk_runs(self) -> None:
        first = self._demo_scan("bulk")
        second = self._demo_scan("bulk")
        third = self._demo_scan("bulk")
        self.assertFalse(third["buffers_released"])
        old = self.client.get(f"/api/scans/{first['scan_id']}").json()
        self.assertTrue(old["buffers_released"], "oldest scan should have released its buffers")
        keep = self.client.get(f"/api/scans/{second['scan_id']}").json()
        self.assertFalse(keep["buffers_released"], "the newest two scans stay re-exportable")
        r = self.client.post(f"/api/scans/{first['scan_id']}/export", json={"formats": ["jpg"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("buffers were released", r.json()["detail"])

    def test_auto_ai_enhance_setting_round_trips(self) -> None:
        self.assertFalse(self.client.get("/api/settings").json()["auto_ai_enhance"])
        put = self.client.put("/api/settings", json={"auto_ai_enhance": True})
        self.assertEqual(put.status_code, 200, put.text)
        self.assertTrue(put.json()["auto_ai_enhance"])
        self.assertTrue(self.client.get("/api/settings").json()["auto_ai_enhance"])

    def test_live_preflight_fails_fast(self) -> None:
        with mock.patch.object(
            self.manager.engine, "probe_status",
            return_value={"connected": True, "media_code": 0x00},
        ):
            with self.assertRaisesRegex(RuntimeError, "No media detected"):
                self.manager._preflight_live_scan()

        with mock.patch.object(
            self.manager.engine, "probe_status",
            return_value={"connected": True, "media_code": 0x24},
        ):
            with self.assertRaisesRegex(RuntimeError, "still scanning"):
                self.manager._preflight_live_scan()

        # Ejected / loaded media is allowed: recovery will load it.
        for code in (0x10, 0x11, 0x30):
            with mock.patch.object(
                self.manager.engine, "probe_status",
                return_value={"connected": True, "media_code": code},
            ):
                self.manager._preflight_live_scan()

        with mock.patch.object(
            self.manager.engine, "probe_status",
            return_value={"connected": False, "error": "timed out"},
        ):
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                self.manager._preflight_live_scan()

    def test_live_scan_preflight_failure_allocates_no_scan_id(self) -> None:
        before = self.manager.settings.next_sequence_number
        with mock.patch.object(
            self.manager, "_preflight_live_scan",
            side_effect=RuntimeError("No media detected. Load a sheet."),
        ):
            with self.assertRaisesRegex(RuntimeError, "No media detected"):
                self.manager.execute_scan_workflow(source_mode="live")
            r = self.client.post("/api/scan/start", json={"source_mode": "live"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("No media detected", r.json()["detail"])
        self.assertEqual(self.manager.settings.next_sequence_number, before)
        self.assertIsNone(self.manager._active_cancel)

    def test_cancel_without_active_scan(self) -> None:
        r = self.client.post("/api/scan/cancel")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(r.json()["message"], "No scan in progress")

    def test_cancel_endpoint_targets_active_scan(self) -> None:
        ev = threading.Event()
        with self.manager._cancel_lock:
            self.manager._active_scan_id = "scan_cancel_test"
            self.manager._active_cancel = ev
        try:
            r = self.client.post("/api/scan/cancel")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["ok"])
            self.assertEqual(r.json()["scan_id"], "scan_cancel_test")
            self.assertTrue(ev.is_set())
        finally:
            with self.manager._cancel_lock:
                self.manager._active_scan_id = None
                self.manager._active_cancel = None

    def test_pre_cancelled_scan_returns_cancelled_job(self) -> None:
        ev = threading.Event()
        ev.set()
        job = self.manager.execute_scan_workflow(
            source_mode="demo_architectural",
            simulate_thermal_drift=False,
            demo_rows=120,
            cancel_event=ev,
        )
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.exported, {})
        self.assertNotIn(job.scan_id, self.manager.jobs)
        self.assertIsNone(self.manager._active_cancel)
        out_dir = self.base_dir / "scans_output"
        self.assertEqual(list(out_dir.glob(f"{job.scan_id}.*")), [])

    def test_scan_form_settings_persist(self) -> None:
        r = self.client.put("/api/settings", json={
            "scan_source": "demo_architectural",
            "scan_paper_size": "ansi_a",
            "scan_orientation": "landscape",
            "scan_width_inches": 12.5,
            "scan_dpi": 600,
            "scan_color_mode": "grayscale",
            "seam_feather_mode": "artwork",
        })
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertEqual(s["scan_source"], "demo_architectural")
        self.assertEqual(s["scan_paper_size"], "ansi_a")
        self.assertEqual(s["scan_orientation"], "landscape")
        self.assertEqual(s["scan_width_inches"], 12.5)
        self.assertEqual(s["scan_dpi"], 600)
        self.assertEqual(s["scan_color_mode"], "grayscale")
        self.assertEqual(s["seam_feather_mode"], "artwork")
        # Survives a fresh manager on the same settings directory.
        again = ScanWorkspaceManager(config_dir=self.base_dir / "config")
        self.assertEqual(again.settings.scan_paper_size, "ansi_a")
        self.assertEqual(again.settings.scan_orientation, "landscape")
        self.assertEqual(again.settings.scan_dpi, 600)
        self.assertEqual(again.settings.scan_width_inches, 12.5)
        self.assertEqual(again.settings.seam_feather_mode, "artwork")
        panel_kwargs = again._settings_scan_kwargs()
        self.assertEqual(panel_kwargs["source_mode"], "live")
        self.assertEqual(panel_kwargs["paper_size"], "ansi_a")
        self.assertEqual(panel_kwargs["orientation"], "landscape")
        self.assertEqual(panel_kwargs["width_inches"], 12.5)
        self.assertEqual(panel_kwargs["dpi"], 600)
        self.assertEqual(panel_kwargs["color_mode"], "grayscale")

    def test_autosave_respects_export_formats(self) -> None:
        # The format checkboxes are persisted into settings; autosave must honour them.
        r_set = self.client.put("/api/settings", json={"export_formats": ["jpg"]})
        self.assertEqual(r_set.status_code, 200, r_set.text)
        self.assertEqual(r_set.json()["export_formats"], ["jpg"])

        r = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": False,
            "demo_rows": 120,
            "filename_prefix": "fmt",
        })
        self.assertEqual(r.status_code, 200, r.text)
        job = r.json()
        self.assertEqual([f["format"] for f in job["exported"]["files"]], ["jpg"])

        out_dir = self.base_dir / "scans_output"
        base = job["exported"]["base_name"]
        self.assertTrue((out_dir / f"{base}.jpg").exists())
        self.assertFalse((out_dir / f"{base}.tiff").exists())
        self.assertFalse((out_dir / f"{base}.pdf").exists())

        # Never persist an empty format list: autosave would write no document at all.
        r_empty = self.client.put("/api/settings", json={"export_formats": []})
        self.assertEqual(r_empty.status_code, 200, r_empty.text)
        self.assertEqual(r_empty.json()["export_formats"], ["jpg"])

    def test_landscape_scan_uses_long_edge_and_records_orientation(self) -> None:
        r = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": False,
            "paper_size": "ansi_a",
            "orientation": "landscape",
            "demo_rows": 120,
        })
        self.assertEqual(r.status_code, 200, r.text)
        job = r.json()
        self.assertEqual(job["preset_id"], "architectural-neutral")
        self.assertEqual(job["width_inches"], 11.0)
        self.assertEqual(job["stitch_metadata"]["orientation"], "landscape")
        self.assertEqual(job["stitch_metadata"]["document_width"], 3300)

        r2 = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": False,
            "paper_size": "ansi_a",
            "orientation": "portrait",
            "demo_rows": 120,
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        portrait = r2.json()
        self.assertEqual(portrait["width_inches"], 8.5)
        self.assertEqual(portrait["stitch_metadata"]["orientation"], "portrait")
        self.assertEqual(portrait["stitch_metadata"]["document_width"], 2550)

    def test_end_to_end_rest_workflow_and_cli_parser(self) -> None:
        # Verify CLI parser has `app` and `mcp`
        parser = build_parser()
        args_app = parser.parse_args(["app", "--bind", "0.0.0.0", "--http-port", "9090"])
        self.assertEqual(args_app.command, "app")
        self.assertEqual(args_app.http_port, 9090)

        args_mcp = parser.parse_args(["mcp", "--transport", "stdio"])
        self.assertEqual(args_mcp.command, "mcp")

        # 1. Check status
        r_status = self.client.get("/api/status")
        self.assertEqual(r_status.status_code, 200)
        self.assertIn("settings", r_status.json())

        # 2. Initiate scan with simulated thermal drift
        r_scan = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": True,
            "color_mode": "color",
            "demo_rows": 140,
        })
        self.assertEqual(r_scan.status_code, 200)
        scan_data = r_scan.json()
        scan_id = scan_data["scan_id"]
        self.assertEqual(scan_data["status"], "awaiting_drift_decision")
        self.assertEqual(scan_data["drift_report"]["verdict"], "DRIFT")

        # 2b. Save a reusable debug capture from the raw retained in the job
        r_dbg = self.client.post(
            f"/api/scans/{scan_id}/save-debug-capture",
            json={"formats": ["rec", "tiff", "png"]},
        )
        self.assertEqual(r_dbg.status_code, 200)
        dbg_files = r_dbg.json()["files"]
        self.assertTrue(pathlib.Path(dbg_files["rec"]).exists())
        self.assertTrue(pathlib.Path(dbg_files["tiff"]).exists())
        self.assertTrue(pathlib.Path(dbg_files["png"]).exists())

        # 3. Resolve drift by choosing auto_adjust and saving a new preset
        r_resolve = self.client.post(f"/api/scans/{scan_id}/drift-decision", json={
            "choice": "auto_adjust",
            "save_as_new_preset_name": "Warm Camera Corrected",
        })
        self.assertEqual(r_resolve.status_code, 200)
        self.assertEqual(r_resolve.json()["active_render_mode"], "auto_adjust")
        self.assertEqual(r_resolve.json()["status"], "ready")
        self.assertTrue(len(r_resolve.json()["exported"]["files"]) >= 3)

        # 4. Run Official Contex Calibration Sheet Wizard on the session
        r_cal = self.client.post(f"/api/scans/{scan_id}/calibrate-sheet", json={
            "sheet_mode": "it8_full",
            "save_as_preset_name": "Official Sheet Calibrated",
        })
        self.assertEqual(r_cal.status_code, 200)
        self.assertEqual(r_cal.json()["scan"]["active_render_mode"], "preset")

        # 5. Run AI Post-Processing (including Analog Seam Color Blending + Micro-Inpaint)
        r_ai = self.client.post(f"/api/scans/{scan_id}/enhance", json={
            "blend_seam_colors": True,
            "seam_blend_width_px": 144,
            "seam_blend_strength": 0.88,
            "seam_feather_mode": "artwork",
            "heal_seams": True,
            "seam_radius_px": 6,
            "clean_background": True,
            "denoise_shadows": True,
            "sharpen_detail": True,
        })
        self.assertEqual(r_ai.status_code, 200)
        ai_json = r_ai.json()
        self.assertEqual(ai_json["active_render_mode"], "ai_enhanced")
        self.assertTrue(ai_json["stitch_metadata"]["seam_blend_info"]["enabled"])
        self.assertEqual(ai_json["stitch_metadata"]["seam_blend_info"]["blend_width_px"], 144)
        self.assertEqual(ai_json["stitch_metadata"]["seam_blend_info"]["mode"], "artwork")

        # 6. Export in Grayscale PDF & JPG with Debug Mode
        r_exp = self.client.post(f"/api/scans/{scan_id}/export", json={
            "formats": ["jpg", "pdf"],
            "color_mode": "grayscale",
            "debug_mode": True,
        })
        self.assertEqual(r_exp.status_code, 200)
        exp_data = r_exp.json()["exported"]
        self.assertEqual(len(exp_data["files"]), 2)
        self.assertGreaterEqual(len(exp_data["debug_files"]), 3)

    def test_auto_numbering_never_overwrites_scans_or_exports(self) -> None:
        # Scan #1 (no drift -> autosaves immediately as drawing_0001)
        r1 = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": False,
            "filename_prefix": "drawing",
            "demo_rows": 120,
        })
        self.assertEqual(r1.status_code, 200)
        j1 = r1.json()
        self.assertRegex(j1["scan_id"], r"^drawing_\d{10}_0001$")
        self.assertEqual(j1["exported"]["base_name"], j1["scan_id"])
        self.assertEqual(j1["next_sequence_number"], 2)

        # Scan #2 (no drift -> autosaves immediately as drawing_0002)
        r2 = self.client.post("/api/scan/start", json={
            "source_mode": "demo_architectural",
            "simulate_thermal_drift": False,
            "filename_prefix": "drawing",
            "demo_rows": 120,
        })
        self.assertEqual(r2.status_code, 200)
        j2 = r2.json()
        self.assertRegex(j2["scan_id"], r"^drawing_\d{10}_0002$")
        self.assertEqual(j2["exported"]["base_name"], j2["scan_id"])
        self.assertEqual(j2["next_sequence_number"], 3)

        # Manual re-export of Scan #1 -> must save as drawing_0003 instead of overwriting drawing_0001
        r_exp = self.client.post(f"/api/scans/{j1['scan_id']}/export", json={
            "formats": ["jpg"],
            "filename_prefix": "drawing",
        })
        self.assertEqual(r_exp.status_code, 200)
        exp_res = r_exp.json()
        stamp = j1["scan_id"].split("_")[1]
        self.assertEqual(exp_res["exported"]["base_name"], f"drawing_{stamp}_0003")
        self.assertEqual(exp_res["next_sequence_number"], 4)

        out_dir = self.base_dir / "scans_output"
        self.assertTrue((out_dir / f"drawing_{stamp}_0001.jpg").exists())
        self.assertTrue((out_dir / f"drawing_{stamp}_0002.jpg").exists())
        self.assertTrue((out_dir / f"drawing_{stamp}_0003.jpg").exists())


if __name__ == "__main__":
    unittest.main()
