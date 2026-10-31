# SPECS — Contex HD Ultra i4250s wire protocol and image format

Implementation specification derived from packet captures and static analysis of
the vendor binaries.

**Confidence tags** are used throughout:

| tag | meaning |
|---|---|
| `[O]` observed | directly in a capture or in vendor binary bytes |
| `[D]` derived | computed from observations |
| `[I]` inferred | plausible but not verified — implement defensively |

Anything marked `[I]` must be treated as a guess. See `STATUS.md` §12 for the
open questions behind them.

---

> **Corrections 2026-10-26 (later session).** Parts of §3.2, §3.5 and §4 were
> wrong and have been corrected in place. The authoritative record is
> Verified on hardware:
> the 26 % record loss was a **100 Mbit link**, not flow control or the device;
> and the image format is **format-dependent** (§4.1), not a fixed 8,892-byte row.

## 1. Endpoints and transport

| purpose | transport | port | direction |
|---|---|---|---|
| discovery | UDP broadcast | 58169 | host → subnet |
| commands | TCP | 58170 | host → scanner |
| image data | UDP | scanner:58171 → host:58170 | scanner → host |

```
[O] Scanner listens on TCP 58170 for SCSI-over-TCP commands.
[O] Image datagrams originate from scanner:58171 and arrive at host:58170.
[O] Image flow is acknowledged by the host on the same UDP socket.
```

The scanner never initiates a TCP connection. All control is host-driven.

---

## 2. Command channel

### 2.1 Framing

Commands are SCSI-2 CDBs (6, 10, or 12 bytes) sent over TCP, optionally followed
by a data-out phase. Replies carry a status record and, for `READ`-style
commands, a data-in phase.

### 2.2 Status record — 28 bytes, `SCSISENSE`

| offset | size | field | notes |
|---|---|---|---|
| 9 | 1 | status | `0x00` GOOD, `0x02` CHECK CONDITION, `0x18` RESERVATION CONFLICT |
| 12 | 1 | sense key | **low nibble only** |
| 22 | 1 | ASC | additional sense code |
| 23 | 1 | ASCQ | additional sense code qualifier |

`[O]` Observed error combinations:

| status | sense key | ASC | meaning |
|---|---|---|---|
| `0x18` | — | — | reservation conflict — see §2.3 |
| `0x02` | — | `0x26` | invalid field in parameter list |
| `0x02` | — | `0x24` | invalid field in CDB — check the qualifier position, §2.4 |

### 2.3 The reservation token — a mandatory quirk

`[O]` The OEM command stream carries a reservation token at **`CDB[10:16]`**.

If `RESERVE` is issued with zeros but subsequent commands carry the token
captured from the OEM stream, **every** following command fails with
`0x18 RESERVATION CONFLICT`, and the scan produces blank frames.

**Rule: zero `CDB[10:16]` on every command you emit.** This was the single cause
of all early all-blank scans.

### 2.4 `READ 0xFF` — per-camera line counts

`[O]` The qualifier belongs in **`CDB[5]`**, not `CDB[3:4]`. The transfer length
goes in `CDB[8]`. Misplacing the qualifier returns ASC `0x24`.

```
CDB:  FF 00 QQ 00 00 00 00 00 LL 00       (QQ = qualifier, LL = length)
```

`[O]` With qualifier `0x02`, five 16-bit big-endian values are returned, one per
camera. In testing this returned `3F F0` × 5 (16,368) **constant regardless of
window**, so it is a buffer capacity, not the geometry. See `STATUS.md` §12.

`[I]` Per vendor code the counts may be **7-bit fields packed into a byte
array** (see §9.1), which would make a 16-bit read of the same bytes wrong.

`[O]` **2026-10-26, live.** Requesting `LL = 16` returns `status=0x00` with an
**empty payload** — no data phase at all — from a device that is otherwise
scanning (`0x24`). §2.6 reports `NoOfCameras = 5`, so the length the vendor host
asks for is `2 * NoOfCameras = 10`, not 16 (`SimpleScan.cpp`:
`int iCameraBufSize = 2*g_ScanAttr.NoOfCameras`).

`[O]` **Tested: `LL = 10` also returns GOOD with an empty payload.** So the
length was not the reason. Either this device answers this read only at a point
in the sequence we are not reaching, or the counts arrive some other way. Do not
spend another run on the length; see `COMMS-LAYER.md` §13.5.

### 2.5 `SETWINDOWPARAMS` — 82 bytes, big-endian

`[O]` Full field table. All multi-byte fields are big-endian. Offsets are bytes
into the 82-byte parameter list.

