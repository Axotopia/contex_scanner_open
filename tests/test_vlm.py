"""Tests for the native Vision-Language LLM (VLM) engine and auto-describe."""

import base64
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from ctx.vlm import (
    DEFAULT_DESCRIBE_PROMPT,
    VlmConfig,
    VlmEngine,
    clean_slug,
    prepare_image_for_vlm,
)


class TestVlm(unittest.TestCase):
    def test_clean_slug(self) -> None:
        self.assertEqual(clean_slug("Ground Floor Plan.pdf"), "Ground-Floor-Plan")
        self.assertEqual(
            clean_slug("2026-10-31_Acme-Tower_A-101.jpg"), "Acme-Tower-A-101"
        )
        self.assertEqual(clean_slug("Water Lilies (Oil on Canvas)"), "Water-Lilies-Oil-on-Canvas")
        self.assertEqual(clean_slug("20261104_Map_City_New_York"), "Map-City-New-York")
        self.assertEqual(clean_slug(""), "Scan-Document")

    def test_prepare_image_for_vlm(self) -> None:
        # Create a small RGB test image
        img = Image.new("RGB", (100, 200), color=(128, 64, 32))
        data_uri, size = prepare_image_for_vlm(img, max_dimension=150)
        self.assertEqual(size, (100, 200))
        self.assertTrue(data_uri.startswith("data:image/jpeg;base64,"))

        # Test downscaling for large dimensions
        large = Image.new("RGB", (3000, 1500), color=(255, 255, 255))
        data_uri_large, orig_size = prepare_image_for_vlm(large, max_dimension=1000)
        self.assertEqual(orig_size, (3000, 1500))
        # Verify base64 decoded image has max dimension <= 1000
        b64 = data_uri_large.split(",", 1)[1]
        decoded = Image.open(io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(decoded.size, (1000, 500))

    def test_extract_json_markdown_fences(self) -> None:
        text = """```json
{
  "doc_type": "Architectural-Drawing",
  "title": "Ground Floor Plan",
  "description": "Architectural drawing of ground floor",
  "visible_text": "SCALE 1:100",
  "slug": "Ground-Floor-Plan",
  "tags": ["architecture", "floorplan"]
}
```"""
        parsed = VlmEngine._extract_json(text)
        self.assertEqual(parsed.get("doc_type"), "Architectural-Drawing")
        self.assertEqual(parsed.get("title"), "Ground Floor Plan")
        self.assertEqual(parsed.get("slug"), "Ground-Floor-Plan")
        self.assertEqual(parsed.get("tags"), ["architecture", "floorplan"])

    def test_extract_json_raw_fallback(self) -> None:
        raw_text = "Some intro text: {\"doc_type\": \"Map\", \"title\": \"Subway Map\", \"slug\": \"Subway-Map\"} extra words"
        parsed = VlmEngine._extract_json(raw_text)
        self.assertEqual(parsed.get("doc_type"), "Map")
        self.assertEqual(parsed.get("title"), "Subway Map")

    def test_vlm_status_disabled(self) -> None:
        engine = VlmEngine(VlmConfig(enabled=False))
        status = engine.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["provider"], "disabled")

    @mock.patch("httpx.Client")
    def test_vlm_describe_mocked(self, mock_client_cls: mock.MagicMock) -> None:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        # Mock status probe / models endpoint
        mock_client.get.return_value.status_code = 200
        mock_client.get.return_value.json.return_value = {
            "models": [{"name": "qwen2.5vl:3b", "capabilities": ["vision"]}]
        }
        # Mock chat completion response
        mock_client.post.return_value.status_code = 200
        mock_client.post.return_value.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "doc_type": "Blueprint",
                                "title": "Structural Foundation Plan",
                                "description": "Reinforced concrete footing and foundation schedule",
                                "visible_text": "SHEET S-01 | FOUNDATION",
                                "slug": "Structural-Foundation-Plan",
                                "tags": ["blueprint", "structural", "foundation"],
                            }
                        )
                    }
                }
            ]
        }

        engine = VlmEngine(VlmConfig(enabled=True, model="qwen2.5vl:3b"))
        img = Image.new("RGB", (200, 200), color=(10, 20, 30))
        result = engine.describe(img)

        self.assertTrue(result["ok"])
        self.assertEqual(result["doc_type"], "Blueprint")
        self.assertEqual(result["title"], "Structural Foundation Plan")
        self.assertEqual(result["slug"], "Structural-Foundation-Plan")
        self.assertEqual(
            result["stem"], "Blueprint_Structural-Foundation-Plan"
        )
        self.assertEqual(result["model"], "qwen2.5vl:3b")


if __name__ == "__main__":
    unittest.main()

