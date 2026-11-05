"""Tiny MCP stdio server used by tests/test_ocr_bridge.py.

Speaks just enough of the protocol to exercise the client: initialize,
tools/list, tools/call.  Set FAKE_OCR_FAIL=1 to return an isError result.
"""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {"name": "ocr_ingest", "description": "fake ingest", "inputSchema": {"type": "object"}},
    {"name": "ocr_describe", "description": "fake describe", "inputSchema": {"type": "object"}},
    {"name": "ocr_suggest_name", "description": "fake name", "inputSchema": {"type": "object"}},
    {"name": "ocr_vision_status", "description": "fake vision", "inputSchema": {"type": "object"}},
]


def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            respond(
                request_id,
                {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-ocr", "version": "0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            respond(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            if os.environ.get("FAKE_OCR_FAIL"):
                respond(
                    request_id,
                    {"content": [{"type": "text", "text": "boom"}], "isError": True},
                )
                continue
            payload = {
                "ok": True,
                "tool": params.get("name"),
                "arguments": params.get("arguments"),
                "name": {"stem": "2026-10-14_Tax-Invoice_Acme", "confidence": 0.9},
            }
            respond(
                request_id,
                {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
            )
        elif method in ("shutdown", "exit"):
            return 0
        else:
            respond(request_id, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
