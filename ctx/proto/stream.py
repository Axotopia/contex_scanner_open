"""Account for the image stream: de-duplicate, order, ACK, and report gaps.

This is the part of the receive path that is pure logic. :mod:`ctx.net.image`
owns the socket; this class owns the decisions, so the ACK policy and gap
accounting can be tested deterministically.

Policy, from the configuration that produced real scans (``SPECS.md`` §3.5):

* ACK every 32 new data records, ``ai0`` = 110.
* **Never NAK.** NAKing makes the scanner stall or abort, so gaps are recorded
  for the caller to judge rather than requested again.
* ``dlen`` trims each record; the datagram may be longer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import image as img
from .image import Record, RecordType

#: ACK the highest consecutive sequence number received, or the count of
#: records. Both appear in working scripts; ``seq`` is the default because it is
#: unambiguous under packet loss, but the ambiguity is recorded in
#: ``analysis/COMMS-LAYER.md`` rather than hidden.
ACK_MODE_SEQ = "seq"
ACK_MODE_COUNT = "count"


@dataclass
class StreamStats:
    datagrams: int = 0
    data_records: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    missing_records: int = 0
    bytes: int = 0
    finals: int = 0
    transfer_starts: int = 0
    credits: int = 0
    naks: int = 0
    unknown_types: int = 0
    truncated: int = 0          # records whose dlen exceeded the payload

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class StreamAssembler:
    ack_every: int = img.DEFAULT_ACK_EVERY
    ack_ai0: int = img.DEFAULT_ACK_AI0
    ack_mode: str = ACK_MODE_SEQ
    collect: bool = True

    chunks: dict[int, bytes] = field(default_factory=dict, repr=False)
    stats: StreamStats = field(default_factory=StreamStats)
    _last_seq: int | None = None
    _since_ack: int = 0
    _pending_acks: list[bytes] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------- input
    def feed(self, datagram: bytes) -> list[bytes]:
        """Consume one datagram; return any control datagrams to transmit."""
        try:
            record = Record.parse(datagram)
        except ValueError:
            self.stats.unknown_types += 1
            return []
        return self.feed_record(record)

    def feed_record(self, record: Record) -> list[bytes]:
        self.stats.datagrams += 1
        typ = record.type

        if typ == RecordType.DATA:
            if record.dlen > len(record.payload):
                self.stats.truncated += 1
            if record.seq in self.chunks:
                self.stats.duplicates += 1
                return []
            data = record.data
            if self.collect:
                self.chunks[record.seq] = data
            self.stats.data_records += 1
            self.stats.bytes += len(data)

            if self._last_seq is not None:
                if record.seq == self._last_seq + 1:
                    pass
                elif record.seq > self._last_seq + 1:
                    self.stats.gaps += 1
                    self.stats.missing_records += record.seq - self._last_seq - 1
                else:
                    self.stats.out_of_order += 1
            self._last_seq = record.seq if self._last_seq is None else max(
                self._last_seq, record.seq)

            self._since_ack += 1
            if self.ack_every and self._since_ack >= self.ack_every:
                self._since_ack = 0
                value = (self.stats.data_records if self.ack_mode == ACK_MODE_COUNT
                         else self._last_seq)
                return [img.ack(value or 0, ai0=self.ack_ai0)]
            return []

        if typ == RecordType.FINAL:
            self.stats.finals += 1
        elif typ in (RecordType.START, RecordType.START_PROPS):
            self.stats.transfer_starts += 1
        elif typ == RecordType.CREDIT:
            self.stats.credits += 1
        elif typ == RecordType.NAK:
            self.stats.naks += 1
        else:
            self.stats.unknown_types += 1
        return []

    # ------------------------------------------------------------------ output
    def stream_bytes(self) -> bytes:
        """Concatenate data records in sequence order (``SPECS.md`` §3.4)."""
        return b"".join(self.chunks[k] for k in sorted(self.chunks))

    def missing(self, limit: int = 64) -> list[int]:
        """Sequence numbers never seen, up to *limit* entries."""
        if not self.chunks:
            return []
        lo, hi = min(self.chunks), max(self.chunks)
        out = []
        for seq in range(lo, hi + 1):
            if seq not in self.chunks:
                out.append(seq)
                if len(out) >= limit:
                    break
        return out

    @property
    def complete(self) -> bool:
        return self.stats.missing_records == 0

    def summary(self) -> str:
        s = self.stats
        span = ""
        if self.chunks:
            span = f" seq {min(self.chunks)}..{max(self.chunks)}"
        return (f"{s.data_records} records, {s.bytes} bytes{span}, "
                f"dup={s.duplicates} gaps={s.gaps} (missing {s.missing_records}), "
                f"finals={s.finals}, starts={s.transfer_starts}")
