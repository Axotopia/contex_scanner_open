"""The TCP command channel: 16-byte CDBs in, 28-byte ``SCSISENSE`` records out.

There is **no framing at all** (``SPECS.md`` §2.1): the socket is a raw byte
stream, the CDB carries the data-out length, and the scanner terminates a reply
with a status record. Consequently a reader cannot know where a reply ends from
length alone; it reads until the line goes quiet, which is what every working
script does.

Two invariants are enforced in :meth:`CommandChannel.transact`, so no caller can
forget them:

* the reservation token at ``CDB[10:16]`` is zeroed on every command;
* :func:`ctx.proto.cdb.assert_safe` refuses flash-committing commands.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from ..errors import CommandFailed, ProtocolError, TransportError
from ..proto import cdb as cdbmod
from ..proto import sense as sensemod
from ..proto.sense import Sense

MAGIC = sensemod.MAGIC


def _check_data_out(cdb: bytes, data_out: bytes | None) -> None:
    """A CDB declares its data-out length; sending a different amount is a bug.

    The vendor client sometimes declares a length and sends nothing (the
    "apply/commit" SEND); *we* never do, so a mismatch here is a programming
    error worth refusing before it reaches the device.
    """
    declared = cdbmod.data_out_length(cdb)
    provided = len(data_out or b"")
    if declared != provided:
        raise ProtocolError(
            f"{cdbmod.describe(cdb)} declares {declared} data-out bytes but "
            f"{provided} were provided")
DEFAULT_PORT = 58170
#: How long the line must stay quiet before a reply is considered complete.
DEFAULT_QUIET = 1.0
#: Hard cap on waiting for the first byte of a reply.
DEFAULT_REPLY_TIMEOUT = 6.0


@dataclass
class Response:
    data: bytes = b""
    senses: list[Sense] = field(default_factory=list)
    raw: bytes = b""

    @property
    def sense(self) -> Sense | None:
        """Final status record, which is the one that matters."""
        return self.senses[-1] if self.senses else None

    @property
    def answered(self) -> bool:
        return bool(self.senses)

    @property
    def ok(self) -> bool:
        s = self.sense
        return bool(s and s.ok)

    def raise_for_status(self, opcode: int) -> None:
        s = self.sense
        if s is None:
            raise TransportError(f"opcode 0x{opcode:02x}: no status record returned")
        if not s.ok:
            raise CommandFailed.from_sense(opcode, s)


@dataclass
class Transaction:
    """One request/response, for transcripts."""

    cdb: bytes
    data_out_len: int
    response: Response
    elapsed: float

    @property
    def opcode(self) -> int:
        return self.cdb[0]

    def as_dict(self) -> dict:
        s = self.response.sense
        return {
            "cdb": self.cdb.hex(),
            "op": cdbmod.describe(self.cdb),
            "out": self.data_out_len,
            "in": len(self.response.data),
            "status": None if s is None else s.status,
            "sense_key": None if s is None else s.sense_key,
            "asc": None if s is None else s.asc,
            "ascq": None if s is None else s.ascq,
            "ms": round(self.elapsed * 1000, 1),
        }


class CommandChannel:
    """A single TCP session to the scanner.

    The device grants one command session; after close it needs tens of seconds
    before a new connection reaches the SCSI layer, so connect once and keep the
    socket open for the whole scan.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, *,
                 connect_timeout: float = 6.0,
                 quiet: float = DEFAULT_QUIET,
                 reply_timeout: float = DEFAULT_REPLY_TIMEOUT,
                 nodelay: bool = True,
                 token: bytes | None = None,
                 on_transaction=None):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.quiet = quiet
        self.reply_timeout = reply_timeout
        self.nodelay = nodelay
        self.token = token
        if token is not None and len(token) != 6:
            raise ValueError("session token must be 6 bytes")
        self.on_transaction = on_transaction
        self.sock: socket.socket | None = None
        self.history: list[Transaction] = []

    # ------------------------------------------------------------------ session
    def connect(self) -> "CommandChannel":
        if self.sock is not None:
            return self
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        try:
            sock.connect((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise TransportError(f"connect {self.host}:{self.port} failed: {exc}") from exc
        if self.nodelay:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
        self.sock = sock
        return self

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.close()      # graceful FIN; do not RST
        except OSError:
            pass
        self.sock = None

    def __enter__(self) -> "CommandChannel":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- transport
    def transact(self, cdb: bytes = b"", *, data_out: bytes | None = None,
                 quiet: float | None = None,
                 reply_timeout: float | None = None,
                 require_reply: bool = False) -> Response:
        """Send one CDB and read its reply.

        ``data_out`` is sent immediately after the CDB, matching the working
        recipe. Held by design: the token is zeroed and flash safety is checked
        here, not in the caller.
        """
        if self.sock is None:
            raise TransportError("command channel is not connected")
        if len(cdb) != cdbmod.CDB_LEN:
            raise ValueError(f"CDB must be {cdbmod.CDB_LEN} bytes, got {len(cdb)}")
        cdbmod.assert_safe(cdb)
        # The vendor carries one session token consistently; the zeroed form is a
        # degenerate case that works for some commands but makes SEND 0x88 (host
        # registration) fail with ASC 0x2C.
        if getattr(self, "token", None) is not None:
            cdb = cdb[:10] + self.token + cdb[16:]
        else:
            cdb = cdbmod.zero_token(cdb)
        _check_data_out(cdb, data_out)

        started = time.monotonic()
        try:
            self.sock.sendall(cdb)
            if data_out:
                self.sock.sendall(data_out)
        except OSError as exc:
            raise TransportError(f"send failed: {exc}") from exc

        raw = self._read_reply(quiet if quiet is not None else self.quiet,
                              reply_timeout if reply_timeout is not None
                              else self.reply_timeout,
                              require_reply=require_reply)
        data, senses = sensemod.split_scsisense(raw)
        response = Response(data=data, senses=senses, raw=raw)
        txn = Transaction(cdb=cdb, data_out_len=len(data_out or b""),
                          response=response, elapsed=time.monotonic() - started)
        self.history.append(txn)
        if self.on_transaction is not None:
            self.on_transaction(txn)
        return response

    def _read_reply(self, quiet: float, reply_timeout: float,
                    require_reply: bool) -> bytes:
        assert self.sock is not None
        buf = bytearray()
        started = time.monotonic()
        last = started
        self.sock.settimeout(0.02)
        while True:
            now = time.monotonic()
            if buf and now - last >= quiet:
                break
            if not buf and now - started >= reply_timeout:
                if require_reply:
                    raise TransportError(
                        f"no reply within {reply_timeout:.1f}s")
                break
            if buf and now - started >= reply_timeout + quiet:
                break
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                raise TransportError(f"receive failed: {exc}") from exc
            if not chunk:
                break                      # peer closed
            buf += chunk
            last = time.monotonic()
        return bytes(buf)


class PlanCommandChannel:
    """Records the commands a session *would* send, without a socket.

    Used by ``ctx plan`` and by the tests that check the arming sequence against
    the OEM recipe. It applies the same token-zeroing and safety checks as the
    real channel, so a plan is evidence about what would be transmitted. Replies
    are synthetic GOOD statuses: the plan describes intent, not device state.
    """

    def __init__(self):
        self.steps: list[tuple[bytes, bytes]] = []
        self.history: list[Transaction] = []

    def connect(self) -> "PlanCommandChannel":
        return self

    def close(self) -> None:
        pass

    @staticmethod
    def _good() -> Response:
        record = bytearray(sensemod.RECORD_LEN)
        record[0:9] = sensemod.MAGIC
        return Response(data=b"", senses=[Sense.from_record(bytes(record))], raw=bytes(record))

    def transact(self, cdb: bytes = b"", *, data_out: bytes | None = None, **_) -> Response:
        if len(cdb) != cdbmod.CDB_LEN:
            raise ValueError(f"CDB must be {cdbmod.CDB_LEN} bytes, got {len(cdb)}")
        cdbmod.assert_safe(cdb)
        # The vendor carries one session token consistently; the zeroed form is a
        # degenerate case that works for some commands but makes SEND 0x88 (host
        # registration) fail with ASC 0x2C.
        if getattr(self, "token", None) is not None:
            cdb = cdb[:10] + self.token + cdb[16:]
        else:
            cdb = cdbmod.zero_token(cdb)
        _check_data_out(cdb, data_out)
        self.steps.append((cdb, data_out or b""))
        return self._good()

    def render(self) -> str:
        lines = []
        for i, (cdb, body) in enumerate(self.steps, 1):
            line = f"{i:3d}  {cdb.hex()}  {cdbmod.describe(cdb)}"
            if body:
                head = body[:16].hex()
                line += f"  | {len(body)}B {head}{'…' if len(body) > 16 else ''}"
            lines.append(line)
        return "\n".join(lines)
