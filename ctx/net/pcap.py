"""Offline reads of pcap/pcapng captures: fragment reassembly to image records.

This is the library counterpart of ``tools/cap/reassemble.py``. It exists so the
receive path can be exercised end to end without hardware: feeding a real
capture through :class:`ctx.proto.stream.StreamAssembler` runs the same code the
live socket runs.

The fragmentation rule is not optional (``SPECS.md`` §3.3): every image record is
a 2,960-byte UDP datagram split into 1,480 + 1,480 at the IP layer. Reading only
the first fragment discards 49.5 % of every record.
"""
from __future__ import annotations

import collections
import os
import struct

from ..proto.image import Record, HEADER_LEN

SCANNER_PORT = 58171
HOST_PORT = 58170

LINK_ETHERNET = 1
LINK_RAW_IP = 101
LINK_LINUX_SLL = 113
LINK_RAW_IPV4 = 228
LINK_LINUX_SLL2 = 276


# ------------------------------------------------------------------ containers
def read_pcap(path: str) -> list[tuple[float, int, bytes]]:
    """Return ``[(timestamp, linktype, frame_bytes), ...]`` for pcap or pcapng."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        return _read_pcapng(data)
    return _read_classic_pcap(data, os.path.basename(path))


def _read_classic_pcap(data: bytes, name: str) -> list[tuple[float, int, bytes]]:
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
        endian, nanosecond = ("<" if magic == b"\xd4\xc3\xb2\xa1" else ">"), False
    elif magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        endian, nanosecond = ("<" if magic == b"\x4d\x3c\xb2\xa1" else ">"), True
    else:
        raise ValueError(f"{name}: unknown capture magic {magic.hex()}")
    _vmaj, _vmin, _tz, _sig, _snap, link = struct.unpack_from(endian + "HHiIII", data, 4)
    scale = 1e-9 if nanosecond else 1e-6
    out = []
    off = 24
    while off + 16 <= len(data):
        ts, frac, incl, _orig = struct.unpack_from(endian + "IIII", data, off)
        off += 16
        frame = data[off:off + incl]
        off += incl
        if frame:
            out.append((ts + frac * scale, link, frame))
    return out


def _read_pcapng(data: bytes) -> list[tuple[float, int, bytes]]:
    out: list[tuple[float, int, bytes]] = []
    links: list[int] = []
    endian = "<"
    off = 0
    while off + 12 <= len(data):
        if data[off:off + 4] == b"\x0a\x0d\x0d\x0a":      # SHB resets interfaces
            bom = data[off + 8:off + 12]
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            block_len = struct.unpack_from(endian + "I", data, off + 4)[0]
            if block_len < 12:
                break
            links = []
            off += block_len
            continue
        block_type, block_len = struct.unpack_from(endian + "II", data, off)
        if block_len < 12 or off + block_len > len(data):
            break
        body = data[off + 8:off + block_len - 4]
        if block_type == 1:                                # IDB
            if len(body) >= 2:
                links.append(struct.unpack_from(endian + "H", body, 0)[0])
        elif block_type == 6:                              # EPB
            if len(body) >= 20:
                iface, ts_hi, ts_lo, cap, _orig = struct.unpack_from(endian + "IIIII", body, 0)
                frame = body[20:20 + cap]
                link = links[iface] if iface < len(links) else LINK_ETHERNET
                out.append(((ts_hi << 32 | ts_lo) / 1e6, link, frame))
        elif block_type == 3:                              # Simple Packet Block
            if len(body) >= 4:
                orig = struct.unpack_from(endian + "I", body, 0)[0]
                frame = body[4:4 + orig]
                out.append((0.0, links[0] if links else LINK_ETHERNET, frame))
        off += block_len
    return out


# ------------------------------------------------------------------- link/IP
def ipv4(link: int, frame: bytes) -> bytes | None:
    """Strip the link layer, returning the IPv4 packet or ``None``."""
    if link == LINK_ETHERNET:
        if len(frame) < 14:
            return None
        ethertype = struct.unpack_from(">H", frame, 12)[0]
        off = 14
        while ethertype in (0x8100, 0x88A8) and len(frame) >= off + 4:
            ethertype = struct.unpack_from(">H", frame, off + 2)[0]
            off += 4
        return frame[off:] if ethertype == 0x0800 else None
    if link in (LINK_RAW_IP, LINK_RAW_IPV4):
        return frame
    if link == LINK_LINUX_SLL:
        return frame[16:] if len(frame) > 16 else None
    if link == LINK_LINUX_SLL2:
        return frame[20:] if len(frame) > 20 else None
    return None


def _fragment(ip: bytes):
    """Return ``(src, dst, ident, offset, more_fragments, payload)``."""
    if len(ip) < 20 or ip[0] >> 4 != 4:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl:
        return None
    total = struct.unpack_from(">H", ip, 2)[0]
    ident = struct.unpack_from(">H", ip, 4)[0]
    flags_frag = struct.unpack_from(">H", ip, 6)[0]
    offset = (flags_frag & 0x1FFF) * 8
    more = bool(flags_frag & 0x2000)
    end = total if 0 < total <= len(ip) else len(ip)
    return ip[12:16], ip[16:20], ident, offset, more, ip[ihl:end]


# ---------------------------------------------------------------- reassembly
def udp_datagrams(path: str, *, src_port: int | None = SCANNER_PORT,
                  dst_port: int | None = HOST_PORT,
                  stats: dict | None = None) -> list[bytes]:
    """Reassemble IP fragments and return UDP payloads matching the port filter.

    Order is first-seen order of each ``(src, dst, ident)`` group, which is the
    order the scanner emitted them.
    """
    groups: dict[tuple, dict[int, bytes]] = collections.defaultdict(dict)
    order: list[tuple] = []
    incomplete = 0

    for _ts, link, frame in read_pcap(path):
        ip = ipv4(link, frame)
        if ip is None or ip[9] != 17:                      # IPv4 only, UDP only
            continue
        frag = _fragment(ip)
        if frag is None:
            continue
        src, dst, ident, offset, _more, payload = frag
        key = (src, dst, ident)
        if key not in groups:
            order.append(key)
        groups[key][offset] = payload

    out: list[bytes] = []
    for key in order:
        parts = groups[key]
        if 0 not in parts:
            incomplete += 1
            continue
        buf = b"".join(parts[off] for off in sorted(parts))
        if len(buf) < 8:
            incomplete += 1
            continue
        sport, dport, ulen = struct.unpack_from(">HHH", buf, 0)
        if src_port is not None and sport != src_port:
            continue
        if dst_port is not None and dport != dst_port:
            continue
        length = max(0, ulen - 8)
        out.append(buf[8:8 + length])

    if stats is not None:
        stats.update(datagrams=len(out), groups=len(order), incomplete_groups=incomplete)
    return out


def records(path: str, **kw) -> list[Record]:
    """Image records from a capture, arrival order preserved."""
    out = []
    bad = 0
    for payload in udp_datagrams(path, **kw):
        if len(payload) < HEADER_LEN:
            bad += 1
            continue
        try:
            out.append(Record.parse(payload))
        except ValueError:
            bad += 1
    return out


def image_stream(path: str, **kw) -> tuple[bytes, dict]:
    """Concatenated type-1 payloads in sequence order, plus receive stats."""
    from ..proto.stream import StreamAssembler

    assembler = StreamAssembler()
    for record in records(path, **kw):
        assembler.feed_record(record)
    return assembler.stream_bytes(), assembler.stats.as_dict()
