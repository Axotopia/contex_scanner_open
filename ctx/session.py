"""The session state machine: order, retries, and when to tear down.

``HANDOFF-COMMS.md`` §6.2 says the session state machine is "currently implicit
in ``ctxscan_proxy.py``". This module makes it explicit and executable, and
``ctx plan`` prints it so a sequence can be diffed against a vendor capture
before anything is sent.

The order is the one that produced real pixels (``STATUS.md`` §13)::

    TUR -> RESERVE(zeros) -> OBJECT POSITION(load) -> media ready
        -> SEND 0x84 -> WRITE BUFFER 0x48 -> SET WINDOW
        -> WRITE BUFFER 0xA2 -> tables 0..4 -> START STOP UNIT
        -> SEND 0x80/item 1/0x01 (the trigger)
        -> type-7 + type-9 handshake -> data -> ACK every 32

Everything up to and including ``INQUIRY``/capability pages is read-only (M1).
Arming and starting drive hardware (M2); the receive path (M3) only listens.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import data as recipe
from .errors import Action, CommandFailed, TransportError
from .net.command import CommandChannel, Response
from .net.image import ImageEndpoint
from .proto import cdb as C
from .proto import image as I
from .proto.stream import StreamAssembler
from .proto.window import WindowParams

DEFAULT_HOST = "0.0.0.0"
COMMAND_PORT = 58170
IMAGE_PORT = 58170

#: Media-status probe: READ type 0x00, item 0x80, two bytes. Observed ready
#: value is 04 30 (``scan_once.py`` checks the second byte).
MEDIA_READ_TYPE = 0x80
MEDIA_READ_ITEM = 0x00
MEDIA_READY_BYTES = (0x04, 0x30)

#: VPD pages read during session setup. The vendor reads 0xC7/0xC3/0xC2/0xC1/0xC0
#: (``ctxscan.py``); 0xC5 is added because it carries the camera positions and the
#: live device answers it (verified 2026-10-25).
DEVICE_VPD_PAGES = (0xC7, 0xC5, 0xC3, 0xC2, 0xC1, 0xC0)


class State(Enum):
    NEW = "new"
    CONNECTED = "connected"
    IDENTIFIED = "identified"
    READY = "ready"
    ARMED = "armed"
    SCANNING = "scanning"
    DONE = "done"
    CLOSED = "closed"


@dataclass(frozen=True)
class VpdIdentity:
    """Identity strings carried in VPD page 0xC7 (live offsets, 2026-10-25)."""

    serial: str = ""
    firmware: str = ""
    ip: str = ""
    mac: str = ""
    station: str = ""
    vendor: str = ""
    product: str = ""

    def describe(self) -> str:
        bits = []
        if self.firmware:
            bits.append(f"firmware {self.firmware}")
        if self.serial:
            bits.append(f"serial {self.serial}")
        if self.mac:
            bits.append(f"mac {self.mac}")
        if self.station:
            bits.append(f"station {self.station!r}")
        return ", ".join(bits)


@dataclass(frozen=True)
class DeviceIdentity:
    peripheral_type: int
    vendor: str
    product: str
    revision: str
    vpd: dict[int, bytes] = field(default_factory=dict)
    vpd_identity: VpdIdentity | None = None

    @property
    def is_scanner(self) -> bool:
        return self.peripheral_type == 0x06

    def describe(self) -> str:
        kind = "SCANNER" if self.is_scanner else f"type 0x{self.peripheral_type:02x}"
        rev = f" rev {self.revision}" if self.revision.strip(" \x00") else ""
        return f"{kind} vendor={self.vendor!r} product={self.product!r}{rev}"


@dataclass(frozen=True)
class Capabilities:
    """Best-effort decode of the vendor capability pages.

    Fields are ``None`` where a page was missing or a value failed a plausibility
    check; ``raw`` always holds what the device actually returned, so a wrong
    guess here cannot lose evidence.
    """

    cameras: int | None = None
    max_width_units: int | None = None
    window_block_len: int | None = None
    camera_positions: tuple[int, ...] = ()
    resolutions: tuple[int, ...] = ()
    raw: dict[int, bytes] = field(default_factory=dict)

    def describe(self) -> str:
        bits = []
        if self.cameras is not None:
            bits.append(f"{self.cameras} cameras")
        if self.max_width_units is not None:
            bits.append(f"max width {self.max_width_units} units "
                        f"({self.max_width_units / 1200:.2f} in)")
        if self.window_block_len:
            bits.append(f"window block {self.window_block_len} B")
        if self.camera_positions:
            bits.append("positions " + ", ".join(
                f"{p / 1200:.1f}" for p in self.camera_positions) + " in")
        if self.resolutions:
            bits.append("dpi " + "/".join(str(d) for d in self.resolutions))
        return "; ".join(bits) if bits else "no capability fields decoded"


@dataclass(frozen=True)
class SessionConfig:
    host: str = DEFAULT_HOST
    port: int = COMMAND_PORT
    image_bind: tuple[str, int] = ("", IMAGE_PORT)
    image_port: int = IMAGE_PORT
    quiet: float = 1.0
    reply_timeout: float = 6.0
    ack_every: int = I.DEFAULT_ACK_EVERY
    ack_ai0: int = I.DEFAULT_ACK_AI0
    ack_mode: str = "seq"
    table_count: int = 5
    ready_timeout: float = 30.0
    ready_poll_interval: float = 0.25
    max_transfers: int = 8
    transfer_timeout: float = 180.0
    transfer_idle: float = 1.2
    empty_transfer_limit: int = 2
    collect: bool = True
    on_event: Callable[[str, dict], None] | None = None


# --------------------------------------------------------------------- plan items
@dataclass(frozen=True)
class Command:
    """A command the session will send verbatim."""

    name: str
    cdb: bytes
    data_out: bytes = b""
    tolerate: bool = False


@dataclass(frozen=True)
class Poll:
    """A decision point the session resolves by reading the device."""

    name: str
    kind: str


PlanItem = Command | Poll


def arming_commands(window: WindowParams, *, table_count: int = 5) -> list[Command]:
    """The writes that stage a scan, in the recovered order."""
    if not 0 <= table_count <= 10:
        raise ValueError("table_count must be 0..10")
    send84_cdb, send84_body = recipe.command("send_0x84")
    wb48_cdb, wb48_body = recipe.command("write_buffer_0x48")
    wba2_cdb, wba2_body = recipe.command("write_buffer_0xa2")

    block = window.to_block()

    def zero(cdb: bytes) -> bytes:
        # A plan must show what would actually be transmitted, so the OEM token
        # is dropped here as well as in the transport.
        return C.zero_token(cdb)

    out = [
        Command("send-0x84", zero(send84_cdb), send84_body),
        Command("write-buffer-0x48", zero(wb48_cdb), wb48_body),
        Command("set-window", C.set_window(len(block)), block),
        Command("write-buffer-0xa2", zero(wba2_cdb), wba2_body),
    ]
    for index, (cdb, body) in enumerate(recipe.tables(table_count)):
        out.append(Command(f"table-{index}", zero(cdb), body))
    return out


def scan_plan(window: WindowParams, *, table_count: int = 5,
              include_trigger: bool = True) -> list[PlanItem]:
    """The complete static sequence, with the media poll where it belongs."""
    trigger_cdb, trigger_body = recipe.command("trigger")
    items: list[PlanItem] = [
        Command("test-unit-ready", C.test_unit_ready(), tolerate=True),
        Command("reserve", C.reserve()),
        Command("object-position-load", C.object_position_load(), tolerate=True),
        Poll("media-ready", "media"),
    ]
    items.extend(arming_commands(window, table_count=table_count))
    items.append(Command("start-stop-unit", C.start_stop_unit()))
    if include_trigger:
        items.append(Command("trigger", C.zero_token(trigger_cdb), trigger_body,
                             tolerate=True))
    return items


@dataclass
class ScanResult:
    window: WindowParams
    stream: bytes
    stats: dict
    transfers: list[dict]
    duration: float
    acks_sent: int
    media_ready: bool

    @property
    def records(self) -> int:
        return int(self.stats.get("data_records", 0))

    @property
    def complete(self) -> bool:
        return int(self.stats.get("missing_records", 0)) == 0

    def summary(self) -> str:
        s = self.stats
        return (f"{self.records} records, {len(self.stream)} bytes in "
                f"{self.duration:.1f}s ({len(self.transfers)} transfer(s)); "
                f"gaps={s.get('gaps', 0)} missing={s.get('missing_records', 0)} "
                f"dup={s.get('duplicates', 0)} acks={self.acks_sent} "
                f"media_ready={self.media_ready}")


class ScannerSession:
    """One command session, from connect to release.

    ``channel`` and ``image`` are injectable so the whole state machine can be
    driven by a scripted fake in tests, and by ``ctx plan`` with no I/O at all.
    """

    def __init__(self, config: SessionConfig | None = None, *,
                 channel=None, image=None):
        self.config = config or SessionConfig()
        cfg = self.config
        self.channel = channel if channel is not None else CommandChannel(
            cfg.host, cfg.port, quiet=cfg.quiet, reply_timeout=cfg.reply_timeout)
        self.image = image if image is not None else ImageEndpoint(
            cfg.host, bind=cfg.image_bind, port=cfg.image_port)
        self.state = State.NEW
        self.identity: DeviceIdentity | None = None
        self.capabilities: Capabilities | None = None
        self.transcript: list[dict] = []
        self._acks_sent = 0
        self._media_ready = False

    # ------------------------------------------------------------------ logging
    def _emit(self, kind: str, payload: dict) -> None:
        entry = {"t": round(time.monotonic(), 4), "kind": kind, **payload}
        self.transcript.append(entry)
        if self.config.on_event is not None:
            self.config.on_event(kind, payload)

    # ----------------------------------------------------------------- lifecycle
    def open(self) -> "ScannerSession":
        if self.state is not State.NEW:
            return self
        self.channel.connect()
        self.image.open()
        self.state = State.CONNECTED
        self._emit("open", {"host": self.config.host, "port": self.config.port})
        return self

    def close(self, *, release: bool = True) -> None:
        if self.state is State.CLOSED:
            return
        if release and self.state in (State.READY, State.ARMED, State.SCANNING, State.DONE):
            try:
                self.release()
            except (CommandFailed, TransportError):
                pass
        try:
            self.channel.close()
        finally:
            self.image.close()
        self.state = State.CLOSED
        self._emit("close", {})

    def __enter__(self) -> "ScannerSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- the device
    def _transact(self, item: Command, *, quiet: float | None = None) -> Response:
        response = self.channel.transact(
            item.cdb, data_out=item.data_out or None,
            quiet=self.config.quiet if quiet is None else quiet,
            reply_timeout=self.config.reply_timeout,
            require_reply=True,
        )
        sense = response.sense
        self._emit("command", {"name": item.name, "cdb": item.cdb.hex(),
                               "out": len(item.data_out),
                               "in": len(response.data),
                               "status": None if sense is None else sense.status,
                               "asc": None if sense is None else sense.asc})
        if sense is None:
            if item.tolerate:
                return response
            raise TransportError(f"{item.name}: no status record returned")
        if not sense.ok and not item.tolerate:
            raise CommandFailed.from_sense(item.cdb[0], sense)
        return response

    def identify(self) -> DeviceIdentity:
        """M1, read-only: TUR, standard INQUIRY, vendor VPD pages."""
        self.open()
        self._transact(Command("test-unit-ready", C.test_unit_ready(), tolerate=True))
        response = self._transact(Command("inquiry", C.inquiry(96)))
        data = response.data
        if len(data) < 36:
            raise TransportError(f"INQUIRY returned {len(data)} bytes, need 36")
        vpd: dict[int, bytes] = {}
        for page in DEVICE_VPD_PAGES:
            try:
                reply = self._transact(
                    Command(f"vpd-0x{page:02x}", C.inquiry(255, evpd=True, page=page),
                            tolerate=True), quiet=2.2)
                if reply.data:
                    vpd[page] = reply.data
            except (CommandFailed, TransportError) as exc:
                self._emit("vpd-error", {"page": page, "error": str(exc)})
        self.identity = DeviceIdentity(
            peripheral_type=data[0],
            vendor=data[8:16].decode("latin-1", "replace").strip(),
            product=data[16:32].decode("latin-1", "replace").strip("\x00 "),
            revision=data[32:36].decode("latin-1", "replace").strip("\x00 "),
            vpd=vpd,
            vpd_identity=_parse_vpd_identity(vpd.get(0xC7, b"")),
        )
        self.capabilities = _parse_capabilities(vpd)
        self.state = State.IDENTIFIED
        self._emit("identify", {"identity": self.identity.describe(),
                                "capabilities": self.capabilities.describe()})
        return self.identity

    # ----------------------------------------------------------------- the plan
    def plan(self, window: WindowParams) -> list[PlanItem]:
        return scan_plan(window, table_count=self.config.table_count)

    def run_plan(self, items: list[PlanItem]) -> None:
        """Execute plan items in order, resolving polls against the device."""
        for item in items:
            if isinstance(item, Poll):
                if item.kind == "media":
                    self._media_ready = self.wait_media_ready()
                else:  # pragma: no cover - guarded by construction
                    raise ValueError(f"unknown poll kind {item.kind!r}")
            elif item.name == "reserve":
                # Route through reserve() so a RESERVATION CONFLICT gets the
                # documented release-then-re-reserve recovery.
                self.reserve()
            else:
                self._transact(item)
        if self.state in (State.CONNECTED, State.IDENTIFIED):
            self.state = State.ARMED

    def reserve(self) -> None:
        """RESERVE, with the one documented recovery: clear, then re-reserve."""
        try:
            self._transact(Command("reserve", C.reserve()))
        except CommandFailed as exc:
            if exc.action is not Action.CLEAR_RESERVATION:
                raise
            self._emit("reserve-conflict", {"detail": exc.detail})
            self._transact(Command("release", C.release(), tolerate=True))
            self._transact(Command("reserve", C.reserve()))
        if self.state is State.CONNECTED:
            self.state = State.READY

    def wait_media_ready(self, timeout: float | None = None) -> bool:
        """Poll the media-status read until it reports ready.

        Returns ``False`` on timeout rather than raising: ``scan_once.py``
        proceeds anyway, and the failure mode is visible in the transfer stats.
        """
        deadline = time.monotonic() + (self.config.ready_timeout if timeout is None
                                       else timeout)
        poll = Command("media-status", C.read(MEDIA_READ_TYPE, MEDIA_READ_ITEM, 2),
                       tolerate=True)
        while time.monotonic() < deadline:
            try:
                data = self._transact(poll).data
            except TransportError:
                data = b""
            if len(data) >= 2 and (data[1] == MEDIA_READY_BYTES[1]
                                   or data[:2] == bytes(MEDIA_READY_BYTES)):
                return True
            time.sleep(self.config.ready_poll_interval)
        self._emit("media-timeout", {})
        return False

    def arm(self, window: WindowParams) -> None:
        """Stage the window and tables without starting the paper motion."""
        window.validate()
        for item in arming_commands(window, table_count=self.config.table_count):
            self._transact(item)
        self.state = State.ARMED

    def start(self) -> None:
        self._transact(Command("start-stop-unit", C.start_stop_unit()))

    def stop(self) -> None:
        """Stop the paper motor. Best effort: the vendor does this after the last
        data record, before ejecting and releasing."""
        try:
            self._transact(Command("start-stop-unit-stop", C.start_stop_unit(False),
                                   tolerate=True))
        except TransportError as exc:
            self._emit("stop-failed", {"error": str(exc)})

    def trigger(self) -> None:
        cdb, body = recipe.command("trigger")
        self._transact(Command("trigger", C.zero_token(cdb), body, tolerate=True))

    # ----------------------------------------------------------------- the scan
    def scan(self, window: WindowParams, *, collect: bool | None = None) -> ScanResult:
        """Run one full scan and return the received byte stream."""
        window.validate()
        if collect is None:
            collect = self.config.collect
        started = time.monotonic()
        self.open()
        # The plan ends with START STOP UNIT and the trigger; sending them a
        # second time leaves the busy scanner unable to answer (learned the hard
        # way: the duplicate START simply gets no reply).
        self.run_plan(self.plan(window))
        self.state = State.SCANNING

        assembler = StreamAssembler(ack_every=self.config.ack_every,
                                    ack_ai0=self.config.ack_ai0,
                                    ack_mode=self.config.ack_mode,
                                    collect=collect)
        self._acks_sent = 0
        transfers: list[dict] = []
        empty = 0
        for index in range(self.config.max_transfers):
            before = assembler.stats.data_records
            self.image.handshake()
            stats = self._pump_transfer(assembler, before)
            transfers.append({"index": index, **stats})
            if assembler.stats.data_records == before:
                empty += 1
                if empty >= self.config.empty_transfer_limit:
                    break
            else:
                empty = 0

        self.stop()
        self.state = State.DONE
        result = ScanResult(
            window=window,
            stream=assembler.stream_bytes() if collect else b"",
            stats=assembler.stats.as_dict(),
            transfers=transfers,
            duration=time.monotonic() - started,
            acks_sent=self._acks_sent,
            media_ready=self._media_ready,
        )
        self._emit("scan-complete", {"summary": result.summary()})
        return result

    def _pump_transfer(self, assembler: StreamAssembler, before_records: int) -> dict:
        """Receive one buffer's worth of records, ACK-ing as they arrive."""
        cfg = self.config
        started = time.monotonic()
        deadline = started + cfg.transfer_timeout
        last_data = started
        finals_before = assembler.stats.finals
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            item = self.image.recv(timeout=0.05)
            if item is None:
                if now - last_data >= cfg.transfer_idle:
                    break
                continue
            datagram, _addr = item
            for control in assembler.feed(datagram):
                self.image.send(control)
                self._acks_sent += 1
            if assembler.stats.finals > finals_before and now - last_data > 0.3:
                break
            last_data = time.monotonic()
        return {
            "records": assembler.stats.data_records - before_records,
            "finals": assembler.stats.finals - finals_before,
            "seconds": round(time.monotonic() - started, 3),
        }

    # ------------------------------------------------------- parameter blocks (M4)
    def read_buffer(self, bufid: int, offset: int = 0, length: int = 256,
                    *, mode: int = 0x01) -> bytes:
        """Safe READ BUFFER. Never a write; see ``DESIGN.md`` §1."""
        item = Command(f"read-buffer-0x{bufid:02x}",
                       C.read_buffer(bufid, length, offset, mode))
        return self._transact(item).data

    def read_parameter_block(self, bufid: int = 0xBF, offset: int = 0,
                             length: int = 0x91) -> bytes:
        """Read a parameter block.

        The defaults reproduce the vendor's observed read (``READ BUFFER 0xBF``,
        ``offset 0``, 0x91 bytes, ``proxy-session3.log``), and were verified
        against the live device on 2026-10-25.

        Offset semantics, established by probing (``analysis/param-blocks-1025.json``):

        * ``0xBF`` (and content buffers generally) use **byte offsets**;
        * ``0xA0`` is *not* a flat array — it behaves as a selector returning one
          4-byte value, zero-padded, and selector 15 yields the length of the
          0xBF block (0x91). Use :meth:`read_block_directory` for it.

        The block's internal structure is still undecoded. It holds factory
        calibration metadata (ASCII timestamps from 2014 and 2023), so capture it
        with a matched ``.cdf`` before interpreting any field.
        """
        return self.read_buffer(bufid, offset, length)

    def read_block_directory(self, bufid: int = 0xA0, count: int = 16) -> list[int]:
        """Read the ``0xA0`` selector table: one big-endian u32 per selector."""
        values = []
        for selector in range(count):
            data = self.read_buffer(bufid, selector, 4)
            values.append(int.from_bytes(data[:4], "big") if len(data) >= 4 else -1)
        return values

    def release(self) -> None:
        self._transact(Command("release", C.release(), tolerate=True))


