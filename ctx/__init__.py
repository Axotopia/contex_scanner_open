"""ctx — Contex HD Ultra comms layer.

A Linux/macOS userspace driver for Contex large-format scanners (HD Ultra
i4250s and siblings), built from the protocol in ``SPECS.md`` and the state
machine in ``analysis/STATUS.md``.

Layering (``DESIGN.md`` §8)::

    ctx.proto   byte layouts — CDBs, SCSISENSE, the 82-byte window block,
                the 16-byte UDP record header. Pure functions, no I/O.
    ctx.net     transports — UDP discovery, the TCP command channel, the UDP
                image endpoint, and offline pcap reassembly.
    ctx.session the session state machine that orders the two together.
    ctx.frame   wire bytes -> rows -> image (line-length detection).

Nothing in ``ctx.proto`` or ``ctx.frame`` touches a socket, so all of it is
testable without a scanner. See ``analysis/COMMS-LAYER.md`` for what has been
verified against recorded traffic and what still needs hardware.
"""

__version__ = "0.1.0"

from .errors import (  # noqa: F401
    Action,
    CommandFailed,
    ContexError,
    DiscoveryError,
    ProtocolError,
    SafetyViolation,
    TransportError,
)
