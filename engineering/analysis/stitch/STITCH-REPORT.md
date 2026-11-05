# 5-camera stitch — report

Capture `analysis/fresh/live-5cam.rec` (comp1), validated on `live-5cam-2nd.rec`
(comp2) and `analysis/color/gridsheet-scan.rec` (gridsheet).
Stitcher: `analysis/stitch/stitch16.py`.  Colour reference: `analysis/color/white-scan.rec`.

**Result**: 13,306 × 2,775 px full-array composite (44.35 in at 300 dpi); the
42.000-in document is the first **12,600 px**.  No dead band, no zero columns,
no wrapped content, geometry reproducible to ±1 px on all three captures, and
paper level flat to 100–103 % across every join.

---

## 1. What the raster actually is  [FACT — measured]

| quantity | value | how measured |
|---|---|---|
| record count / holes | 77,420 / 0 | `calib_lib.load_rec_concat` |
| rows | 2,775 | payload bytes 227,108,936 = 2775·81,840 + 2,936 (one trailing datagram) |
| row stride | 81,840 B = 5 × 16,368 B | raw-byte autocorrelation 0.9846 at 81,840 (−0.12 at 40,920) |
| strip | 2,728 px × 3 ch × u16 LE | `STRIP_B/6` |
| hard-zero run | **23 px at strip col [490, 512]** → raster 490 + 2728·k, k = 0…4 | column profile ≤ 0 |
| masked ramp after each gap | 24–25 px at a fixed dark level (8–10 % of paper) | column profile vs post-gap paper median |
| partly masked columns *before* each gap | ~14 px | same |
| total removed per junction | **70, 66, 66, 66, 66 px** = 334 px | `module_gaps()` |
| pieces between removed bands | **468, 2658, 2662, 2662, 2662, 2194 px** = 13,306 | |
| grid period (clean windows) | **58.83 … 59.16 px**, median 58.95 ⇒ **300.5 dpi** | lattice fit, 7 of 24 windows (the rest content-dominated) |
| y offset between adjacent cameras | **+1 … +2 rows** (row-profile NCC 0.62–0.995) | not corrected — see §6 |

### The frame is a circular readout  [FACT]

The 23-px runs sit at `490 + 2728·k`, i.e. *not* on the 2,728-px datagram
boundaries, and one module is cut in two by the raster frame.  With the split
module identified as camera 5, the raster reads

```
[ cam5 right 476 ][ gap ][ cam1 2667 ][ cam2 2670 ][ cam3 2670 ][ cam4 2670 ][ cam5 left 2194 ]
```

Evidence that the module order is cam5R, cam1…cam4, cam5L:

* content is **continuous** across the four joins at raster 3212, 5940, 8668,
  11396 (cam1|cam2, cam2|cam3, cam3|cam4, cam4|cam5L) — ruler ticks, page
  borders and grid lines all line up at 1:1;
* content is **continuous across the raster frame boundary** (13,639 → 0), i.e.
  cam5's left and right halves are adjacent in the bed;
* content is **discontinuous only** at the join after piece 0 (cam5R|cam1).

The brief's "all four raster seams are physical adjacencies" is therefore
untrue: four are camera-to-camera, the fifth is the readout frame.

The brief's **"co-visible overlap o ≈ 2,492 px" model is discarded.**  Its
correlation peak came from the split module: one camera's own content appears in
two consecutive datagram strips, so strip-to-strip correlation peaks near the
strip pitch.  The sheet's 5 mm grid (59 px), 10 mm ruler labels (118 px) and four
identical target pages (≈3,300 px) alias every correlation of this sheet at
±59 / ±118 / ±3,300 px, which is why five different estimators (1-D and 2-D NCC,
multi-block, FFT, template search) agreed with themselves and disagreed with each
other.  There is no pitch to place: once the masked bands are removed, the camera
images are simply butted.

---

## 2. Pipeline (order matters)

1. **Order the camera strips 1–5.**  Reassemble each camera image from its one or
   two datagram segments (`raw_camera_images`).  Camera widths 2667–2670 px.
