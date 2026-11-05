"""Capability-page decoding, checked against the pages the live device returned.

The fixture is real evidence (``analysis/vpd-pages-live-1025.json``), captured
with ``ctx info`` on 2026-10-25 from firmware 3-5-1-015. Where these offsets
disagree with ``SPECS.md``, the device is right and the difference is recorded in
``analysis/COMMS-LAYER.md``.
"""
from __future__ import annotations

import json
import os
import unittest

from ctx.session import (
    DeviceIdentity,
    _cstring,
    _parse_capabilities,
    _parse_vpd_identity,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(REPO, "engineering", "analysis", "vpd-pages-live-1025.json")


def live_pages() -> dict[int, bytes] | None:
    try:
        with open(LIVE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return {int(k, 16): bytes.fromhex(v) for k, v in raw["pages"].items()}


class LiveDevice(unittest.TestCase):
    def setUp(self):
        if live_pages() is None:
            self.skipTest("engineering/analysis/vpd-pages-live-1025.json not present")
        self.pages = live_pages()

    def test_cameras_is_u32_at_16(self):
        caps = _parse_capabilities(self.pages)
        self.assertEqual(caps.cameras, 5)

    def test_max_width(self):
        self.assertEqual(_parse_capabilities(self.pages).max_width_units, 50400)

    def test_window_block_length_is_a_byte_at_66(self):
        self.assertEqual(_parse_capabilities(self.pages).window_block_len, 82)

    def test_camera_positions(self):
        positions = _parse_capabilities(self.pages).camera_positions
        self.assertEqual(positions, (5040, 15120, 25200, 35280, 45360))
        self.assertEqual([p / 1200 for p in positions], [4.2, 12.6, 21.0, 29.4, 37.8])

    def test_resolutions(self):
        self.assertEqual(_parse_capabilities(self.pages).resolutions,
                         (100, 200, 300, 400, 600, 1200))

    def test_describe_mentions_the_facts(self):
        text = _parse_capabilities(self.pages).describe()
        self.assertIn("5 cameras", text)
        self.assertIn("42.00 in", text)
        self.assertIn("82 B", text)
        self.assertIn("100/200/300/400/600/1200", text)

    def test_vpd_identity(self):
        vpd = _parse_vpd_identity(self.pages[0xC7])
        self.assertEqual(vpd.firmware, "3-5-1-015")
        self.assertEqual(vpd.serial, "57947")
        self.assertEqual(vpd.mac, "00-08-C3-00-1F-26")
        self.assertEqual(vpd.ip, "10.10.80.40")
        self.assertEqual(vpd.station, "70-SUN-A09")
        self.assertEqual(vpd.vendor, "Contex")
        self.assertEqual(vpd.product, "HD Ultra i4250s")

    def test_inquiry_identity(self):
        with open(LIVE, encoding="utf-8") as fh:
            data = bytes.fromhex(json.load(fh)["inquiry"])
        identity = DeviceIdentity(
            peripheral_type=data[0],
            vendor=data[8:16].decode("latin-1").strip(),
            product=data[16:32].decode("latin-1").strip("\x00 "),
            revision=data[32:36].decode("latin-1").strip("\x00 "),
        )
        self.assertTrue(identity.is_scanner)
        self.assertEqual(identity.vendor, "Contex")
        self.assertEqual(identity.product, "HD Ultra i4250s")


class DegenerateInput(unittest.TestCase):
    """A wrong offset must produce an empty field, never a plausible lie."""

    def test_no_pages(self):
        caps = _parse_capabilities({})
        self.assertIsNone(caps.cameras)
        self.assertIsNone(caps.max_width_units)
        self.assertEqual(caps.camera_positions, ())
        self.assertEqual(caps.resolutions, ())

    def test_zeroed_c1_is_rejected(self):
        caps = _parse_capabilities({0xC1: bytes(255)})
        self.assertIsNone(caps.cameras)
        self.assertIsNone(caps.max_width_units)
        self.assertIsNone(caps.window_block_len)

    def test_implausible_positions_are_rejected(self):
        page = bytearray(255)
        page[4:8] = (5).to_bytes(4, "big")
        page[8:12] = (5000).to_bytes(4, "big")     # not increasing
        page[12:16] = (4000).to_bytes(4, "big")
        self.assertEqual(_parse_capabilities({0xC5: bytes(page)}).camera_positions, ())

    def test_garbage_c7_yields_empty_identity(self):
        vpd = _parse_vpd_identity(bytes([0xFF]) * 255)
        self.assertEqual(vpd.firmware, "")
        self.assertEqual(vpd.mac, "")
        self.assertEqual(vpd.ip, "")

    def test_cstring_rejects_binary(self):
        self.assertEqual(_cstring(b"\x01\x02\x03\x00", 0), "")
        self.assertEqual(_cstring(b"abc\x00", 0), "abc")
        self.assertEqual(_cstring(b"", 0), "")
        self.assertEqual(_cstring(b"abc", 10), "")


if __name__ == "__main__":
    unittest.main()
