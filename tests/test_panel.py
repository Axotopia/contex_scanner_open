"""Tests for front-panel key decoding (``ctx/panel.py``).

The bit layout is empirical, measured on the live scanner: SCAN ``0x20``,
COPY ``0x10``, EMAIL ``0x08`` in status page byte 63.  These tests pin the
decoding rules so a future refactor cannot silently change which key is which.
"""
from __future__ import annotations

import unittest

from ctx.panel import (
    KEY_COPY,
    KEY_EMAIL,
    KEY_MASK,
    KEY_SCAN,
    KEYS,
    LED_ITEM_OFF,
    LED_ITEM_ON,
    LED_PAYLOAD,
    LED_SEND_TYPE,
    MIN_POLL_HZ,
    PanelKey,
    decode_key_presses,
)


class TestDecodeKeyPresses(unittest.TestCase):
    def test_no_press_on_idle(self):
        self.assertEqual(decode_key_presses(0x00, 0x00), [])

    def test_each_key_raises_its_own_name(self):
        self.assertEqual(decode_key_presses(KEY_SCAN, 0), [("scan", KEY_SCAN)])
        self.assertEqual(decode_key_presses(KEY_COPY, 0), [("copy", KEY_COPY)])
        self.assertEqual(decode_key_presses(KEY_EMAIL, 0), [("email", KEY_EMAIL)])

    def test_held_key_does_not_repeat(self):
        """Holding a key is one event, not one per poll."""
        self.assertEqual(decode_key_presses(KEY_SCAN, KEY_SCAN), [])

    def test_release_then_press_fires_again(self):
        self.assertEqual(decode_key_presses(KEY_SCAN, KEY_SCAN), [])
        self.assertEqual(decode_key_presses(0x00, KEY_SCAN), [])
        self.assertEqual(decode_key_presses(KEY_SCAN, 0x00), [("scan", KEY_SCAN)])

    def test_two_keys_at_once(self):
        got = decode_key_presses(KEY_SCAN | KEY_COPY, 0)
        self.assertEqual(got, [("scan", KEY_SCAN), ("copy", KEY_COPY)])

    def test_unrelated_bits_are_ignored(self):
        """Only the three labelled keys are decoded; 0xC8 masks bits 1-6."""
        noise = 0xFF & ~KEY_MASK
        self.assertEqual(decode_key_presses(noise, 0), [])

    def test_key_map_matches_measurement(self):
        self.assertEqual(KEYS, {0x20: "scan", 0x10: "copy", 0x08: "email"})
        self.assertEqual(KEY_MASK, 0x38)

    def test_poll_rate_honours_the_measured_floor(self):
        # A 2 Hz poll was measured to miss presses; the floor must stay above it.
        self.assertGreaterEqual(MIN_POLL_HZ, 4.0)


class TestPanelKeyEvent(unittest.TestCase):
    def test_as_dict_is_json_safe(self):
        ev = PanelKey(key="scan", mask=KEY_SCAN, at=1.5)
        self.assertEqual(ev.as_dict(), {"key": "scan", "mask": 0x20, "at": 1.5})


class TestLedCommandConstants(unittest.TestCase):
    def test_led_items_are_the_documented_pair(self):
        self.assertEqual((LED_SEND_TYPE, LED_ITEM_ON, LED_ITEM_OFF), (0x85, 0x07, 0x08))

    def test_led_payload_is_four_bytes(self):
        self.assertEqual(LED_PAYLOAD, bytes([0x00, 0x00, 0x40, 0x00]))


class TestPanelSettingsDefaults(unittest.TestCase):
    """Only SCAN is wired for now; COPY/EMAIL emit events but do not scan."""

    def test_only_scan_triggers_a_scan_by_default(self):
        from ctx.server import AppSettings

        s = AppSettings()
        self.assertFalse(s.panel_keys_enabled)      # opt-in: it moves hardware
        self.assertEqual(s.panel_scan_on_keys, ["scan"])

    def test_from_dict_backfills_scan_only(self):
        from ctx.server import AppSettings

        self.assertEqual(AppSettings.from_dict({}).panel_scan_on_keys, ["scan"])


if __name__ == "__main__":
    unittest.main()