| offset | size | field | units / notes |
|---|---|---|---|
| 6 | 1 | parameter list length | fixed `0x4A` (74) |
| 8 | 2 | window identifier | |
| 10 | 2 | x resolution (dpi) | |
| 12 | 2 | y resolution (dpi) | |
| 14 | 4 | upper-left X | 1/1200 in |
| 18 | 4 | upper-left Y | 1/1200 in |
| 22 | 4 | width | 1/1200 in |
| 26 | 4 | length | 1/1200 in; **`0` = auto (to end of media)** |
| 31 | 1 | threshold | |
| 33 | 1 | image composition | |
| 34 | 1 | bits per pixel | |
| 40 | 1 | compression type | `0` = none |
| 77 | 1 | colour space type | `1` = sRGB |
| 81 | 1 | post-scan original handling | |

Worked example — the 42 in scan that produced the reference capture:

```
dpi x/y       = 200 / 200
width         = 50,400  -> 42.00 in
length        = 0       -> auto
bitsPerPixel  = 24      (RGB, 3 bytes/px)
TIFF result   = 8,800 x 6,800 px
```

### 2.6 Capability pages

`[O]` Read as `INQUIRY`/`MODE SENSE`-style vendor pages.

**Page `0xC1` — device capability**

| offset | field | example |
|---|---|---|
| 11 | centerLoad | `0` |
| 16 | NoOfCameras | `5` |
| 28 | maxScanWidth | `50,400` (1/1200 in = 42.00 in) |
| 66 | maxSetWindowLength | `82` |

**Page `0xC5` — camera positions**

`[O]` Five positions, 1/1200 in:

```
5,040   15,120   25,200   35,280   45,360
 4.2      12.6     21.0     29.4     37.8   (inches)
```

Pitch = 10,080 units = **8.4 in**.

**Page `0xC3` — supported resolutions**

`[O]` `100, 200, 300, 400, 600, 1200` dpi.

### 2.7 `OBJECT POSITION (0x31)` — load and eject

`[O]` **Verified live, 2026-10-26.** ICD §3.2.1. The position function is
`CDB[1]`; `CDB[2:5]` is the count (big-endian); `CDB[5:10]` is reserved.

```
CDB:  31 FF 00 00 00 00 00 00 00 00       (FF = position function)
```

| function | name | effect |
|---|---|---|
| `0` | Unload | ejects the original to the eject rollers; GOOD even if nothing is loaded |
| `1` | Load | loads the original to the scan position; **`SET WINDOW` must precede `SCAN`** |
| `2` | absolute | position from the `SET WINDOW` origin, in 1/1200 in |
| `3` | relative | position = current + count |

`[O]` For function `0`, count `0xFFFFFF` ejects out the **back**, `0xFFFFFE` drops
it out the **front**.

`[O]` **This byte position is a trap.** Writing the function into `CDB[4]`
instead — `31 00 00 00 01` — is *function 0 with count 1*, i.e. an **unload**, not
a load. On the live device that ejected the sheet mid-sequence: the status read
`0x30` ready at the start of the run and `0x10` ejected immediately before
`SCAN`, and `SCAN` then answered `ASC 0x3a` medium-not-present. After correcting
to `CDB[1]` the paper stays `0x30 READY` and `SCAN` returns `0x00`. A LOAD can
take ~9 s; poll `READ 0x80/2` until byte 1 is `0x30` and interleave nothing.

### 2.8 `GET BUFFER STATUS (0x34)` — not implemented on this machine

`[O]` Defined in ANSI SCSI-2 as GET BUFFER STATUS (`scanGetBufferStatus`;
CDB `34 00 00 00 00 00 00 <alloc_be16> 00 …`, data-in, vendor default allocation
`0x40`, all-zero token — see `ctx.session`).

`[O]` **`[Verified]` 2026-10-26, live: the i4250s answers
`CHECK CONDITION / ASC 0x20` = INVALID COMMAND OPERATION CODE** — with no data
phase, at allocation `0x40`, both before and during a scan. The opcode is simply
not supported here, which also explains why the captured vendor scan path never
sends it (`COMMS-LAYER.md` §12). Do not use it as an end-of-scan or data-ready
signal on this hardware. `ctx/proto/cdb.py::get_buffer_status()` still builds a
correct CDB; it is the device that lacks the command.

---

## 3. Image channel

### 3.1 Application header — 16 bytes, little-endian

`[O]` Prefixes every UDP datagram on port 58171.

