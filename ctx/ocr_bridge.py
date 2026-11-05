"""MCP client for the local OCR module (``ocr_md``).

Open.NEXT does not vendor the OCR engine.  It speaks the Model Context
Protocol to a small stdio server (``python -m ocr_md.mcp_server``), which is the
same server ``ocr-pdf-watcher`` ships, so the app and the daemon always agree on
OCR, vision and naming behaviour.

The transport is newline-delimited JSON-RPC 2.0.  Only the standard library is
used: the scanner's venv does not need an MCP SDK, and the OCR server can live
in its own virtualenv.

Command resolution, first hit wins:

1. ``settings.ocr_command`` (shell-like string, e.g.
   ``"/path/venv/bin/python -m ocr_md.mcp_server"``);
2. ``OPENNEXT_OCR_COMMAND``;
3. ``OPENNEXT_OCR_PYTHON`` (or this interpreter) with the module importable;
4. a sibling ``ocr-pdf-watcher`` checkout (``.venv`` + repo root);
5. ``/opt/ocr-pdf-watcher``;
6. ``python3 -m ocr_md.mcp_server`` on ``PATH``.

Every candidate is probed once with ``python -c "import ocr_md.mcp_server"``;
failures are cached so an unavailable OCR install costs milliseconds, not a
timeout on every scan.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2026-07-28"
DEFAULT_TIMEOUT = 900.0
_PROBE_TIMEOUT = 8.0

#: Upper bound for an operator-supplied description prompt.  It rides every
#: ingest call, so a pasted document would only slow the request down.
MAX_VISION_PROMPT_CHARS = 4000


def normalize_vision_prompt(value: Any) -> str:
    """Trim a custom describe prompt and cap its size (empty means default)."""
    return str(value or "").strip()[:MAX_VISION_PROMPT_CHARS]


class OcrBridgeError(RuntimeError):
    """The OCR server could not be started, or the call failed."""


@dataclass
class OcrEndpoint:
    command: List[str]
    cwd: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT
    source: str = ""


def _split_command(value: str) -> List[str]:
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return [str(part) for part in parsed]
        return []
    return shlex.split(value)


def _probe(python: str, cwd: Optional[str]) -> bool:
    if not python:
        return False
    if os.path.sep in python and not Path(python).is_file():
        return False
    try:
        completed = subprocess.run(
            [python, "-c", "import ocr_md.mcp_server"],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class OcrBridge:
    """Small, thread-safe MCP stdio client with lazy start and restart."""

    def __init__(
        self,
        command: Optional[str] = None,
        *,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self._configured = (command or "").strip()
        self._configured_cwd = cwd
        self._extra_env = dict(env or {})
        self._timeout = float(timeout)
        self._endpoint: Optional[OcrEndpoint] = None
        self._probe_failed = False
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._messages: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._stderr_tail: List[str] = []
        self._lock = threading.RLock()
        self._request_id = 0
        self._started = False
        self._closed = False

    # -- discovery ---------------------------------------------------------

    def resolve(self, *, refresh: bool = False) -> Optional[OcrEndpoint]:
        if self._endpoint and not refresh:
            return self._endpoint

        explicit = self._configured or os.environ.get("OPENNEXT_OCR_COMMAND", "")
        if explicit:
            parts = _split_command(explicit)
            if parts:
                self._endpoint = OcrEndpoint(
                    command=parts,
                    cwd=self._configured_cwd or os.environ.get("OPENNEXT_OCR_CWD"),
                    timeout=self._timeout,
                    source="configured",
                )
                return self._endpoint

        if self._probe_failed and not refresh:
            return None

        candidates: List[tuple[str, Optional[str], str]] = []
        env_python = os.environ.get("OPENNEXT_OCR_PYTHON", "")
        env_cwd = os.environ.get("OPENNEXT_OCR_CWD")
        if env_python:
            candidates.append((env_python, env_cwd, "OPENNEXT_OCR_PYTHON"))
        candidates.append((sys.executable, None, "this interpreter"))

        home = Path.home()
        for root in (
            env_cwd,
            str(home / "Documents" / "ocr-pdf-watcher"),
            "/opt/ocr-pdf-watcher",
        ):
            if not root:
                continue
            python = Path(root) / ".venv" / "bin" / "python"
            if python.is_file():
                candidates.append((str(python), str(root), f"checkout {root}"))

        which = shutil.which("python3")
        if which:
            candidates.append((which, env_cwd, "python3 on PATH"))

        for python, cwd, source in candidates:
            if _probe(python, cwd):
                self._endpoint = OcrEndpoint(
                    command=[python, "-m", "ocr_md.mcp_server"],
                    cwd=cwd,
                    timeout=self._timeout,
                    source=source,
                )
                logger.info("OCR module resolved via %s (%s)", source, python)
                return self._endpoint

        self._probe_failed = True
        logger.warning(
            "OCR module not found; install ocr-md or set OPENNEXT_OCR_COMMAND. "
            "OCR, vision and descriptive naming stay disabled."
        )
        return None

    def available(self) -> bool:
        return self.resolve() is not None

    # -- process lifecycle -------------------------------------------------

    def _ensure_started(self) -> None:
        endpoint = self.resolve()
        if endpoint is None:
            raise OcrBridgeError(
                "OCR module is not available. Install it with "
                "`pip install -e /path/to/ocr-pdf-watcher` or set "
                "OPENNEXT_OCR_COMMAND='python -m ocr_md.mcp_server'."
            )
        if self._proc is not None and self._proc.poll() is None and self._started:
            return
        self._stop_process()
        while not self._messages.empty():
            try:
                self._messages.get_nowait()
            except queue.Empty:
                break
        try:
            self._proc = subprocess.Popen(
                endpoint.command,
                cwd=endpoint.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env={**os.environ, **self._extra_env},
            )
        except OSError as exc:
            raise OcrBridgeError(f"could not start OCR server {endpoint.command}: {exc}") from exc

        self._closed = False
        self._reader = threading.Thread(target=self._read_stdout, name="ocr-mcp-out", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="ocr-mcp-err", daemon=True
        )
        self._stderr_reader.start()

        try:
            response = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "open-next", "version": __version__},
                },
                timeout=30.0,
            )
        except Exception:
            self._stop_process()
            raise
        if not isinstance(response, dict):
            raise OcrBridgeError("OCR server returned an invalid initialize response")
        self._notify("notifications/initialized", {})
        self._started = True
        logger.info(
            "OCR server ready: %s",
            (response.get("serverInfo") or {}).get("name", "ocr-md"),
        )

    def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None
        self._started = False
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def restart(self) -> None:
        """Drop the subprocess so the next call launches a fresh one.

        Called after a timeout: responses are correlated by JSON-RPC id, so a
        late reply is discarded rather than misdelivered, but the server is
        probably still working on the abandoned request.  Restarting is the only
        way to guarantee the next call is not queued behind it.
        """
        with self._lock:
            self._stop_process()
            self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_process()

    # -- IO ----------------------------------------------------------------

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._messages.put(None)
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                logger.debug("Ignoring non-JSON OCR server output: %s", line[:200])
                continue
            self._messages.put(message if isinstance(message, dict) else None)
        self._messages.put(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            text = line.rstrip()
            if not text:
                continue
            self._stderr_tail.append(text)
            del self._stderr_tail[:-40]
            logger.debug("[ocr] %s", text)

    def _write(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise OcrBridgeError("OCR server is not running")
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise OcrBridgeError(f"OCR server pipe closed: {exc}") from exc

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(
        self, method: str, params: Dict[str, Any], *, timeout: Optional[float] = None
    ) -> Any:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            deadline = time.monotonic() + (timeout or self._timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OcrBridgeError(f"OCR server timed out on {method}")
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty:
                    raise OcrBridgeError(f"OCR server timed out on {method}") from None
                if message is None:
                    tail = " | ".join(self._stderr_tail[-3:])
                    raise OcrBridgeError(
                        f"OCR server exited during {method}"
                        + (f": {tail}" if tail else "")
                    )
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    raise OcrBridgeError(str(error.get("message") or error))
                return message.get("result")

    # -- public API --------------------------------------------------------

    def status(self, *, refresh: bool = False) -> Dict[str, Any]:
        endpoint = self.resolve(refresh=refresh)
        if endpoint is None:
            return {"available": False, "reason": "ocr module not found", "source": ""}
        try:
            with self._lock:
                self._ensure_started()
            tools = self._request("tools/list", {}) or {}
            vision: Dict[str, Any] = {}
            try:
                vision = self.call_tool("ocr_vision_status", {}, timeout=20.0)
            except OcrBridgeError as exc:
                vision = {"error": str(exc)}
            return {
                "available": True,
                "source": endpoint.source,
                "command": " ".join(endpoint.command),
                "tools": [tool.get("name") for tool in tools.get("tools", [])],
                "vision": vision,
            }
        except OcrBridgeError as exc:
            return {"available": False, "reason": str(exc), "source": endpoint.source}

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._ensure_started()
            result = self._request(
                "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
            )
        if not isinstance(result, dict):
            raise OcrBridgeError(f"OCR tool {name} returned an invalid result")
        content = result.get("content") or []
        text = next(
            (item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"),
            "",
        )
        if result.get("isError"):
            raise OcrBridgeError(text or f"OCR tool {name} failed")
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except ValueError:
            return {"text": text}
        return payload if isinstance(payload, dict) else {"result": payload}

    # Convenience wrappers -------------------------------------------------

    def ingest(
        self,
        path: str,
        *,
        timeout: Optional[float] = None,
        **options: Any,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"path": path}
        arguments.update({key: value for key, value in options.items() if value is not None})
        return self.call_tool("ocr_ingest", arguments, timeout=timeout)

    def describe(self, path: str, *, max_pages: int = 3, prompt: str | None = None) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"path": path, "max_pages": max_pages}
        if prompt:
            arguments["prompt"] = prompt
        return self.call_tool("ocr_describe", arguments)

    def suggest_name(self, path: str) -> Dict[str, Any]:
        return self.call_tool("ocr_suggest_name", {"path": path})
