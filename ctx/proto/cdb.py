"""SCSI-2 CDBs as this device expects them: 16 bytes, big-endian, token zeroed.

Everything here is derived from recorded vendor traffic. The observed stream
(``analysis/proxy-session3.log``, ``analysis/scan-sequence.json``) uses 16-byte
CDBs for every opcode, including the ones that are 10 bytes in the standard.

Two conventions are load-bearing and are enforced here rather than documented
and forgotten (``SPECS.md`` §2.3, ``STATUS.md`` §14):

* **``CDB[10:16]`` must be zero on every command.** The OEM stream carries a
  reservation token there; emitting the captured token after a zeroed RESERVE
  makes every following command fail with ``0x18`` and produces blank scans.
* **``READ 0xFF`` puts its qualifier in ``CDB[5]``**, not ``CDB[3:4]``.
  Misplacing it returns ASC ``0x24``.

Flash safety is enforced by :func:`assert_safe`, not by convention: the only
write-buffer ids and modes we have seen used for staging a scan are allowed, and
anything that would commit to flash raises :class:`SafetyViolation` before a
socket is touched.
"""
from __future__ import annotations

import struct
from enum import IntEnum

from ..errors import SafetyViolation

CDB_LEN = 16

#: Byte offset of the OEM reservation token that must always be zeroed.
TOKEN = slice(10, 16)


class Opcode(IntEnum):
    TEST_UNIT_READY = 0x00
    REQUEST_SENSE = 0x03
    INQUIRY = 0x12
    MODE_SELECT = 0x15
    RESERVE_UNIT = 0x16
    RELEASE_UNIT = 0x17
    MODE_SENSE = 0x1A
    START_STOP_UNIT = 0x1B
    RECEIVE_DIAGNOSTIC = 0x1C
    SEND_DIAGNOSTIC = 0x1D
    SET_WINDOW = 0x24
    READ = 0x28
    SEND = 0x2A
    OBJECT_POSITION = 0x31
    GET_BUFFER_STATUS = 0x34
    WRITE_BUFFER = 0x3B
    READ_BUFFER = 0x3C


OPCODE_NAMES = {int(op): op.name.replace("_", " ") for op in Opcode}

#: ``WRITE BUFFER`` ids used by the arming recipe. The scanner stages these in
#: RAM; none of them is the flash-commit path.
SAFE_WRITE_BUFFER_IDS = frozenset({0x48, 0xA2, 0x95})

#: Buffer ids that are never safe to *write*. ``0xA0`` is the documented
#: "Copy RAM->Flash & free RAM" path (``STATUS.md`` §9.5); the others are
#: neighbouring firmware/calibration regions we have only ever *read*.
FLASH_LIKE_BUFFER_IDS = frozenset({0xA0, 0xA1, 0xA3, 0xBF})

#: WRITE BUFFER modes whose SPC meaning includes "save" or "activate" — i.e.
#: commit to non-volatile storage rather than stage in RAM.
COMMITTING_MODES = frozenset({0x05, 0x06, 0x07, 0x0E, 0x0F})


def _cdb(fields: dict[int, int] | None = None, length: int = CDB_LEN) -> bytes:
    out = bytearray(length)
    for off, value in (fields or {}).items():
        out[off] = value & 0xFF
    return bytes(out)


def zero_token(cdb: bytes) -> bytes:
    """Return *cdb* with the OEM reservation token zeroed."""
    if len(cdb) != CDB_LEN:
        raise ValueError(f"CDB must be {CDB_LEN} bytes, got {len(cdb)}")
    return cdb[:TOKEN.start] + bytes(TOKEN.stop - TOKEN.start) + cdb[TOKEN.stop:]