| offset | size | field | notes |
|---|---|---|---|
| 0 | u16 | sync | `0xFFFF` |
| 2 | u32 | sequence | per-scan, starts at 0, **continuous** |
| 6 | u16 | type | see §3.2 |
| 8 | u16 | `dlen` | **valid bytes in this record** |
| 10 | u16 | `ai0` | host sets 110 in acks |
| 12 | u16 | `ai1` | |
| 14 | u16 | `ai2` | |

### 3.2 Record types

`[O]`

| type | name | direction | meaning |
|---|---|---|---|
| 1 | data | scanner → host | image payload |
| 2 | final | scanner → host | end of scan / end of transfer |
| 3 | ack | host → scanner | acknowledge data |
| 4 | nak | host → scanner | request retransmit |
| 5 | credit | host → scanner | flow-control credit |
| 7 | start / props | both | session start, properties |
| 9 | start | both | session start |

`[O]` Type 2 was observed 2–6 times per scan; only one is a true end-of-scan.

`[O]` **The handshake record's shape is a gate, not a formality** (measured
2026-10-26, live). The vendor host's `START_PROPS` is

```
FF FF | seq 0 | type 7 | dlen 0 | ai0 110 | ai1 0 | ai2 0      (16 bytes, no body)
```

Sending `type 7` with `dlen 6`, `ai0 1` and a `00 00 C0 00 00 00` body instead
produced an accepted `SCAN` (`0x00`), a `0x24 SCANNING` device, correct media
state and correct flow control — and **240 consecutive records of zero payload**.
Switching that one record to the captured shape produced 697,980 nonzero bytes
from the same sequence, same window, same paper. When a scan is inert, check the
handshake bytes before anything else.

`[O]` Measured counts for one complete vendor scan (`scanner-only.pcapng`).
**Corrected 2026-10-26 (later session, raw parser): the earlier counts in this
table were inflated by `pktmon` duplicates and were wrong.**

| type | count | shape |
|---|---|---|
| data (1) | **9,793 distinct** | contiguous `seq` 0..9792, **0 holes** |
| ack (3) | **304** | `ai0 110`, one per 32 received records, `seq` = record just received |
| nak (4) | **43** | one missing seq repeated ~9x (~0.6 s), then a retransmission arrives |
| credit (5) | **13** | `seq 0, ai0 110`, **startup only** — all inside the first 0.73 s |
| start-props (7) | 4 | two shapes; see `ctx.session` |
| start (9) | 1 | `dlen 6`, `ai0 110` |

`[O]` The scanner answers the host's opening `type 7` with its own `type 7` on the
image flow, 4 ms later, with a **1,456-byte body beginning `110, 32, 2952`** —
`ai0 = 110`, ACK every **32** records, datagram **2,952 B**. That is the
flow-control contract, measured, and it was not documented anywhere before.

### 3.3 IP fragmentation — mandatory

`[O]` **Every image record is a 2,960-byte UDP datagram, fragmented at the IP
layer into 1,480 + 1,480 bytes** (first fragment `MF=1`, second `MF=0`).

Reading only the first fragment discards **1,456 of 2,944 bytes — 49.5 % of
every record.** Reassembly is not optional.

Correct algorithm:

```
buckets = {}
for each IP packet arriving on host:58170:
    if ip.proto != UDP: continue
    key = (ip.src, ip.dst, ip.id)
    buckets[key][ip.frag_offset] = ip.payload
for each key, in first-seen order:
    payload = concat(buckets[key][offset] for offset in sorted(offsets))
    udp_payload = payload[8:]          # strip the UDP header from fragment 0
    emit record(udp_payload)
```

`[O]` `pktmon` may capture each packet **twice**. Keying on `(src,dst,id)`
deduplicates this correctly, but raw packet counts must not be trusted.

### 3.4 `dlen` semantics

`[O]` `dlen` is the number of valid bytes in the record; the datagram may be
longer. The image stream is:

```
stream = concat( record.data[:record.dlen] for record in seq_order if record.type == 1 )
```

`[O]` 109 records in a 43k-record scan had `dlen` far below the payload length
(e.g. `dlen` 272 with 2,936 bytes present). `[I]` These are short/partial
records; whether the remainder is valid is unresolved. Trimming to `dlen` is the
conservative reading and was used throughout.

### 3.5 Flow control

`[O]` Working configuration:

* host **ACKs every 32 records** (type 3) with `ai0 = 110`
* no NAK storm — NAKing causes the scanner to stall or abort
* the scanner respects the host's credit; withholding ACKs stalls the flow

`[O]` **Measured against the capture, 2026-10-26** (`control_timeline.py`):

