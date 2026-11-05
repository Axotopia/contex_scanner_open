"""The 16-byte record header and the ACK/gap accounting logic."""
from __future__ import annotations

import struct
import unittest

from ctx.proto import image as I
from ctx.proto.stream import ACK_MODE_COUNT, StreamAssembler

from .helpers import sense_record


def data_record(seq: int, payload: bytes | None = None, dlen: int | None = None) -> bytes:
    payload = payload if payload is not None else bytes([seq & 0xFF]) * 64
    return I.control(seq, I.RecordType.DATA,
                     dlen=len(payload) if dlen is None else dlen, payload=payload)


class Header(unittest.TestCase):
    def test_round_trip(self):
        datagram = I.control(7, I.RecordType.DATA, dlen=5, payload=b"abcde")
        record = I.Record.parse(datagram)
        self.assertEqual(record.seq, 7)
        self.assertEqual(record.type, I.RecordType.DATA)
        self.assertEqual(record.dlen, 5)
        self.assertEqual(record.data, b"abcde")
        self.assertEqual(record.to_bytes(), datagram)

    def test_size_and_endianness(self):
        self.assertEqual(I.HEADER_LEN, 16)
        datagram = I.control(0x11223344, 1, dlen=0)
        self.assertEqual(struct.unpack_from("<H", datagram, 0)[0], 0xFFFF)
        self.assertEqual(struct.unpack_from("<I", datagram, 2)[0], 0x11223344)
        self.assertEqual(struct.unpack_from("<H", datagram, 6)[0], 1)

    def test_bad_sync_rejected(self):
        datagram = bytearray(I.control(0, 1))
        datagram[0:2] = b"\x00\x00"
        with self.assertRaises(ValueError):
            I.Record.parse(bytes(datagram))

    def test_short_datagram_rejected(self):
        with self.assertRaises(ValueError):
            I.Record.parse(bytes(8))

    def test_dlen_trims_longer_payload(self):
        datagram = I.control(1, 1, dlen=4, payload=b"abcdefgh")
        record = I.Record.parse(datagram)
        self.assertEqual(len(record.payload), 8)
        self.assertEqual(record.data, b"abcd")

    def test_ack_carries_ai0_110(self):
        datagram = I.ack(31)
        self.assertEqual(struct.unpack_from("<H", datagram, 6)[0], 3)
        self.assertEqual(struct.unpack_from("<H", datagram, 10)[0], 110)
        self.assertEqual(len(datagram), 16)

    def test_handshake_pair(self):
        pair = I.handshake_payload()
        self.assertEqual(len(pair), 2)
        first, second = (I.Record.parse(d) for d in pair)
        self.assertEqual(first.type, 7)
        self.assertEqual(first.ai0, 1)
        self.assertEqual(second.type, 9)
        self.assertEqual(second.ai0, 110)
        self.assertEqual(first.payload, bytes.fromhex("0000c0000000"))
        self.assertEqual(first.dlen, 6)


class Assembler(unittest.TestCase):
    def test_collects_in_order(self):
        asm = StreamAssembler()
        for seq in range(5):
            asm.feed(data_record(seq, bytes([seq])))
        self.assertEqual(asm.stream_bytes(), bytes(range(5)))
        self.assertTrue(asm.complete)

    def test_ack_every_32_by_sequence(self):
        asm = StreamAssembler(ack_every=32)
        controls = []
        for seq in range(40):
            controls += asm.feed(data_record(seq))
        self.assertEqual(len(controls), 1)
        self.assertEqual(I.Record.parse(controls[0]).seq, 31)
        self.assertEqual(I.Record.parse(controls[0]).type, 3)

    def test_ack_mode_count(self):
        asm = StreamAssembler(ack_every=32, ack_mode=ACK_MODE_COUNT)
        controls = []
        for seq in range(32):
            controls += asm.feed(data_record(seq))
        self.assertEqual(I.Record.parse(controls[0]).seq, 32)

    def test_never_naks(self):
        asm = StreamAssembler(ack_every=4)
        controls = []
        for seq in (0, 1, 5, 9):
            controls += asm.feed(data_record(seq))
        self.assertTrue(controls)
        self.assertTrue(all(I.Record.parse(c).type == 3 for c in controls))

    def test_duplicates_dropped(self):
        asm = StreamAssembler()
        datagram = data_record(0, b"x")
        asm.feed(datagram)
        asm.feed(datagram)
        self.assertEqual(asm.stats.data_records, 1)
        self.assertEqual(asm.stats.duplicates, 1)
        self.assertEqual(asm.stream_bytes(), b"x")

    def test_gap_accounting(self):
        asm = StreamAssembler()
        for seq in (0, 1, 4, 5):
            asm.feed(data_record(seq, bytes([seq])))
        self.assertEqual(asm.stats.gaps, 1)
        self.assertEqual(asm.stats.missing_records, 2)
        self.assertEqual(asm.missing(), [2, 3])
        self.assertFalse(asm.complete)

    def test_out_of_order_is_sorted_on_output(self):
        asm = StreamAssembler()
        for seq in (2, 0, 1):
            asm.feed(data_record(seq, bytes([seq])))
        self.assertEqual(asm.stream_bytes(), b"\x00\x01\x02")
        self.assertEqual(asm.stats.out_of_order, 2)

    def test_final_and_start_counted(self):
        asm = StreamAssembler()
        asm.feed(I.control(0, I.RecordType.START_PROPS, dlen=0))
        asm.feed(I.control(0, I.RecordType.FINAL, dlen=0))
        self.assertEqual(asm.stats.transfer_starts, 1)
        self.assertEqual(asm.stats.finals, 1)

    def test_truncated_record_counted(self):
        asm = StreamAssembler()
        asm.feed(I.control(0, 1, dlen=1000, payload=b"\x01" * 10))
        self.assertEqual(asm.stats.truncated, 1)
        self.assertEqual(asm.stats.bytes, 10)

    def test_garbage_datagram_ignored(self):
        asm = StreamAssembler()
        self.assertEqual(asm.feed(b"\x00" * 32), [])
        self.assertEqual(asm.stats.unknown_types, 1)

    def test_no_collect_still_counts(self):
        asm = StreamAssembler(collect=False)
        for seq in range(3):
            asm.feed(data_record(seq))
        self.assertEqual(asm.stats.data_records, 3)
        self.assertEqual(asm.stream_bytes(), b"")

    def test_summary_mentions_gaps(self):
        asm = StreamAssembler()
        asm.feed(data_record(0))
        asm.feed(data_record(3))
        self.assertIn("gaps=1", asm.summary())


if __name__ == "__main__":
    unittest.main()
