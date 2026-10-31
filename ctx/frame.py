"""Wire bytes -> rows -> image (M3).

There is no explicit line length on the wire (``SPECS.md`` §4.1); it is
``sum(dlen)`` per row and must be recovered from the stream. The true length
produces an autocorrelation peak of 0.99+, wrong candidates do not.

numpy is imported lazily so the rest of the package works without it.
"""
from __future__ import annotations

import struct
import zlib

#: The wire carries 3 bytes per pixel for colour (``SPECS.md`` §4).
BYTES_PER_PIXEL = 3

#: Acceptance values from ``DESIGN.md`` §7.
KNOWN_LINE_BYTES = (26928, 8712, 9204)


def detect_line_bytes(stream: bytes, *, min_bytes: int = 900, max_bytes: int = 65536,
                      min_rows: int = 8, multiple_of: int | None = BYTES_PER_PIXEL
                      ) -> tuple[int, float]:
    """Return ``(line_bytes, autocorrelation_score)`` for the best candidate.

    Candidates below *min_bytes*, above *max_bytes*, leaving fewer than
    *min_rows* rows, or not a multiple of *multiple_of* (3 for RGB) are excluded.
    """
    import numpy as np

    x = np.frombuffer(stream, dtype=np.uint8).astype(np.float64)
    n = x.size
    if n < min_bytes * min_rows:
        raise ValueError(f"stream is {n} bytes: too short to contain {min_rows} rows")
    x -= x.mean()
    spectrum = np.fft.rfft(x)
    acf = np.fft.irfft(spectrum * np.conjugate(spectrum), n)
    if acf[0] == 0:
        raise ValueError("stream has zero variance")
    acf /= acf[0]

    step = max(1, multiple_of or 1)
    lo = max(min_bytes, step)
    lo = -(-lo // step) * step                      # round up to a whole pixel
    hi = min(max_bytes, n // max(min_rows, 1))
    if hi <= lo:
        raise ValueError(f"no viable lag range ({lo}..{hi})")

    lags = np.arange(lo, hi + 1, step)
    scores = acf[lags]
    best = int(np.argmax(scores))
    return int(lags[best]), float(scores[best])


def to_rows(stream: bytes, line_bytes: int, *, rows: int | None = None):
    import numpy as np

    if line_bytes <= 0:
        raise ValueError("line_bytes must be positive")
    available = len(stream) // line_bytes
    if rows is not None:
        available = min(available, rows)
    if available == 0:
        raise ValueError(f"stream of {len(stream)} B holds no {line_bytes}-byte row")
    flat = np.frombuffer(stream[:available * line_bytes], dtype=np.uint8)
    return flat.reshape(available, line_bytes)


def to_image(stream: bytes, line_bytes: int, *, roll: int = 0,
             pixel_bytes: int = BYTES_PER_PIXEL, rows: int | None = None):
    """Reshape to ``(rows, width, 3)`` RGB, undoing the constant row roll.

    ``SPECS.md`` §4.2: every row starts at a non-zero x and wraps, and the
    offset is constant for a whole scan, so one roll per row corrects it.
    """
    import numpy as np

    flat_rows = to_rows(stream, line_bytes, rows=rows)
    width = line_bytes // pixel_bytes
    image = flat_rows[:, :width * pixel_bytes].reshape(flat_rows.shape[0], width, pixel_bytes)
    if roll:
        image = np.roll(image, -roll % width, axis=1)
    return image


def write_png(path: str, image) -> None:
    """Minimal RGB PNG writer, so no imaging dependency is required."""
    import numpy as np

    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    elif arr.ndim == 3 and arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    height, width, channels = arr.shape
    if channels != 3:
        raise ValueError(f"expected RGB, got {channels} channels")

    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # filter: none
        raw += arr[y].tobytes()

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + \
            struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        fh.write(chunk(b"IEND", b""))