def assert_safe(cdb: bytes) -> None:
    """Refuse commands that could commit to the scanner's flash.

    Conservative by construction: the allow-list names the exact staging writes
    the recovered arming recipe uses, so an unrecognised buffer id is refused
    rather than trusted.
    """
    op = cdb[0]
    if op == Opcode.WRITE_BUFFER:
        mode, bufid = cdb[1], cdb[2]
        if bufid in FLASH_LIKE_BUFFER_IDS:
            raise SafetyViolation(
                f"WRITE BUFFER 0x{bufid:02x} is the flash/calibration path; "
                "refusing (no recovery image exists for a partial write)"
            )
        if mode in COMMITTING_MODES:
            raise SafetyViolation(
                f"WRITE BUFFER mode 0x{mode:02x} commits to non-volatile storage; refusing"
            )
        if bufid not in SAFE_WRITE_BUFFER_IDS:
            raise SafetyViolation(
                f"WRITE BUFFER 0x{bufid:02x} is not a known staging buffer "
                f"(allowed: {sorted(f'0x{b:02x}' for b in SAFE_WRITE_BUFFER_IDS)})"
            )
    elif op == Opcode.SEND:
        # SEND 0x84/0x80/0x81 were all observed in normal scans; anything else is
        # unknown and would be a guess, but none of them is a flash commit.
        pass


# --------------------------------------------------------------------- builders
def test_unit_ready() -> bytes:
    return bytes(CDB_LEN)


def inquiry(alloc: int = 96, evpd: bool = False, page: int = 0) -> bytes:
    """``12 00|01 page hi lo`` — standard INQUIRY or vendor VPD page."""
    return _cdb({0: 0x12, 1: 0x01 if evpd else 0x00, 2: page,
                 3: (alloc >> 8) & 0xFF, 4: alloc & 0xFF})


def reserve() -> bytes:
    return _cdb({0: 0x16})


def release() -> bytes:
    return _cdb({0: 0x17})


def object_position_load() -> bytes:
    """``31 01`` — load media to the scan position."""
    return _cdb({0: 0x31, 1: 0x01})


def read(rdtype: int, item: int, length: int) -> bytes:
    """``28`` READ: type in CDB[2], item/qualifier in CDB[5], length in CDB[7:9]."""
    cdb = bytearray(CDB_LEN)
    cdb[0] = 0x28
    cdb[2] = rdtype & 0xFF
    cdb[5] = item & 0xFF
    struct.pack_into(">H", cdb, 7, length & 0xFFFF)
    return bytes(cdb)


def send(send_type: int, item: int, length: int) -> bytes:
    """``2a`` SEND: type in CDB[2], item in CDB[5], length in CDB[7:9]."""
    cdb = bytearray(CDB_LEN)
    cdb[0] = 0x2A
    cdb[2] = send_type & 0xFF
    cdb[5] = item & 0xFF
    struct.pack_into(">H", cdb, 7, length & 0xFFFF)
    return bytes(cdb)


def write_buffer(bufid: int, length: int, offset: int = 0, mode: int = 0x01) -> bytes:
    """``3b`` WRITE BUFFER: mode[1], id[2], offset[3:6], length[6:9]."""
    cdb = bytearray(CDB_LEN)
    cdb[0] = 0x3B
    cdb[1] = mode & 0xFF
    cdb[2] = bufid & 0xFF
    cdb[3] = (offset >> 16) & 0xFF
    cdb[4] = (offset >> 8) & 0xFF
    cdb[5] = offset & 0xFF
    cdb[6] = (length >> 16) & 0xFF
    cdb[7] = (length >> 8) & 0xFF
    cdb[8] = length & 0xFF
    cdb = bytes(cdb)
    assert_safe(cdb)
    return cdb


def read_buffer(bufid: int, length: int, offset: int = 0, mode: int = 0x01) -> bytes:
    """``3c`` READ BUFFER — the safe direction; used for parameter blocks."""
    cdb = bytearray(CDB_LEN)
    cdb[0] = 0x3C
    cdb[1] = mode & 0xFF
    cdb[2] = bufid & 0xFF
    cdb[3] = (offset >> 16) & 0xFF
    cdb[4] = (offset >> 8) & 0xFF
    cdb[5] = offset & 0xFF
    cdb[6] = (length >> 16) & 0xFF
    cdb[7] = (length >> 8) & 0xFF
    cdb[8] = length & 0xFF
    return bytes(cdb)


