# DESIGN — a Linux/macOS driver architecture

How a modern open driver for the Contex HD Ultra i4250s should be built, given
what `SPECS.md` establishes and what `STATUS.md` leaves open.

The short version: **the scanner is a sensor, not an imaging device.** It
delivers raw RGB for five camera bands; all correction, stitching and colour
work belongs on the host. Our job is to reproduce the vendor's *software
processing* path — which is, by their own error strings, the path they consider
modern:

```
!!! Error for ScanWing: Scanner does not have the correct firmware
    to allow software processing !!!
```

---

## 1. Why host-side, not scanner-side

| evidence | consequence |
|---|---|
| the wire carries raw, unbalanced RGB | the scanner applies no colour transform to image data |
| per-camera per-channel error codes `ERROR_CALIB_A_R … _E_B` exist | the scanner stores and validates per-camera calibration |
| `WScalibrate` writes FLASH/EEPROM, and keeps a `\Calibration\` file backup | calibration is *stored* in the device, and *backed up* on the host |
| the driver applies gain/matrix/gamma | the transform lives in software, on the host |

So calibration data has an authoritative home in the scanner's flash and a
working copy on the host. **We implement the working copy.** The flash copy is
an optional source to read (`READ BUFFER 0x3C`), never something we depend on.

### Hard rule: read, never write

```
ALLOWED    READ BUFFER 0x3C            (parameter blocks; already in OEM stream)
ALLOWED    replay of observed scan sequences
NEVER      WRITE BUFFER 0xA0           (Copy RAM->Flash & free RAM)
NEVER      entering programming mode
```

Flashing offers no benefit — our calibration is host-side — and carries a
bricking risk with **no recovery image held** (no `.hex`, `.s19`, firmware
`.bin`, or bootloader image exists in the installer payloads). A partially
written flash has no documented recovery path. Do not go there.

---

## 2. Pipeline

Eight stages. The order is not arbitrary; two orderings are load-bearing and are
called out below.

```
   wire
    │
1   ├─ reassemble      fragments → records → byte stream
    │
2   ├─ frame          cut rows at line length; undo the row roll
    │
3   ├─ step           apply the mid-scan horizontal step        (per scan)
    │
4   ├─ band           slice each row into 5 camera bands
    │
5   ├─ radiometry     black/white normalise + per-camera gain
    │
6   ├─ stitch         drop the 42 px gap, align overlap, blend
    │
7   ├─ colour         3×3 matrix + gamma LUT                   (global)
    │
