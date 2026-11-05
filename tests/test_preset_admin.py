"""Preset rename/delete through the store and the HTTP API."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ctx.server import ScanJob, ScanWorkspaceManager, create_app


class TestPresetAdmin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = ScanWorkspaceManager(self.tmp.name)
        self.client = TestClient(create_app(self.ws))
        base = self.ws.preset_store.get_preset("architectural-neutral")
        custom = copy.deepcopy(base)
        custom.id = "custom-1"
        custom.name = "Old Name"
        custom.is_builtin = False
        self.ws.preset_store.save_preset(custom)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rename_keeps_id_and_file(self) -> None:
        res = self.client.patch("/api/presets/custom-1", json={"name": "New Name"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["preset"]["name"], "New Name")
        self.assertTrue(
            (Path(self.tmp.name) / self.ws.settings.scanner_serial / "presets" / "custom-1.json").exists()
        )
        self.assertEqual(self.ws.preset_store.get_preset("custom-1").name, "New Name")

    def test_rename_rejects_empty(self) -> None:
        res = self.client.patch("/api/presets/custom-1", json={"name": "   "})
        self.assertEqual(res.status_code, 400)

    def test_delete_removes_preset_and_backups(self) -> None:
        # Create a backup by renaming once.
        self.client.patch("/api/presets/custom-1", json={"name": "Backed Up"})
        presets_dir = Path(self.tmp.name) / self.ws.settings.scanner_serial / "presets"
        self.assertTrue(list(presets_dir.glob("custom-1.json.bak-*")))
        res = self.client.delete("/api/presets/custom-1")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse((presets_dir / "custom-1.json").exists())
        self.assertFalse(list(presets_dir.glob("custom-1.json.bak-*")))

    def test_builtin_cannot_be_deleted(self) -> None:
        res = self.client.delete("/api/presets/architectural-neutral")
        self.assertEqual(res.status_code, 400)

    def test_active_preset_falls_back_on_delete(self) -> None:
        self.ws.settings.active_preset_id = "custom-1"
        self.ws._save_settings(self.ws.settings)
        res = self.client.delete("/api/presets/custom-1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["active_preset_id"], "architectural-neutral")
        self.assertEqual(self.ws.settings.active_preset_id, "architectural-neutral")

    def test_preset_in_use_by_running_scan_is_not_deleted(self) -> None:
        self.ws.jobs["scan_9001"] = ScanJob(
            scan_id="scan_9001",
            created_at="now",
            source="live",
            width_inches=42,
            dpi=300,
            color_mode="color",
            preset_id="custom-1",
            status="scanning",
            active_render_mode="preset",
        )
        res = self.client.delete("/api/presets/custom-1")
        self.assertEqual(res.status_code, 409)
        self.assertIn("scan_9001", res.json()["detail"])

    def test_finished_scan_is_reassigned_not_blocking(self) -> None:
        self.ws.jobs["scan_9002"] = ScanJob(
            scan_id="scan_9002",
            created_at="now",
            source="live",
            width_inches=42,
            dpi=300,
            color_mode="color",
            preset_id="custom-1",
            status="ready",
            active_render_mode="preset",
        )
        res = self.client.delete("/api/presets/custom-1")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["reassigned_scans"], ["scan_9002"])
        self.assertEqual(self.ws.jobs["scan_9002"].preset_id, "architectural-neutral")


if __name__ == "__main__":
    unittest.main()
