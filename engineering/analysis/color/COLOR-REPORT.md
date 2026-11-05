# COLOR-REPORT — Route A per-camera colour calibration (2026-10-26)

Workstream: make the five cameras in one scan agree in colour and tone, with no
visible colour step at the seams. Nothing else. All work under `analysis/color/`.
No tracked docs were edited; nothing was committed.

---

## 0. Headline

> **Route A succeeded.** A per-camera, per-channel **flat-field correction**
> (divide each camera by its own white-field profile `W_i(x, channel)`, rescale to
> camera 0's white) brings all five cameras to chromaticity agreement of
> **0.0006–0.0011 peak-to-peak** (raw spread was **0.134**; blind transfer
> 0.0015), and drops every seam white-step from up to **2,742** u16 to
> **≤166 u16 self-calibrated / ≤256 u16 blind** — below the measured noise
> floor — with **bit-identical geometry** (pointwise operation, no resampling).
> Route B ran: forcing `0x95` write #4 to `0xFF` changed the image with a
> smooth bed-wide profile (−2% → +24% → +1% in R) — the writes are a real
> device-side lever, but a per-camera mapping is **not** demonstrated (§3).

---

## 1. FACT (measured this session, commands/scripts under `analysis/color/`)

### Capture / container facts

* `analysis/fresh/live-5cam.rec`: 77,420 records, seqs 0..77,419 dense, **zero
  holes**; 182 records with `dlen < 2936`; 227,108,936 payload bytes → **2,775
  rows**. `live-5cam-2nd.rec`: 77,646 records, zero holes, 2,783 rows.
* **Reconstruction = concatenation of payloads in seq order.** Proof: all 182
  short records end at exactly one residue class, `B ≡ 2936 (mod 81,840)`, in
  concat space (182/182); concat autocorr r = **0.9846** @ 81,840 vs **0.9393**
  for slot placement; concat row count matches the verdict's 2,775. The short
  datagrams are scanner scheduling, not holes; no bytes are skipped.
* White-field scan (`analysis/color/white-scan.rec`, back of the calibration
  sheet, this session): 202,016 records, zero holes, 7,240 rows, 592.5 MB.
  Only allowed writes used (`0x48`, `0xA2`, `0x95`×5; the `0xA0` entries are
  READ BUF, not the forbidden flash-commit write).

### The defect, quantified (white field, raw)

| camera | white R | white G | white B | chroma r | chroma b |
|---|---|---|---|---|---|
| 0 | 41,000 | 44,693 | 47,546 | 0.3077 | 0.3569 |
| 1 | 41,200 | 46,860 | 48,719 | 0.3012 | 0.3562 |
| 2 | 36,033 | 45,216 | 47,340 | 0.2802 | 0.3681 |
| 3 | 16,991 | 31,355 | 43,975 | 0.1840 | 0.4763 |
| 4 | 14,794 | 31,926 | 42,622 | 0.1656 | 0.4771 |

The "blue" of cameras 3–4 is a **red-channel deficit of ~2.4–2.8×** with B
essentially unchanged — a per-camera spectral response difference, not a
scalar gain.

### Structure facts (new, load-bearing)

* **Dead band**: every strip has exactly one hard-zero band at columns
  **[490, 512]** (23 px core; ~45 px with transition). All five identical.
* **Two illumination regimes per strip**: the white field shows strip *i*'s
  columns 0–489 (left of the band) carry the **left neighbour's lamp level**
  (e.g. strip 3 cols 0–512 read R≈31k = camera-2-like, vs R≈15k in its own
  zone), and columns 513–2727 carry the strip's own lamp. Strip 0's cols 0–511
  are **array-edge background** (nothing left of camera 1); the sheet's left
  edge is further right. The visible tone step in any assembly therefore
  happens **across each dead band**, not at the overlap.
* **Within-camera illumination fall-off is real and steep**: e.g. camera 3's
  own-zone white R falls 16.7k → 11.4k (−32%) across its width; camera 4's B
  swings 39.1k → 45.8k → 39.5k. A single per-camera gain **cannot** match the
  seams (it would leave a ~30% white-level step at seam 3).

### The correction that works

* Model: `corrected = clip(round(raw / W_i_c(x) · K_c))`, per camera `i`, per
  channel `c`, pointwise in uint16 (no resampling, no shift). `W_i_c(x)` =
  32-px-group medians of flat-white block means (locally adaptive mask:
  `L > 0.88 × group-p95`, block std < 1500, dead band excluded, absolute
  background floor `0.50 × strip-paper-level`, strip 0 cols < 512 excluded),
  linear interpolation across gaps, 3-group edge-padded smoothing. `K` =
  camera 0's mean white. Full definition in `analysis/color/calib.json`.
* Two equivalent fitting routes, both verified:
  1. **Dedicated white field** (white-scan.rec → fixed calibration, applied to
     other scans).
  2. **Self-calibration** (fit `W` from any capture's own white regions — the
     composite is mostly white paper; recommended default because it also
     absorbs per-scan lamp state and sheet-side differences).

### Noise floor (from the dedicated white field, corrected)

* Two flat white regions per camera (row halves): per-channel `|meanA − meanB|`
  = **4–383 u16** (median ≈ 125).
* Per-pixel σ on white: **825–1,393 u16** (cameras 3–4's R inflated by the
  ×2.9–3.9 correction gain).
* Corrected white flatness vs K across x: ±0.8–3.4k worst (camera 3 R ≈ ±8%),
  residual of the profile fit on steep gradients.

---

## 2. Acceptance numbers (§6)

### (1) Seam colour step — mean signed Δ per channel across each overlap band
(white-pixel-restricted, nominal pairing; `src − ref`; u16)

| seam | raw comp1 | corrected comp1 (self) | blind comp2 (W from comp1) | raw comp2 | corrected comp2 (self) |
|---|---|---|---|---|---|
| 0 | −1202/−1239/−1955 | **+49/+40/+30** | +96/+127/+112 | −1145/−1146/−1879 | **−1/+11/+11** |
| 1 | −2742/−1784/−2230 | **+12/−8/−23** | +249/+229/+197 | −2522/−1540/−2013 | **+22/−2/−45** |
| 2 | −846/−1587/−2124 | **−45/−53/−14** | −67/−123/−77 | −875/−1672/−2206 | **+44/+48/+58** |
| 3 | −409/−972/−1228 | **−13/−65/−164** | −27/−59/−142 | −414/−973/−1213 | **−26/−28/−174** |

All corrected steps ≤ **249 u16** (≤ 0.6%), below the two-region noise floor
(≤ 383, median ≈ 125). **Pass.**

### (2) Neutrality — per-camera chromaticity in flat white (r, g, b)

* Raw chroma spread across cameras: **0.134** in r (0.1656…0.3181).
* Corrected (self-cal), peak-to-peak across cameras: comp1 **0.0010** (r) /
  0.0009 (b); comp2 **0.0011** / 0.0009; white field **0.0006** / 0.0006.
  **Pass.**

### (3) Cross-camera consistency — same physical patch, two cameras

* The corrected white means on the dedicated white field agree within
  **±100 u16** of K on every camera/channel (e.g. R: 43,175/43,084/43,105/
  43,146/43,250). On the composites the seam-pair numbers in (1) are the
  same-patch test. **Pass.**

### (4) No geometry change

* Correction is pointwise per (camera, column, channel): dead-band columns are
  **bit-identical** raw vs corrected on all three captures (verified by
  re-detection); seam x-positions unchanged; no resampling anywhere. **Pass.**

### (5) Blind validation

* `W` fitted on `live-5cam.rec` applied to `live-5cam-2nd.rec` (never seen by
  the fit): chroma spread 0.0015 peak-to-peak, seam steps ≤ 256 u16. **Pass.**

### Visual

* `analysis/color/corrected-comp1.png`, `corrected-comp2-blind.png` vs
  `assembled-raw-comp1.png` — the blue cast is gone; all five panels neutral.
  Known rendering caveat: the printed grid lines render slightly warm on
  cameras 3–4 (see UNKNOWN/next steps). The assembly uses raster order
  [cam1..cam5] at nominal 2,520 px pitch; overlap/dead-band cropping and the
  cam-5 wrap are the stitching module's scope.

---

## 3. Route B verdict — **the writes are functional, but a per-camera lever is NOT demonstrated**

Two white-sheet scans, same sheet, same format (202,016 / 201,956 records,
zero holes each): baseline with the captured `0x95` payloads
(`white-scan.rec`), then `--slot-fill 4=255` (`slot4.rec`; the tool logged
`slot 4 of 5: payload forced to 0xff x693`, all five writes GOOD). An earlier
attempt with no sheet in the feeder aborted before any scan (`recover failed`,
1 frame — kept as `slot4-aborted.rec`).

**Result: the image changed far beyond drift, and the change is a smooth
spatial gradient across the whole bed — not a single-camera switch.**

Scan-to-scan drift reference: comp1→comp2 (8 min apart) per-camera white means
moved ≤ 0.13%. Against that, forcing write #4 produced, on the white field:

| raster strip | Δwhite R | ΔG | ΔB | R-delta profile along x |
|---|---|---|---|---|
| 0 | −2.3% | −3.0% | −1.0% | uniform ≈ −2% |
| 1 | +0.3% | −2.1% | −0.1% | −2% rising to +5.7% |
| 2 | +7.4% | −1.4% | +0.5% | +6% rising to +11.5% |
| 3 | **+17.0%** | +6.3% | +2.7% | +10% rising to **+23.7%** |
| 4 | **+9.3%** | +3.3% | +2.0% | +21% falling to +1.4% |

(full per-group profiles: `route_b_verdict.py`; numbers:
`route_b_verdict.json`; full-image evidence: `route_b_full_base_vs_test.png`
— the complete raster of both scans, all five cameras — and
`route_b_ratio_map.png` — per-pixel R ratio slot4/baseline, stretched
0.95–1.25, showing the smooth bed-wide gradient directly. Strip-mean R
ratios: 0.978 / 1.000 / 1.069 / 1.195 / 1.096. The ratio map also shows
feed-direction banding of ~1–2% and the front-side printing ghosting
through the sheet; both are below the effect's amplitude.)

Read plainly: the `0x95` writes are **not inert** — a device-side tone/level
lever exists. But write #4 alone moved **every** camera with a smooth
bed-wide profile (negative at the far left, peaking ≈ +24% R near the
camera-4/5 boundary, ≈ +1% at the far right). The five writes' mapping to
cameras or bed regions is **not established** by this experiment; candidates
(segments of one position-indexed gain/flat-field table, blended per-camera
tables, or a lamp/AGC interaction — the far-left dip would fit a global
auto-exposure compensating the boost) are not separable without more
experiments. Per the brief: recorded, stopped, no device tuning attempted.

**Practical consequence unchanged: Route A (host-side flat field) is the
reliable path for camera matching.** The device lever is a lead for a future
workstream, nothing more.

Applying the SAME baseline-fitted flat field to both scans makes the lever's
nature visible: the corrected baseline is neutral on all five cameras
(chroma spread 0.0006), while the corrected slot4 scan develops a residual
per-camera chroma spread of **0.028** (cams 3-4 read R-heavy: r = 0.336 /
0.323 vs 0.309-0.312 on cams 0-1) — the write perturbs each camera's
*colour balance*, not just level, so it cannot substitute for calibration.
Render: `route_b_corrected_base_vs_test.png`; numbers:
`route_b_corrected_chroma.json`. Note also that the raw qualitative blue
cast is essentially unchanged by the write (+17% R against the ~170%
needed), which is why the uncorrected A/B pair looks like "the same
colour shift".

Self-calibration closes the loop: fitting `W` on the slot4 scan itself
restores chroma spread to **0.0006** (`route_b_selfcal_chroma.json`) —
i.e. even under a deliberately perturbed device state, per-scan
self-calibration from the capture's own white regions absorbs the state
completely. Practical recipe: self-calibrate every scan that contains
white margins; fall back to the dedicated white-field calibration only
for scans without usable white.

---

## 4. INFERENCE (strong but not proven)

* Each camera's lamp illuminates its own window **plus the first ~490 px of the
  next camera's FOV** (left of that camera's slit). This explains: identical
  dead-band columns in all strips; the neighbour-level white response in each
  strip's left segment; why seam-pair tone agreement is achievable at all; and
  why the visible tone step sits at each dead band.
* The per-camera spectral differences (cams 3–4 R-deficient) live in the
  lamp/sensor combination per zone; a fixed calibration normalized on one
  paper side transfers white-level perfectly but leaves a small chroma
  difference (Δr ≈ 0.016) between lamp zones on the *other* paper side —
  hence the self-calibration recommendation.
* The scanner's short datagrams (182 in live-5cam.rec) are periodic scheduling
  artefacts at row-boundary-related positions, not data loss; concatenation is
  the correct raster reconstruction for these captures.

## 5. UNKNOWN

* What the five `0x95` writes actually address (per-camera tables, segments of
  one position-indexed table, or lamp/AGC control): write #4 demonstrably
  changes the image with a smooth bed-wide profile, but per-camera mapping is
  unproven (§3).
* Why cams 3–4 are R-deficient (lamp spectrum vs sensor response — not
  separable without a per-camera spectral target).
* The exact bed↔array offset and the cam-5 window wrap (stitching module's
  scope; the raster order [cam1..cam5] is established by the dead-band
  positions and cross-seam content correlation, but the window crop/wrap is not).
* Whether the corrected white flatness residual on cams 3–4 R (±8%) is profile-
  fit error or real illumination instability within a scan.

## 6. Next measurements (in priority order)

1. `0x95` semantics: an A/B series (one write at a time, slots 1–5, fixed
   sheet) would map the writes to bed regions; a grey/step target would show
   whether the effect is level-dependent (tone curve) or pure gain.
2. If non-neutral ink colour matters (grid lines render warm on cams 3–4):
   fit a per-camera **3×3 matrix + offset** on the co-visible overlap after the
   flat field (the brief's escalation path; the flat field is a prerequisite —
   it removed the level differences that previously swamped any matrix fit).
3. Refit `W` at 16–32 px groups with a robust smoother if the cams 3–4 R
   flatness residual (±8%) matters for the application.
4. Stitching integration: the flat field is a pointwise pre-pass — apply
   `correct()` (selfcal.py / calib.json) to any 16-bit 5-camera raster before
   a `tools/render_stitched.py`-style pipeline. That renderer currently
   targets the 8-bit format (line-bytes 26,928, `scan007-*`); adapting its
   framing to stride 81,840 is the stitching module's scope.

## 9. Drift analyzer (delivered after the main report)

`tools/colour_drift.py` — checks a scan's white margins against the stored
calibration and returns OK / WARN / DRIFT / INSUFFICIENT_WHITE
(thresholds: chroma spread 0.006/0.015, white-level deviation 2%/6%).
`--popup` asks the operator to ignore or recalibrate; recalibrate = promote
the scan's own self-fit into the stored calibration file (backup kept).
Demonstrated live on `gridsheet-scan.rec` (front-side grid-geometric sheet,
77,450 records, zero holes): stored back-side white-field calibration →
DRIFT (level_dev 7.7%) → promote → OK (spread 0.0011, level 0.8%).
`calib.json` now holds the promoted app-format calibration
(`scanner-color/flatfield-v1-app`). Caveat: the stored calibration tracks
the paper family of the last promoted scan; the white-sheet button remains
the neutral reference flow.

## 7. Repo claims discarded or corrected this session

* "Reuse `decode_rec.py`" — its slot-placement reconstruction (`buf[s*2936]`)
  is **wrong for these captures** (autocorr 0.9393 vs 0.9846 concat; wrong row
  count). Concatenation is correct; kept as `analysis/color/calib_lib.py`.
* Nominal-pitch overlap fitting (Route A as originally sketched) was **not
  needed** for the colour model — alignment-free white-field matching replaced
  it; the aliasing swamp (59-px grid comb in every content correlation) is
  documented in `analysis/color/offset_*.py` outputs for the record.
* The prior session's `5cam-assembled.png` arrangement (partial cam 5 at the
  left) is a render-time rotation; the raster itself is [cam1..cam5] with
  per-strip dead bands at [490,512] — the crop/wrap belongs to the stitcher.