def set_window(param_length: int) -> bytes:
    """``24`` SET WINDOW: parameter list length in CDB[7:9] (82 for the block)."""
    cdb = bytearray(CDB_LEN)
    cdb[0] = 0x24
    struct.pack_into(">H", cdb, 7, param_length & 0xFFFF)
    return bytes(cdb)


def scan(start: bool = True) -> bytes:
    """``1b`` SCAN — ANSI SCSI-2 Scanner command set.

    Command identity confirmed against the published ANSI SCSI-2 Scanner command set and live wire captures.

    Measured on ``letter-1024-1929.pcapng``: 20,220 data records follow this
    command, while the UDP handshake alone yields only a ~191-frame pre-roll.
    """
    return _cdb({0: 0x1B, 4: 0x01 if start else 0x00})


start_stop_unit = scan          # legacy name; kept so existing callers work


def get_buffer_status(alloc: int = 0x40) -> bytes:
    """``34`` GET BUFFER STATUS — allocation length big-endian at CDB[7:9].

    The vendor driver has this (``scanGetBufferStatus``); ``ctx`` never sent it.
    It is the obvious candidate for the end-of-scan / data-ready signal.
    """
    return _cdb({0: 0x34, 7: (alloc >> 8) & 0xFF, 8: alloc & 0xFF})


# ------------------------------------------------------------------- inspection
def data_out_length(cdb: bytes) -> int:
    """Declared data-out byte count, from the recovered CDB layout."""
    op = cdb[0]
    if op in (Opcode.SET_WINDOW, Opcode.SEND, Opcode.WRITE_BUFFER):
        return (cdb[7] << 8) | cdb[8]
    if op == 0x1D:  # SEND DIAGNOSTIC, not used by the scan path
        return (cdb[3] << 8) | cdb[4]
    return 0


def data_in_length(cdb: bytes) -> int:
    """Declared data-in byte count."""
    op = cdb[0]
    if op in (Opcode.READ, Opcode.READ_BUFFER):
        return (cdb[7] << 8) | cdb[8]
    if op == Opcode.INQUIRY:
        return (cdb[3] << 8) | cdb[4]
    if op == 0x1C:  # RECEIVE DIAGNOSTIC
        return (cdb[6] << 8) | cdb[7]
    return 0


def describe(cdb: bytes) -> str:
    """One-line human summary, used in transcripts."""
    op = cdb[0]
    name = OPCODE_NAMES.get(op, f"0x{op:02x}")
    extra = ""
    if op == Opcode.SEND:
        extra = f" type=0x{cdb[2]:02x} item={cdb[5]} len={data_out_length(cdb)}"
    elif op == Opcode.READ:
        extra = f" type=0x{cdb[2]:02x} item={cdb[5]} len={data_in_length(cdb)}"
    elif op == Opcode.WRITE_BUFFER:
        extra = f" mode=0x{cdb[1]:02x} id=0x{cdb[2]:02x} len={data_out_length(cdb)}"
    elif op == Opcode.READ_BUFFER:
        off = (cdb[3] << 16) | (cdb[4] << 8) | cdb[5]
        extra = f" mode=0x{cdb[1]:02x} id=0x{cdb[2]:02x} off={off} len={data_in_length(cdb)}"
    elif op == Opcode.SET_WINDOW:
        extra = f" len={data_out_length(cdb)}"
    elif op == Opcode.INQUIRY:
        extra = f" evpd={cdb[1]} page=0x{cdb[2]:02x} alloc={data_in_length(cdb)}"
    elif op == Opcode.START_STOP_UNIT:
        extra = f" start={bool(cdb[4] & 1)}"
    return f"{name}{extra}"
