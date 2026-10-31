"""The 28-byte ``SCSISENSE`` status record.

Layout (verified against live wire captures (SPECS.md §2.2))::

    0..8    ASCII "SCSISENSE"
    9       SCSI status          0x00 GOOD, 0x02 CHECK CONDITION, 0x18 RESERVATION CONFLICT
    10..27  fixed-format sense[0..17]
               sense[2] & 0x0f   sense key
               sense[12]         ASC
               sense[13]         ASCQ

so within the 28-byte record: status @9, sense key @12, ASC @22, ASCQ @23.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import Action

MAGIC = b"SCSISENSE"
RECORD_LEN = 28

STATUS_GOOD = 0x00
STATUS_CHECK_CONDITION = 0x02
STATUS_RESERVATION_CONFLICT = 0x18

#: ASC values whose meaning has been established on this device.
ASC_INVALID_CDB_FIELD = 0x24
ASC_INVALID_PARAMETER = 0x26
ASC_NOT_READY = 0x04
ASC_MEDIUM_NOT_PRESENT = 0x3A
ASC_READ_ERROR = 0x11
ASC_ABORTED_COMMAND = 0x2C

_ACTIONS = {
    # (status, asc) -> action
    (STATUS_GOOD, None): Action.OK,
    # RESERVATION CONFLICT is never transient: the token at CDB[10:16] is wrong.
    (STATUS_RESERVATION_CONFLICT, None): Action.CLEAR_RESERVATION,
    # Qualifier in the wrong CDB byte (the READ 0xFF / CDB[5] trap).
    (STATUS_CHECK_CONDITION, ASC_INVALID_CDB_FIELD): Action.FIX_CDB,
    # Malformed parameter block (the 82-byte window block, or a table).
    (STATUS_CHECK_CONDITION, ASC_INVALID_PARAMETER): Action.FIX_PARAMETERS,
    (STATUS_CHECK_CONDITION, ASC_NOT_READY): Action.RETRY_READY,
    (STATUS_CHECK_CONDITION, ASC_MEDIUM_NOT_PRESENT): Action.RETRY_READY,
    (STATUS_CHECK_CONDITION, ASC_READ_ERROR): Action.RETRY,
    (STATUS_CHECK_CONDITION, ASC_ABORTED_COMMAND): Action.RETRY,
}

_HINTS = {
    Action.OK: "",
    Action.RETRY: "transient — retry the same command",
    Action.RETRY_READY: "device or paper not ready — poll until ready",
    Action.CLEAR_RESERVATION: (
        "reservation token mismatch — every CDB must carry zeros in CDB[10:16]; "
        "release, then re-reserve with zeros"
    ),
    Action.FIX_CDB: (
        "invalid field in CDB — check qualifier placement: READ 0xFF puts the "
        "qualifier in CDB[5]; the SET WINDOW length lives in CDB[7:9]"
    ),
    Action.FIX_PARAMETERS: (
        "invalid field in the parameter list — the window block must be exactly "
        "82 bytes with its length field at [6:8]"
    ),
    Action.ABORT: "no recovery known — abort and keep the transcript",
}


@dataclass(frozen=True)
class Sense:
    """One parsed status record."""

    status: int
    sense_key: int
    asc: int
    ascq: int

    @classmethod
    def from_record(cls, record: bytes) -> "Sense":
        if len(record) < RECORD_LEN:
            raise ValueError(f"SCSISENSE record is {len(record)} bytes, need {RECORD_LEN}")
        if record[:9] != MAGIC:
            raise ValueError(f"not a SCSISENSE record: {record[:9]!r}")
        return cls(
            status=record[9],
            sense_key=record[12] & 0x0F,
            asc=record[22],
            ascq=record[23],
        )

    @property
    def ok(self) -> bool:
        return self.status == STATUS_GOOD

    @property
    def action(self) -> Action:
        if self.status == STATUS_GOOD:
            return Action.OK
        if self.status == STATUS_RESERVATION_CONFLICT:
            return Action.CLEAR_RESERVATION
        if self.status == STATUS_CHECK_CONDITION:
            return _ACTIONS.get((self.status, self.asc), Action.ABORT)
        return Action.ABORT

    def describe_hint(self) -> str:
        if self.status == STATUS_GOOD:
            return ""
        if self.status == STATUS_RESERVATION_CONFLICT:
            return _HINTS[Action.CLEAR_RESERVATION]
        return _HINTS.get(self.action, "")

    def describe(self) -> str:
        return (
            f"status=0x{self.status:02x} sense_key=0x{self.sense_key:02x} "
            f"ASC=0x{self.asc:02x} ASCQ=0x{self.ascq:02x}"
        )

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.describe()


def split_scsisense(buf: bytes) -> tuple[bytes, list[Sense]]:
    """Split a command-channel reply into (data, status records).

    Replies are an unframed byte stream in which 28-byte ``SCSISENSE`` records
    are interleaved with data-in payloads; the first record terminates the
    data-out phase, a final record terminates a data-in reply. We keep every
    record and concatenate the bytes between them.
    """
    offsets = [i for i in range(len(buf)) if buf.startswith(MAGIC, i)]
    if not offsets:
        return buf, []
    senses = []
    chunks = [buf[:offsets[0]]]          # data-in before the first record is payload
    for k, off in enumerate(offsets):
        end = offsets[k + 1] if k + 1 < len(offsets) else len(buf)
        try:
            senses.append(Sense.from_record(buf[off:off + RECORD_LEN]))
        except ValueError:
            continue
        chunks.append(buf[off + RECORD_LEN:end])
    return b"".join(chunks), senses
