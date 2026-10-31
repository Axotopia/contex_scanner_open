"""Unified hardware and transport controller for the Contex HD Ultra i4250s.

Integrates the proven 32-command 16-bit 5-camera arming sequence, UDP flow
control, and paper recovery cycle from ``tools/cap/replay_vendor.py`` with
the safety guardrails of ``ctx.proto.cdb`` (``DESIGN.md`` §1, ``SPECS.md`` §7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform
import socket
import struct
import threading
import time
from typing import Any, Callable

from .calibration import (
    DEFAULT_SERIAL,
    STRIDE_16BIT_5CAM,
    load_rec_concat,
    read_file_chunked,
    to_raster_16bit,
)
from .net import discovery
from .proto import cdb as proto_cdb

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCANNER_IP = "0.0.0.0"
COMMAND_PORT = 58170
IMAGE_PORT = 58170
DISCOVERY_PORT = 58169
MAGIC_SCSISENSE = b"SCSISENSE"
CAPTURED_TOKEN = bytes.fromhex("60dd8e2271cf")
HANDSHAKE_BODY = bytes.fromhex("0000c0000000")

STATUS_TAGS: dict[int, str] = {
    0x00: "no media",
    0x10: "ejected",
    0x11: "inserted",
    0x20: "busy loading",
    0x21: "busy moving",
    0x22: "busy positioning",
    0x23: "ejecting",
    0x24: "SCANNING",
    0x30: "READY",
    0x31: "ready moved",
}


def _pack_udp_header(seq: int, rec_type: int, dlen: int = 0, ai0: int = 110) -> bytes:
    return struct.pack("<HIHHHHH", 0xFFFF, seq & 0xFFFFFFFF, rec_type, dlen, ai0, 0, 0)


def _build_vendor_cdb(
    op: int,
    *,
    b1: int = 0,
    b2: int = 0,
    b3: int = 0,
    b4: int = 0,
    b5: int = 0,
    b7: int = 0,
    b8: int = 0,
    token: bool = False,
) -> bytes:
    """Build a 16-byte CDB matching the ICD byte layout and enforce flash safety."""
    c = bytearray(16)
    c[0] = op
    c[1] = b1
    c[2] = b2
    c[3] = b3
    c[4] = b4
    c[5] = b5
    c[7] = b7
    c[8] = b8
    if token:
        c[10:16] = CAPTURED_TOKEN
    # Hard safety rule (DESIGN.md §1): never emit WRITE BUFFER 0xA0 or flash commit modes
    proto_cdb.assert_safe(bytes(c))
    return bytes(c)


def check_network_preflight(scanner_ip: str = DEFAULT_SCANNER_IP) -> dict[str, Any]:
    """Check host network interface speed (1 Gbps requirement from FRESH-EYES-VERDICT.md §1)
    and UDP socket receive buffer allocation."""
    warnings: list[str] = []
    iface_speeds: dict[str, int] = {}

    if platform.system().lower() == "linux":
        net_dir = Path("/sys/class/net")
        if net_dir.exists():
            for iface_path in sorted(net_dir.iterdir()):
                if iface_path.name == "lo":
                    continue
                speed_file = iface_path / "speed"
                oper_file = iface_path / "operstate"
                try:
                    oper = oper_file.read_text(encoding="utf-8").strip() if oper_file.exists() else "unknown"
                    if oper == "up" and speed_file.exists():
                        spd = int(speed_file.read_text(encoding="utf-8").strip())
                        if spd > 0:
                            iface_speeds[iface_path.name] = spd
                            if spd < 1000:
                                warnings.append(
                                    f"Interface {iface_path.name} is negotiated at {spd} Mbps (< 1000 Mbps). "
                                    "A 100 Mbps link causes ~26% switch egress UDP packet drops during 16-bit 5-camera scans."
                                )
                except Exception:
                    continue

    # Test UDP receive buffer allocation (target 64 MB)
    actual_rcvbuf = 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 << 20)
        actual_rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        sock.close()
    except Exception:
        pass

    return {
        "scanner_ip": scanner_ip,
        "platform": platform.system(),
        "interfaces_mbps": iface_speeds,
        "gigabit_ok": all(v >= 1000 for v in iface_speeds.values()) if iface_speeds else True,
        "udp_rcvbuf_bytes": actual_rcvbuf,
        "warnings": warnings,
    }


@dataclass
class RawScanCapture:
    """Raw output of a live or offline scan before calibration/stitching."""

    source: str
    width_inches: float
    dpi: int
    records_count: int
    holes: int
    payload_bytes: int
    rows: int
    duration_seconds: float
    raw_rec_bytes: bytes
    transcript: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False


class ScannerEngine:
    """Thread-safe controller for the Contex HD Ultra i4250s."""

    def __init__(self, host: str = DEFAULT_SCANNER_IP, port: int = COMMAND_PORT):
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self.last_status: dict[str, Any] = {
            "connected": False,
            "media_code": None,
            "media_state": "unknown",
            "serial": DEFAULT_SERIAL,
            "model": "HD Ultra i4250s",
        }

    def discover(self, timeout_ms: int = 900) -> list[dict[str, Any]]:
        try:
            found = discovery.discover(window_ms=timeout_ms)
            results = [f.__dict__ for f in found]
            if results:
                first = results[0]
                if first.get("ip"):
                    self.host = str(first["ip"])
                if first.get("serial"):
                    self.last_status["serial"] = str(first["serial"])
            return results
        except Exception:
            return []

    def _transact_tcp(
        self,
        sock: socket.socket,
        cdb_bytes: bytes,
        data_out: bytes = b"",
        quiet: float = 0.35,
    ) -> tuple[int, int, int, int, bytes] | None:
        proto_cdb.assert_safe(cdb_bytes)
        sock.sendall(cdb_bytes + data_out if data_out else cdb_bytes)
        buf = b""
        last = time.time()
        sock.settimeout(0.05)
        while time.time() - last < quiet:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            last = time.time()
        offs = [i for i in range(len(buf)) if buf.startswith(MAGIC_SCSISENSE, i)]
        if not offs:
            return None
        nxt = offs[1] if len(offs) > 1 else len(buf)
        r = buf[offs[0] : offs[0] + 28]
        return (r[9], r[12] & 0x0F, r[22], r[23], buf[offs[0] + 28 : nxt])

    def probe_status(self, timeout: float = 2.5) -> dict[str, Any]:
        """Connect briefly to TCP 58170 and read media status ``READ 0x80/2``."""
        preflight = check_network_preflight(self.host)
        with self._lock:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout)
                    s.connect((self.host, self.port))
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    r = self._transact_tcp(
                        s,
                        _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True),
                        quiet=0.35,
                    )
                pl = r[4] if r else b""
                code = pl[1] if len(pl) >= 2 else None
                tag = STATUS_TAGS.get(code, "unknown") if code is not None else "no reply"
                self.last_status.update({
                    "connected": True,
                    "host": self.host,
                    "port": self.port,
                    "media_code": code,
                    "media_hex": f"0x{code:02x}" if code is not None else None,
                    "media_state": tag,
                    "ready": code in (0x30, 0x31),
                    "preflight": preflight,
                })
            except Exception as exc:
                self.last_status.update({
                    "connected": False,
                    "host": self.host,
                    "port": self.port,
                    "media_code": None,
                    "media_hex": None,
                    "media_state": "offline (simulation ready)",
                    "ready": False,
                    "error": str(exc),
                    "preflight": preflight,
                })
            return dict(self.last_status)

    def paper_control(self, action: str = "load") -> dict[str, Any]:
        """Execute paper movement commands (SPECS.md §2.7):
          - ``"load"``: ``OBJECT POSITION`` function 1, poll to ``0x30 READY``
          - ``"unload"``: ``OBJECT POSITION`` function 0, count 0
          - ``"eject_back"``: ``OBJECT POSITION`` function 0, count ``0xFFFFFF``
          - ``"eject_front"``: ``OBJECT POSITION`` function 0, count ``0xFFFFFE``
          - ``"recover"``: Unload -> wait for ``0x10``/``0x00`` -> Load -> wait for ``0x30``
        """
        action = action.lower().strip()
        with self._lock:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((self.host, self.port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                self._transact_tcp(s, _build_vendor_cdb(0x16, token=True), quiet=0.35)
                if action == "load":
                    self._transact_tcp(s, _build_vendor_cdb(0x31, b1=0x01, token=True), quiet=0.5)
                    ok = self._poll_ready_socket(s, timeout=35.0)
                    return {"action": action, "ok": ok, "status": self.last_status.get("media_state")}

                if action == "unload":
                    self._transact_tcp(s, _build_vendor_cdb(0x31, b1=0x00, token=True), quiet=0.5)
                    return {"action": action, "ok": True}

                if action == "eject_back":
                    cdb = _build_vendor_cdb(0x31, b1=0x00, b2=0xFF, b3=0xFF, b4=0xFF, token=True)
                    self._transact_tcp(s, cdb, quiet=0.5)
                    return {"action": action, "ok": True}

                if action == "eject_front":
                    cdb = _build_vendor_cdb(0x31, b1=0x00, b2=0xFF, b3=0xFF, b4=0xFE, token=True)
                    self._transact_tcp(s, cdb, quiet=0.5)
                    return {"action": action, "ok": True}

                if action == "recover":
                    self._transact_tcp(s, _build_vendor_cdb(0x31, b1=0x00, token=True), quiet=0.5)
                    t_end = time.time() + 25.0
                    while time.time() < t_end:
                        rr = self._transact_tcp(s, _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True), quiet=0.4)
                        pl = rr[4] if rr else b""
                        code = pl[1] if len(pl) >= 2 else None
                        if code in (0x10, 0x00):
                            break
                        time.sleep(0.5)
                    self._transact_tcp(s, _build_vendor_cdb(0x31, b1=0x01, token=True), quiet=0.5)
                    ok = self._poll_ready_socket(s, timeout=35.0)
                    return {"action": action, "ok": ok, "status": self.last_status.get("media_state")}

                raise ValueError(f"Unsupported paper action {action!r}")
            finally:
                try:
                    self._transact_tcp(s, _build_vendor_cdb(0x17, token=True), quiet=0.25)
                except Exception:
                    pass
                s.close()

    def _poll_ready_socket(self, sock: socket.socket, timeout: float = 35.0, interval: float = 0.6) -> bool:
        t_end = time.time() + timeout
        while time.time() < t_end:
            r = self._transact_tcp(sock, _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True), quiet=0.4)
            pl = r[4] if r else b""
            code = pl[1] if len(pl) >= 2 else None
            if code is not None:
                self.last_status["media_code"] = code
                self.last_status["media_state"] = STATUS_TAGS.get(code, "unknown")
            if code in (0x30, 0x31):
                return True
            time.sleep(interval)
        return False

    def build_oem_arming_sequence(
        self,
        width_inches: float = 42.0,
        dpi: int = 300,
        length_inches: float | None = None,
    ) -> list[tuple[str, bytes, bytes]]:
        """Build the exact 32-command OEM arming sequence from ``replay_vendor.py``,
        patching ``dpi`` (offsets 10..14), ``width`` (offsets 22..26), and ``length`` (offsets 26..30)."""
        oem = json.loads((REPO_ROOT / "ctx" / "data" / "oem_recipe.json").read_text(encoding="utf-8"))
        cap = json.loads((REPO_ROOT / "ctx" / "data" / "vendor-scan-1017-params.json").read_text(encoding="utf-8"))
        win_ba = bytearray(bytes.fromhex(cap["window"]))

        units = int(round(float(width_inches) * 1200))
        if not 0 < units <= 50400:
            raise ValueError("width_inches must be within (0, 42.0]")
        dpi_val = int(dpi)
        if dpi_val not in (100, 200, 300, 400, 600, 1200):
            raise ValueError(f"Unsupported DPI {dpi_val}; expected one of (100, 200, 300, 400, 600, 1200)")
        len_units = 0 if (length_inches is None or length_inches <= 0) else int(round(float(length_inches) * 1200))

        struct.pack_into(">H", win_ba, 10, dpi_val)
        struct.pack_into(">H", win_ba, 12, dpi_val)
        struct.pack_into(">I", win_ba, 22, units)
        struct.pack_into(">I", win_ba, 26, len_units)
        window = bytes(win_ba)
        table = bytes.fromhex(cap["tables"][0])

        w48 = bytes.fromhex(oem["write_buffer_0x48"])
        wa2 = bytes.fromhex(oem["write_buffer_0xa2"])
        s84 = bytes.fromhex(oem["send_0x84"])

        seq: list[tuple[str, bytes, bytes]] = [
            ("WB 0x48 #1", w48[:16], w48[16:]),
            ("READ BUF 0xA0@0f", _build_vendor_cdb(0x3C, b1=0x01, b2=0xA0, b5=0x0F, b8=0x04, token=True), b""),
            ("READ 0x82", _build_vendor_cdb(0x28, b2=0x82, b8=121), b""),
            ("READ 0x84 a", _build_vendor_cdb(0x28, b2=0x84, b8=4), b""),
            ("READ 0x84 b", _build_vendor_cdb(0x28, b2=0x84, b8=4), b""),
            ("READ 0x84 c", _build_vendor_cdb(0x28, b2=0x84, b8=4), b""),
            ("READ 0x84 d", _build_vendor_cdb(0x28, b2=0x84, b8=4), b""),
            ("SEND 0x84", s84[:16], s84[16:]),
            ("WB 0x48 #2", w48[:16], w48[16:]),
            ("READ 0x80/2 pre", _build_vendor_cdb(0x28, b2=0x80, b8=2), b""),
            ("RECV DIAG #1", _build_vendor_cdb(0x1C, b3=0x00, b4=0x04), b""),
            ("RESERVE", _build_vendor_cdb(0x16, token=True), b""),
            ("OBJ POS LOAD", _build_vendor_cdb(0x31, b1=0x01, token=True), b""),
            ("READ BUF 0xA0@0f", _build_vendor_cdb(0x3C, b1=0x01, b2=0xA0, b5=0x0F, b8=0x04, token=True), b""),
            ("READ 0x92", _build_vendor_cdb(0x28, b2=0x92, b8=255), b""),
            ("READ BUF 0xBF", _build_vendor_cdb(0x3C, b1=0x01, b2=0xBF, b7=0x00, b8=0x91, token=True), b""),
            ("READ 0x80/78", _build_vendor_cdb(0x28, b2=0x80, b8=78, token=True), b""),
            ("READ 0x88", _build_vendor_cdb(0x28, b2=0x88, b8=64), b""),
            ("READ 0xFD", _build_vendor_cdb(0x28, b2=0xFD, b5=0x05, b8=32, token=True), b""),
            ("RECV DIAG #2", _build_vendor_cdb(0x1C, b4=0x04, token=True), b""),
            ("READ 0x80/255", _build_vendor_cdb(0x28, b2=0x80, b8=255, token=True), b""),
            ("READ 0x80/2", _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True), b""),
            ("SET WINDOW", _build_vendor_cdb(0x24, b8=len(window), token=True), window),
            ("RECV DIAG #3", _build_vendor_cdb(0x1C, b3=0x00, b4=0x04, token=True), b""),
            ("READ 0x80/2", _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True), b""),
            ("WB 0xA2", wa2[:16], wa2[16:]),
        ]
        for i in range(5):
            seq.append((
                f"WB 0x95 #{i + 1}",
                _build_vendor_cdb(0x3B, b1=0x01, b2=0x95, b7=0x02, b8=0xB5, token=True),
                table,
            ))
        seq.append(("SCAN 0x1b", _build_vendor_cdb(0x1B, b4=0x01, token=True), b"\x00"))
        seq.append(("READ 0xFF/0x02", _build_vendor_cdb(0x28, b2=0xFF, b5=0x02, b8=10, token=True), b""))
        return seq

    def run_live_scan(
        self,
        *,
        width_inches: float = 42.0,
        dpi: int = 300,
        length_inches: float | None = None,
        seconds: float = 25.0,
        recover: bool = True,
        ack_every: int = 1,
        post_scan_eject: str = "hold",
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> RawScanCapture:
        """Execute a live hardware scan using the proven ``replay_vendor.py`` protocol.

        ``cancel_event`` is checked while arming and while collecting the stream.
        Cancelling stops host reception and releases the command socket; it does
        not command the paper motion (the scanner may finish feeding the sheet).
        """
        def _notify(stage: str, info: dict[str, Any]) -> None:
            if on_progress is not None:
                try:
                    on_progress(stage, info)
                except Exception:
                    pass

        seq = self.build_oem_arming_sequence(
            width_inches=width_inches,
            dpi=dpi,
            length_inches=length_inches,
        )
        transcript: list[dict[str, Any]] = []
        cancelled = False
        t0 = time.time()

        with self._lock:
            _notify("connecting", {"host": self.host, "port": self.port})
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect((self.host, self.port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            local_port = s.getsockname()[1]

            data_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            data_udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            data_udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 << 20)
            data_udp.bind(("", IMAGE_PORT))
            data_udp.settimeout(0.2)

            ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                ctrl.bind(("", local_port))
            except OSError:
                pass

            frames: dict[int, bytes] = {}
            dlens: dict[int, int] = {}
            got: dict[str, int] = {"n": 0, "acks": 0, "naks": 0, "credits": 0}
            stop_ev = threading.Event()

            def _rx_worker() -> None:
                expect = 0
                last_nak_t = 0.0
                while not stop_ev.is_set():
                    try:
                        d, _ = data_udp.recvfrom(65536)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if len(d) < 16 or d[:2] != b"\xff\xff":
                        continue
                    _, sq, ty, dl, _ai0, _a1, _a2 = struct.unpack_from("<HIHHHHH", d, 0)
                    got[f"type{ty}"] = got.get(f"type{ty}", 0) + 1
                    if ty != 1:
                        continue
                    if sq not in frames:
                        frames[sq] = d[16:]
                        dlens[sq] = dl
                        got["n"] += 1
                    got["maxseen"] = max(got.get("maxseen", 0), sq)
                    while expect in frames:
                        expect += 1
                    got["expect"] = expect
                    if sq - got.get("last_ack", -(10**9)) >= max(1, ack_every):
                        try:
                            ctrl.sendto(_pack_udp_header(sq, 3, ai0=110), (self.host, self.port))
                            got["acks"] += 1
                            got["last_ack"] = sq
                        except OSError:
                            pass
                    now_t = time.time()
                    if expect < got.get("maxseen", 0) and now_t - last_nak_t > 0.1:
                        try:
                            ctrl.sendto(_pack_udp_header(expect, 4, ai0=110), (self.host, self.port))
                            got["naks"] += 1
                            last_nak_t = now_t
                        except OSError:
                            pass
                    if got["n"] % 40 == 0:
                        try:
                            ctrl.sendto(_pack_udp_header(0, 5, ai0=110), (self.host, self.port))
                            got["credits"] += 1
                        except OSError:
                            pass

            rx_thread = threading.Thread(target=_rx_worker, daemon=True)
            rx_thread.start()

            try:
                # 1. Media check & recovery
                pre = self._transact_tcp(s, _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True), quiet=0.4)
                already_ready = bool(pre and len(pre[4]) >= 2 and pre[4][1] == 0x30)
                if recover and not already_ready:
                    _notify("recovering_media", {"message": "Running Unload -> Load recovery cycle"})
                    self._transact_tcp(s, _build_vendor_cdb(0x31, b1=0x00, token=True), quiet=0.5)
                    t_end = time.time() + 25.0
                    while time.time() < t_end:
                        rr = self._transact_tcp(s, _build_vendor_cdb(0x28, b2=0x80, b8=2, token=True), quiet=0.4)
                        pl = rr[4] if rr else b""
                        code = pl[1] if len(pl) >= 2 else None
                        if code in (0x10, 0x00):
                            break
                        time.sleep(0.5)
                    self._transact_tcp(s, _build_vendor_cdb(0x31, b1=0x01, token=True), quiet=0.5)
                    self._poll_ready_socket(s, timeout=35.0)

                # 2. Execute 32-command OEM arming sequence
                _notify("arming", {"commands": len(seq), "total": len(seq)})
                for idx, (label, c_, d_out) in enumerate(seq, start=1):
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        _notify("cancelled", {"phase": "arming"})
                        break
                    if label.startswith("SCAN"):
                        data_udp.sendto(_pack_udp_header(0, 7, 0, ai0=110), (self.host, self.port))
                        time.sleep(0.01)
                        data_udp.sendto(_pack_udp_header(0, 9, 0, ai0=110), (self.host, self.port))
                        time.sleep(0.5)  # pre-roll drain
                    reply = self._transact_tcp(
                        s,
                        c_,
                        d_out,
                        quiet=2.0 if label.startswith("READ 0xFF") else 0.35,
                    )
                    transcript.append({
                        "t": round(time.time() - t0, 3),
                        "label": label,
                        "cdb": c_.hex(),
                        "status": reply[0] if reply else None,
                        "asc": reply[2] if reply else None,
                    })
                    _notify("arming", {"index": idx, "total": len(seq), "command": label})
                    if label == "OBJ POS LOAD":
                        if not self._poll_ready_socket(s, timeout=35.0):
                            raise RuntimeError("No media detected at scan position after LOAD")

                # 3. Credit pump during stream collection
                def _credit_pump() -> None:
                    while not stop_ev.is_set():
                        for _ in range(2):
                            try:
                                ctrl.sendto(_pack_udp_header(0, 5, ai0=110), (self.host, self.port))
                                got["credits"] += 1
                            except OSError:
                                return
                            time.sleep(0.005)
                        time.sleep(0.05)

                threading.Thread(target=_credit_pump, daemon=True).start()

                # 4. Collect image stream
                if not cancelled:
                    t_stream = time.time()
                    idle_since = t_stream
                    last_count = 0
                    while time.time() - t_stream < seconds:
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            _notify("cancelled", {"phase": "streaming"})
                            break
                        time.sleep(0.4)
                        cur_n = got["n"]
                        if cur_n > last_count:
                            last_count = cur_n
                            idle_since = time.time()
                        elif cur_n > 1000 and (time.time() - idle_since) > 2.5:
                            # Stream finished early
                            break
                        _notify(
                            "streaming",
                            {
                                "records": cur_n,
                                "elapsed_s": round(time.time() - t_stream, 1),
                                "acks": got["acks"],
                                "naks": got["naks"],
                            },
                        )

                stop_ev.set()

                # Post-scan optional eject (not on a cancelled scan: the host is
                # no longer reading, and the operator should recover the media).
                if not cancelled and post_scan_eject == "eject_back":
                    self._transact_tcp(
                        s,
                        _build_vendor_cdb(0x31, b1=0x00, b2=0xFF, b3=0xFF, b4=0xFF, token=True),
                        quiet=0.4,
                    )
                elif not cancelled and post_scan_eject == "eject_front":
                    self._transact_tcp(
                        s,
                        _build_vendor_cdb(0x31, b1=0x00, b2=0xFF, b3=0xFF, b4=0xFE, token=True),
                        quiet=0.4,
                    )
                self._transact_tcp(s, _build_vendor_cdb(0x17, token=True), quiet=0.3)
            finally:
                stop_ev.set()
                try:
                    s.close()
                except Exception:
                    pass
                try:
                    data_udp.close()
                except Exception:
                    pass
                try:
                    ctrl.close()
                except Exception:
                    pass

        def _eff_len(seq_k: int) -> int:
            dl = dlens.get(seq_k, 0)
            return dl if dl else len(frames[seq_k])

        sorted_seqs = sorted(frames.keys())
        holes = (sorted_seqs[-1] - sorted_seqs[0] + 1 - len(sorted_seqs)) if sorted_seqs else 0
        rec_parts: list[bytes] = []
        payload_total = 0
        for sq in sorted_seqs:
            n_b = _eff_len(sq)
            payload = frames[sq][:n_b]
            payload_total += len(payload)
            rec_parts.append(struct.pack("<IH", sq, len(payload)) + payload)

        raw_rec_bytes = b"".join(rec_parts)
        rows = payload_total // STRIDE_16BIT_5CAM
        return RawScanCapture(
            source=f"live://{self.host}",
            width_inches=width_inches,
            dpi=int(dpi),
            records_count=len(sorted_seqs),
            holes=holes,
            payload_bytes=payload_total,
            rows=rows,
            duration_seconds=round(time.time() - t0, 2),
            raw_rec_bytes=raw_rec_bytes,
            transcript=transcript,
            cancelled=cancelled,
        )

    def load_rec_file(
        self,
        rec_path: str | Path,
        width_inches: float = 42.0,
        dpi: int = 300,
    ) -> RawScanCapture:
        """Load an existing ``.rec`` capture from disk into a ``RawScanCapture``."""
        t0 = time.time()
        p = Path(rec_path)
        raw_rec_bytes = read_file_chunked(p)
        buf, info = load_rec_concat(p)
        rows = len(buf) // STRIDE_16BIT_5CAM
        return RawScanCapture(
            source=str(p),
            width_inches=width_inches,
            dpi=int(dpi),
            records_count=int(info["records"]),
            holes=int(info["holes"]),
            payload_bytes=int(info["payload_bytes"]),
            rows=rows,
            duration_seconds=round(time.time() - t0, 2),
            raw_rec_bytes=raw_rec_bytes,
            transcript=[{"kind": "offline_rec", "path": str(p), "info": info}],
        )
