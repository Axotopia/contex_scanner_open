"""Tests for the Open.NEXT <-> ocr-md MCP bridge and its settings."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctx.ocr_bridge import (
    MAX_VISION_PROMPT_CHARS,
    OcrBridge,
    OcrBridgeError,
    normalize_vision_prompt,
)
from ctx.server import AppSettings, ScanJob

FAKE = Path(__file__).with_name("fake_ocr_server.py")


def fake_command() -> str:
    return f"{sys.executable} {FAKE}"


class TestOcrBridge(unittest.TestCase):
    def test_status_lists_tools(self) -> None:
        bridge = OcrBridge(command=fake_command(), timeout=30)
        try:
            status = bridge.status()
        finally:
            bridge.close()
        self.assertTrue(status["available"])
        self.assertIn("ocr_ingest", status["tools"])
        self.assertEqual(status["source"], "configured")

    def test_ingest_returns_payload(self) -> None:
        bridge = OcrBridge(command=fake_command(), timeout=30)
        try:
            result = bridge.ingest("/tmp/scan.pdf", apply="copy", naming="heuristic")
        finally:
            bridge.close()
        self.assertTrue(result["ok"])
        self.assertEqual(result["arguments"]["apply"], "copy")
        self.assertEqual(result["name"]["stem"], "2026-10-14_Tax-Invoice_Acme")

    def test_tool_error_raises(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_OCR_FAIL": "1"}):
            bridge = OcrBridge(command=fake_command(), timeout=30)
            try:
                with self.assertRaises(OcrBridgeError):
                    bridge.describe("/tmp/scan.pdf")
            finally:
                bridge.close()

    def test_describe_forwards_a_custom_prompt(self) -> None:
        bridge = OcrBridge(command=fake_command(), timeout=30)
        try:
            result = bridge.describe("/tmp/scan.pdf", prompt="Describe only the room layout.")
        finally:
            bridge.close()
        self.assertEqual(result["tool"], "ocr_describe")
        self.assertEqual(
            result["arguments"]["prompt"], "Describe only the room layout."
        )

    def test_describe_omits_an_empty_prompt(self) -> None:
        bridge = OcrBridge(command=fake_command(), timeout=30)
        try:
            result = bridge.describe("/tmp/scan.pdf", prompt="")
        finally:
            bridge.close()
        self.assertNotIn("prompt", result["arguments"])

    def test_missing_module_is_reported_not_raised(self) -> None:
        bridge = OcrBridge(command=f"{sys.executable} -c 'import sys; sys.exit(1)'", timeout=5)
        status = bridge.status()
        self.assertFalse(status["available"])

    def test_restart_after_close(self) -> None:
        bridge = OcrBridge(command=fake_command(), timeout=30)
        try:
            self.assertTrue(bridge.status()["available"])
            bridge.close()
            # A closed bridge must be able to start a new server process.
            self.assertTrue(bridge.status()["available"])
        finally:
            bridge.close()


class TestOcrSettings(unittest.TestCase):
    def test_defaults(self) -> None:
        s = AppSettings()
        self.assertFalse(s.ocr_auto)
        self.assertEqual(s.ocr_naming, "heuristic")
        self.assertEqual(s.ocr_apply, "copy")
        self.assertEqual(s.ocr_vision, "auto")
        self.assertEqual(s.ocr_vision_prompt, "")
        self.assertFalse(s.ocr_image_only)
        self.assertFalse(s.ocr_architectural)
        self.assertTrue(s.ocr_cleanup)
        self.assertTrue(s.ocr_timestamp_prefix)

    def test_roundtrip_and_validation(self) -> None:
        s = AppSettings.from_dict(
            {
                "ocr_auto": True,
                "ocr_command": "python -m ocr_md.mcp_server",
                "ocr_naming": "bogus",
                "ocr_apply": "destroy",
                "ocr_vision": "sometimes",
                "ocr_vision_prompt": "  Describe only the title block.  ",
                "ocr_max_pages": 0,
                "ocr_image_only": True,
                "ocr_architectural": True,
                "ocr_cleanup": False,
                "ocr_timestamp_prefix": False,
            }
        )
        self.assertTrue(s.ocr_auto)
        self.assertEqual(s.ocr_naming, "heuristic")
        self.assertEqual(s.ocr_apply, "copy")
        self.assertEqual(s.ocr_vision, "auto")
        self.assertEqual(s.ocr_max_pages, 1)
        self.assertEqual(s.ocr_vision_prompt, "Describe only the title block.")
        self.assertTrue(s.ocr_image_only)
        self.assertTrue(s.ocr_architectural)
        self.assertFalse(s.ocr_cleanup)
        self.assertFalse(s.ocr_timestamp_prefix)
        again = AppSettings.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(again.ocr_command, s.ocr_command)
        self.assertEqual(again.ocr_vision_prompt, s.ocr_vision_prompt)

    def test_vision_prompt_is_trimmed_and_capped(self) -> None:
        self.assertEqual(normalize_vision_prompt(None), "")
        self.assertEqual(normalize_vision_prompt("   "), "")
        self.assertEqual(normalize_vision_prompt("  keep me  "), "keep me")
        long_text = "x" * (MAX_VISION_PROMPT_CHARS + 250)
        self.assertEqual(len(normalize_vision_prompt(long_text)), MAX_VISION_PROMPT_CHARS)
        self.assertEqual(
            AppSettings.from_dict({"ocr_vision_prompt": long_text}).ocr_vision_prompt,
            long_text[:MAX_VISION_PROMPT_CHARS],
        )

    def test_scan_job_exposes_ocr_state(self) -> None:
        job = ScanJob(
            scan_id="scan_0001",
            created_at="now",
            source="live",
            width_inches=42,
            dpi=300,
            color_mode="color",
            preset_id="architectural-neutral",
            status="ready",
            active_render_mode="preset",
        )
        job.ocr = {"ok": True, "name": {"stem": "2026-10-30_Plan"}}
        self.assertEqual(job.to_public_dict()["ocr"]["name"]["stem"], "2026-10-30_Plan")


class TestOcrWorkflow(unittest.TestCase):
    def test_ocr_scan_uses_exported_pdf_and_settings(self) -> None:
        from ctx.server import ScanWorkspaceManager

        with tempfile.TemporaryDirectory() as tmp:
            ws = ScanWorkspaceManager(tmp)
            # Isolate the preview cache: an image-only profile prefers the
            # preview PNG, and the default output dir is the repo's.
            ws.update_settings({"output_dir": str(Path(tmp) / "out"),
                                "filename_prefix_enabled": False})
            pdf = Path(tmp) / "scan_0001.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            job = ScanJob(
                scan_id="scan_0001",
                created_at="now",
                source="live",
                width_inches=42,
                dpi=300,
                color_mode="color",
                preset_id="architectural-neutral",
                status="ready",
                active_render_mode="preset",
                exported={"files": [{"format": "pdf", "path": str(pdf)}]},
            )
            ws.jobs["scan_0001"] = job
            ws.settings.ocr_apply = "copy"
            ws.settings.ocr_naming = "heuristic"
            ws.settings.ocr_vision = "auto"
            ws.settings.ocr_image_only = True
            ws.settings.ocr_architectural = True
            ws.settings.ocr_cleanup = True
            fake = mock.Mock()
            fake.ingest.return_value = {"ok": True, "name": {"stem": "2026-10-14_Invoice"}}
            with mock.patch.object(ws, "ocr_bridge", return_value=fake):
                result = ws.ocr_scan("scan_0001")

            self.assertTrue(result["ok"])
            self.assertEqual(job.ocr["name"]["stem"], "2026-10-14_Invoice")
            fake.ingest.assert_called_once()
            args, kwargs = fake.ingest.call_args
            self.assertEqual(args[0], str(pdf))
            self.assertEqual(kwargs["apply"], "rename")
            self.assertTrue(kwargs["cleanup"])
            # The app composes the timestamp; the module must not add its own.
            self.assertFalse(kwargs["timestamp_prefix"])
            # Naming is derived, not read from the persisted flags:
            # full-page OCR is skipped and the module does the routing.
            self.assertEqual(kwargs["naming"], "llm")
            self.assertEqual(kwargs["vision"], "auto")
            self.assertTrue(kwargs["skip_ocr"])
            self.assertTrue(kwargs["architectural"])

    def test_ocr_scan_without_pdf_reports_clearly(self) -> None:
        from ctx.server import ScanWorkspaceManager

        with tempfile.TemporaryDirectory() as tmp:
            ws = ScanWorkspaceManager(tmp)
            job = ScanJob(
                scan_id="scan_0002",
                created_at="now",
                source="live",
                width_inches=42,
                dpi=300,
                color_mode="color",
                preset_id="architectural-neutral",
                status="ready",
                active_render_mode="preset",
                exported={"files": []},
            )
            ws.jobs["scan_0002"] = job
            with self.assertRaises(ValueError):
                ws.ocr_scan("scan_0002")

    def test_cleanup_removes_numbered_export_and_updates_meta(self) -> None:
        from ctx.server import ScanWorkspaceManager

        with tempfile.TemporaryDirectory() as tmp:
            ws = ScanWorkspaceManager(tmp)
            # No render variant and no File Prefix: this exercises the rename and
            # meta rewrite only.  The output dir is the temp dir itself, so the
            # files, the sidecar and output_dir all agree.
            ws.update_settings({"output_dir": str(tmp), "filename_prefix_enabled": False})
            original = Path(tmp) / "arch_0001.pdf"
            original.write_bytes(b"%PDF-1.4\n")
            meta = Path(tmp) / "arch_0001.meta.json"
            meta.write_text(
                json.dumps({"files": [{"format": "pdf", "path": str(original)}]})
            )
            # What the module writes, and what we expect it to become.
            final = Path(tmp) / "2026-10-14_Tax-Invoice_Acme.pdf"
            final.write_bytes(b"%PDF-1.4\n")
            expected = Path(tmp) / "2610311350_0001_Tax-Invoice_Acme.pdf"
            job = ScanJob(
                scan_id="scan_0001",
                created_at="2026-10-31T13:50:00",
                source="live",
                width_inches=42,
                dpi=300,
                color_mode="color",
                preset_id="architectural-neutral",
                status="ready",
                active_render_mode="preset",
                exported={"files": [{"format": "pdf", "path": str(original)}]},
            )
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1
            ws.settings.ocr_cleanup = True
            ws.settings.ocr_naming = "heuristic"
            fake = mock.Mock()
            fake.ingest.return_value = {
                "ok": True,
                "cleanup": True,
                "pdf_path": str(final),
                "name": {
                    "stem": "2026-10-14_Tax-Invoice_Acme",
                    "doc_type": "Tax Invoice",
                    "date": "2026-10-14",
                    "subject": "Kitchen Renovation",
                    "entity": "Acme Construction",
                    "confidence": 0.8,
                    "method": "heuristic",
                    "signals": ["date:mtime", "subject:vision"],
                },
                "description": "An invoice from Acme Construction for a kitchen renovation.",
                "title_block": None,
                "vision": {"used": True, "model": "qwen2.5vl:3b", "mode": "description"},
                "warnings": [],
            }
            with mock.patch.object(ws, "ocr_bridge", return_value=fake):
                ws.ocr_scan("scan_0001")

            self.assertFalse(original.exists())
            self.assertTrue(expected.is_file())
            self.assertEqual(job.exported["files"][0]["path"], str(expected))
            # The sidecar follows the file it describes.
            moved_meta = expected.with_name(f"{expected.stem}.meta.json")
            self.assertTrue(moved_meta.is_file())
            self.assertFalse(meta.exists())
            payload = json.loads(moved_meta.read_text())
            self.assertEqual(payload["schema"], 2)
            self.assertEqual(payload["files"][0]["path"], str(expected))
            self.assertEqual(payload["files"][0]["rel_path"], expected.name)
            self.assertEqual(payload["final_pdf"], str(expected))

            # One sidecar per scan also records what the file *is*.
            ocr = payload["ocr"]
            self.assertEqual(ocr["description"], "An invoice from Acme Construction for a kitchen renovation.")
            self.assertEqual(ocr["doc_type"], "Tax Invoice")
            self.assertEqual(ocr["entity"], "Acme Construction")
            self.assertEqual(ocr["date"], "2026-10-14")   # the date dropped from the name
            self.assertTrue(ocr["named"])
            self.assertEqual(ocr["route"]["route"], "local")
            self.assertEqual(ocr["vision"]["model"], "qwen2.5vl:3b")


class TestOcrSourceRouting(unittest.TestCase):
    """Which file is handed to the module, and when a name is worth keeping."""

    def test_image_only_prefers_the_preview(self) -> None:
        from ctx.server import select_ocr_source

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            preview = base / "preview_preset.png"
            preview.write_bytes(b"\x89PNG\r\n")
            jpg = base / "scan_0001.jpg"
            jpg.write_bytes(b"\xff\xd8")
            exported = {"files": [{"format": "jpg", "path": str(jpg)}]}
            self.assertEqual(select_ocr_source(exported, preview_path=preview), str(preview))
            # Without a preview the exported raster is used.
            self.assertEqual(select_ocr_source(exported, preview_path=None), str(jpg))
            self.assertEqual(
                select_ocr_source(exported, preview_path=preview, prefer_preview=False),
                str(jpg),
            )

    def test_rasters_work_without_a_pdf(self) -> None:
        from ctx.server import select_ocr_source

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tiff = base / "scan_0001.tiff"
            tiff.write_bytes(b"II*\x00")
            jpg = base / "scan_0001.jpg"
            jpg.write_bytes(b"\xff\xd8")
            exported = {
                "files": [
                    {"format": "tiff", "path": str(tiff)},
                    {"format": "jpg", "path": str(jpg)},
                ]
            }
            # JPG decodes faster than LZW TIFF and is what a naming pass needs.
            self.assertEqual(select_ocr_source(exported), str(jpg))
            self.assertEqual(
                select_ocr_source({"files": [{"format": "tiff", "path": str(tiff)}]}),
                str(tiff),
            )
            self.assertIsNone(select_ocr_source({"files": []}))

    def test_missing_files_are_not_selected(self) -> None:
        from ctx.server import select_ocr_source

        self.assertIsNone(
            select_ocr_source({"files": [{"format": "pdf", "path": "/nope/missing.pdf"}]})
        )

    def test_weak_names_are_recognised(self) -> None:
        from ctx.server import ocr_name_is_weak

        self.assertTrue(ocr_name_is_weak({}))
        self.assertTrue(ocr_name_is_weak({"name": {"stem": "", "method": "heuristic"}}))
        self.assertTrue(ocr_name_is_weak({"name": {"stem": "x", "method": "off"}}))
        self.assertTrue(ocr_name_is_weak({"name": {"stem": "scan"}}))
        self.assertTrue(ocr_name_is_weak({"name": {"stem": "scan_0001"}}, "scan_0001.jpg"))
        self.assertFalse(
            ocr_name_is_weak({"name": {"stem": "26-10-31_Acme_A-102", "method": "heuristic"}})
        )
        # A plain string name is accepted too.
        self.assertFalse(ocr_name_is_weak({"name": "Acme Tower A-102"}))


class TestSequenceNumber(unittest.TestCase):
    """A timestamp must never be accepted as a scan or export number."""

    def test_polluted_counter_is_discarded_on_load(self) -> None:
        self.assertEqual(
            AppSettings.from_dict({"next_sequence_number": 2610311442}).next_sequence_number, 1
        )
        self.assertEqual(AppSettings.from_dict({"next_sequence_number": 0}).next_sequence_number, 1)
        self.assertEqual(AppSettings.from_dict({"next_sequence_number": 42}).next_sequence_number, 42)

    def test_export_sequence_ignores_a_timestamp(self) -> None:
        from ctx.server import ScanWorkspaceManager

        with tempfile.TemporaryDirectory() as tmp:
            ws = ScanWorkspaceManager(tmp)
            ws.update_settings({"next_sequence_number": 5, "output_dir": str(Path(tmp) / "out")})
            ws._note_export_sequence(2610311442)
            self.assertEqual(ws.settings.next_sequence_number, 5)
            ws._note_export_sequence(6)
            self.assertEqual(ws.settings.next_sequence_number, 7)

class TestOcrHealth(unittest.TestCase):
    def test_breaker_opens_after_repeated_local_failures(self) -> None:
        from ctx.server import OcrHealth

        health = OcrHealth(window=5, failures_to_open=3)
        health.record(False)
        health.record(True)
        self.assertFalse(health.prefer_cloud())
        health.record(False)
        self.assertFalse(health.prefer_cloud())
        health.record(False)
        self.assertTrue(health.prefer_cloud())
        health.reset()
        self.assertFalse(health.prefer_cloud())

    def test_old_failures_age_out_of_the_window(self) -> None:
        from ctx.server import OcrHealth

        health = OcrHealth(window=3, failures_to_open=3)
        for _ in range(3):
            health.record(False)
        self.assertTrue(health.prefer_cloud())
        health.record(True)
        health.record(True)
        self.assertFalse(health.prefer_cloud())


class TestCloudFallback(unittest.TestCase):
    """Local-first naming with the cloud vision provider as the fallback."""

    def _manager(self, tmp: str):
        from ctx.server import ScanWorkspaceManager

        ws = ScanWorkspaceManager(tmp)
        ws.update_settings({"output_dir": str(Path(tmp) / "out")})
        return ws

    def test_healthy_local_never_calls_the_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._manager(tmp)
            local, cloud = mock.Mock(), mock.Mock()
            local.ingest.return_value = {"ok": True, "name": {"stem": "26-10-31_Acme_A-102"}}
            with mock.patch.object(ws, "ocr_bridge", return_value=local), \
                    mock.patch.object(ws, "ocr_cloud_bridge", return_value=cloud):
                result, route = ws._ingest_with_fallback("scan_0001.jpg", {})

            self.assertEqual(route["route"], "local")
            self.assertEqual(route["named_by"], "local")
            cloud.ingest.assert_not_called()
            self.assertEqual(result["name"]["stem"], "26-10-31_Acme_A-102")

    def test_weak_local_name_falls_through_to_the_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._manager(tmp)
            local, cloud = mock.Mock(), mock.Mock()
            local.ingest.return_value = {"ok": True, "name": {"stem": "scan", "method": "heuristic"}}
            cloud.ingest.return_value = {"ok": True, "name": {"stem": "26-10-31_Monet-Water-Lilies"}}
            with mock.patch.object(ws, "ocr_bridge", return_value=local), \
                    mock.patch.object(ws, "ocr_cloud_bridge", return_value=cloud):
                result, route = ws._ingest_with_fallback("scan_0001.jpg", {})

            self.assertEqual(route["route"], "local->cloud")
            self.assertEqual(route["named_by"], "cloud")
            self.assertEqual(route["reason"], "weak-name")
            self.assertEqual(result["name"]["stem"], "26-10-31_Monet-Water-Lilies")

    def test_local_timeout_restarts_the_bridge_and_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._manager(tmp)
            local, cloud = mock.Mock(), mock.Mock()
            local.ingest.side_effect = OcrBridgeError("OCR server timed out on ocr_ingest")
            cloud.ingest.return_value = {"ok": True, "name": {"stem": "26-10-31_Monet"}}
            with mock.patch.object(ws, "ocr_bridge", return_value=local), \
                    mock.patch.object(ws, "ocr_cloud_bridge", return_value=cloud):
                _result, route = ws._ingest_with_fallback("scan_0001.jpg", {})

            self.assertEqual(route["route"], "local->cloud")
            self.assertIn("timed out", route["reason"])
            # The abandoned call may still be running; the process is replaced.
            local.restart.assert_called_once()

    def test_no_cloud_configured_raises_instead_of_silently_unnamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._manager(tmp)
            local = mock.Mock()
            local.ingest.side_effect = OcrBridgeError("boom")
            with mock.patch.object(ws, "ocr_bridge", return_value=local), \
                    mock.patch.object(ws, "ocr_cloud_bridge", return_value=None):
                with self.assertRaises(OcrBridgeError):
                    ws._ingest_with_fallback("scan_0001.jpg", {})

    def test_open_breaker_goes_straight_to_the_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._manager(tmp)
            local, cloud = mock.Mock(), mock.Mock()
            local.call_tool.return_value = {"ready": False}
            cloud.ingest.return_value = {"ok": True, "name": {"stem": "26-10-31_Acme"}}
            for _ in range(3):
                ws._ocr_health.record(False)
            with mock.patch.object(ws, "ocr_bridge", return_value=local), \
                    mock.patch.object(ws, "ocr_cloud_bridge", return_value=cloud):
                _result, route = ws._ingest_with_fallback("scan_0001.jpg", {})

            self.assertEqual(route["route"], "cloud-first")
            local.ingest.assert_not_called()

    def test_working_local_probe_closes_the_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._manager(tmp)
            local, cloud = mock.Mock(), mock.Mock()
            local.call_tool.return_value = {"ready": True}
            local.ingest.return_value = {"ok": True, "name": {"stem": "26-10-31_Acme_A-102"}}
            for _ in range(3):
                ws._ocr_health.record(False)
            with mock.patch.object(ws, "ocr_bridge", return_value=local), \
                    mock.patch.object(ws, "ocr_cloud_bridge", return_value=cloud):
                _result, route = ws._ingest_with_fallback("scan_0001.jpg", {})

            self.assertEqual(route["route"], "local")
            cloud.ingest.assert_not_called()
            self.assertFalse(ws._ocr_health.prefer_cloud())


class TestRasterNaming(unittest.TestCase):
    """``<Prefix>_<YYMMDDHHMM>_<auto#>_<description>`` applied to a scan."""

    CREATED = "2026-10-31T13:50:00"   # -> 2610311350
    STAMP = "2610311350"

    def _manager(self, tmp, out, **settings):
        from ctx.server import ScanWorkspaceManager

        ws = ScanWorkspaceManager(tmp)
        ws.update_settings({"output_dir": str(out), **settings})
        return ws

    def _job(self, exported, *, scan_id="scan_0001", created_at=None):
        job = ScanJob(
            scan_id=scan_id,
            created_at=created_at or self.CREATED,
            source="live",
            width_inches=42,
            dpi=300,
            color_mode="color",
            preset_id="architectural-neutral",
            status="ready",
            active_render_mode="auto_adjust",
            exported={"files": exported},
        )
        job.ocr = {}
        return job

    def test_rasters_take_the_composed_name_and_the_extra_pdf_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / "scan_0001_Auto.jpg"
            jpg.write_bytes(b"\xff\xd8")
            tiff = out / "scan_0001_Auto.tiff"
            tiff.write_bytes(b"II*\x00")
            descriptive = out / "202610311350_Monet-Water-Lilies.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([
                {"format": "jpg", "path": str(jpg)},
                {"format": "tiff", "path": str(tiff)},
            ])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            stem = f"{self.STAMP}_0001_Monet-Water-Lilies"
            self.assertTrue((out / f"{stem}.jpg").is_file())
            self.assertTrue((out / f"{stem}.tiff").is_file())
            self.assertFalse(jpg.exists())
            self.assertFalse(descriptive.exists())          # PDF was not requested

    def test_single_render_carries_no_marker(self) -> None:
        """``_Auto`` is redundant when only one view of the scan exists."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / "scan_0001_Auto.jpg"
            jpg.write_bytes(b"\xff\xd8")
            descriptive = out / "202610311350_Monet.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([{"format": "jpg", "path": str(jpg)}])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            self.assertTrue((out / f"{self.STAMP}_0001_Monet.jpg").is_file())
            self.assertFalse((out / f"{self.STAMP}_0001_Monet_Auto.jpg").exists())

    def test_render_marker_returns_only_to_break_a_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            plain = out / "scan_0001.jpg"
            plain.write_bytes(b"\xff\xd8")
            auto = out / "scan_0001_Auto.jpg"
            auto.write_bytes(b"\xff\xd8")
            descriptive = out / "202610311350_Monet.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([
                {"format": "jpg", "path": str(plain)},
                {"format": "jpg", "path": str(auto)},
            ])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            self.assertTrue((out / f"{self.STAMP}_0001_Monet.jpg").is_file())
            self.assertTrue((out / f"{self.STAMP}_0001_Monet_Auto.jpg").is_file())

    def test_pdf_selected_keeps_the_searchable_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / "scan_0001_Auto.jpg"
            jpg.write_bytes(b"\xff\xd8")
            descriptive = out / "202610311350_Monet.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([
                {"format": "jpg", "path": str(jpg)},
                {"format": "pdf", "path": str(descriptive)},
            ])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            self.assertTrue((out / f"{self.STAMP}_0001_Monet.pdf").is_file())
            self.assertTrue((out / f"{self.STAMP}_0001_Monet.jpg").is_file())

    def test_image_only_with_pdf_selected_adopts_both(self) -> None:
        """Artwork sampled from the preview still adopts its PDF export."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / "scan_0001_Auto.jpg"
            jpg.write_bytes(b"\xff\xd8")
            numbered_pdf = out / "scan_0001_Auto.pdf"
            numbered_pdf.write_bytes(b"%PDF-1.4\n")
            descriptive = out / "202610311350_Monet.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([
                {"format": "jpg", "path": str(jpg)},
                {"format": "pdf", "path": str(numbered_pdf)},
            ])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            self.assertFalse(numbered_pdf.exists())
            self.assertTrue((out / f"{self.STAMP}_0001_Monet.pdf").is_file())
            paths = {i["format"]: i["path"] for i in job.exported["files"]}
            self.assertTrue(paths["pdf"].endswith(f"{self.STAMP}_0001_Monet.pdf"))
            self.assertTrue(paths["jpg"].endswith(f"{self.STAMP}_0001_Monet.jpg"))

    def test_file_prefix_is_applied_to_the_descriptive_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / "scan_0001_Auto.jpg"
            jpg.write_bytes(b"\xff\xd8")
            descriptive = out / "202610311350_Monet.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix="CU4KS")
            job = self._job([{"format": "jpg", "path": str(jpg)}])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            self.assertTrue((out / f"CU4KS_{self.STAMP}_0001_Monet.jpg").is_file())

    def test_ocr_suffix_never_reaches_the_filename(self) -> None:
        """A module left in copy mode emits ``x.ocr.pdf``; that must not surface."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / "scan_0001.jpg"
            jpg.write_bytes(b"\xff\xd8")
            descriptive = out / "2610311350_Monet.ocr.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([{"format": "jpg", "path": str(jpg)}])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            self.assertTrue((out / f"{self.STAMP}_0001_Monet.jpg").is_file())

    def test_second_save_still_gets_the_descriptive_name(self) -> None:
        """A re-save must rename too, not abandon the name because it exists."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            first = out / "scan_0001.jpg"
            first.write_bytes(b"\xff\xd8")
            descriptive = out / "2026-10-31_Monet.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix_enabled=False)
            job = self._job([{"format": "jpg", "path": str(first)}])
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 1
            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})
            self.assertTrue((out / f"{self.STAMP}_0001_Monet.jpg").is_file())

            # A re-save writes a fresh numbered file and names it again.
            again = out / "scan_0001_0002.jpg"
            again.write_bytes(b"\xff\xd8")
            job.exported = {"files": [{"format": "jpg", "path": str(again)}]}
            second = out / "2026-10-31_Monet.pdf"   # same description -> real collision
            second.write_bytes(b"%PDF-1.4\n")
            result = {"pdf_path": str(second)}
            ws._adopt_raster_name(job, result)

            self.assertGreaterEqual(result["renamed_exports"], 1)
            self.assertFalse(again.exists())
            self.assertTrue((out / f"{self.STAMP}_0001_Monet-2.jpg").is_file())

    def test_filename_uses_the_stamp_the_scan_was_allocated_with(self) -> None:
        """A long scan must not name the file with a later minute than its id."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            ws = self._manager(tmp, out, filename_prefix="CU4KS")
            base, seq = ws.allocate_next_scan("CU4KS")
            allocated_stamp = ws._scan_stamps[base]

            # The job object is built later, as happens after acquisition.
            job = self._job([], scan_id=base, created_at="2027-01-01T09:00:00")

            name = ws._final_name(job, "Drawing_X")
            self.assertTrue(name.startswith(f"CU4KS_{allocated_stamp}_{seq:04d}_"))
            self.assertNotIn("2701010900", name)

    def test_timestamp_is_not_mistaken_for_a_scan_number(self) -> None:
        """An unnumbered scan must not re-emit its stamp as the number."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            jpg = out / f"CU4KS_{self.STAMP}.jpg"
            jpg.write_bytes(b"\xff\xd8")
            descriptive = out / "2026-10-31_Drawing_Architectural-Drawing-of-a-House.pdf"
            descriptive.write_bytes(b"%PDF-1.4\n")
            ws = self._manager(tmp, out, filename_prefix="CU4KS")
            job = self._job([{"format": "jpg", "path": str(jpg)}],
                            scan_id=f"CU4KS_{self.STAMP}")
            ws.jobs[job.scan_id] = job
            ws._scan_numbers[job.scan_id] = 0     # allocated without a number

            ws._adopt_raster_name(job, {"pdf_path": str(descriptive)})

            expected = f"CU4KS_{self.STAMP}_Drawing_Architectural-Drawing-of-a-House.jpg"
            self.assertTrue((out / expected).is_file())
            self.assertFalse(
                (out / f"CU4KS_{self.STAMP}_{self.STAMP}_Drawing_Architectural-Drawing-of-a-House.jpg").exists()
            )

    def test_module_timestamp_and_calendar_date_are_stripped(self) -> None:
        """The module's own stamp and ``YYYY-MM-DD`` do not belong in the name."""
        from ctx.server import ScanWorkspaceManager

        self.assertEqual(
            ScanWorkspaceManager._clean_description(
                "202610311444_2026-10-31_Map_City-of-New-York_Sectional-Map"
            ),
            "Map_City-of-New-York_Sectional-Map",
        )
        self.assertEqual(
            ScanWorkspaceManager._clean_description("202610311350_Monet.ocr"),
            "Monet",
        )
        # The module writes its missing fields out literally.
        self.assertEqual(
            ScanWorkspaceManager._clean_description(
                "202610311501_2026-10-31_Drawing_empty_Architectural-Sketch-of-a-Building"
            ),
            "Drawing_Architectural-Sketch-of-a-Building",
        )


if __name__ == "__main__":
    unittest.main()
