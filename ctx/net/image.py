"""The UDP image endpoint: scanner ``:58171`` -> host ``:58170``.

The kernel reassembles IP fragments before delivering to a socket, so a live
receiver sees whole ~2,960-byte records. No fragment handling is needed here;
that only matters for captures, in :mod:`ctx.net.pcap`.

Host -> scanner control datagrams (acks, credits, handshake) go to the command
port by default. A note from the proxy work (``tools/ctxscan_proxy.py``) says the
UDP speed negotiation must be answered on the port the datagrams are *sent from*
(observed 58171), so ``learn_source`` is available for that path; during a scan
the fixed port is what the working recipe used.
"""
from __future__ import annotations

import socket

from ..errors import TransportError
from ..proto import image as img

DEFAULT_IMAGE_PORT = 58170
DEFAULT_RCVBUF = 64 << 20


class ImageEndpoint:
    def __init__(self, scanner_ip: str, *, bind: tuple[str, int] = ("", DEFAULT_IMAGE_PORT),
                 port: int = DEFAULT_IMAGE_PORT, rcvbuf: int = DEFAULT_RCVBUF,
                 learn_source: bool = False, timeout: float = 0.05):
        self.scanner_ip = scanner_ip
        self.bind = bind
        self.port = port
        self.rcvbuf = rcvbuf
        self.learn_source = learn_source
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.last_source: tuple[str, int] | None = None
        self.received = 0

    def open(self) -> "ImageEndpoint":
        if self.sock is not None:
            return self
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf)
        except OSError:
            pass
        try:
            sock.bind(self.bind)
        except OSError as exc:
            sock.close()
            raise TransportError(
                f"cannot bind UDP {self.bind[0] or '*'}:{self.bind[1]} ({exc}); "
                "another receiver (proxy, vendor software) probably holds it"
            ) from exc
        sock.settimeout(self.timeout)
        self.sock = sock
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> "ImageEndpoint":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------- io
    def send(self, datagram: bytes) -> None:
        if self.sock is None:
            raise TransportError("image endpoint is not open")
        dest = self.last_source if (self.learn_source and self.last_source) \
            else (self.scanner_ip, self.port)
        try:
            self.sock.sendto(datagram, dest)
        except OSError as exc:
            raise TransportError(f"image control send to {dest} failed: {exc}") from exc

    def recv(self, timeout: float | None = None) -> tuple[bytes, tuple[str, int]] | None:
        """Return one datagram, or ``None`` on timeout."""
        if self.sock is None:
            raise TransportError("image endpoint is not open")
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        try:
            data, addr = self.sock.recvfrom(65536)
        except socket.timeout:
            return None
        except OSError as exc:
            raise TransportError(f"image receive failed: {exc}") from exc
        self.received += 1
        self.last_source = addr
        return data, addr

    def handshake(self) -> None:
        """Send the type-7 + type-9 pair that opens one transfer."""
        for datagram in img.handshake_payload():
            self.send(datagram)


class PlanImageEndpoint:
    """Records image-channel control datagrams without a socket."""

    def __init__(self):
        self.sent: list[bytes] = []

    def open(self) -> "PlanImageEndpoint":
        return self

    def close(self) -> None:
        pass

    def send(self, datagram: bytes) -> None:
        self.sent.append(datagram)

    def recv(self, timeout: float | None = None):  # pragma: no cover - plan only
        return None

    def handshake(self) -> None:
        self.send(img.handshake_payload()[0])
        self.send(img.handshake_payload()[1])
