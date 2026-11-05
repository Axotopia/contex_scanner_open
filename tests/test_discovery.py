"""Discovery probe and reply parsing, against the replies actually recorded."""
from __future__ import annotations

import unittest

from ctx.net import discovery

# Exactly what the proxy answered during the 2026-10-24 sessions

REAL_REPLY = b"1;10.0.0.108;HD Ultra i4250s;58170;eth;ctx-proxy;UNKNOWN;;"


class Probe(unittest.TestCase):
    def test_probe_shape(self):
        probe = discovery.build_probe("HOST")
        self.assertEqual(probe, b"0;HOST;List Shared scanners;")
        self.assertTrue(probe.startswith(b"0;"))
        self.assertIn(b"List Shared scanners", probe)


class Reply(unittest.TestCase):
    def test_real_reply(self):
        info = discovery.parse_reply(REAL_REPLY, ("10.0.0.40", 58169))
        self.assertIsNotNone(info)
        self.assertEqual(info.model, "HD Ultra i4250s")
        self.assertEqual(info.port, 58170)
        self.assertEqual(info.transport, "eth")
        self.assertEqual(info.network, "ctx-proxy")
        self.assertEqual(info.serial, "UNKNOWN")
        self.assertEqual(info.reported_ip, "10.0.0.108")

    def test_source_address_wins_over_payload(self):
        # SPECS/HANDOFF: the scanner address comes from the UDP source, never the
        # payload. A lying payload must not redirect us.
        info = discovery.parse_reply(REAL_REPLY, ("192.0.2.9", 4000))
        self.assertEqual(info.ip, "192.0.2.9")
        self.assertEqual(info.reported_ip, "10.0.0.108")

    def test_kind_1_0_prefix(self):
        info = discovery.parse_reply(REAL_REPLY.replace(b"1;", b"1,0;", 1),
                                     ("10.0.0.5", 58169))
        self.assertIsNotNone(info)
        self.assertEqual(info.kind, "1,0")
        self.assertEqual(info.model, "HD Ultra i4250s")

    def test_lic_transport(self):
        info = discovery.parse_reply(
            b"1;10.0.0.9;HD Ultra i3650;58170;lic;scan-room;123;;",
            ("10.0.0.9", 58169))
        self.assertEqual(info.transport, "lic")

    def test_not_a_scanner(self):
        self.assertIsNone(discovery.parse_reply(b"0;host;List Shared scanners;", None))
        self.assertIsNone(discovery.parse_reply(b"", None))
        self.assertIsNone(discovery.parse_reply(b"garbage", None))

    def test_tab_and_lf_also_separate(self):
        info = discovery.parse_reply(b"1;10.0.0.9\tModel X\t58170\teth\t\t123", None)
        self.assertIsNotNone(info)
        self.assertEqual(info.ip, "10.0.0.9")
        self.assertEqual(info.model, "Model X")

    def test_missing_port_is_tolerated(self):
        info = discovery.parse_reply(b"1;10.0.0.9;M;;eth;;1;", None)
        self.assertEqual(info.port, 0)

    def test_summary(self):
        info = discovery.parse_reply(REAL_REPLY, ("10.0.0.40", 58169))
        self.assertIn("10.0.0.40:58170", str(info))
        self.assertIn("UNKNOWN", str(info))


if __name__ == "__main__":
    unittest.main()
