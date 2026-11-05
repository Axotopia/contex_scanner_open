"""The session state machine, driven by scripted fakes.

No socket is opened: ``ScannerSession`` takes its transports by injection, which
is what makes the ordering and the safety rules testable without hardware.
"""
from __future__ import annotations

import unittest

from ctx.errors import Action, CommandFailed, SafetyViolation
from ctx.net.command import PlanCommandChannel, Response
from ctx.proto import cdb as C
from ctx.proto import image as I
from ctx.proto.window import WindowParams
from ctx.session import (
    MEDIA_READ_ITEM,
    MEDIA_READ_TYPE,
    Command,
    Poll,
    ScannerSession,
    SessionConfig,
    arming_commands,
    scan_plan,
)

from .helpers import FakeCommandChannel, FakeImageEndpoint, failed_response, good_response


def cfg(**kw) -> SessionConfig:
    base = dict(transfer_idle=0.01, transfer_timeout=0.2, empty_transfer_limit=1,
                max_transfers=1, ready_poll_interval=0.001, ready_timeout=0.05,
                table_count=5)
    base.update(kw)
    return SessionConfig(**base)


def media_ready_channel() -> FakeCommandChannel:
    channel = FakeCommandChannel()
    channel.on_opcode(0x28, good_response(b"\x04\x30"))
    return channel


def window() -> WindowParams:
    return WindowParams.from_inches(8.5, 11, dpi=200)


class PlanShape(unittest.TestCase):
    def test_order(self):
        items = scan_plan(window(), table_count=5)
        names = [item.name for item in items]
        self.assertEqual(names, [
            "test-unit-ready", "reserve", "object-position-load", "media-ready",
            "send-0x84", "write-buffer-0x48", "set-window", "write-buffer-0xa2",
            "table-0", "table-1", "table-2", "table-3", "table-4",
            "start-stop-unit", "trigger",
        ])
        self.assertIsInstance(items[3], Poll)
        self.assertTrue(all(isinstance(i, Command) for i in items[:3]))

    def test_every_planned_cdb_zeroes_the_token(self):
        for item in scan_plan(window()):
            if isinstance(item, Command):
                with self.subTest(step=item.name):
                    self.assertEqual(item.cdb[10:16], bytes(6))

    def test_set_window_carries_the_block(self):
        items = scan_plan(window())
        step = next(i for i in items if i.name == "set-window")
        self.assertEqual(step.data_out, window().to_block())
        self.assertEqual(C.data_out_length(step.cdb), len(step.data_out))

    def test_no_flash_writes_in_the_plan(self):
        for item in scan_plan(window()):
            if isinstance(item, Command):
                if item.cdb[0] == 0x3B:
                    self.assertNotIn(item.cdb[2], (0xA0, 0xA1, 0xA3, 0xBF),
                                     item.name)

    def test_table_count_is_honoured(self):
        items = [i for i in arming_commands(window(), table_count=0)]
        self.assertEqual([i.name for i in items][-1], "write-buffer-0xa2")
        items10 = arming_commands(window(), table_count=10)
        self.assertEqual(len([i for i in items10 if i.name.startswith("table-")]), 10)

    def test_table_count_bounds(self):
        with self.assertRaises(ValueError):
            arming_commands(window(), table_count=11)


