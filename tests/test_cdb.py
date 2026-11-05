"""CDB encoding must reproduce the OEM recipe byte for byte."""
from __future__ import annotations

import unittest

from ctx import data as recipe
from ctx.errors import SafetyViolation
from ctx.proto import cdb as C


class RecipeFidelity(unittest.TestCase):
    """The captured vendor blocks are ground truth; our encoders must agree."""

    def test_captured_stream_mixes_zeroed_and_tokened_cdbs(self):
        # The OEM stream is NOT internally consistent: some CDBs carry the
        # reservation token, others are already zero. Replaying it verbatim
        # after a zeroed RESERVE therefore fails on exactly the tokened ones —
        # which is why the transport zeroes the field on every emit.
        tokened = {name for name in ("send_0x84", "write_buffer_0x48", "set_window",
                                     "write_buffer_0xa2", "start_stop_unit")
                   if recipe.command(name)[0][10:16] != bytes(6)}
        self.assertEqual(tokened, {"set_window", "write_buffer_0xa2", "start_stop_unit"})

    def test_send_0x84(self):
        cdb, body = recipe.command("send_0x84")
        self.assertEqual(C.zero_token(cdb).hex(),
                         "2a008400000000000400000000000000")
        self.assertEqual(body, bytes(4))
        self.assertEqual(C.describe(C.zero_token(cdb)),
                         "SEND type=0x84 item=0 len=4")

    def test_write_buffer_0x48(self):
        cdb, body = recipe.command("write_buffer_0x48")
        self.assertEqual(C.zero_token(cdb).hex(),
                         "3b014800000000000800000000000000")
        self.assertEqual(body.hex(), "fda8000000000000")

    def test_write_buffer_0xa2(self):
        cdb, body = recipe.command("write_buffer_0xa2")
        self.assertEqual(C.zero_token(cdb).hex(),
                         "3b01a200000000000200000000000000")
        self.assertEqual(body.hex(), "0258")

    def test_tables(self):
        entries = recipe.tables(5)
        self.assertEqual(len(entries), 5)
        for index, (cdb, body) in enumerate(entries):
            with self.subTest(table=index):
                self.assertEqual(C.zero_token(cdb).hex(),
                                 "3b01950000000002b500000000000000")
                self.assertEqual(len(body), 693)
                self.assertEqual(body[0], index)     # each table is tagged

    def test_start_stop_unit(self):
        cdb, _ = recipe.command("start_stop_unit")
        self.assertEqual(C.zero_token(cdb).hex(),
                         "1b000000010000000000000000000000")
        self.assertTrue(C.zero_token(cdb)[4] & 1)

    def test_trigger(self):
        cdb, body = recipe.command("trigger")
        cdb = C.zero_token(cdb)
        self.assertEqual(cdb[2], 0x80)          # SEND type
        self.assertEqual(cdb[5], 0x01)          # item
        self.assertEqual(C.data_out_length(cdb), 1)
        self.assertEqual(body, b"\x01")


class Layout(unittest.TestCase):
    def test_read_qualifier_is_in_cdb_5(self):
        cdb = C.read(0xFF, 0x02, 10)
        self.assertEqual(cdb[0], 0x28)
        self.assertEqual(cdb[2], 0xFF)
        self.assertEqual(cdb[5], 0x02, "QUAL: qualifier must live in CDB[5]")
        self.assertEqual(C.data_in_length(cdb), 10)

    def test_write_buffer_fields(self):
        cdb = C.write_buffer(0x95, 693)
        self.assertEqual(cdb[1], 0x01)          # mode
        self.assertEqual(cdb[2], 0x95)          # buffer id
        self.assertEqual(cdb[3:6], bytes(3))    # offset
        self.assertEqual(C.data_out_length(cdb), 693)

    def test_read_buffer_offset_and_length(self):
        cdb = C.read_buffer(0xBF, 0x91, offset=15)
        self.assertEqual(cdb[0], 0x3C)
        self.assertEqual(cdb[3:6], bytes([0, 0, 15]))
        self.assertEqual(C.data_in_length(cdb), 0x91)

    def test_set_window_length(self):
        self.assertEqual(C.data_out_length(C.set_window(82)), 82)

    def test_inquiry_branches(self):
        std = C.inquiry(96)
        self.assertEqual((std[0], std[1], std[2]), (0x12, 0x00, 0x00))
        self.assertEqual(C.data_in_length(std), 96)
        vpd = C.inquiry(255, evpd=True, page=0xC1)
        self.assertEqual((vpd[1], vpd[2]), (0x01, 0xC1))

    def test_reserve_and_release(self):
        self.assertEqual(C.reserve()[0], 0x16)
        self.assertEqual(C.release()[0], 0x17)
        self.assertEqual(len(C.reserve()), 16)


class TokenAndSafety(unittest.TestCase):
    def test_zero_token_is_idempotent(self):
        cdb = bytes.fromhex("2400000000000000520060dd8e2271cf")
        once = C.zero_token(cdb)
        self.assertEqual(once[10:16], bytes(6))
        self.assertEqual(C.zero_token(once), once)
        self.assertEqual(once[:10], cdb[:10])

    def test_zero_token_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            C.zero_token(bytes(10))

    def test_write_to_flash_buffer_is_refused(self):
        with self.assertRaises(SafetyViolation):
            C.write_buffer(0xA0, 8)

    def test_write_to_bf_is_refused(self):
        with self.assertRaises(SafetyViolation):
            C.write_buffer(0xBF, 0x91)

    def test_committing_mode_is_refused(self):
        with self.assertRaises(SafetyViolation):
            C.write_buffer(0xA2, 2, mode=0x0F)

    def test_unknown_buffer_id_is_refused(self):
        with self.assertRaises(SafetyViolation):
            C.write_buffer(0x40, 8)

    def test_staging_writes_are_allowed(self):
        for bufid in (0x48, 0xA2, 0x95):
            with self.subTest(bufid=bufid):
                C.assert_safe(C.write_buffer(bufid, 8))

    def test_reading_calibration_buffers_is_allowed(self):
        # Reading is safe and is what the vendor driver already does.
        for bufid in (0xA0, 0xBF):
            with self.subTest(bufid=bufid):
                C.assert_safe(C.read_buffer(bufid, 4))


if __name__ == "__main__":
    unittest.main()


class OpcodeNames(unittest.TestCase):
    def test_scan_is_0x1b_not_start_stop_unit(self):
        self.assertEqual(C.scan()[0], 0x1B)
        self.assertEqual(C.scan()[4], 0x01)
        self.assertEqual(C.scan(False)[4], 0x00)
        # legacy alias must stay identical
        self.assertEqual(C.start_stop_unit(), C.scan())

    def test_get_buffer_status_carries_the_allocation_length(self):
        cdb = C.get_buffer_status(0x40)
        self.assertEqual(cdb[0], 0x34)
        self.assertEqual((cdb[7], cdb[8]), (0x00, 0x40))
