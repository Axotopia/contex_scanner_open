"""The 16-byte little-endian record header on the UDP image channel.

``SPECS.md`` §3::

    0   u16  sync        0xFFFF
    2   u32  sequence    continuous per scan
    6   u16  type        1 data, 2 final, 3 ack, 4 nak, 5 credit, 7/9 start
    8   u16  dlen        valid bytes in this record
    10  u16  ai0         host sets 110 in acks
    12  u16  ai1
    14  u16  ai2

Live sockets receive whole datagrams: the kernel reassembles the IP fragments,
so a record arrives as ~2,960 bytes, not 1,480. Fragment reassembly matters
only when reading captures — that is :mod:`ctx.net.pcap`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

HEADER_LEN = 16
SYNC = 0xFFFF
_STRUCT = struct.Struct("<HIHHHHH")

#: Host ACK cadence from the working configuration (``SPECS.md`` §3.5).
DEFAULT_ACK_EVERY = 32
DEFAULT_ACK_AI0 = 110

#: Observed record size from the scanner, before dlen trimming. The acceptance
#: test in ``DESIGN.md`` §7 is that reassembly yields 2960, not 1504.
OBSERVED_RECORD_SIZE = 2960


class RecordType(IntEnum):
    DATA = 1
    FINAL = 2
    ACK = 3
    NAK = 4
    CREDIT = 5
    START_PROPS = 7
    START = 9


TYPE_NAMES = {
    1: "data", 2: "final", 3: "ack", 4: "nak", 5: "credit", 7: "start-props", 9: "start",
}


@dataclass(frozen=True)
class Record:
    """One image-channel datagram."""

    seq: int
    type: int
    dlen: int
    payload: bytes = b""
    ai0: int = 0
    ai1: int = 0
    ai2: int = 0

    @classmethod
    def parse(cls, datagram: bytes) -> "Record":
        if len(datagram) < HEADER_LEN:
            raise ValueError(f"datagram is {len(datagram)} bytes, need >= {HEADER_LEN}")
        sync, seq, typ, dlen, ai0, ai1, ai2 = _STRUCT.unpack_from(datagram, 0)
        if sync != SYNC:
            raise ValueError(f"bad sync 0x{sync:04x}, expected 0x{SYNC:04x}")
        return cls(seq=seq, type=typ, dlen=dlen, payload=datagram[HEADER_LEN:],
                   ai0=ai0, ai1=ai1, ai2=ai2)

    @property
    def data(self) -> bytes:
        """The valid payload.

        ``SPECS.md`` §3.4 reads ``dlen`` as the valid byte count, but the device
        also emits records whose entire payload is data with ``dlen == 0``: the
        1,456-byte records, including the blank lead-in lines before the paper
        reaches the head. Treating 0 as empty discards every such record, which
        is exactly the bug that made a live scan look like 120 empty frames.
        So: ``0`` means "the whole payload is valid".
        """
        if self.dlen == 0:
            return self.payload
        return self.payload[:self.dlen] if self.dlen <= len(self.payload) else self.payload

    @property
    def is_data(self) -> bool:
        return self.type == RecordType.DATA

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"0x{self.type:02x}")

    def to_bytes(self) -> bytes:
        return _STRUCT.pack(SYNC, self.seq, self.type, self.dlen,
                            self.ai0, self.ai1, self.ai2) + self.payload


def control(seq: int, rec_type: int, dlen: int = 0, ai0: int = DEFAULT_ACK_AI0,
            payload: bytes = b"", ai1: int = 0, ai2: int = 0) -> bytes:
    """Build a host -> scanner control datagram (ack/nak/credit/start)."""
    return _STRUCT.pack(SYNC, seq & 0xFFFFFFFF, rec_type, dlen, ai0, ai1, ai2) + payload


def ack(seq: int, ai0: int = DEFAULT_ACK_AI0) -> bytes:
    return control(seq, RecordType.ACK, ai0=ai0)


def credit(seq: int, ai0: int = DEFAULT_ACK_AI0) -> bytes:
    return control(seq, RecordType.CREDIT, dlen=0, ai0=ai0)


def nak(seq: int, ai0: int = DEFAULT_ACK_AI0) -> bytes:
    """Build a NAK. Kept because the format is known, not because it is safe:
    ``SPECS.md`` §3.5 records that NAK storms make the scanner stall or abort.
    The stream assembler never emits one."""
    return control(seq, RecordType.NAK, ai0=ai0)


def handshake_payload(ai0_type7: int = 1, ai0_type9: int = DEFAULT_ACK_AI0) -> list[bytes]:
    """The two datagrams the OEM sends to open one transfer.

    From ``scan_once.py`` / ``replay_oem_window.py``, which both produced real
    pixels with this pair. One handshake yields one buffer (~190-1600 lines).
    """
    body = bytes.fromhex("0000c0000000")
    return [control(0, RecordType.START_PROPS, dlen=len(body), ai0=ai0_type7, payload=body),
            control(0, RecordType.START, dlen=len(body), ai0=ai0_type9, payload=body)]