# ------------------------------------------------------------------ page decoding
def _u32(data: bytes, off: int) -> int | None:
    return int.from_bytes(data[off:off + 4], "big") if len(data) >= off + 4 else None


def _parse_capabilities(pages: dict[int, bytes]) -> Capabilities:
    """Decode pages 0xC1/0xC3/0xC5, keeping raw bytes regardless.

    Offsets below are from the live device (firmware 3-5-1-015), not from
    ``SPECS.md``; where they differ the capture wins and the difference is
    recorded in ``analysis/COMMS-LAYER.md``:

    * 0xC1 NoOfCameras is a **u32 at 16** (the u16 at 18 is the same value's low
      half, so either reading returns 5 here);
    * 0xC1 maxSetWindowLength is a **single byte at 66** (0x52 = 82), not a u16;
    * 0xC5 has a u32 count at 4 then that many u32 positions from 8;
    * 0xC3 has a u32 count at 4, an extra value at 8, then the list from 12.
    """
    cameras = max_width = block_len = None
    positions: tuple[int, ...] = ()
    resolutions: tuple[int, ...] = ()

    page_c1 = pages.get(0xC1)
    if page_c1:
        cam = _u32(page_c1, 16)
        if cam is not None and 1 <= cam <= 16:
            cameras = cam
        width = _u32(page_c1, 28)
        if width is not None and 1200 <= width <= 200_000:
            max_width = width
        if len(page_c1) > 66 and 16 <= page_c1[66] <= 4096:
            block_len = page_c1[66]

    page_c5 = pages.get(0xC5)
    if page_c5:
        count = _u32(page_c5, 4)
        if count is not None and 1 <= count <= 16:
            found = []
            for i in range(count):
                value = _u32(page_c5, 8 + 4 * i)
                if value is None or not 0 < value <= 200_000:
                    found = []
                    break
                found.append(value)
            if found and all(b > a for a, b in zip(found, found[1:])):
                positions = tuple(found)

    page_c3 = pages.get(0xC3)
    if page_c3:
        count = _u32(page_c3, 4)
        if count is not None and 1 <= count <= 16:
            found = []
            for i in range(count):
                value = _u32(page_c3, 12 + 4 * i)
                if value is None or not 50 <= value <= 2400:
                    found = []
                    break
                found.append(value)
            resolutions = tuple(found)

    return Capabilities(cameras=cameras, max_width_units=max_width,
                        window_block_len=block_len, camera_positions=positions,
                        resolutions=resolutions, raw=dict(pages))