2. **Colour-correct each camera image** — pointwise, 16-bit, no resampling:
   `corrected = raw / (W(camera, col, ch) · s(camera, ch)) · K`.
   * `W` = that camera's spatial white response from the blank white-sheet scan
     (covers **every** column, including the steeply vignetted module edges).
   * `s` = one gain per camera/channel fitted on *this* capture's own white
     margins (absorbs lamp state and which side of the paper was scanned).
   * `K` = 40,145 (median paper level).
3. **Stitch**: concatenate the corrected camera images.  Integer column
   selection only — no resampling anywhere, `y` never searched.

`s` is a smooth **profile** (robust degree-5 polynomial in the camera's own column),
not a constant: a constant gain leaves a spatial residual where the white-sheet
response and this scan's own white differ, which shows up as a tone ramp inside
each camera at its edges.  Fitted R-channel range per camera (comp1):
`cam1 0.985–1.009`, `cam2 0.981–1.009`, `cam3 0.973–1.018`,
`cam4 1.081–1.118`, `cam5 1.100–1.140` — the known R-deficiency of cameras 4–5.

### Why the stored `calib.json` was not applied  [FACT]

`tools/colour_drift.py` verdicts, recorded before stitching:

| capture | verdict |
|---|---|
| `live-5cam.rec` (comp1) | **stored calibration does not match this scan: recalibrate before relying on colour** |
| `live-5cam-2nd.rec` (comp2) | same |
| `gridsheet-scan.rec` | stored calibration matches this scan; apply it |

Two further reasons: its `W` tables are indexed **per 2,728-px strip**, but the
camera images now in the output straddle strip boundaries; and `W["0"]` groups
0–15 (strip cols 0–511) are a flat extrapolation (`26415`) because the colour
fit skipped that region as "array-edge background" — that region is in fact
camera 5's right edge and is part of the output.  The colour *model* is
unchanged (pointwise 16-bit flat field, `analysis/color/selfcal.py`); only the
reference profile was rebuilt to cover the columns that are now visible.

Corrected white after the pre-pass: R/G/B **40,690 … 41,153** across all five
cameras (level spread 1.1 %); chroma **0.3319 … 0.3345**, spread **0.0026**
against a raw spread of 0.134.  Camera 5 is ~1 % brighter and ~0.0015 warmer than
cameras 1–4 and is visible as a faint warm tint at the right of the preview.

---

## 3. Per-seam table (comp1)

| seam | join at (bed order) | cut band | width | grid period L / R | dy | cameras |
|---|---|---|---|---|---|---|
| 1 | 2658  | 468–538   | 70 | 59.16 / 58.93 | +1 | cam1 \| cam2 |
| 2 | 5320  | 3196–3262 | 66 | 58.83 / 58.55 | +2 | cam2 \| cam3 |
| 3 | 7982  | 5924–5990 | 66 | 58.93 / 58.95 | +2 | cam3 \| cam4 |
| 4 | 10644 | 8652–8718 | 66 | 58.36 / 58.95 | +2 | cam4 \| cam5 |

Grid periods are the values from windows whose lattice fit converged
(rms ≤ 1.3 px); near every join the content is dense and the fit is not
trustworthy, so **no ±0.2 px across-join period claim is made** — see §6.

---

## 4. Six acceptance tests

**1. Feature continuity — PASS (observer-confirmed + renders).**
1:1, full-height, ±150 px renders at every join, for comp1, comp2 and gridsheet:
`seam-{comp1,comp2,gridsheet}-bed-join{0..3}-x*.png`; wider ±350 px 1:1 viewers
with pixel rulers: `viewer-seam{0..3}.png`; raw camera pairs at the ruler band at
5×: `marks-seam{0..3}.png`.  The observer reviewed seams 0–3 and judged the
alignment good.  Caveat in §5: on a 59-px grid, sub-period misalignment is nearly
invisible, so the aperiodic features (page borders, circle arcs, text) are the
real test and they are what was judged.

**2. Grid phase — PARTIAL.** The period is 58.95 px (300.5 dpi) and does not
change across the document.  The *phase* steps at each join by the removed masked
band (58 px), because that content is imaged by no camera.  **58 px ≈ 0.98 of one
grid period** — which is why the grid still looks continuous across the joins; it
is a coincidence, not evidence of alignment.

**3. No dead band / no zero columns / no wrapped content — PASS.**
`zero_columns = 0`; `dark_columns = 0`; and the paper level across each join
(90th percentile per column, rows 200–2600) is **100–103 %** of its ±60…160 px
neighbourhood with exactly **one** column per join below 90 % — that column is a
1-px grid line (present at all four joins at the 59-px pitch), not a seam
artifact.  Camera 5's two halves are joined *inside* the image (bed order) rather
than wrapped around the ends.

**4. Determinism — PASS.** Same parameters, three captures:
comp1 **13,306 px / joins 2658, 5320, 7982, 10644**;
comp2 **13,306 px / identical joins**;
gridsheet **13,305 px**, joins 2658, 5320, 7982, 10643.
Largest difference **1 px** (tolerance ±2 px).

**5. Geometry report — this section.** Output 13,306 px = 44.35 in; document crop
= first 12,600 px = 42.000 in (drops 706 px from camera 5's far end); bed mapping
= camera order 1→5, cam5 = raster piece 5 + piece 0.

**6. Colour sanity — recorded** (§2).  Colour was not treated as a variable:
the model and code path are the colour workstream's; the reference profile was
re-pointed at the columns this geometry actually exposes.

---

## 5. Window / crop decision  [INFERENCE]

The array images 13,306 px (44.35 in) of bed; the commanded window is 42.000 in =
12,600 px.  The window is a contiguous bed interval starting at the first camera-1
column, so the crop drops the far end of camera 5 (706 px = 2.35 in).  That is the
"camera-5 overhang cropped" the brief anticipated, and it is why the composite is
42.000 in and not 44.35 in.  Two renders are provided because the two readings of
"camera 5's 4 cm at the left" differ by a rotation of camera 5:

* `stitched-comp1-bed.png` — **bed order** `[cam1][cam2][cam3][cam4][cam5L][cam5R]`;
  no content discontinuity anywhere.  Recommended.
* `stitched-comp1-raster.png` — **raster order** `[cam5R][cam1][cam2][cam3][cam4][cam5L]`,
  i.e. camera 5 cut at the image's two ends exactly as the readout delivers it.

---

## 6. UNKNOWN — and the one measurement that would close it

* **Do adjacent cameras overlap at all?**  Narrow-template tests (80 px templates
  16–96 px inside each camera edge) find matches implying 140–170 px of overlap,
  *differing per seam*; wider templates find none, and the same tests on raw
  strips give 118–413 px.  All of these are alias-contaminated.  The observer
  judged the butted joins good, so the current render keeps them butted.  If a
  real overlap exists it is ≤ ~200 px, the composite would shorten by ~1.5–2 mm
  per seam, and the crop would be `width_left − (c_left − c_right)` where the two
  columns are the same physical mark seen in both cameras.  **This is the
  measurement to make next** — one distinctive mark visible in both halves of two
  `marks-seam*.png` images, read as two column numbers.
* **Masked content is unrecoverable.**  ~58 px (1.9 mm) of bed per junction is
  imaged by no camera; the joins are therefore content-lossy by construction, not
  merely misaligned.
* **y offset +1…+2 rows** between adjacent cameras.  Not corrected: the scan is a
  single raster (one feed), so a y roll would duplicate a row.  It is within the
  brief's "y is exact" margin and is the likely source of the small step seen on
  near-horizontal features at some joins.
* **Residual chroma** between cameras 1–3 and 4–5 (~0.002 r) — visible as a faint
  warm tint on the right third of the preview; level agrees to 1.6 %.
* **Committed** as `15436a0` plus the repeatability script; `analysis/stitch/`
  ignores `*.png`, `*.npy` and `cache/`, so the rendered evidence stays out of
  git while the code, numbers and report are tracked.

---

## 6b. Repeatability of the workflow  [FACT — measured, three scans]

`analysis/stitch/repeatability.py` runs the delivered pipeline unchanged
(camera order -> colour correction -> stitch) on each capture and compares the
three results.  All three captures are the **same calibration sheet**; the third
was scanned live for this test via
`tools/cap/replay_vendor.py --seconds 25 --recover --feed-credits --ack-every 1
--handshake enable --window-width 42.0 --save-records analysis/fresh/live-5cam-3rd.rec`
(77,650 records, 0 lost, 25 s).

```
.venv/bin/python analysis/stitch/repeatability.py \
    analysis/fresh/live-5cam.rec analysis/fresh/live-5cam-2nd.rec \
    analysis/fresh/live-5cam-3rd.rec --cache analysis/stitch/cache \
    --json analysis/stitch/repeatability.json
```

| capture | rows | gaps at | cameras | composite | joins |
|---|---|---|---|---|---|
| live-5cam | 2775 | 468, 3196, 5924, 8652, 11380 | 2658, 2662, 2662, 2662, 2662 | 13306 | 2658, 5320, 7982, 10644 |
| live-5cam-2nd | 2783 | identical | 2658, 2662, 2662, 2662, 2662 | 13306 | identical |
| live-5cam-3rd | 2783 | identical | 2657, 2662, 2662, 2662, 2662 | 13305 | 2657, 5319, 7981, 10643 |

**geometry**: gap starts **0 px** spread, gap widths <=1 px, composite <=1 px,
joins <=1 px.  The module geometry is a property of the device and reproduces
exactly.

**colour**: K spread 285 u16 (0.7 %); corrected-white level spread **481 u16
(1.2 %)** across all 15 camera/channel pairs; chroma-r spread **0.0022**.  The
per-scan gains do move (cam4's R gain ranged 1.08-1.26 between scans), which is
the point of fitting them per scan: the *result* stays inside 1.2 %.

**content** (each run registered to the reference by integer shift, then compared):

| comparison | shift | structure NCC | mean abs diff | page lines | verdict |
|---|---|---|---|---|---|
| 2nd vs 1st | dx -4, dy -11 | **0.942** | 2.25 % of paper | 13/14 within 1 px median | repeatable |
| 3rd vs 1st | dx +71, dy -8 | **0.775** | 4.46 % | 9/14 within 1 px median | repeatable |

**verdict: repeatable** (tolerance: geometry <=2 px, structure NCC > 0.60,
median page-line deviation <=3 px).

The 3rd scan scores lower for a mechanical reason worth stating: it was placed
**71 px (6 mm) further across the bed**, so the ~66 px masked band at each
junction removes a *different* slice of the sheet.  Geometry and colour still
reproduce exactly; the residual difference is where the unavoidable masked
content falls, not in the stitch.  Placing the sheet at the same bed position
would raise the 1st-vs-2nd figure (NCC 0.94) to be the normal case.

## 6c. Official colour calibration  [FACT — fitted and verified]

The scanner emits **raw, uncalibrated RGB**; the vendor corrects on the host with a
3x3 matrix plus offset (repo record: 264 IT8.7/2 patches, raw 64.2/63.7/64.6 ->
fitted 22.7/17.6/19.6).  The calibration sheet is the **official reference**: it
carries an IT8.7/2 chart whose absolute colorimetry ships with the scanner and is
matched to the sheet's printed charge.

```
sheet        p/n 6779G014        charge R170829
reference    extracted/wide_payload/TargetDir_-_IT8/R170829.it8
             IT8.7/2, L*a*b* D50/2 deg, SERIAL "5x7 R170829 Batch Average Data"
grey box     cols 378-2384, rows 838-2300   (34 mm / 59 mm from the sheet corner, 7x5 in)
patch grid   22 x 12 = 264 colour patches, cols 581-2264, rows 1011-1942
bands        reference white rows 2795-3545, reference black rows 3861-4766
```

Pipeline: **camera order 1-5 -> camera match -> colorimetric transform**.

1. camera 1 is the reference, but its raw white is *not* neutral (R 14 % below B),
   so the sheet's white *and* black bands set both endpoints;
2. cameras 2-5 never see the chart, so they are mapped onto camera 1 on both
   endpoints at **every column**:
   `mapped = (raw - B_i(c)) (W_1(c) - B_1(c)) / (W_i(c) - B_i(c)) + B_1(c)`;
3. `v = (mapped/65535)^0.95`, `XYZ(D50) = M v + offset`, then linear sRGB for display.

Achieved on the 264 patches (`analysis/stitch/colourcal.json`, `colourcal.py`):

| metric | value |
|---|---|
| mean abs dL, da, db | **0.83, 1.93, 2.24** |
| dE76 mean / median / p95 / max | **3.64 / 2.30 / 10.15 / 27.84** |
| dE < 3 / < 6 / > 10 | **161 / 211 / 14** of 264 |

The residual concentrates in the saturated blues (b* ~ 95-105): a 3x3 matrix is
linear and cannot bend a curved spectral locus; that needs a 3-D LUT.

**Discarded hypothesis — vendor pitch 8.4 in = 2,520 px.**  Setting the camera
pitch to 2,520 px crops ~141 px (4.7 mm) of the sheet at every seam: the seam
shows a smeared grey band with colour fringing, and the sheet's continuous cm
ruler reads 21 -> 23, 42 -> 44, 64 -> 66, 85 -> 87, i.e. exactly one label lost
per join.  The vendor's nominal figure does not describe this mode's pixel pitch;
the cameras are butted at their own width (2,658-2,662 px), which is what ships.
`stitch(..., pitch=)` remains available but defaults to off.

## 7. Reproduce

```bash
# Paths below are relative to engineering/analysis/ in the private development tree;
# the shipped stitcher lives in ctx/pipeline.py and ctx/registration.py.
python3 analysis/stitch/stitch16.py probe   analysis/fresh/live-5cam.rec
python3 analysis/stitch/stitch16.py render  analysis/fresh/live-5cam.rec analysis/stitch --order bed --tag comp1
python3 analysis/stitch/stitch16.py metrics analysis/fresh/live-5cam.rec
# validation (same parameters):
python3 analysis/stitch/stitch16.py render analysis/fresh/live-5cam-2nd.rec analysis/stitch --order bed --tag comp2
python3 analysis/stitch/stitch16.py render analysis/color/gridsheet-scan.rec analysis/stitch --order bed --tag gridsheet
```

## 8. Files

| file | content |
|---|---|
| `stitch16.py` | stitcher + colour pre-pass + acceptance metrics (CLI: `probe`, `render`, `metrics`) |
| `stitched-comp1-bed.png` | 12,600 px document preview, bed order (recommended) |
| `stitched-comp1-raster.png` | 12,600 px document preview, raster order |
| `stitched-comp1-full-bed.png` | 13,306 px full-array preview |
| `stitched-comp2-bed.png`, `stitched-gridsheet-bed.png` | validation previews |
| `seam-comp1-bed-join{0..3}-x*.png` | 1:1 full-height ±150 px at each join |
| `seam-comp2-bed-*`, `seam-gridsheet-bed-*` | same, validation captures |
| `viewer-seam{0..3}.png` | ±350 px 1:1 viewers with pixel rulers |
| `marks-seam{0..3}.png` | raw camera pairs at the ruler band, 5×, for reading a shared mark |
| `colourcal.py`, `colourcal.json` | official IT8 two-point + 3x3 + gamma calibration (fit / apply) |
| `sheet_cal.py` | earlier white-and-black-only two-point route (superseded by `colourcal.py`) |
| `repeatability.py` | runs camera order -> colour -> stitch on N captures and compares them |
| `repeatability.json` | the three-scan comparison behind §6b |
| `seam-table.json`, `grid-periods.json` | the measured numbers behind §3 and §4 |
| `stitch-comp{1,2}-bed.json`, `stitch-gridsheet-bed.json` | per-run parameters and results |
