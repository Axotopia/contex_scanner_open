"""The 82-byte ``SETWINDOWPARAMS`` parameter block.

``SPECS.md`` §2.5 gives the field table. This module implements it against the
one block we know the device accepts — the OEM block captured in
``analysis/scan-sequence.json`` (``set_window``, body after the CDB) — and
preserves every byte whose meaning is unknown.

Two corrections to ``SPECS.md`` are recorded here because the capture is
authoritative:

* the parameter-list length field at ``[6:8]`` reads **74**, which is the block
  size minus its 8-byte header (74 + 8 = 82). ``SPECS.md`` says the field is
  "fixed 0x4A (74)" at offset 6 and calls it one byte; it is big-endian u16.
* the byte holding **1 (sRGB)** is at **76**, not 77. Offset 77 holds 100 and is
  carried through untouched. Both bytes are preserved.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace

BLOCK_LEN = 82

#: The OEM block that produced a real scan. Every unknown byte is carried from
#: here so that patching only the fields we understand stays byte-exact.
OEM_BLOCK = bytes.fromhex(
    "000000000000004a"      #  0..7   header; [6:8] = 74 = payload length after header
    "0000006400640000"      #  8..15  window id, x dpi @10, y dpi @12
    "0000000000000000"      # 16..23  upper-left x @14, y @18 (hex)
    "c4e0000007080000"      # 24..31  width @22, length @26   (50400, 1800)
    "0002080000000000"      # 32..39  threshold @31, composition @33, bpc @34
    "0000000000000000"      # 40..47  compression @40
    "0000000000000000"      # 48..55
    "0040000000000000"      # 56..63  offset 57 = 0x40, carried
    "00000000ee000000"      # 64..71  offset 68 = 0xee, carried
    "0000000001640000"      # 72..79  colour space @76 = 1, offset 77 = 100, carried
    "1f00"                  # 80..81  offset 80 = 0x1f (carried), post-scan @81
)

OFF_PARAM_LEN = 6
OFF_DPI_X = 10
OFF_DPI_Y = 12
OFF_UPPER_LEFT_X = 14
OFF_UPPER_LEFT_Y = 18
OFF_WIDTH = 22
OFF_LENGTH = 26
OFF_THRESHOLD = 31
OFF_COMPOSITION = 33
OFF_BITS_PER_COMPONENT = 34
OFF_COMPRESSION = 40
OFF_COLOUR_SPACE = 76
OFF_POST_SCAN = 81

#: Resolutions the device advertises on capability page 0xC3 (``SPECS.md`` §2.6).
SUPPORTED_DPI = (100, 200, 300, 400, 600, 1200)

#: Full width of the HD Ultra i4250s bar, in 1/1200 inch (page 0xC1 @28).
MAX_WIDTH_UNITS = 50400

COMPOSITION_GREY = 1
COMPOSITION_COLOUR = 2

#: ``bits_per_component`` 8 with composition 2 gives 3 bytes/pixel on the wire,
#: matching every capture. Reading the field as bits-per-pixel would wrongly
#: predict greyscale, so it is per-component.
BITS_PER_COMPONENT_8 = 8


@dataclass(frozen=True)
class WindowParams:
    """A scan window. Units are 1/1200 inch unless stated otherwise."""

    dpi_x: int = 200
    dpi_y: int | None = None          # None -> same as dpi_x
    width_units: int = MAX_WIDTH_UNITS
    length_units: int = 0             # 0 = auto (to end of media)
    upper_left_x: int = 0
    upper_left_y: int = 0
    threshold: int = 0
    composition: int = COMPOSITION_COLOUR
    bits_per_component: int = BITS_PER_COMPONENT_8
    compression: int = 0
    colour_space: int = 1             # 1 = sRGB, per SPECS §2.5
    post_scan: int = 0
    base: bytes = field(default=OEM_BLOCK, repr=False)

    def __post_init__(self) -> None:
        if len(self.base) != BLOCK_LEN:
            raise ValueError(f"base block must be {BLOCK_LEN} bytes, got {len(self.base)}")
        if self.dpi_y is None:
            object.__setattr__(self, "dpi_y", self.dpi_x)

    # ------------------------------------------------------------- constructors
    @classmethod
    def from_inches(cls, width_in: float, length_in: float | None = None, *,
                    dpi: int = 200, colour: bool = True, **kw) -> "WindowParams":
        return cls(
            dpi_x=dpi,
            width_units=int(round(width_in * 1200)),
            length_units=0 if length_in is None else int(round(length_in * 1200)),
            composition=COMPOSITION_COLOUR if colour else COMPOSITION_GREY,
            **kw,
        )

    @classmethod
    def from_block(cls, block: bytes) -> "WindowParams":
        if len(block) != BLOCK_LEN:
            raise ValueError(f"window block must be {BLOCK_LEN} bytes, got {len(block)}")

        def u16(off: int) -> int:
            return struct.unpack_from(">H", block, off)[0]

        def u32(off: int) -> int:
            return struct.unpack_from(">I", block, off)[0]

        return cls(
            dpi_x=u16(OFF_DPI_X),
            dpi_y=u16(OFF_DPI_Y),
            width_units=u32(OFF_WIDTH),
            length_units=u32(OFF_LENGTH),
            upper_left_x=u32(OFF_UPPER_LEFT_X),
            upper_left_y=u32(OFF_UPPER_LEFT_Y),
            threshold=block[OFF_THRESHOLD],
            composition=block[OFF_COMPOSITION],
            bits_per_component=block[OFF_BITS_PER_COMPONENT],
            compression=block[OFF_COMPRESSION],
            colour_space=block[OFF_COLOUR_SPACE],
            post_scan=block[OFF_POST_SCAN],
            base=block,
        )

    def with_(self, **changes) -> "WindowParams":
        return replace(self, **changes)

    # ------------------------------------------------------------------- output
    def to_block(self) -> bytes:
        """Serialise, patching only known fields over the OEM base."""
        blk = bytearray(self.base)
        struct.pack_into(">H", blk, OFF_PARAM_LEN, BLOCK_LEN - 8)
        struct.pack_into(">H", blk, OFF_DPI_X, self.dpi_x & 0xFFFF)
        struct.pack_into(">H", blk, OFF_DPI_Y, (self.dpi_y or self.dpi_x) & 0xFFFF)
        struct.pack_into(">I", blk, OFF_UPPER_LEFT_X, self.upper_left_x & 0xFFFFFFFF)
        struct.pack_into(">I", blk, OFF_UPPER_LEFT_Y, self.upper_left_y & 0xFFFFFFFF)
        struct.pack_into(">I", blk, OFF_WIDTH, self.width_units & 0xFFFFFFFF)
        struct.pack_into(">I", blk, OFF_LENGTH, self.length_units & 0xFFFFFFFF)
        blk[OFF_THRESHOLD] = self.threshold & 0xFF
        blk[OFF_COMPOSITION] = self.composition & 0xFF
        blk[OFF_BITS_PER_COMPONENT] = self.bits_per_component & 0xFF
        blk[OFF_COMPRESSION] = self.compression & 0xFF
        blk[OFF_COLOUR_SPACE] = self.colour_space & 0xFF
        blk[OFF_POST_SCAN] = self.post_scan & 0xFF
        return bytes(blk)

    # ------------------------------------------------------------------ derived
    @property
    def colour(self) -> bool:
        return self.composition == COMPOSITION_COLOUR

    @property
    def bytes_per_pixel(self) -> int:
        return 3 if self.colour else 1

    def expected_pixels(self) -> tuple[int, int | None]:
        """Pixel width and height, or ``None`` height when length is auto."""
        w = round(self.width_units / 1200 * self.dpi_x)
        h = None if self.length_units == 0 else round(self.length_units / 1200 * self.dpi_x)
        return w, h

    def describe(self) -> str:
        w, h = self.expected_pixels()
        mode = "colour" if self.colour else "grey"
        length = "auto" if h is None else f"{self.length_units / 1200:.2f} in ({h} px)"
        return (f"{self.dpi_x}x{self.dpi_y} dpi, {mode} "
                f"{self.width_units / 1200:.2f} in ({w} px) x {length}, "
                f"{self.bits_per_component} bits/component, {self.bytes_per_pixel} B/px")

    # --------------------------------------------------------------- validation
    def problems(self) -> list[str]:
        """Non-fatal inconsistencies, reported rather than raised."""
        out = []
        if self.dpi_x != self.dpi_y:
            out.append(f"anisotropic resolution {self.dpi_x}x{self.dpi_y} dpi "
                       "(no capture has exercised this)")
        if self.composition == COMPOSITION_COLOUR and self.bits_per_component == 16:
            out.append("16 bits/component colour has never been observed")
        if self.length_units == 0:
            out.append("length 0 = auto: the scanner decides the row count")
        return out

    def validate(self, *, max_width_units: int = MAX_WIDTH_UNITS,
                 supported_dpi: tuple[int, ...] = SUPPORTED_DPI) -> None:
        """Raise ``ValueError`` for values the device cannot accept."""
        if self.dpi_x not in supported_dpi:
            raise ValueError(
                f"dpi {self.dpi_x} is not in the advertised set {supported_dpi}"
            )
        if (self.dpi_y or self.dpi_x) not in supported_dpi:
            raise ValueError(f"dpi_y {self.dpi_y} is not in the advertised set")
        if self.width_units <= 0:
            raise ValueError("width must be positive")
        if self.width_units > max_width_units:
            raise ValueError(
                f"width {self.width_units} exceeds the bar ({max_width_units} units = "
                f"{max_width_units / 1200:.2f} in)"
            )
        if self.length_units < 0:
            raise ValueError("length must be >= 0 (0 means auto)")
        if self.upper_left_x < 0 or self.upper_left_y < 0:
            raise ValueError("upper-left origin must be >= 0")
        if self.upper_left_x + self.width_units > max_width_units:
            raise ValueError("window runs past the right edge of the bar")
        if self.composition not in (COMPOSITION_GREY, COMPOSITION_COLOUR):
            raise ValueError(f"composition {self.composition} is not 1 (grey) or 2 (colour)")
        if self.bits_per_component not in (8, 16):
            raise ValueError(f"bits/component {self.bits_per_component} is not 8 or 16")
        if self.compression != 0:
            raise ValueError("only uncompressed (0) is implemented")
