"""Offline capture path: fragment reassembly and the 49.5 % trap."""
from __future__ import annotations

import os
import tempfile
import unittest

from ctx.net import pcap
from ctx.proto import image as I
from ctx.proto.stream import StreamAssembler

from .helpers import build_pcapng, fragmented_capture

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIG_CAPTURE = os.path.join(REPO, "analysis", "incoming", "logrun-1025-1406.pcapng")

#: Every image record on the wire is 2,960 bytes (SPECS 3.3).
RECORD_SIZE = I.OBSERVED_RECORD_SIZE


def make_datagrams(count: int = 3, payload: int = RECORD_SIZE - I.HEADER_LEN):
    out = []
    for seq in range(count):
        body = bytes(((i + seq) % 251) for i in range(payload))
        out.append(I.control(seq, I.RecordType.DATA, dlen=len(body), payload=body))
    return out


class TempCapture:
    def __init__(self, blob: bytes):
        self.blob = blob
        self.path = None

    def __enter__(self) -> str:
        fd, self.path = tempfile.mkstemp(suffix=".pcapng")
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.blob)
        return self.path

    def __exit__(self, *exc) -> None:
        if self.path and os.path.exists(self.path):
            os.unlink(self.path)


class Reassembly(unittest.TestCase):
    def test_records_are_reassembled_whole(self):
        datagrams = make_datagrams(3)
        with TempCapture(fragmented_capture(datagrams)) as path:
            stats: dict = {}
            got = pcap.udp_datagrams(path, stats=stats)
        self.assertEqual(len(got), 3)
        self.assertEqual(got, datagrams)
        self.assertTrue(all(len(d) == RECORD_SIZE for d in got))
        self.assertEqual(stats["incomplete_groups"], 0)

    def test_header_parses_and_stream_matches(self):
        datagrams = make_datagrams(3)
        with TempCapture(fragmented_capture(datagrams)) as path:
            records = pcap.records(path)
            stream, rx = pcap.image_stream(path)
        self.assertEqual([r.seq for r in records], [0, 1, 2])
        self.assertTrue(all(r.dlen == RECORD_SIZE - I.HEADER_LEN for r in records))
        expected = b"".join(I.Record.parse(d).data for d in datagrams)
        self.assertEqual(stream, expected)
        self.assertEqual(rx["missing_records"], 0)

    def test_first_fragment_only_loses_half(self):
        # The failure this whole module exists to prevent (STATUS.md 4).
        datagrams = make_datagrams(2)
        full = sum(len(I.Record.parse(d).data) for d in datagrams)
        with TempCapture(fragmented_capture(datagrams, first_fragment_only=True)) as path:
            stream, rx = pcap.image_stream(path)
        self.assertLess(len(stream), full * 0.6)
        self.assertGreater(len(stream), full * 0.4)
        self.assertEqual(rx["truncated"], 2)

    def test_duplicate_packets_do_not_duplicate_data(self):
        # pktmon captures every packet twice; the reassembler must key it out.
        datagrams = make_datagrams(3)
        with TempCapture(fragmented_capture(datagrams, duplicate_every=1)) as path:
            got = pcap.udp_datagrams(path)
        self.assertEqual(got, datagrams)

    def test_missing_first_fragment_is_reported(self):
        # Without fragment 0 there is no UDP header to parse; the record is lost
        # and must be counted, not silently stitched from the remaining fragment.
        import struct

        from .helpers import ethernet_ipv4_udp

        datagram = make_datagrams(1)[0]
        udp = struct.pack(">HHHH", 58171, 58170, 8 + len(datagram), 0) + datagram
        parts = [udp[i:i + 1480] for i in range(0, len(udp), 1480)]
        frames = [
            ethernet_ipv4_udp("10.10.80.40", "10.10.80.108", 58171, 58170, 1,
                              part * 1480, part < len(parts) - 1, chunk)
            for part, chunk in enumerate(parts) if part != 0
        ]
        with TempCapture(build_pcapng(frames)) as path:
            stats: dict = {}
            got = pcap.udp_datagrams(path, stats=stats)
        self.assertEqual(got, [])
        self.assertEqual(stats["incomplete_groups"], 1)

    def test_empty_capture(self):
        with TempCapture(build_pcapng([])) as path:
            self.assertEqual(pcap.udp_datagrams(path), [])
            self.assertEqual(pcap.records(path), [])

    def test_port_filter(self):
        datagrams = make_datagrams(2)
        with TempCapture(fragmented_capture(datagrams, sport=9999, dport=9998)) as path:
            self.assertEqual(pcap.udp_datagrams(path), [])


class AssemblerIntegration(unittest.TestCase):
    def test_capture_feeds_the_same_assembler_as_the_socket(self):
        datagrams = make_datagrams(40, payload=32)
        with TempCapture(fragmented_capture(datagrams)) as path:
            asm = StreamAssembler(ack_every=32)
            controls = []
            for record in pcap.records(path):
                controls += asm.feed_record(record)
        self.assertEqual(asm.stats.data_records, 40)
        self.assertEqual(len(controls), 1)
        self.assertEqual(I.Record.parse(controls[0]).seq, 31)


class RealCapture(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CTX_BIG_CAPTURE_TESTS"),
                         "set CTX_BIG_CAPTURE_TESTS=1 to run against the 275 MB capture")
    def test_reference_capture(self):  # pragma: no cover - opt-in, slow
        if not os.path.exists(BIG_CAPTURE):
            self.skipTest("reference capture not present")
        stats: dict = {}
        datagrams = pcap.udp_datagrams(BIG_CAPTURE, stats=stats)
        self.assertGreater(len(datagrams), 40000)
        self.assertTrue(all(len(d) == RECORD_SIZE for d in datagrams))
        stream, rx = pcap.image_stream(BIG_CAPTURE)
        from ctx.frame import detect_line_bytes
        line_bytes, score = detect_line_bytes(stream)
        self.assertEqual(line_bytes, 26928)
        self.assertGreater(score, 0.99)


if __name__ == "__main__":
    unittest.main()