8   └─ emit           TIFF / PNG / PDF, tiled or streamed
```

### Why 3 before 4
The mid-scan step shifts whole rows horizontally. Applied before the split it is
one array roll; applied after, it is five separate corrections that must be kept
consistent.

### Why 5 before 7
Two cameras with different gains blended together produce a visible seam;
blended after normalising to a common scale, they do not. The colour matrix is
then applied once, to the joined row.

---

## 3. Do not build five pipelines

The wire delivers camera bands **left to right within each row**, so "splitting
into cameras" is four slice operations at fixed x, not demultiplexing:

```c
row[0     .. 1877]   /* camera 1 */
row[1898  .. 3629]   /* camera 2, black gap removed */
row[3650  .. 5383]   /* camera 3 */
row[5404  .. 7134]   /* camera 4 */
row[7155  .. 8976]   /* camera 5 */
```

Five code paths would be slower *and* more complex. Instead treat the row as
**one array with per-column parameters**, built once per scan:

| LUT | size | contents |
|---|---|---|
| gain | 8,976 × 3 | per-camera gain, ramped across overlaps |
| blend | 8,976 | 1.0 in band interiors, ramp across the ~111 px overlaps |
| matrix | one 3×3 | global, applied to every pixel |

The inner loop then branches zero times:

```
out[i] = colour_matrix( row[i] * gain[i] * blend[i] + black[i] )
```

and the seam handling stops being special cases — it becomes a weight array.
This also vectorises cleanly and fits a tile of rows in L1.

---

## 4. Streaming, not batch

```
image        8,976 × 4,730 = 42.5 Mpx = 127 MB raw RGB
wire rate    42 MB/s sustained → ~1,560 rows/s
per row      26,928 bytes in
```

The driver should process **row by row as records arrive**, holding one row
(27 KB) plus an output tile. Consequences:

- RAM use is independent of image size
- the first rows can be written before the last arrive
- latency to first output is ~1 s, not the full scan time

The budget is a 3×3 multiply, a gain multiply-add and a blend per pixel at
~1,560 rows/s. That is one modern CPU core in C. **A GPU is unnecessary** — the
work is memory-bandwidth-bound on a 42 MB/s stream, and adding a GPU introduces
transfer latency for no throughput gain.

---

## 5. Calibration profile

Calibration is **data, not code**: it varies per unit, per recalibration, and
per firmware. It must never be compiled in.

```jsonc
// ~/.config/contex/<serial>.json
{
  "meta": {
    "serial": "…", "model": "HD Ultra i4250s",
    "firmware": "3-5-1-015",
    "created": "2026-10-25T00:00:00Z",
    "tool_version": "0.1",
    "target": "IT8.7/2-1993 batch 180119"
  },

  "geometry": {
    "line_bytes":  26928,
    "dpi":         200,
    "roll_px":     0,          // per-scan row rotation
    "step_cut_row": 2880,      // mid-scan step, null if none detected
    "step_shift_px": 194
  },

  "cameras": [
    // five entries, A..E
    {
      "seam_gap_px":  42,
      "overlap_px":   111,
      "x_offset_px":  0,
      "gain":        [1.05, 1.05, 1.09],
      "black":       [18, 13, 17],
      "white":       [244, 224, 146]
    }
  ],

  "colour": {
    "matrix": [[ 1.570,  0.017,  0.010],
               [-0.653,  1.327, -0.417],
               [ 0.135, -0.069,  1.829]],
    "offset": [53.5, 48.1, 40.6],
    "gamma_lut": [0, 1, 3, /* … 256 entries */]
  },

  "flat_field": {
    "columns": 8976,           // per-column gain, from a white reference
    "gain_r": "…",             // stored as a compact base64 or sidecar
    "gain_g": "…",
    "gain_b": "…"
  }
}
```

### Rules

1. **Degrade gracefully.** No profile → emit raw and say so. Never silently
   output an uncalibrated image that looks plausibly correct.
2. **Version everything.** A profile fitted against one firmware must not be
   silently reused after an update.
3. **Keep provenance.** Which target, which date, which tool version, which
   scans. When a profile goes wrong, this is what makes it debuggable.

---

## 6. Acquiring the calibration

### 6.1 Flat field first — it matters more than colour

Our decode is not merely warm, it is **uneven**: brightness falls off across the
width and differs per camera. A colour chart samples ~22 points and cannot
correct a gradient. A white strip spanning the full 42 in can.

The existing calibration sheet already carries a white area and the black band
across the full width, so **the flat field is obtainable from captures we
already hold**, before any new scanning.

### 6.2 Per-camera colour with one chart

Five IT8 charts are not needed. Use the one chart and shift the sheet by one
camera pitch (8.4 in) between scans:

```
scan 1: chart under camera 1
scan 2: shift 8.4 in  → camera 2
...
scan 5: shift again  → camera 5
```

Same physical chart every time, so comparing camera *i* to camera *i+1* on
identical patches yields the inter-camera gain directly. Chart position is
recovered from the image, so placement need not be precise.

### 6.3 What each scan yields

| parameter | source |
|---|---|
| black level | darkest patch |
| white level, per-channel gain | white patch + grey ladder |
| 3×3 matrix | colour patches — measured 25 % → 8 % error |
| gamma | the 22-step grey ladder |
| seam gap, overlap, x offset | seam measurement + content registration |

### 6.4 "Correct" has three meanings — be explicit about which

| goal | requires | status |
|---|---|---|
| cameras consistent with each other | same target seen by each camera | achievable |
| match the vendor's rendition | matched pair (pcap + TIFF from one scan) | achievable |
| true colorimetry | the chart's reference values for its batch | probably unavailable |

A printed IT8 target is only "true colour" if its measured batch data is at
hand. Without it we calibrate **relatively**, which is what most drivers do and
is sufficient here. Do not claim absolute colour without the reference data.

---

## 7. Validation

Everything must be checked against a **matched pair**: one scan, one pcap, one
TIFF, saved together. Two scans of the same sheet look identical to a human and
share almost no pixels — that mistake cost this project weeks.

```bash
python -m ctx plan                                     # verify sequence & dry-run
```

Acceptance targets, from `SPECS.md` §11:

- reassembly yields 2,960-byte records, not 1,504
- autocorrelation peaks at 26,928 with ≥ 0.99
- correlation against the matched TIFF ≥ 0.76 (current state), target ≥ 0.95
- ruler reads 2 → 108 continuously
- seam joins ≤ 8 (mean |Δ| across the cut)
- all 264 colour patches present; ≤ 25 mean error after the matrix

---

## 8. Component layout

```
ctx                    CLI entry point
  net/    discovery, TCP command channel, UDP image receiver
          fragment reassembly (§SPECS 3.3)          <- most critical code
  frame/  row framing, roll, mid-scan step
  calib/  profile load/save/validate; LUT construction
  proc/   radiometry, stitch, colour (streaming, row-oriented)
  out/    TIFF/PNG/PDF writers, tiling