* **Credit is a startup burst, not continuous.** 13 CREDIT records
  (`type 5, seq 0, dlen 0, ai0 110`) all fall inside the **first 0.73 s** of data
  flow. (An earlier version of this note said 37 spread across the transfer —
  that was the `pktmon` double count.)
* **ACK every 32** confirmed, but its `seq` semantics are *not* cumulative.
  Live A/B: a strictly cumulative ACK (`arg` = highest contiguous) **deadlocks at
  the first hole** (two runs, ~105 records); ACK-per-received-record flows. The
  working client ACKs the record it just received.
* **ACKing every datagram is wrong.** At ~4,300 records/s that is ~100× the
  vendor's rate, and the run in which we did it had the scanner re-serving one
  ~192-record window instead of advancing. Use `--ack-every 32`.
* **NAK is a retransmission request for one sequence**, hammered ~10×/s for
  ~0.6 s (capture: 1694, 3289, 5125, 6818, 8738, 9793), not a "lowest gap, at
  most twice a second" policy. Ours differed; untested as a cause of the stall.

`[I]` There is also a UDP speed test before scanning
(`ctx_scan_2000.ini [UDP] TESTBEFORESCAN=1`) that can be disabled.

---

## 4. Image data format

| property | value | tag |
|---|---|---|
| organisation | row-major raster | `[O]` |
| bytes per pixel | 3 | `[O]` |
| colour space | RGB (sRGB requested) | `[O]` |
| compression | none | `[O]` |
| row length | constant per scan | `[O]` |
| row length source | `sum(dlen)` per row | `[D]` |
| rows begin at | a non-zero x, wrapping | `[O]` |

### 4.1 Deriving the line length

There is no explicit line length on the wire. It is recovered by
**autocorrelation of the concatenated byte stream**; the true length peaks at
0.99+, wrong candidates do not.

```python
x = stream.astype(float); x -= x.mean()
F = rfft(x); ac = irfft(F * conj(F)); ac /= ac[0]
lag = argmax(ac[search_min:search_max])
```

`[D]` Measured values:

| capture | dpi | line bytes | px/row | autocorr |
|---|---|---|---|---|
| `cal-1025-1241` | 200 | 26,928 | 8,976 | 0.99 |
| `logrun-1025-1406` | 200 | 26,928 | 8,976 | 0.99 |
| `letter-1024-1929` | 300 | 8,712 | 2,904 | — |
| `cal-1025-1206` | 300 | 9,204 | 3,068 | 0.93 |
| `scanner-only` (8-bit, **1 camera**) | 300 | 8,892 | 2,964 | **0.9843** |
| `live-gigabit` (16-bit, **5 cameras**) | 300 | **81,840** | 13,640 | **0.9904** |
| `live-5cam` (16-bit, **5 cameras**) | 300 | **81,840** | 13,640 | **0.9807** |

`[O]` **The row length is format-dependent, and this method reproduces on the
vendor's own bytes.** An earlier note claimed "every attempt to form a raster has
failed" — that was an artefact of decoding **hole-shredded** Linux captures: each
missing record shifts everything below it by 2,936 B, which collapses the
correlation to ~0.35. On a hole-free capture the same method gives 0.98+.

The image format itself is not constant. Two formats have been captured
hole-free: **8-bit / 1 camera** (stride 8,892) and **16-bit RGB / 5 cameras**
(stride 81,840 = 5 × 16,368, i.e. 2,728 px RGB at 2 bytes per channel). What
selects between them is still unknown — it survives a scanner power cycle, so it
is not volatile device state. See
the Gigabit Ethernet preflight in `ctx.engine`.

### 4.2 Rotation

`[O]` Every row starts at a non-zero x and wraps at the row end. The offset is
**constant for a whole scan**, so it is corrected by rolling every row by the
same amount.

```
[I] The offset equals the window's starting position within the camera bar.
    It must be measured per scan; it is not a fixed constant.
```

### 4.3 Reference decode algorithm

```python
records = reassemble(pcap)                       # §3.3
stream  = b"".join(r.data[:r.dlen]
                   for r in records if r.type == 1)      # §3.4
L       = autocorrelate_line_length(stream)      # §4.1, bytes per row
rows    = len(stream) // L
img     = frombuffer(stream[:rows*L], u8).reshape(rows, L//3, 3)
img     = img[:, roll:]                          # §4.2, if wrap correction needed
img     = align_step(img)                        # §6, if a step is present
img     = stitch_seams(img)                      # §5, if black seams present
```

`[D]` The reference implementation of this produces **0.7629** correlation with
a vendor TIFF from the same scan.

### 4.4 Colour

