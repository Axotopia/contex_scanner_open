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


def _print_stitch_progress(m: dict, dpi: int) -> None:
    """Print band or seam progress; the two stages report different keys."""
    if "band" in m:
        total = m["rows_total"] / float(dpi)
        print(f"  band {m['band']:3d}  {m['inches_done']:8.1f} / {total:.1f} in  ({m['elapsed_s']}s)")
    elif "join_x" in m:
        print(f"  seam x={m['join_x']:5d}: rgb offset {m.get('delta_rgb')} "
              f"(ramp ±{m.get('window_px')} px)")


def cmd_longscan(args) -> int:
    """Capture a long sheet without holding it in RAM, then stitch it in bands.

    Peak memory is one band, not the whole sheet: the wire stream goes straight
    to a spool file and ``ctx.banded`` processes it in row bands.
    """
    from pathlib import Path

    from .banded import load_preset, process_spool_banded, rec_to_spool, spool_to_rec
    from .engine import ScannerEngine

    preset = load_preset(args.preset)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{args.prefix}_{time.strftime('%y%m%d%H%M')}"
    spool = Path(args.spool).expanduser() if args.spool else out_dir / f"{base}.spool"

    if args.from_rec:
        print(f"converting {args.from_rec} -> {spool}")
        info = rec_to_spool(args.from_rec, spool)
        print(f"  {info['records']} records, {info['rows']} rows "
              f"({info['rows'] / float(args.dpi):.1f} in @ {args.dpi} dpi)")
    else:
        engine = ScannerEngine(host=args.ip, port=args.port)
        status = engine.probe_status(timeout=2.0)
        if not status.get("connected"):
            print(f"error: scanner unreachable at {args.ip}: {status.get('error')}",
                  file=sys.stderr)
            return 1
        length = float(args.length_in) if args.length_in and args.length_in > 0 else None
        where = "auto length (to end of media)" if length is None else f"{length:.1f} in"
        if args.recover:
            print("WARNING: --recover is on. If the media is mid-path the app will run "
                  "Unload -> Load at full roller speed; long sheets can skew and tear.\n"
                  "         Prefer recovering the sheet by hand and re-running without it.")
        if not status.get("ready"):
            print(f"error: media is not at the load position ({status.get('media_state')}). "
                  "Recover the sheet BY HAND, then start again. Nothing was moved.",
                  file=sys.stderr)
            return 1
        print(f"scanning {args.width_in:g} in wide @ {args.dpi} dpi, {where}")
        print(f"  spooling to {spool}")

        def _progress(stage: str, info: dict) -> None:
            if stage == "streaming":
                n = int(info.get("records", 0) or 0)
                if n and n % 100_000 < 4_000:
                    print(f"  streaming: {n:,} records ({info.get('elapsed_s')}s, "
                          f"{n * 2960 / 1e9:.2f} GB)")
            elif stage == "recovering_media":
                print("  recovering media (Unload -> Load)")

        cap = engine.run_live_scan(
            width_inches=args.width_in,
            dpi=args.dpi,
            length_inches=length,
            seconds=args.seconds,
            recover=bool(args.recover),
            post_scan_eject="hold",
            spool_path=str(spool),
            on_progress=_progress,
        )
        print(f"  captured {cap.rows:,} rows ({cap.rows / float(args.dpi):.1f} in), "
              f"{cap.records_count:,} records, holes={cap.holes}, {cap.duration_seconds}s")
        if cap.rows < 8:
            print("error: no usable rows captured", file=sys.stderr)
            return 1

    print(f"stitching in bands of {args.band_rows} rows "
          f"(cross-fade {args.overlap}) ...")
    manifest = process_spool_banded(
        spool,
        out_dir,
        preset=preset,
        width_inches=args.width_in,
        dpi=args.dpi,
        band_rows=args.band_rows,
        overlap_rows=args.overlap,
        jpg_quality=args.jpg_quality,
        part_rows=args.part_rows,
        out_format=args.out_format,
        single_file=args.single_file,
        seam_equalize=args.seam_equalize,
        seam_window_px=args.seam_window_px,
        base_name=base,
        progress=lambda m: _print_stitch_progress(m, args.dpi),
    )
    print(f"\n{manifest['rows']:,} rows = {manifest['inches']:.1f} in @ "
          f"{manifest['dpi']} dpi, {manifest['document_width_px']:,} px wide, "
          f"{manifest['bands']} bands, preset {manifest['preset_id']}")
    for part in manifest["parts"]:
        print(f"  part {part['part']}: {part['path']}")
        print(f"           {part['inches_start']:.1f}-{part['inches_end']:.1f} in, "
              f"{part['px'][0]}x{part['px'][1]} px, {part['size_bytes'] / 1e6:.1f} MB")
    print(f"  manifest: {manifest['metadata_path']}")
    if not args.from_rec:
        # Keep the capture: a .rec can be reloaded by the app or reprocessed with
        # `longscan --from-rec`, so a 20 ft sheet never has to be fed twice.
        if args.keep_capture:
            rec_path = out_dir / "captures" / f"{base}.rec"
            print(f"writing capture {rec_path} ...", flush=True)
            cap = spool_to_rec(
                spool, rec_path,
                progress=lambda m: print(
                    f"  capture {m['bytes_done'] / 1e9:.2f} / {m['bytes_total'] / 1e9:.2f} GB",
                    flush=True),
            )
            print(f"  capture: {cap['rec_path']}  {cap['rec_bytes'] / 1e9:.2f} GB, "
                  f"{cap['records']:,} records, {cap['rows']:,} rows")
    # The working spool is temporary whether it came from the scanner or was
    # rebuilt from a .rec; leaving it behind costs ~5.6 GB per reprocess.
    if not args.keep_spool:
        try:
            spool.unlink()
            print("  removed working spool")
        except OSError:
            pass
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

    ls = sub.add_parser(
        "longscan",
        help="memory-bounded capture of a long sheet (spool to disk, stitch in bands)",
    )
    add_device_options(ls)
    ls.add_argument("--dpi", type=int, default=200)
    ls.add_argument("--width-in", type=float, default=42.0)
    ls.add_argument("--length-in", type=float, default=0.0,
                    help="inches to capture; 0 = auto to end of media")
    ls.add_argument("--seconds", type=float, default=1800.0,
                    help="host read window; the stream ends early on end of media")
    ls.add_argument("--band-rows", type=int, default=4000,
                    help="rows per band (4000 = 20 in at 200 dpi)")
    ls.add_argument("--overlap", type=int, default=200,
                    help="rows cross-faded at each band join")
    ls.add_argument("--out", default="~/Pictures", help="output directory")
    ls.add_argument("--prefix", default="longscan", help="output file prefix")
    ls.add_argument("--preset", default=None, help="preset id (default: the app's active one)")
    ls.add_argument("--jpg-quality", type=int, default=95)
    ls.add_argument("--no-seam-equalize", dest="seam_equalize", action="store_false",
                    help="skip the global camera-seam tone equalisation")
    ls.add_argument("--seam-window-px", type=int, default=256,
                    help="half-width of the seam correction ramp, in document pixels")
    ls.add_argument("--format", dest="out_format", default="jpg", choices=["jpg", "tiff"],
                    help="jpg (split above part-rows) or tiff (always one file)")
    ls.add_argument("--single-file", action="store_true",
                    help="one output file; errors if a JPEG would exceed the 65,535-row limit")
    ls.add_argument("--part-rows", type=int, default=60000,
                    help="max rows per JPEG part (JPEG limit is 65,535; lower it for "
                         "wide sheets, where the encode buffer is rows x width x 3)")
    ls.add_argument("--from-rec", default=None,
                    help="process an existing .rec capture instead of scanning")
    ls.add_argument("--recover", action="store_true",
                    help="DANGEROUS: let the app run Unload -> Load if media is mid-path; "
                         "it feeds at full roller speed and can tear long sheets. "
                         "Default is to stop and ask you to recover by hand.")
    ls.add_argument("--spool", default=None,
                    help="spool file path (default: alongside the output)")
    ls.add_argument("--no-capture", dest="keep_capture", action="store_false",
                    help="do NOT keep a .rec capture (default is to keep one, so the "
                         "sheet never has to be fed twice)")
    ls.add_argument("--keep-spool", action="store_true",
                    help="also keep the raw working spool (rarely needed)")
    ls.set_defaults(func=cmd_longscan)

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
