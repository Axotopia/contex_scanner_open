"""``python -m ctx`` — the command line.

    ctx discover                     broadcast probe, list scanners
    ctx info                         M1: read-only identify + capability pages
    ctx plan                         print the exact command sequence, no I/O
    ctx scan --out NAME              M2/M3: run one scan to NAME.bin + .jsonl
    ctx param --buffer 0xbf          M4: READ BUFFER, dump a parameter block
    ctx pcap CAPTURE                 offline: reassemble a capture to a stream
    ctx app                          launch the Contex Studio WebUI & REST server
    ctx mcp                          launch the Model Context Protocol (MCP) server

Everything except ``discover``/``info``/``plan`` needs the scanner. ``plan`` is
deliberately dependency-free: it is how a new sequence is reviewed against a
vendor capture before it is ever transmitted.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .errors import CommandFailed, ContexError, DiscoveryError, TransportError
from .net import discovery
from .net import pcap as pcapmod
from .proto.window import MAX_WIDTH_UNITS, WindowParams
from .session import SessionConfig, ScannerSession, scan_plan


# ------------------------------------------------------------------ formatting
def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:06x}  {chunk.hex():{width * 2}s}  {text}")
    return "\n".join(lines)


def _window_from_args(args) -> WindowParams:
    return WindowParams(
        dpi_x=args.dpi,
        width_units=int(round(args.width_in * 1200)),
        length_units=0 if args.length_in is None else int(round(args.length_in * 1200)),
        composition=2 if args.colour else 1,
    )


def _emit_json(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


# ------------------------------------------------------------------- commands
def cmd_discover(args) -> int:
    try:
        found = discovery.discover(hostname=args.hostname, window_ms=args.window_ms,
                                   broadcast=args.broadcast,
                                   exclude_ips=tuple(args.exclude or ()))
    except DiscoveryError as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 1
    if not found:
        print("no scanner answered", file=sys.stderr)
        return 1
    if args.json:
        _emit_json([f.__dict__ for f in found])
        return 0
    for info in found:
        note = ""
        if info.reported_ip and info.reported_ip != info.ip:
            note = f"  (payload claimed {info.reported_ip})"
        print(f"{info}{note}")
    return 0


def _connect_session(args) -> ScannerSession:
    cfg = SessionConfig(host=args.ip, port=args.port, quiet=args.quiet,
                        table_count=args.tables)
    return ScannerSession(cfg)


def cmd_info(args) -> int:
    session = _connect_session(args)
    try:
        with session:
            identity = session.identify()
            caps = session.capabilities
            if args.json:
                _emit_json({
                    "identity": identity.__dict__ | {"vpd": {hex(k): v.hex()
                                                             for k, v in identity.vpd.items()}},
                    "capabilities": caps.describe(),
                })
                return 0
            print(f"identity : {identity.describe()}")
            if identity.vpd_identity is not None and identity.vpd_identity.describe():
                print(f"device   : {identity.vpd_identity.describe()}")
            print(f"pages    : {', '.join(f'0x{p:02X} ({len(d)}B)' for p, d in identity.vpd.items()) or 'none'}")
            print(f"decoded  : {caps.describe()}")
            if args.hex:
                for page, data in identity.vpd.items():
                    print(f"\n--- VPD 0x{page:02X} ({len(data)} bytes) ---")
                    print(_hexdump(data))
    except (ContexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_plan(args) -> int:
    window = _window_from_args(args)
    problems = window.problems()
    window.validate(max_width_units=MAX_WIDTH_UNITS)
    block = window.to_block()

    print(f"window   : {window.describe()}")
    print(f"block    : {len(block)} bytes  {block.hex()}")
    for note in problems:
        print(f"note     : {note}")
    print()
    items = scan_plan(window, table_count=args.tables)
    for index, item in enumerate(items, 1):
        if hasattr(item, "cdb"):
            line = f"{index:3d}  {item.cdb.hex()}  {item.name:<20s}"
            from .proto import cdb as cdbmod
            line += f"  {cdbmod.describe(item.cdb)}"
            if item.data_out:
                head = item.data_out[:12].hex()
                line += f"  | {len(item.data_out)}B {head}{'…' if len(item.data_out) > 12 else ''}"
            print(line)
        else:
            print(f"{index:3d}  {'—':32s}  {item.name:<20s}  (poll: {item.kind})")
    print(f"\n{len(items)} steps before the image channel is opened.")
    return 0


def cmd_scan(args) -> int:
    window = _window_from_args(args)
    session = _connect_session(args)
    started = time.time()
    prefix = args.out or f"scan-{time.strftime('%m%d-%H%M')}"
    try:
        with session:
            result = session.scan(window)
    except (ContexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        # A failed scan is still evidence: keep the command transcript.
        with open(f"{prefix}.failed.jsonl", "w", encoding="utf-8") as fh:
            for entry in session.transcript:
                fh.write(json.dumps(entry) + "\n")
        print(f"transcript: {prefix}.failed.jsonl", file=sys.stderr)
        return 1

    prefix = args.out or f"scan-{time.strftime('%m%d-%H%M')}"
    if result.stream:
        with open(f"{prefix}.bin", "wb") as fh:
            fh.write(result.stream)
    with open(f"{prefix}.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "window", "block": window.to_block().hex(),
                             "describe": window.describe()}) + "\n")
        for entry in session.transcript:
            fh.write(json.dumps(entry) + "\n")

    print(result.summary())
    if result.stream:
        print(f"wrote {prefix}.bin ({len(result.stream)} B)")
        line_bytes, score = _try_line_length(result.stream)
        if line_bytes:
            rows = len(result.stream) // line_bytes
            print(f"line length {line_bytes} B ({line_bytes // 3} px/row), "
                  f"autocorr {score:.4f}, {rows} rows")
    print(f"wrote {prefix}.jsonl ({len(session.transcript)} entries)")
    print(f"elapsed {time.time() - started:.1f}s")
    return 0 if result.complete else 2


def _try_line_length(stream: bytes):
    try:
        from .frame import detect_line_bytes
        return detect_line_bytes(stream)
    except Exception as exc:                       # numpy missing, too short, etc.
        print(f"note: line length not detected ({exc})", file=sys.stderr)
        return 0, 0.0


def cmd_param(args) -> int:
    # --dir defaults to the 0xA0 selector register; a block read defaults to 0xBF.
    buffer = args.buffer if args.buffer is not None else (0xA0 if args.dir else 0xBF)
    session = _connect_session(args)
    try:
        with session:
            if args.dir:
                values = session.read_block_directory(buffer, args.count)
            else:
                data = session.read_buffer(buffer, args.offset, args.length,
                                           mode=args.mode)
    except (ContexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dir:
        print(f"selectors on buffer 0x{buffer:02x} "
              f"(4-byte values, 'len' is the implied block length):")
        for selector, value in enumerate(values):
            text = f"0x{value:08x}  ({value})" if value >= 0 else "read failed"
            print(f"  {selector:3d}  {text}")
        return 0

    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(data)
        print(f"wrote {args.out} ({len(data)} B)")
    else:
        print(f"buffer 0x{buffer:02x} offset {args.offset} "
              f"length {args.length}: {len(data)} B")
        print(_hexdump(data))
        runs = []
        current = b""
        start = 0
        for index, byte in enumerate(data):
            if 32 <= byte < 127:
                if not current:
                    start = index
                current += bytes([byte])
            else:
                if len(current) >= 4:
                    runs.append((start, current.decode("latin-1")))
                current = b""
        if len(current) >= 4:
            runs.append((start, current.decode("latin-1")))
        for offset, text in runs:
            print(f"  ascii @{offset:03d}: {text!r}")
    return 0 if data else 1


def cmd_pcap(args) -> int:
    stats: dict = {}
    try:
        datagrams = pcapmod.udp_datagrams(args.capture, stats=stats)
        records = pcapmod.records(args.capture)
        stream, rx = pcapmod.image_stream(args.capture)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"datagrams : {stats.get('datagrams', 0)} "
          f"({stats.get('incomplete_groups', 0)} incomplete fragment groups)")
    sizes = sorted({len(d) for d in datagrams}) if datagrams else []
    if sizes:
        print(f"sizes     : {sizes[:4]}{' …' if len(sizes) > 4 else ''}")
    print(f"records   : {len(records)}")
    print(f"stream    : {len(stream)} B  "
          f"records={rx.get('data_records', 0)} gaps={rx.get('gaps', 0)} "
          f"missing={rx.get('missing_records', 0)} dup={rx.get('duplicates', 0)}")
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(stream)
        print(f"wrote {args.out}")

    if args.png:
        from .frame import detect_line_bytes, to_image, write_png
        line_bytes = args.line_bytes or 0
        score = float("nan")
        if not line_bytes:
            line_bytes, score = detect_line_bytes(stream)
            if score < 0.99:
                print(f"warning: detected line length {line_bytes} scores only "
                      f"{score:.3f} (expect >= 0.99); framing is probably wrong — "
                      "pass --line-bytes if it is known", file=sys.stderr)
        image = to_image(stream, line_bytes, roll=args.roll)
        write_png(args.png, image)
        print(f"wrote {args.png} ({image.shape[1]}x{image.shape[0]}, "
              f"line {line_bytes} B, autocorr {score:.4f})")
    return 0 if rx.get("missing_records", 0) == 0 else 2


def cmd_app(args) -> int:
    from .server import run_app_server
    run_app_server(
        host=args.bind,
        port=args.http_port,
        scanner_ip=args.ip,
        scanner_port=args.port,
    )
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run_mcp_server
    run_mcp_server(transport=args.transport)
    return 0


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx", description="Contex HD Ultra comms layer",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--version", action="version", version=f"ctx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_device_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ip", default="0.0.0.0", help="scanner address (or discover via `ctx discover`)")
        p.add_argument("--port", type=int, default=58170, help="command port")
        p.add_argument("--quiet", type=float, default=1.0,
                       help="seconds of silence that ends a reply")
        p.add_argument("--tables", type=int, default=5,
                       help="write-buffer 0x95 tables to send (working value: 5)")

    def add_window_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dpi", type=int, default=200)
        p.add_argument("--width-in", type=float, default=42.0)
        p.add_argument("--length-in", type=float, default=None,
                       help="default: 0 = auto to end of media")
        p.add_argument("--colour", dest="colour", action="store_true", default=True)
        p.add_argument("--grey", dest="colour", action="store_false")

    d = sub.add_parser("discover", help="broadcast discovery")
    d.add_argument("--window-ms", type=int, default=1001)
    d.add_argument("--broadcast", default="255.255.255.255")
    d.add_argument("--hostname", default=None,
                   help="hostname to put in the probe (default: this host)")
    d.add_argument("--exclude", action="append", default=[],
                   help="IP to ignore, e.g. a proxy on this host")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_discover)

    i = sub.add_parser("info", help="read-only identify and capability pages (M1)")
    add_device_options(i)
    i.add_argument("--hex", action="store_true", help="dump raw VPD pages")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_info)

    p = sub.add_parser("plan", help="print the command sequence without sending it")
    add_window_options(p)
    add_device_options(p)
    p.set_defaults(func=cmd_plan)

    s = sub.add_parser("scan", help="run one scan (M2/M3)")
    add_window_options(s)
    add_device_options(s)
    s.add_argument("--out", default=None, help="output prefix (default: scan-MMDD-HHMM)")
    s.set_defaults(func=cmd_scan)

    m = sub.add_parser("param", help="READ BUFFER dump (M4)")
    add_device_options(m)
    m.add_argument("--buffer", type=lambda x: int(x, 0), default=None,
                   help="buffer id (default 0xBF, or 0xA0 with --dir)")
    m.add_argument("--offset", type=lambda x: int(x, 0), default=0,
                   help="byte offset; 0xA0 is selector-indexed, use --dir")
    m.add_argument("--length", type=lambda x: int(x, 0), default=0x91)
    m.add_argument("--mode", type=lambda x: int(x, 0), default=1)
    m.add_argument("--dir", action="store_true",
                   help="read the 0xA0 selector table instead of a block")
    m.add_argument("--count", type=int, default=16,
                   help="selectors to read with --dir")
    m.add_argument("--out", default=None)
    m.set_defaults(func=cmd_param)

    c = sub.add_parser("pcap", help="reassemble a capture offline")
    c.add_argument("capture")
    c.add_argument("--out", default=None, help="write the image stream here")
    c.add_argument("--png", default=None, help="also write a PNG")
    c.add_argument("--line-bytes", type=int, default=0,
                   help="row length; 0 = detect by autocorrelation")
    c.add_argument("--roll", type=int, default=0,
                   help="undo the constant per-row roll (SPECS 4.2)")
    c.set_defaults(func=cmd_pcap)

    a = sub.add_parser("app", help="launch the Contex Studio WebUI & REST server")
    add_device_options(a)
    a.add_argument("--bind", default="127.0.0.1", help="HTTP bind address")
    a.add_argument("--http-port", type=int, default=8080, help="HTTP server port")
    a.set_defaults(func=cmd_app)

    mcp_p = sub.add_parser("mcp", help="launch the Model Context Protocol (MCP) server")
    mcp_p.add_argument("--transport", default="stdio", choices=["stdio", "sse"],
                       help="MCP transport mode")
    mcp_p.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
