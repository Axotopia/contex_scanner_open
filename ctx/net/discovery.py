"""UDP discovery on port 58169.

``HANDOFF-COMMS.md`` §6.1: a broadcast ASCII probe,
``"0;<hostname>;List Shared scanners;\\0"``, with a 1001 ms collection window.
The reply is tokenised on ``;``/TAB/LF, the kind prefix is ``1;`` or ``1,0;``,
and **the scanner address is the UDP source address, never the payload** — the
payload address is only reported for diagnostics.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from ..errors import DiscoveryError

DETECT_PORT = 58169
PROBE_TEMPLATE = "0;{hostname};List Shared scanners;"
DEFAULT_WINDOW_MS = 1001

#: 1-based field position 5 (index 4) carries the transport.
TRANSPORT_ETH = "eth"
TRANSPORT_LIC = "lic"


@dataclass(frozen=True)
class ScannerInfo:
    ip: str
    port: int
    model: str
    serial: str
    transport: str
    network: str
    kind: str
    reported_ip: str | None = None
    source: tuple[str, int] | None = field(default=None, repr=False)

    def __str__(self) -> str:
        return (f"{self.model or 'scanner'} at {self.ip}:{self.port} "
                f"serial={self.serial or '?'} transport={self.transport or '?'}")


def build_probe(hostname: str | None = None) -> bytes:
    host = hostname or socket.gethostname()
    return PROBE_TEMPLATE.format(hostname=host).encode("ascii", "replace")


def parse_reply(data: bytes, source: tuple[str, int] | None = None) -> ScannerInfo | None:
    """Parse one discovery reply; ``None`` if it is not a scanner reply.

    Tolerant by design: the payload address is kept as ``reported_ip`` but the
    authoritative address is the packet source.
    """
    text = data.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
    if not text:
        return None
    tokens = [t.strip() for t in text.replace("\t", ";").replace("\n", ";").split(";")]
    if len(tokens) < 4:
        return None
    kind = tokens[0]
    if not kind.startswith("1"):
        return None

    def tok(i: int) -> str:
        return tokens[i] if i < len(tokens) else ""

    try:
        port = int(tok(3) or 0)
    except ValueError:
        port = 0
    ip = source[0] if source else (tok(1) or "")
    return ScannerInfo(
        ip=ip,
        port=port,
        model=tok(2),
        serial=tok(6),
        transport=tok(4),
        network=tok(5),
        kind=kind,
        reported_ip=tok(1) or None,
        source=source,
    )


def discover(*, hostname: str | None = None, window_ms: int = DEFAULT_WINDOW_MS,
             broadcast: str = "255.255.255.255", port: int = DETECT_PORT,
             bind: tuple[str, int] = ("", DETECT_PORT),
             exclude_ips: tuple[str, ...] = (),
             extra_targets: tuple[str, ...] = ()) -> list[ScannerInfo]:
    """Broadcast the probe and collect replies for *window_ms*.

    ``exclude_ips`` exists because a proxy that impersonates a scanner will
    answer its own broadcast; the host itself is normally excluded by the caller.
    ``extra_targets`` adds unicast destinations for networks where broadcast is
    filtered.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            sock.bind(bind)
        except OSError:
            sock.bind((bind[0], 0))
        probe = build_probe(hostname)
        targets = {broadcast, *extra_targets}
        sent = 0
        for dest in targets:
            try:
                sock.sendto(probe, (dest, port))
                sent += 1
            except OSError:
                continue
        if not sent:
            raise DiscoveryError("could not send the discovery probe on any target")

        found: list[ScannerInfo] = []
        seen: set[tuple] = set()
        deadline = time.monotonic() + window_ms / 1000.0
        sock.settimeout(0.05)
        while time.monotonic() < deadline:
            try:
                data, source = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:  # pragma: no cover - platform dependent
                raise DiscoveryError(f"discovery receive failed: {exc}") from exc
            info = parse_reply(data, source)
            if info is None:
                continue
            if info.ip in exclude_ips:
                continue
            key = (info.ip, info.port, info.serial, info.model)
            if key in seen:            # identical repeats are dropped
                continue
            seen.add(key)
            found.append(info)
        return found
    finally:
        sock.close()