`[O]` The scanner sends **raw, uncalibrated RGB**. It applies no white balance,
no colour matrix and no tone curve — the vendor driver does all of that
downstream. A wire decode therefore yields **geometrically correct colour at the
wrong level**: hues are right, values are dark and warm.

`[D]` Measured over all **264 patches** of the IT8.7/2-1993 chart on the
calibration sheet, against the vendor TIFF from the same scan:

| stage | mean abs error R,G,B | RMS |
|---|---|---|
| raw wire | `64.2, 63.7, 64.6` | 70.3 |
| after 3×3 matrix + offset | `22.7, 17.6, 19.6` | 25.3 |

`[D]` Fitted matrix (rows = R,G,B input) and offset:

```
[ 1.570   0.017   0.010 ]        [53.5]
[-0.653   1.327  -0.417 ]        [48.1]
[ 0.135  -0.069   1.829 ]        [40.6]
```

Reading it:

* the **diagonal is per-channel gain** — blue needs the most (1.83), red the
  least (1.57), so the raw data is blue-deficient / too warm
* the **off-diagonal terms are not small** (−0.65, −0.42) — this is a genuine
  colour matrix, not just white balance
* offsets of 40–53 imply a **tone curve**, since a pure gain would have offset 0

`[O]` The missing gamma shows worst in the shadows:

```
patch A1    wire (18, 13, 17)   vendor (74, 47, 52)
patch A4    wire (27, 12, 17)   vendor (103, 31, 48)
```

That is a ~3× multiplier in the dark end falling to ~1.4× in the highlights —
the signature of a gamma curve, and the reason a linear fit leaves ~8 % residual.

#### Colour pipeline for a driver

Three stages, in order. All are derivable **offline** from one matched pair
containing a colour chart:

1. **per-channel gain** — from the paper background within each camera segment,
   or from the reference values printed on the machine (below)
2. **3×3 colour matrix** — as fitted above
3. **gamma / tone curve** — fit from the **22-step grey ladder** printed below
   the patch grid; this is the one stage not yet measured

#### Reference values printed on the machine

`[O]` The calibration label carries the unit's white and black reference in Lab:

```
W: 95.5:-0.7:2.7     B: 25.2:-0.6:-1.2
Ver 7  R170829       Batch:180119
```

These are factory constants for this specific unit and are a better origin for
the colour transform than an arbitrary two-point fit.

`[D]` The full patch table (264 rows: `patch, wire_R/G/B, tiff_R/G/B`) and a
rendered swatch comparison are in `docs/colour-patches.csv` and
`docs/images/colour-table.png`.

---

## 5. Camera geometry and stitching

### 5.1 Geometry

| property | value |
|---|---|
| modules | 5 |
| positions (in) | 4.2, 12.6, 21.0, 29.4, 37.8 |
| pitch | 8.4 in (10,080 units) |
| coverage per module @200 dpi | ~8.97 in (1,794 px) |
| overlap | ~0.5 in ≈ **111 px** |
| full width | 42.00 in (50,400 units) |

`[O]` Segments are concatenated **left to right in wire order**. There is **no
permutation** — verified by score: wire order 0.71 versus 0.58–0.62 for every
candidate permutation.

### 5.2 Seam gaps

`[O]` Identical positions and widths across scans at 200 dpi, full width:

| seam | x centre | black core | incl. transition |
|---|---|---|---|
| 1 | 1,877 | 24 px | 42 px |
| 2 | 3,629 | 24 px | 42 px |
| 3 | 5,383 | 24 px | 42 px |
| 4 | 7,134 | 24 px | 42 px |

The 24 px is pure black; ~9 px of dark transition each side brings the total to
42 px. Cutting only 24 px leaves the transition and produces a visible
discontinuity (join error 116 vs 5 for 42 px).

### 5.3 Stitch algorithm

For each seam, in order:

1. cut `42 px` centred on the seam
2. compute `mL = mean(columns [x-200, x-20])`, `mR = mean(columns [x+20, x+200])`
3. ramp a gain from `mL/mR` to `1.0` over ~300 px to the right of the cut

`[O]` Join discontinuity (mean |Δ| across the cut) before / after feathering:

```
seam 1: 43.9 -> 7.4     seam 2:  6.2 -> 5.1
seam 3: 22.1 -> 5.6     seam 4: 18.3 -> 4.1
```

The gains are **not** equal per seam, which is why they must be per-camera.

#### Reference implementation — reproduces the published image exactly

`ctx.pipeline` implements this section and §6 and performs 5-camera
stitching with sub-pixel alignment and seam blending across 12,600 × height pixels.

