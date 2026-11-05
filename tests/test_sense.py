"""SCSISENSE parsing and the error -> action table."""
from __future__ import annotations

import unittest

from ctx.errors import Action, CommandFailed
from ctx.proto import sense as S

from .helpers import sense_record


class Parsing(unittest.TestCase):
    def test_good(self):
        sense = S.Sense.from_record(sense_record())
        self.assertTrue(sense.ok)
        self.assertEqual(sense.action, Action.OK)
        self.assertEqual(sense.status, 0x00)

    def test_offsets(self):
        sense = S.Sense.from_record(sense_record(0x02, 0x05, 0x26, 0x01))
        self.assertEqual(sense.status, 0x02)
        self.assertEqual(sense.sense_key, 0x05)
        self.assertEqual(sense.asc, 0x26)
        self.assertEqual(sense.ascq, 0x01)

    def test_sense_key_is_low_nibble(self):
        rec = bytearray(sense_record(0x02, 0x05))
        rec[12] = 0xF5                        # high nibble carries other bits
        self.assertEqual(S.Sense.from_record(bytes(rec)).sense_key, 0x05)

    def test_magic_required(self):
        with self.assertRaises(ValueError):
            S.Sense.from_record(b"NOTASCANNER" + bytes(17))

    def test_short_record_rejected(self):
        with self.assertRaises(ValueError):
            S.Sense.from_record(b"SCSISENSE" + bytes(3))


class ActionTable(unittest.TestCase):
    """HANDOFF-COMMS.md 6.2 item 4: the missing error -> action mapping."""

    def check(self, status, asc, expected, sense_key=0):
        sense = S.Sense.from_record(sense_record(status, sense_key, asc))
        self.assertEqual(sense.action, expected, sense.describe())

    def test_reservation_conflict(self):
        self.check(0x18, 0x00, Action.CLEAR_RESERVATION)

    def test_invalid_cdb_field(self):
        self.check(0x02, 0x24, Action.FIX_CDB)

    def test_invalid_parameter(self):
        self.check(0x02, 0x26, Action.FIX_PARAMETERS)

    def test_not_ready(self):
        self.check(0x02, 0x04, Action.RETRY_READY)

    def test_medium_not_present(self):
        self.check(0x02, 0x3A, Action.RETRY_READY)

    def test_read_error(self):
        self.check(0x02, 0x11, Action.RETRY)

    def test_unmapped_check_condition_aborts(self):
        self.check(0x02, 0x99, Action.ABORT)

    def test_unknown_status_aborts(self):
        self.check(0x01, 0x00, Action.ABORT)

    def test_hints_name_the_fix(self):
        self.assertIn("CDB[10:16]",
                      S.Sense.from_record(sense_record(0x18)).describe_hint())
        self.assertIn("CDB[5]",
                      S.Sense.from_record(sense_record(0x02, 0, 0x24)).describe_hint())
        self.assertIn("82 bytes",
                      S.Sense.from_record(sense_record(0x02, 0, 0x26)).describe_hint())

    def test_command_failed_carries_the_action(self):
        sense = S.Sense.from_record(sense_record(0x18))
        exc = CommandFailed.from_sense(0x16, sense)
        self.assertEqual(exc.action, Action.CLEAR_RESERVATION)
        self.assertEqual(exc.status, 0x18)
        self.assertIn("0x18", str(exc))


class Splitting(unittest.TestCase):
    def test_data_and_two_records(self):
        first = sense_record()
        last = sense_record()
        buf = b"payload-one" + first + b"payload-two" + last
        data, senses = S.split_scsisense(buf)
        self.assertEqual(data, b"payload-onepayload-two")
        self.assertEqual(len(senses), 2)

    def test_no_record_means_no_status(self):
        data, senses = S.split_scsisense(b"just data")
        self.assertEqual(data, b"just data")
        self.assertEqual(senses, [])

    def test_empty(self):
        self.assertEqual(S.split_scsisense(b""), (b"", []))


if __name__ == "__main__":
    unittest.main()