## 8. Deliverables (all under `analysis/color/`)

* `calib.json` — the per-camera transforms (both fitting routes), model,
  fit capture, residuals, noise floor.
* `corrected-comp1.png`, `corrected-comp2-blind.png`, `assembled-raw-comp1.png`
  — renders (raster order, nominal pitch, captioned).
* `seam{0..3}-{raw,corrected}-{comp1,comp2}.png` — seam neighbourhood crops.
* Route B evidence: `route_b_full_base_vs_test.png` (complete raster of both
  scans), `route_b_ratio_map.png` (per-pixel R ratio, 0.95–1.25 stretch),
  `route_b_corrected_base_vs_test.png` (same calibration applied to both —
  the lever's per-camera colour-balance perturbation), `route_b_verdict.py`
  + `route_b_verdict.json`, `route_b_corrected_chroma.json`,
  `route_b_selfcal_chroma.json`; `white-scan.rec` (dedicated white field,
  202,016 records), `slot4.rec` (Route B test scan, 201,956 records,
  slot 4 = 0xFF), `slot4-aborted.rec` (first attempt, no sheet, 1 record —
  kept for the record).
* Scripts: `calib_lib.py` (loader/geometry), `selfcal.py` (fit+apply+metrics),
  `fit_flatfield2.py`, `noise_floor.py`, `offset_*.py` (alignment
  investigation record), `probe_*.py` (container/layout proofs),
  `white_scan_cal.py`, `white_diag.py`, `grid_*.py`, `arc_trace.py`,
  `seam_*.py`, `apply_metrics*.py`; metrics `*.json` +
  `noise_floor.json`. Capture `.rec` files stay local per `.gitignore`
  (copies of the two white scans are committed as evidence renders only).

**Do the five cameras agree now?** Yes — on white, to chroma spread
0.0006–0.0011 peak-to-peak (blind 0.0015) and seam steps ≤166 u16
self-calibrated / ≤256 u16 blind — all below the measured noise floor;
self-calibration also absorbs deliberate device-state perturbation
(§3). What remains is colour
*rendering* of non-neutral ink across lamp zones (item 6.2) and everything
geometric (stitching module).