class Execution(unittest.TestCase):
    def make_session(self, channel, image) -> ScannerSession:
        return ScannerSession(cfg(), channel=channel, image=image)

    def test_run_plan_matches_the_plan(self):
        channel = media_ready_channel()
        session = self.make_session(channel, FakeImageEndpoint())
        items = scan_plan(window())
        session.run_plan(items)
        expected = [i.cdb for i in items if isinstance(i, Command)]
        # the media poll issues its own READ commands in between
        got = [cdb for cdb, _ in channel.transactions
               if not (cdb[0] == 0x28 and cdb[2] == MEDIA_READ_TYPE
                       and cdb[5] == MEDIA_READ_ITEM)]
        self.assertEqual(got, expected)

    def test_media_status_cdb_layout(self):
        # READ: type in CDB[2], item in CDB[5]. The device rejects the reverse
        # with ASC 0x24, which is exactly how this was found.
        self.assertEqual((MEDIA_READ_TYPE, MEDIA_READ_ITEM), (0x80, 0x00))
        cdb = C.read(MEDIA_READ_TYPE, MEDIA_READ_ITEM, 2)
        self.assertEqual((cdb[2], cdb[5]), (0x80, 0x00))

    def test_scan_collects_stream_and_acks(self):
        payloads = [bytes([i]) * 32 for i in range(40)]
        datagrams = [I.control(i, 1, dlen=len(p), payload=p)
                     for i, p in enumerate(payloads)]
        channel = media_ready_channel()
        image = FakeImageEndpoint(datagrams)
        session = self.make_session(channel, image)
        result = session.scan(window())
        self.assertEqual(result.stream, b"".join(payloads))
        self.assertEqual(result.records, 40)
        self.assertTrue(result.complete)
        self.assertEqual(result.acks_sent, 1)
        self.assertEqual(len(image.sent), 1)
        self.assertEqual(I.Record.parse(image.sent[0]).type, 3)
        self.assertEqual(image.handshakes, 1)

    def test_scan_never_emits_a_foreign_token(self):
        channel = media_ready_channel()
        session = self.make_session(channel, FakeImageEndpoint())
        session.scan(window())
        for cdb, _ in channel.transactions:
            self.assertEqual(cdb[10:16], bytes(6))

    def test_scan_never_writes_flash(self):
        channel = media_ready_channel()
        session = self.make_session(channel, FakeImageEndpoint())
        session.scan(window())
        for cdb, _ in channel.transactions:
            if cdb[0] == 0x3B:
                self.assertNotIn(cdb[2], (0xA0, 0xBF))

    def test_reservation_conflict_recovers_then_succeeds(self):
        channel = FakeCommandChannel()
        channel.on_opcode(0x16, failed_response(0x18), good_response())
        session = self.make_session(channel, FakeImageEndpoint())
        session.open()
        session.reserve()
        self.assertEqual(channel.opcodes(), [0x16, 0x17, 0x16])

    def test_arming_failure_aborts_before_start(self):
        channel = FakeCommandChannel()
        channel.on_opcode(0x2A, failed_response(0x02, 0, 0x26))
        session = self.make_session(channel, FakeImageEndpoint())
        with self.assertRaises(CommandFailed) as ctx:
            session.scan(window())
        self.assertIs(ctx.exception.action, Action.FIX_PARAMETERS)
        self.assertNotIn(0x1B, channel.opcodes(), "START must not be sent after a failure")

    def test_reservation_conflict_is_not_retried_blindly(self):
        channel = FakeCommandChannel()
        channel.on_opcode(0x16, failed_response(0x18))     # always conflicts
        session = self.make_session(channel, FakeImageEndpoint())
        session.open()
        with self.assertRaises(CommandFailed) as ctx:
            session.reserve()
        self.assertIs(ctx.exception.action, Action.CLEAR_RESERVATION)
        self.assertEqual(channel.opcodes(), [0x16, 0x17, 0x16])

    def test_media_timeout_is_reported_not_raised(self):
        channel = FakeCommandChannel()
        channel.on_opcode(0x28, good_response(b"\x04\x00"))
        session = self.make_session(channel, FakeImageEndpoint())
        session.open()
        self.assertFalse(session.wait_media_ready(timeout=0.02))

    def test_close_releases_a_reserved_session(self):
        channel = media_ready_channel()
        session = self.make_session(channel, FakeImageEndpoint())
        session.open()
        session.reserve()
        session.close()
        self.assertIn(0x17, channel.opcodes())
        self.assertTrue(channel.closed)

    def test_read_parameter_block_uses_read_buffer(self):
        channel = FakeCommandChannel()
        channel.on_opcode(0x3C, good_response(b"\xaa" * 0x91))
        session = self.make_session(channel, FakeImageEndpoint())
        session.open()
        data = session.read_parameter_block()
        self.assertEqual(len(data), 0x91)
        cdb, _ = channel.transactions[-1]
        self.assertEqual(cdb[0], 0x3C)
        self.assertEqual(cdb[2], 0xBF)
        self.assertEqual(C.data_in_length(cdb), 0x91)

    def test_scan_recovers_from_a_reservation_conflict(self):
        channel = media_ready_channel()
        channel.on_opcode(0x16, failed_response(0x18), good_response())
        session = self.make_session(channel, FakeImageEndpoint())
        session.scan(window())
        opcodes = channel.opcodes()
        first_reserve = opcodes.index(0x16)
        self.assertEqual(opcodes[first_reserve:first_reserve + 3], [0x16, 0x17, 0x16])
        self.assertIn(0x1B, opcodes, "the scan must still start after recovery")

    def test_transfer_stats_are_per_transfer(self):
        datagrams = [I.control(i, 1, dlen=8, payload=b"x" * 8) for i in range(8)]
        channel = media_ready_channel()
        session = self.make_session(channel, FakeImageEndpoint(datagrams))
        result = session.scan(window())
        self.assertEqual(result.transfers[0]["records"], 8)

    def test_transcript_records_commands(self):
        channel = media_ready_channel()
        session = self.make_session(channel, FakeImageEndpoint())
        session.scan(window())
        names = [e.get("name") for e in session.transcript if e["kind"] == "command"]
        self.assertIn("send-0x84", names)
        self.assertIn("start-stop-unit", names)


class OfflinePlan(unittest.TestCase):
    def test_plan_channel_records_what_would_be_sent(self):
        channel = PlanCommandChannel()
        session = ScannerSession(cfg(), channel=channel, image=FakeImageEndpoint())
        # Polls need a device; execute only the static commands.
        for item in scan_plan(window()):
            if isinstance(item, Command):
                session._transact(item)
        self.assertEqual(len(channel.steps), 14)          # 15 plan items, one is a poll
        for cdb, _ in channel.steps:
            self.assertEqual(cdb[10:16], bytes(6))
        self.assertIn("SET WINDOW", channel.render())

    def test_plan_channel_still_refuses_flash(self):
        channel = PlanCommandChannel()
        with self.assertRaises(SafetyViolation):
            channel.transact(C.write_buffer(0xA0, 8), data_out=bytes(8))


if __name__ == "__main__":
    unittest.main()