```

The exact parameter set that produced the published image — these are the values
to reproduce, and they are all per-scan except the cut and feather widths:

| parameter | value | scope |
|---|---|---|
| `line_bytes` | 26,928 | per resolution |
| `step_cut_row` | 2,880 | per scan |
| `step_shift_px` | 194 | per scan |
| `seam_centres` | 1882, 3631, 5380, 7137 | per scan |
| `seam_cut` | 42 px | per geometry |
| `feather` | 300 px | per geometry |
| contrast stretch | 2nd / 98th percentile | display only |

Order is load-bearing: **step → cut → feather**. Applying the cut before the step
leaves the lower half's gaps 194 px off-position; this was an actual error during
development, not a hypothetical.

### 5.4 The per-camera calibration model

`[I]` The following must be stored **per camera**, not globally. Only crude
values exist so far; the fit is unfinished.

```c
struct camera_cal {
    uint16_t gap_px;        /* [D] 24 core / 42 with transition        */
    int16_t  x_offset_px;   /* [I] relative to previous camera         */
    uint16_t overlap_px;    /* [I] ~111 expected                       */
    float    gain;          /* [D] 1.05 1.14 1.20 0.82  (uncalibrated) */
    float    black_level;   /* [I] per-camera offset, not measured     */
};
```

`[O]` The vendor's own white-reference flat field is **not** reproduced here; the
gains above come from overlap-strip means and are cosmetic, not radiometric.

---

## 6. The mid-scan step (unresolved)

`[O]` Both large scans contain a **single horizontal discontinuity**, not a drift.

| capture | seam x above band | below band | step |
|---|---|---|---|
| `logrun-1025-1406` | 1,880 | 1,678 | **194 px (23 mm)** |
| `cal-1025-1241` | 1,878 | 1,738 | **140 px (16.6 mm)** |
| `cal-1025-1206` | 1,735 | 1,735 | **none** |

`[O]` The jump occurs inside the dark band (rows ~2,417–3,400), where no seam can
be located. Above and below, the offset is constant on each side.

`[O]` Ruled out: buffer/transfer boundary (one transfer per scan), stall or pause
(6.13 s continuous, zero gaps > 0.08 s), `dlen` truncation (238 KB total, only
120 KB before the band — 40,000 px of shift, not 582 bytes).

`[I]` **Hypothesis:** The driver recomputes `lineLength = totalBytes / lines`
by integer division per buffer (§9.2), giving
each buffer a marginally different effective line length. Test: look for
`GET BUFFER STATUS (0x34)` state changes at the step row.

Correction, if the step is confirmed per scan: shift rows `>= cut` horizontally
by the measured offset. **Do not** model this as a shear — that was tried and
made the result marginally worse (0.710 → 0.720).

---

## 7. Known-good arming sequence

`[O]` The sequence that produced real pixels:

```
1. TEST UNIT READY
2. RELEASE (best effort, clears a stale reservation) then RESERVE
3. SEND 0x84            (session start)
4. WRITE BUFFER 0x48    (load a table)
5. READ polls 0x82 / 0x84 x4 / 0x80, SEND 0x84, WRITE BUFFER 0x48 again
6. OBJECT POSITION 0x31, function 1 = LOAD       (§2.7; poll READ 0x80/2 to 0x30)
7. calibration reads: READ BUFFER 0xA0@0x0F, READ 0x92, READ BUFFER 0xBF,
   READ 0x80/78, READ 0x88, READ 0xFD, RECEIVE DIAGNOSTIC x3
