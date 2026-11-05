"""Command-channel invariants that must never reach the device."""
from __future__ import annotations

import unittest

from ctx.errors import ProtocolError, SafetyViolation
from ctx.net.command import PlanCommandChannel
from ctx.proto import cdb as C
from ctx.proto.sense import split_scsisense

from .helpers import sense_record


class DataOutLength(unittest.TestCase):
    def setUp(self):
        self.channel = PlanCommandChannel()

    def test_matching_length_is_accepted(self):
        self.channel.transact(C.set_window(4), data_out=b"\x00" * 4)
        self.channel.transact(C.reserve())
        self.assertEqual(len(self.channel.steps), 2)

    def test_declared_longer_than_sent_is_refused(self):
        with self.assertRaises(ProtocolError):
            self.channel.transact(C.set_window(82), data_out=bytes(40))

    def test_declared_shorter_than_sent_is_refused(self):
        with self.assertRaises(ProtocolError):
            self.channel.transact(C.reserve(), data_out=bytes(4))

    def test_a_wrong_window_block_is_caught_before_sending(self):
        # The realistic failure: a block built at the wrong size.
        with self.assertRaises(ProtocolError):
            self.channel.transact(C.set_window(82), data_out=bytes(80))

    def test_flash_safety_still_wins(self):
        with self.assertRaises(SafetyViolation):
            self.channel.transact(C.read_buffer(0xBF, 4).replace(b"\x3c", b"\x3b"),
                                  data_out=bytes(4))


class ReplySplitting(unittest.TestCase):
    def test_record_plus_data(self):
        record = sense_record()
        data, senses = split_scsisense(b"\x01\x02" + record)
        self.assertEqual(data, b"\x01\x02")
        self.assertEqual(len(senses), 1)


if __name__ == "__main__":
    unittest.main()