def _cstring(data: bytes, offset: int, limit: int = 40) -> str:
    """Null-terminated ASCII at *offset*, or ``""`` if it does not look like text."""
    if offset >= len(data):
        return ""
    end = data.find(b"\x00", offset, offset + limit)
    chunk = data[offset:end if end >= 0 else offset + limit]
    if not chunk:
        return ""
    text = chunk.decode("latin-1", "replace").strip()
    if any(not 32 <= ord(ch) < 127 for ch in text):
        return ""
    return text


def _parse_vpd_identity(page: bytes) -> VpdIdentity:
    """Decode page 0xC7. Fields are validated rather than trusted: a wrong
    offset must yield ``""``, never a plausible-looking lie."""
    if not page:
        return VpdIdentity()

    def digits_or_empty(text: str) -> str:
        return text if text and all(ch.isdigit() or ch in ".-" for ch in text) else ""

    def dotted_quad(text: str) -> str:
        parts = text.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return text
        return ""

    def mac(text: str) -> str:
        parts = text.split("-")
        if len(parts) == 6 and all(len(p) == 2 and all(c in "0123456789ABCDEFabcdef" for c in p)
                                   for p in parts):
            return text.upper()
        return ""

    return VpdIdentity(
        serial=digits_or_empty(_cstring(page, 4, 12)),
        ip=dotted_quad(_cstring(page, 14, 16)),
        station=_cstring(page, 44, 16),
        firmware=_cstring(page, 60, 16),
        mac=mac(_cstring(page, 100, 18)),
        vendor=_cstring(page, 118, 16),
        product=_cstring(page, 150, 24),
    )
