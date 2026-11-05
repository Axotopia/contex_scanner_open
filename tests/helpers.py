"""Shared fixtures: a scripted command channel, an image queue, capture builders."""
from __future__ import annotations

import struct

from ctx.net.command import Response
from ctx.proto import cdb as cdbmod
from ctx.proto.sense import Sense

MAGIC = b"SCSISENSE"


def sense_record(status: int = 0, sense_key: int = 0, asc: int = 0, ascq: int = 0) -> bytes:
    """Build a well-formed 28-byte SCSISENSE record."""
    rec = bytearray(28)
    rec[0:9] = MAGIC
    rec[9] = status
    rec[12] = sense_key & 0x0F
    rec[22] = asc
    rec[23] = ascq
    return bytes(rec)


def good_response(data: bytes = b"") -> Response:
    rec = sense_record()
    return Response(data=data, senses=[Sense.from_record(rec)], raw=data + rec)


def failed_response(status: int = 0x02, sense_key: int = 0, asc: int = 0,
                    ascq: int = 0) -> Response:
    rec = sense_record(status, sense_key, asc, ascq)
    return Response(data=b"", senses=[Sense.from_record(rec)], raw=rec)


class FakeCommandChannel:
    """Records every transaction and answers from a script.

    It applies the same safety checks as the real channel, so a test fails if the
    session ever tries to emit a flash-committing command or a non-zero token.
    """

    def __init__(self):
        self.transactions: list[tuple[bytes, bytes]] = []
        self.by_opcode: dict[int, Response | list[Response]] = {}
        self.default = good_response()
        self.closed = False
        self.history: list = []

    def connect(self):
        return self

    def close(self):
        self.closed = True

    def on_opcode(self, opcode: int, *responses: Response):
        self.by_opcode[opcode] = list(responses) if len(responses) > 1 else responses[0]
        return self

    def _next_for(self, opcode: int) -> Response:
        entry = self.by_opcode.get(opcode)
        if entry is None:
            return self.default
        if isinstance(entry, list):
            return entry.pop(0) if len(entry) > 1 else entry[0]
        return entry

    def transact(self, cdb: bytes = b"", *, data_out=None, quiet=None,
                 reply_timeout=None, require_reply: bool = False) -> Response:
        assert len(cdb) == cdbmod.CDB_LEN, len(cdb)
        cdbmod.assert_safe(cdb)
        cdb = cdbmod.zero_token(cdb)
        self.transactions.append((cdb, bytes(data_out or b"")))
        return self._next_for(cdb[0])

    # convenience for assertions
    def opcodes(self) -> list[int]:
        return [cdb[0] for cdb, _ in self.transactions]

    def names(self) -> list[str]:
        return [cdbmod.describe(cdb).split()[0] for cdb, _ in self.transactions]


class FakeImageEndpoint:
    """A deterministic stand-in for the UDP image channel."""

    def __init__(self, datagrams: list[bytes] | None = None):
        self.queue: list[bytes] = list(datagrams or [])
        self.sent: list[bytes] = []
        self.handshakes = 0
        self.opened = False

    def open(self):
        self.opened = True
        return self

    def close(self):
        self.opened = False

    def handshake(self):
        self.handshakes += 1

    def send(self, datagram: bytes):
        self.sent.append(datagram)

    def recv(self, timeout=None):
        if not self.queue:
            return None
        return self.queue.pop(0), ("10.10.80.40", 58171)


# --------------------------------------------------------------- capture building
def ethernet_ipv4_udp(src: str, dst: str, sport: int, dport: int, ident: int,
                      offset: int, more_fragments: bool, payload: bytes) -> bytes:
    total = 20 + len(payload)
    flags_frag = (0x2000 if more_fragments else 0) | (offset // 8)
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, total, ident, flags_frag,
                     64, 17, 0, bytes(map(int, src.split("."))),
                     bytes(map(int, dst.split("."))))
    eth = b"\x02" * 6 + b"\x04" * 6 + struct.pack(">H", 0x0800)
    return eth + ip + payload


def _block(block_type: int, body: bytes) -> bytes:
    pad = (-len(body)) % 4
    length = 12 + len(body) + pad
    return (struct.pack("<II", block_type, length) + body + b"\x00" * pad
            + struct.pack("<I", length))


def build_pcapng(frames: list[bytes], linktype: int = 1) -> bytes:
    shb_body = (struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    idb_body = struct.pack("<HHI", linktype, 0, 65535)
    out = [_block(0x0A0D0D0A, shb_body), _block(1, idb_body)]
    for index, frame in enumerate(frames):
        body = struct.pack("<IIIII", 0, 0, index * 1000, len(frame), len(frame)) + frame
        out.append(_block(6, body))
    return b"".join(out)


def fragmented_capture(datagrams: list[bytes], *, fragment_size: int = 1480,
                       first_fragment_only: bool = False,
                       duplicate_every: int = 0,
                       src: str = "10.10.80.40", dst: str = "10.10.80.108",
                       sport: int = 58171, dport: int = 58170) -> bytes:
    """Build a pcapng of IP-fragmented UDP datagrams, as pktmon records them."""
    frames: list[bytes] = []
    for index, datagram in enumerate(datagrams):
        udp = struct.pack(">HHHH", sport, dport, 8 + len(datagram), 0) + datagram
        chunks = [udp[i:i + fragment_size] for i in range(0, len(udp), fragment_size)]
        for part, chunk in enumerate(chunks):
            frame = ethernet_ipv4_udp(src, dst, sport, dport, index + 1,
                                      part * fragment_size,
                                      part < len(chunks) - 1, chunk)
            frames.append(frame)
            if first_fragment_only and part > 0:
                frames.pop()
            if duplicate_every and (index % duplicate_every == 0):
                frames.append(frame)
    return build_pcapng(frames)