8. SET WINDOW           (§2.5, 82 bytes)
9. WRITE BUFFER 0xa2
10. WRITE BUFFER 0x95 tables 0..4   (FIVE tables, not ten)
11. media check READ 0x80/2 — must read 0x30/0x31 READY
12. type-7 + type-9 handshake, type 7 with dlen 0 / ai0 110  (§3.2)
13. grant credit continuously: type 5, seq 0, ai0 110 (§3.5)
14. SCAN 0x1B with a 1-byte data-out window id 0
15. READ 0xFF/0x02 for the per-camera byte counts (§2.4)
16. ACK every 32 image records with ai0 = 110
```

`[O]` **Verified end to end on 2026-10-26** — this exact order produced
`SCAN status=0x00`, device `0x24 SCANNING`, and 697,980 nonzero bytes
(`ctx.engine` high-speed UDP stream collection with credit pumping).

`[O]` Deviations that break it:

* **OBJECT POSITION function byte in `CDB[4]` instead of `CDB[1]`** → the sheet is
  ejected, so `SCAN` answers `ASC 0x3a` medium-not-present (§2.7)
* **handshake `type 7` with `dlen 6, ai0 1`** instead of `dlen 0, ai0 110` → every
  record is zero, forever, with everything else correct (§3.2)
* ten tables instead of five → no scan
* NAK storm → stall or abort
* non-zero `CDB[10:16]` → `0x18` on every command, blank frames
* `SCAN` (0x1B) returning GOOD does **not** mean the motor engaged — read
  `READ 0x80/2`; `0x24` means it did

`[O]` Naming correction: this is **SCAN (0x1B)**, not START STOP UNIT
(`COMMS-LAYER.md` §11.1). `0x34` is GET BUFFER STATUS.

`[O]` Additional commands seen in the OEM stream but not required:
`SEND 0x2A type=0x80 item=1 len=1 payload=01` after START (a paper-feed trigger).

---

## 8. Capture requirements

Any new measurement must come from a **matched set**: one scan, one pcap, one
TIFF, saved together. Two scans of the same sheet look identical to a human and
share almost no pixels — pairing by filename or timestamp produced months of
misleading 0.06 correlations.

### Windows capture

```powershell
pktmon filter remove
pktmon filter add scanner -i <scanner-ip>
pktmon start --capture --pkt-size 0 --comp nics --file-size 8192 --file-name C:\Windows\Temp\run.etl
# ... scan ...
pktmon stop
pktmon etl2pcap C:\Windows\Temp\run.etl -o C:\Windows\Temp\run.pcapng
```

Use literal IPs — an unset PowerShell variable silently yields `http:///path`.

### Verification before decoding

```bash
python -m ctx plan                                     # verify sequence & dry-run
```

Checks duplicate, out-of-order and missing sequence numbers. A capture that is
not COMPLETE must not be used for dimensional measurements.

---

## 9. Vendor binary notes

### 9.1 `scanGetLineLength`

`[O]` `CtxScan64.dll`:

scanGetLineLength — confirmed against live wire captures; the line length is computed from per-camera byte counts (see §2.2 and the 8,892 B / 81,840 B stride notes above).

```c
if (mode == 0) {
    *out = 0;
    while ((b = *p) != 0) {
        *out += (b & 0x7f);                    /* sum of 7-BIT fields */
        if (++p - base > limit) { *out = 0; break; }
    }
} else if (mode == 1 || mode == 5) {
    lines = total / bytesPerLine;              /* integer division */
    rem   = total % bytesPerLine;
    if (rem) {
        if (bytesPerLine / 2 < rem) lines += 1;
        *out = total / lines;                  /* recomputed */
    }
}
```

`[I]` Consequence 1: per-camera counts may be 7-bit fields in a byte array rather
than 16-bit words. Ten such fields max out at 1,270, which cannot reach 26,928 —
so either the array is longer than `2 × NoOfCameras`, or the bytes returned by
`READ 0xFF/0x02` are not the counts this function consumes. **Unresolved.**

`[I]` Consequence 2: the effective line length is derived by integer division and
need not equal the nominal value. Basis of the §6 hypothesis.

### 9.2 Driver ini — `C:\Windows\ctx_scan_2000.ini`

`[O]` Read once at DLL load; the application must be restarted to apply changes.

```ini
[LOG]
TRACELEVEL=1        ; 0 = off; higher = more trace; slows scanning significantly
SRB_BEFORE=ON       ; log SCSI request blocks before execution
SRB_AFTER=ON        ; ... and after
SRB_IO=OFF          ; log all SCSI bus data — enormous file, avoid
DAYS=30             ; log retention [I]

[UDP]
BUFFERSIZE=32       ; receive buffer, MB
TESTBEFORESCAN=1    ; UDP speed test before scan; 0 disables

[CHECK_LINE_LENGTH]
ENABLE=ON           ; validate RLC line lengths
MAX_ERRORS=10
BEEP_ON_ERROR=ON    ; audible on error
```

`[O]` Note: `CtxScan64.dll` (64-bit) reads this **legacy-named** file; there is no
`CtxScan64.ini`.

### 9.3 `.cdf` dump — the ground-truth format

`[O]` With `[CHECK_LINE_LENGTH] ENABLE=ON`, a line-length error causes the driver
to dump a partial image in the **native Contex data format**:

```
filename pattern in DLL:  ctx%d%.02d%.02d-%.02d%.02d%.02d.cdf
location:                 current folder of the driver process
name recorded in:         ctx_scan_2000.log
```

`[I]` This is the single highest-value artifact still unobtained: it is the
vendor's own row layout, and would settle §6 and §5.4 outright.