ctxcal                 calibration tool
          scan target → fit parameters → write profile
ctxprobe               read-only device probe (capabilities, parameter blocks)
```

`net/fragment` and `frame` are where correctness is won or lost; `proc` is
straightforward arithmetic once the profile is right.

---

## 9. What is hard, and what is not

**Not hard:** the pipeline shape, the per-column LUTs, throughput, the colour
maths, the stitching arithmetic. All of this is a few days of ordinary work.

**Hard, and where effort should go:**

1. **The five per-camera profiles do not exist yet.** Current seam fits are
   hand-measured, gains are crude (1.05 / 1.14 / 1.20 / 0.82), one seam is
   clearly wrong. This is a calibration-fitting problem, not a coding one.
2. **The mid-scan step is unexplained** (`STATUS.md` §7). Until the cause is
   known, a driver must detect it heuristically per scan.
3. **Calibration acquisition** dominates the remaining schedule. The code is
   the easy half.

---

## 10. Known failure modes to encode

| failure | symptom | guard |
|---|---|---|
| fragments not reassembled | half of every record lost | assert record size == 2,960 |
| non-zero `CDB[10:16]` | every command returns `0x18` | zero the token on every emit |
| `READ 0xFF` qualifier misplaced | ASC `0x24` | qualifier in `CDB[5]` |
| wrong TIFF paired to a capture | correlation ~0.06, geometry "unfixable" | require one scan = one pcap = one TIFF |
| stitching judged by correlation | flat score while seams are visibly wrong | use seam position, not correlation |
| periodic target used for registration | every seam matches at the same offset | use non-periodic content or unique numerals |
| profile reused after firmware change | plausible but wrong colour | version the profile |
| uncalibrated output emitted silently | "looks fine", is not | flag missing profile explicitly |

---

## 11. Colour pipeline as built (2026-10-26) — flat field, drift analyzer, recalibrate paths

Measured and implemented in `ctx/calibration.py`. This section is the design-of-record so the
integration is not forgotten.

### 11.1 What was measured (facts that shape the design)

* The wire carries raw, uncalibrated 16-bit RGB; all colour is host-side
  (confirms §1).
* Each strip has **two illumination regimes** split by the dead band at strip
  columns [490, 512]: cols 0–489 are lit by the *left neighbour's lamp*,
  cols 513–2727 by the strip's own lamp. Strip 0's cols 0–511 are array-edge
  background.
* Within-camera illumination fall-off reaches **30%** — a per-camera scalar
  gain cannot match the seams. The calibration is therefore
  **position-indexed**: `W_i(x, channel)` per camera, per channel.
* The defect (cameras D–E render blue) is a red-channel deficit of ~2.4–2.8×
  against cameras A–C, with B comparable — a per-camera spectral difference.
* The 42″ window (12,600 px) is narrower than the camera array (13,640 px):
  camera 5's overhang is **cropped, never wrapped** (the old render's rotation
  was a bug). Stitching owns this in `ctx/pipeline.py`.

### 11.2 The correction (built)

```
corrected[x, c] = clip(round(raw[x, c] / W_camera_channel(x) * K_c), 0, 65535)
```

Pointwise in uint16 — no resampling, geometry bit-identical. W = 32-px-group
white-profile medians (locally adaptive white mask, dead band excluded,
strip-0 background excluded, linear interp, edge-padded smooth); K = camera
A's mean white. Result: per-camera chroma spread 0.134 → 0.0006–0.0011,
seam steps ≤166 u16 (below the measured noise floor). Fit + apply code:
`ctx/calibration.py`.

### 11.3 Calibration data (built)

`ctx/data/calib.json`, schema `scanner-color/flatfield-v1-app`:

```jsonc
{
  "schema": "scanner-color/flatfield-v1-app",
  "K":     {"R": 40305, "G": 41857, "B": 44098},
  "W":     {"0": {"R": [/* 85 groups */], "G": [...], "B": [...]}, /* cameras 1..4 */ },
  "source": "calib_flatfield.json",
  "fitted": "2026-10-26T15:44:48"
}
```

Aligns with §5's rules: data not code, versioned schema, provenance (source
scan + timestamp). **TODO (not forgotten): key the file per scanner serial**
(`~/.config/contex/<serial>.json` per §5) once the app manages devices; today
it is a single-unit workspace.

### 11.4 The two calibration paths (built)

1. **White-sheet button** (operator-initiated, the explicit path): feed a
   **full-width** white sheet (43″ stock; a letter sheet only covers camera A),
   scan with the standard recipe, fit W, **gate the write** on the acceptance
   numbers (seam steps < noise floor, chroma spread small), back up the
   previous file. CLI form of everything after "scan":
   `ctx.calibration.promote_margin_calibration_to_preset(...)`.
2. **Recalibrate from scan margins** (no sheet): any scan with white margins
   can promote its own self-fit — this is what the drift popup's
   "Recalibrate" does. Absorbs lamp drift, sheet-side differences and
   device-state changes (verified: a deliberately perturbed device state,
   0.028 chroma residual under a stale calibration, self-calibrates back to
   0.0006).
   Caveat to remember: the stored calibration tracks the paper family of the
   last promoted scan; a very different paper may legitimately WARN.

### 11.5 Drift analyzer (built)

`ctx.calibration.check_operating_drift` — run after each scan (or on demand):

* Measures the residual per-camera chroma spread and white-level deviation the
  **stored** calibration leaves on the scan's own white margins, and compares
  with a fresh self-fit on the same scan.
* Verdicts: **OK** (spread < 0.006 and level dev < 2%) · **WARN** (< 0.015 /
  < 6%) · **DRIFT** (beyond) · **INSUFFICIENT_WHITE** (no usable margins →
  use the white-sheet flow).
* `--popup`: operator dialog **Ignore / Recalibrate** (tkinter; console
  fallback). Recalibrate = promote this scan's self-fit (§11.4.2), backup
  kept. `--json` for UI integration; exit codes 0/1/2.
* API: `check(rec_path, calib_path, section=...)` — the app calls this after
  every scan and renders its own popup on verdict ≠ OK.

### 11.6 Firmware posture (extends §1's hard rule)

* The five `WRITE BUFFER 0x95` payloads (693 B = 3×231 each) are part of the
  arming sequence: **replay them byte-identical to the vendor capture, ignore
  their content**. They are functional (forcing slot 4 to `0xFF` changes the
  image with a smooth bed-wide profile, −2% → +24% → +1% in R) but their
  mapping is not per-camera — they are NOT a tuning dial. Characterization:
  COLOR-REPORT.md §3.
* `WRITE BUFFER 0xA0` stays **forbidden** (flash commit, no recovery image).
  `0xA0` *reads* are fine.

### 11.7 Integration checklist (TODO — do not forget)

- [ ] App UI: the **Calibrate button** (white-sheet flow, §11.4.1) with the
      acceptance gate and one-click revert to the `.bak`.
- [ ] App: call `colour_drift.check()` after every scan; popup on ≠ OK
      (§11.5) — the analyzer and thresholds exist, the wiring does not.
- [ ] Per-serial calibration storage (§5's `~/.config/contex/<serial>.json`);
      today single-unit.
- [ ] Optional auto-self-cal checkbox (promote from margins without asking) —
      default off; the popup is the default interaction.
- [ ] Colour *rendering* of non-neutral ink (grid lines read warm on cameras
      D–E): per-camera 3×3 matrix + offset fitted on the overlap after the
      flat field — the chart path of §6.2/§6.3 remains the plan; the flat
      field is its prerequisite.
- [x] Stitching: implemented in `ctx/pipeline.py` (crop camera 5's overhang,
      never wrap).
