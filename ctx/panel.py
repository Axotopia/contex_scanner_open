"""Front-panel keys and ready-LED control, without vendor software.

All of this was measured on the live HD Ultra i4250s (serial ``UNKNOWN``)
on 2026-10-27; verified against hardware button press events.

Key events
----------
The live key state is carried in the 78-byte status page (``READ 0x80/78``),
bytes **60-63** (keyboard status).  The low byte (page offset 63) holds one bit
per physical key:

======  ======  ========
key     bit     mask
======  ======  ========
SCAN    5       ``0x20``
COPY    4       ``0x10``
EMAIL   3       ``0x08``
======  ======  ========

That is a subset of the ``0xC8`` keys mask (``0000037e``, bits 1-6), which is
the consistency check that these are the labelled keys.  Pressing a key does
**not** start a scan by itself - the bits are events for host software.

Each press holds the bit for roughly 1.4 s and then clears on its own, so the
monitor samples well above the 4 Hz floor that a 2 Hz poll was measured to miss.

The function-key byte 29 is a *different* mechanism: it stayed ``0x00`` on every
press until a ``SEND 0x88/0x00`` client subscription is made, so it is not used
here.

Ready LED
---------
``SEND type=0x85 item=0x07`` turns the ready LED on and ``item=0x08`` turns it
off, both with the same 4-byte payload ``00 00 40 00`` (the item selects the
function; the payload selects the LED).  This is a *write* and is only reachable
through the explicit :meth:`PanelMonitor.set_ready_led` API - nothing calls it
automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
import socket
import threading
import time
from typing import Any, Callable

from .engine import ScannerEngine, _build_vendor_cdb

#: ``READ 0x80/78`` - the full status page.
STATUS_PAGE_LEN = 78
#: Offset of the keyboard-status low byte within the status page.
KEY_BYTE_OFFSET = 63

KEY_SCAN = 0x20
KEY_COPY = 0x10
KEY_EMAIL = 0x08
KEY_MASK = KEY_SCAN | KEY_COPY | KEY_EMAIL

#: mask -> stable key name, in panel order.
KEYS: dict[int, str] = {KEY_SCAN: "scan", KEY_COPY: "copy", KEY_EMAIL: "email"}

LED_SEND_TYPE = 0x85
LED_ITEM_ON = 0x07
LED_ITEM_OFF = 0x08
LED_PAYLOAD = bytes([0x00, 0x00, 0x40, 0x00])

#: A 2 Hz poll was measured to miss presses; stay comfortably above it.
MIN_POLL_HZ = 4.0
DEFAULT_POLL_HZ = 8.0


@dataclass(frozen=True)
class PanelKey:
    """One front-panel key press."""

    key: str
    mask: int
    at: float           # time.time() when the rising edge was observed

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "mask": self.mask, "at": self.at}


def decode_key_presses(low: int, prev_low: int) -> list[tuple[str, int]]:
    """Rising-edge key detection for one status-page read.

    Returns ``[(key, mask), ...]`` for keys newly held since the previous read.
    Holding a key does not repeat the event; it must be released and pressed
    again, which is what a front-panel user expects.
    """
    pressed = low & KEY_MASK
    rising = pressed & ~(prev_low & KEY_MASK)
    return [(KEYS[m], m) for m in (KEY_SCAN, KEY_COPY, KEY_EMAIL) if rising & m]


class PanelMonitor:
    """Polls the status page for key presses and can drive the ready LED."""

    def __init__(
        self,
        host: str,
        port: int = 58170,
        *,
        poll_hz: float = DEFAULT_POLL_HZ,
        on_key: Callable[[PanelKey], None] | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.poll_hz = max(MIN_POLL_HZ, float(poll_hz))
        self.on_key = on_key
        self.on_state = on_state

        self._stop = threading.Event()
        self._suspend = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._connected = False
        self._last_error: str | None = None
        self._last_key: str | None = None
        self._last_key_at: float | None = None
        self._reads = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="panel-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        self._connected = False

    def suspend(self) -> None:
        """Stop polling (e.g. while the scanner is busy with a scan)."""
        self._suspend.set()

    def resume(self) -> None:
        self._suspend.clear()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- state -------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "connected": self._connected,
            "poll_hz": self.poll_hz,
            "reads": self._reads,
            "last_key": self._last_key,
            "last_key_at": self._last_key_at,
            "last_error": self._last_error,
            "keys": {name: f"0x{mask:02x}" for mask, name in KEYS.items()},
            "host": self.host,
            "port": self.port,
        }

    def _publish_state(self) -> None:
        if self.on_state is not None:
            try:
                self.on_state(self.snapshot())
            except Exception:
                pass

    # -- polling -----------------------------------------------------------
    def _open(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((self.host, self.port))
        return s

    def _run(self) -> None:
        engine = ScannerEngine(host=self.host, port=self.port)
        interval = 1.0 / self.poll_hz
        prev_low = 0
        sock: socket.socket | None = None

        while not self._stop.is_set():
            if self._suspend.is_set():
                if sock is not None:
                    try:
                        sock.close()
                    finally:
                        sock = None
                        self._connected = False
                self._stop.wait(0.2)
                continue

            if sock is None:
                try:
                    sock = self._open()
                    self._connected = True
                    self._last_error = None
                    prev_low = 0
                    self._publish_state()
                except OSError as exc:
                    self._connected = False
                    self._last_error = f"connect: {exc}"
                    self._publish_state()
                    self._stop.wait(2.0)
                    continue

            started = time.monotonic()
            try:
                reply = engine._transact_tcp(
                    sock,
                    _build_vendor_cdb(0x28, b2=0x80, b8=STATUS_PAGE_LEN, token=True),
                    quiet=0.15,
                )
            except OSError as exc:
                self._last_error = f"read: {exc}"
                self._connected = False
                try:
                    sock.close()
                finally:
                    sock = None
                self._publish_state()
                self._stop.wait(0.5)
                continue

            page = reply[4] if reply else b""
            if len(page) < STATUS_PAGE_LEN:
                # Short read: drop the connection rather than parse stale bytes.
                self._last_error = f"short status page ({len(page)}B)"
                try:
                    sock.close()
                finally:
                    sock = None
                self._connected = False
                continue

            self._reads += 1
            low = page[KEY_BYTE_OFFSET]
            for key, mask in decode_key_presses(low, prev_low):
                ev = PanelKey(key=key, mask=mask, at=time.time())
                with self._lock:
                    self._last_key = key
                    self._last_key_at = ev.at
                if self.on_key is not None:
                    try:
                        self.on_key(ev)
                    except Exception:
                        pass
            prev_low = low

            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, interval - elapsed))

        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._connected = False

    # -- LED ---------------------------------------------------------------
    def set_ready_led(self, on: bool) -> dict[str, Any]:
        """Turn the scanner's ready LED on/off.  This WRITES to the scanner."""
        item = LED_ITEM_ON if on else LED_ITEM_OFF
        cdb = _build_vendor_cdb(
            0x2A, b2=LED_SEND_TYPE, b5=item, b8=len(LED_PAYLOAD), token=True
        )
        engine = ScannerEngine(host=self.host, port=self.port)
        try:
            with self._lock:
                s = self._open()
                try:
                    reply = engine._transact_tcp(s, cdb, LED_PAYLOAD, quiet=0.3)
                finally:
                    s.close()
        except OSError as exc:
            return {"ok": False, "item": item, "error": str(exc)}
        status = reply[0] if reply else None
        return {"ok": status == 0, "item": item, "status": status}