`[O]` Log location: `C:\ProgramData\Ctx\temp\ctx_scan_2000.log` (built from
`%ALLUSERSPROFILE%` + `\Ctx\` + `\temp\`).

### 9.4 Software processing, and where calibration lives

`[O]` `ctx_scan_2000.dll` contains:

```
!!! Error for ScanWing: Scanner does not have the correct firmware
    to allow software processing !!!
```

So "software processing" — the host doing the image work on raw data — is a
supported operating mode. That is the mode our captures show: the wire carries
raw, unbalanced RGB with no colour transform applied.

`[O]` Calibration is stored in three places:

| location | evidence |
|---|---|
| scanner flash/EEPROM (authoritative) | `WScalibrate.exe`: `Error programming multiplier FLASH`, `Flash error: Unable to program`, `Vpp low`, `Unable to write to EEPROM/FLASH`; `ScanTrust/*/FullCalib.ini` sets `EstimatedFlashTime=10`, `TaskSetColorMatrices=1` |
| host files (backup) | `\Calibration\`, `No calibration files present in backup`, `No parameter block files present in backup` |
| per-session parameter blocks | `READ BUFFER 0x3C`, `WRITE BUFFER 0x48` / `0xa2` (×5, one per camera) |

`[O]` The scanner validates its calibration per camera and per channel and
reports failures as `ERROR_CALIB_A_R`, `_A_G`, `_A_B` … `ERROR_CALIB_D_B`.

#### Safety constraint — read, never write

```
ALLOWED   READ BUFFER 0x3C           parameter blocks; already in the OEM stream
NEVER     WRITE BUFFER 0xA0          Copy RAM->Flash & free RAM  (bricking risk)
NEVER     programming mode
```

`[O]` The flash commit path is `WRITE BUFFER 0xA0`
(`scanWriteBuffer 0xA0 (Copy RAM->Flash & free RAM)`). Failure modes leave the
device partially written, and **no firmware image exists anywhere in the
installer payloads** — so there is no recovery path. Writing is also
unnecessary: calibration is applied host-side.

Because the scanner applies no colour transform, a driver must supply one. See
`DESIGN.md` for the profile schema and pipeline.

---

## 10. Constants summary

```c
#define CTX_PORT_DISCOVERY    58169
#define CTX_PORT_COMMAND      58170
#define CTX_PORT_IMAGE        58171

#define CTX_SYNC              0xFFFF
#define CTX_RECORD_BYTES      2960    /* fragmented into 1480 + 1480 */
#define CTX_FRAGMENT_BYTES    1480
#define CTX_DEFAULT_ACK_EVERY   32
#define CTX_ACK_AI0            110

#define CTX_CAMERAS              5
#define CTX_UNITS_PER_INCH    1200
#define CTX_CAM_PITCH_UNITS  10080    /* 8.4 in */
#define CTX_MAX_SCAN_WIDTH   50400    /* 42.00 in */

/* 200 dpi, 42 in window — reference geometry */
#define CTX_LINE_200DPI      26928    /* bytes per row */
#define CTX_PX_200DPI         8976
#define CTX_SEAM_CORE_PX        24
#define CTX_SEAM_CUT_PX         42
#define CTX_OVERLAP_PX         111    /* [I] */

/* Colour: raw wire RGB -> vendor appearance. Fitted on 264 IT8.7/2 patches. */
#define CTX_COLOUR_MATRIX  { { 1.570f,  0.017f,  0.010f},  \
                             {-0.653f,  1.327f, -0.417f},  \
                             { 0.135f, -0.069f,  1.829f} }
#define CTX_COLOUR_OFFSET  { 53.5f, 48.1f, 40.6f }
/* residual after the matrix is ~8%: a gamma stage is still required */
```

---

## 11. Conformance checklist

A decoder is conformant when all of the following hold on the reference capture
`logrun-1025-1406.pcapng`:

- [ ] reassembly yields records of 2,960 bytes, not 1,504
- [ ] sequence numbers are continuous with no gaps
- [ ] autocorrelation of the stream peaks at **26,928** with ≥ 0.99
- [ ] the decode is 8,976 px wide and ~4,730 rows tall
- [ ] the ruler reads **2 → 108 continuously**, no restarts
- [ ] correlation against `cal-1025-1406.tif` is **≥ 0.76**
- [ ] after seam stitching, no black vertical lines remain
- [ ] the four seam joins measure ≤ 8 (mean |Δ| across the cut)
- [ ] all 264 colour patches are present, with hues matching the chart layout
- [ ] after the 3×3 matrix, mean per-channel error against the vendor TIFF is ≤ 25

A decoder is **not** conformant if it reports an image but never reassembled
fragments — that decoder is discarding half of every record.
