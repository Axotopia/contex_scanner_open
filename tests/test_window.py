"""The 82-byte window block: byte-exact round trip against the captured OEM block."""
from __future__ import annotations

import json
import os
import struct
import unittest

from ctx.proto.window import (
    BLOCK_LEN,
    COMPOSITION_COLOUR,
    COMPOSITION_GREY,
    MAX_WIDTH_UNITS,
    OEM_BLOCK,
    OFF_BITS_PER_COMPONENT,
    OFF_COLOUR_SPACE,
    OFF_COMPOSITION,
    OFF_DPI_X,
    OFF_DPI_Y,
    OFF_LENGTH,
    OFF_PARAM_LEN,
    OFF_WIDTH,
    WindowParams,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEQUENCE = os.path.join(REPO, "engineering", "analysis", "scan-sequence.json")


def captured_block() -> bytes | None:
    try:
        with open(SEQUENCE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)["set_window"]
    except (OSError, KeyError, ValueError):
        return None
    return bytes.fromhex(raw)[16:]


class OemTemplate(unittest.TestCase):
    def test_embedded_template_matches_the_capture(self):
        block = captured_block()
        if block is None:
            self.skipTest("engineering/analysis/scan-sequence.json not present")
        self.assertEqual(OEM_BLOCK, block)

    def test_round_trip_is_byte_exact(self):
        block = captured_block()
        if block is None:
            self.skipTest("engineering/analysis/scan-sequence.json not present")
        params = WindowParams.from_block(block)
        self.assertEqual(params.to_block(), block)

    def test_unknown_bytes_are_preserved(self):
        block = captured_block()
        if block is None:
            self.skipTest("engineering/analysis/scan-sequence.json not present")
        params = WindowParams.from_block(block).with_(width_units=25200)
        patched = params.to_block()
        # Change one known field; every unknown byte must survive untouched.
        known = {OFF_PARAM_LEN, OFF_PARAM_LEN + 1, OFF_DPI_X, OFF_DPI_X + 1,
                 OFF_DPI_Y, OFF_DPI_Y + 1, *range(OFF_WIDTH, OFF_WIDTH + 4),
                 *range(OFF_LENGTH, OFF_LENGTH + 4), OFF_COMPOSITION,
                 OFF_BITS_PER_COMPONENT, OFF_COLOUR_SPACE}
        for off in range(BLOCK_LEN):
            if off in known:
                continue
            with self.subTest(offset=off):
                self.assertEqual(patched[off], block[off])

    def test_param_length_field(self):
        self.assertEqual(struct.unpack_from(">H", OEM_BLOCK, OFF_PARAM_LEN)[0],
                         BLOCK_LEN - 8)


class Fields(unittest.TestCase):
    def test_from_inches_places_fields(self):
        params = WindowParams.from_inches(42, 21, dpi=200)
        block = params.to_block()
        self.assertEqual(struct.unpack_from(">H", block, OFF_DPI_X)[0], 200)
        self.assertEqual(struct.unpack_from(">H", block, OFF_DPI_Y)[0], 200)
        self.assertEqual(struct.unpack_from(">I", block, OFF_WIDTH)[0], 50400)
        self.assertEqual(struct.unpack_from(">I", block, OFF_LENGTH)[0], 25200)
        self.assertEqual(block[OFF_COMPOSITION], COMPOSITION_COLOUR)

    def test_auto_length_is_zero(self):
        block = WindowParams.from_inches(42, None).to_block()
        self.assertEqual(struct.unpack_from(">I", block, OFF_LENGTH)[0], 0)

    def test_grey_mode(self):
        params = WindowParams.from_inches(8.5, 11, dpi=300, colour=False)
        self.assertFalse(params.colour)
        self.assertEqual(params.bytes_per_pixel, 1)
        self.assertEqual(params.to_block()[OFF_COMPOSITION], COMPOSITION_GREY)

    def test_colour_is_three_bytes_per_pixel(self):
        params = WindowParams()
        self.assertEqual(params.bytes_per_pixel, 3)
        self.assertEqual(params.bits_per_component, 8)   # per component, not per pixel

    def test_expected_pixels(self):
        self.assertEqual(WindowParams.from_inches(42, 21, dpi=200).expected_pixels(),
                         (8400, 4200))
        self.assertEqual(WindowParams.from_inches(42, None, dpi=200).expected_pixels(),
                         (8400, None))


class Validation(unittest.TestCase):
    def test_too_wide_is_rejected(self):
        params = WindowParams(width_units=MAX_WIDTH_UNITS + 1)
        with self.assertRaises(ValueError):
            params.validate()

    def test_window_past_the_bar_is_rejected(self):
        params = WindowParams(width_units=MAX_WIDTH_UNITS, upper_left_x=1200)
        with self.assertRaises(ValueError):
            params.validate()

    def test_unsupported_dpi_is_rejected(self):
        with self.assertRaises(ValueError):
            WindowParams(dpi_x=250).validate()

    def test_compression_is_rejected(self):
        with self.assertRaises(ValueError):
            WindowParams(compression=1).validate()

    def test_zero_width_is_rejected(self):
        with self.assertRaises(ValueError):
            WindowParams(width_units=0).validate()

    def test_auto_length_is_a_warning_not_an_error(self):
        params = WindowParams(length_units=0)
        params.validate()
        self.assertTrue(any("auto" in note for note in params.problems()))

    def test_base_block_length_enforced(self):
        with self.assertRaises(ValueError):
            WindowParams(base=bytes(80))


if __name__ == "__main__":
    unittest.main()
