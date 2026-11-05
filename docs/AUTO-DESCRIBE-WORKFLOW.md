# Auto-Describe & OCR Document Workflow

> **STATUS: HISTORICAL — kept for reference, superseded by the code.**
>
> The three-profile selector (Architectural / Artwork / Document), the full-page
> Tesseract pass, and the multi-format naming diagram below no longer describe
> what ships. Current behaviour:
>
> - **No profile selector.** Every scan follows one path: the pipeline's
>   `<2400 px` preview goes to the vision provider. The OCR module looks for a
>   title block and names from its fields when it finds one, otherwise from a
>   visual description. Verified on a hand-drawn sheet: the block is rejected
>   and the scan is still named (≈7 s, local).
> - **No searchable PDF for large format.** A full-page pass over a 42″ sheet
>   only bought a text layer that is not wanted here; small-format documents
>   belong to `ocr-pdf-watcher`.
> - **Filenames are** `<Prefix>_<YYMMDDHHMM>_<NNNN>_<Slug>.<ext>`. The timestamp
>   is always present; prefix, number and description each appear when their
>   switch is on. A render marker (`_Auto` / `_AI`) is added only when two
>   exports of one scan would otherwise collide. Single Final File is policy,
>   not a setting.
> - **Local-first.** A local pass over 25 s, or one with a name we would not
>   keep, escalates to a cloud vision provider when `ocr_cloud_env` is set.
> - `ocr_image_only`, `ocr_architectural`, `ocr_naming`, `ocr_cleanup` and
>   `ocr_apply` are deprecated: they still load from old settings files but
>   nothing reads them for a decision. Search `DEPRECATED(ui-naming)`.

This document details the architecture, document profiles, and processing pipeline for **Auto-Describe** and **OCR** in Contex Studio (Open.NEXT).

---

## 1. Executive Summary & Design Principles

Legacy document scanners treat OCR as a heavy, multi-stage file conversion pipeline designed around text-heavy office documents. In large-format scanning (42-inch sheets at 300 DPI, generating ~315–378 megapixels per sheet), this legacy approach introduces severe bottlenecks:

1. **60-Second Full-Bed OCR:** Running full-resolution Tesseract OCR on a 42″ sheet takes 45–60 seconds of intensive CPU compute.
2. **Double-Save Disk Thrashing:** Dumping a temporary, unsearchable PDF to disk, re-opening it in a subprocess, rasterizing it to an image, asking an AI model to describe it, and writing a second PDF creates disk thrashing and slow I/O.
3. **Format Blindness:** Only PDFs received descriptive names, leaving TIFF and JPG files as generic numbers (`scan_0001.tif`).

The revised architecture solves this with **three clean Document Profiles**, **direct in-memory Vision AI inspection**, and **unified multi-format file naming**.

---

## 2. The 3 Streamlined Document Profiles

All low-level developer switches (`OCR Depth`, `Image-only (skip OCR)`, `Name Mode`, and `Architectural drawings`) are consolidated into a single **Document Profile** selector:

| Profile | Target Documents | Engine Strategy | Primary Benefit | Sample Output Filename |
| :--- | :--- | :--- | :--- | :--- |
| **`Architectural`** | CAD blueprints, floor plans, civil & MEP drawings | **Full OCR + Title Block Parser**<br>• Tesseract runs for full text searchability<br>• Dedicated 2D spatial clustering scans margin title blocks | Full in-drawing search (`Ctrl + F`) + CAD sheet naming | `2026-10-31_Acme-Tower_A-102_Ground-Floor-Plan.pdf` |
| **`Artwork`** | Fine art, paintings, photographs, maps, sketches | **Direct Vision AI (VLM / Ollama)**<br>• Bypasses 60s Tesseract pass entirely<br>• Evaluates visual style, medium, artist, and subject | Blazing fast (~2–3 s); no wasted CPU on drawings with no text | `2026-10-31_Monet-Water-Lilies_Oil-on-Canvas.jpg` *(or .tif / .pdf)* |
| **`Document`** | Invoices, legal contracts, reports, specifications | **Full OCR + Heuristic Parser**<br>• Tesseract extracts full text layer<br>• Deterministic regex extracts date, vendor, and subject | Searchable PDF + deterministic offline categorization | `2026-10-31_Tax-Invoice_Acme-Supplies.pdf` |

### Internal Settings Mapping

When a user selects a profile in the UI, it automatically configures the underlying engine:

```javascript
// Mapping applied transparently in app.js
switch (profile) {
  case "architectural":
    ocr_architectural = true;
    ocr_image_only    = false; // Full OCR pass for searchable text
    ocr_vision        = "auto";
    ocr_naming        = "heuristic";
    break;
  case "artwork":
    ocr_architectural = false;
    ocr_image_only    = true;  // Skip Tesseract (save ~50s)
    ocr_vision        = "auto";
    ocr_naming        = "llm"; // Use Vision AI (Ollama)
    break;
  case "document":
    ocr_architectural = false;
    ocr_image_only    = false; // Full OCR pass
    ocr_vision        = "auto";
    ocr_naming        = "heuristic";
    break;
}
```

---

## 3. High-Resolution Composites vs. Preview Thumbnails

### The VRAM Problem with 42″ Full Composites
A 42″ × 30″ document scanned at 300 DPI produces a **12,600 × 25,200 pixel raster (~317 Megapixels)**:
* Ingesting 300+ megapixels into a Vision-Language Model (VLM like Qwen2.5-VL or LLaVA) splits the image into hundreds of thousands of visual patch tokens.
* This immediately crashes local GPU VRAM (Out-Of-Memory / OOM) or takes 15–30 seconds simply downsampling tensors.

### The Solution: Downsampled Preview Inspection
The scanner pipeline already generates a 1,500–2,000 px lightweight JPEG preview for the interactive UI canvas.
* **Payload Size:** ~300 KB.
* **Inference Time:** **1.5 to 2.5 seconds** on a local GPU.
* **Accuracy:**
  * **Artwork:** Easily recognizes visual subject matter, painting medium, color palette, and general composition.
  * **Title Blocks:** When fine 8 pt CAD text needs to be read without a full OCR pass, the system crops a high-DPI 800 × 600 px window of the bottom-right corner and feeds *only that crop* to the Vision AI.

---

## 4. End-to-End Processing Workflow

```
                        Contex HD Ultra i4250s (5 Cameras)
                                       │
                                       ▼
                   Stitch & Color Calibrate in Memory
                                       │
       ┌───────────────────────────────┴───────────────────────────────┐
       ▼                                                               ▼
[Artwork Profile]                                           [Architectural Profile]
  • Skip 60s Tesseract pass                                   • Run Tesseract OCR in bg
  • Send ~1500px preview to Vision AI                         • Run spatial title-block parser
  • VLM identifies subject in ~2 s                            • Extract Project, Sheet#, Title
       │                                                               │
       └───────────────────────────────┬───────────────────────────────┘
                                       │
                                       ▼
                     Generate Unified Descriptive Slug:
                 "2026-10-31_Acme-Tower_A-102_Ground-Plan"
                                       │
                                       ▼
              Save directly to all selected export formats:
               ├── 2026-10-31_Acme-Tower_A-102_Ground-Plan.tif  (Master)
               ├── 2026-10-31_Acme-Tower_A-102_Ground-Plan.jpg  (Web)
               └── 2026-10-31_Acme-Tower_A-102_Ground-Plan.pdf  (Searchable)
```

---

## 5. Rationale for Removed Controls

1. **`OCR Depth` (`auto` / `always` / `never`):**
   * `auto` is the native design of `ocr-pdf-watcher`. If text is present, it uses it; if text is absent (<30 words), it activates vision fallback. `always` wastes GPU cycles, and `never` breaks image-only sheets. It is now permanently hardcoded to `"auto"`.
2. **`Image-only (skip OCR)` Checkbox:**
   * Redundant when selecting `Artwork` (which inherently has no text to OCR) vs. `Architectural` (which inherently wants a searchable text layer).
3. **`Architectural drawings` Toggle:**
   * Consolidated into the top-level **Document Profile** dropdown.
4. **`Name Mode` Dropdown (`Descriptive` vs. `Model-assisted` vs. `Keep name`):**
   * The **`Auto describe`** toggle switch already functions as the on/off switch. `Artwork` automatically utilizes model-assisted vision, while `Architectural` and `Document` default to fast deterministic parsing with LLM fallback.
5. **`Single final PDF` / `Batch scan` Toggle:**
   * Removed from the UI. The app enforces a **Single Final File** policy by default. Users never want duplicate temporary numbered backups (`scan_0001.pdf`) or text sidecars cluttering production folders.

---

## 6. Single Final File Policy & Calibration `.rec` Captures

### Single Final File (Zero Backup Clutter)
Production scanning workflows require exactly one clean, final output file per format:
* When a scan is described and named, the final file (e.g. `2026-10-31_Acme-Tower_A-102_Ground-Plan.pdf`) is written.
* Temporary numbered working files and text sidecars (`.ocr.json`, `.ocr.md`) are automatically cleaned up (`ocr_cleanup = true`).
* Operators are never left with duplicate files like `scan_0001.pdf` sitting alongside the named document.

### Raw `.rec` Stream Captures (Calibration Wizard Only)
Raw `.rec` stream files capture the uncalibrated 16-bit 5-camera UDP stream directly from scanner hardware:
* Regular day-to-day scanning jobs **never** save `.rec` files.
* `.rec` capture is strictly isolated to the **Calibration Sheet Wizard** modal via the **`Save Debug Capture (.rec + TIFF/PNG)`** button.
* This ensures reusable raw sensor streams are preserved for IT8/flat-field calibration and diagnostic replay without polluting regular output directories.



---

## 7. Left Sidebar: Scan Pipeline vs. AI Enhance Refactoring

### Scan Pipeline Order of Operations
The scanner engine processes each scan in this exact linear order:
1. **Raw Sensor Line Acquisition**: 16-bit 5-camera UDP stream capture.
2. **Thermal & Operating Temp Drift Compensation** (`apply_camera_calibration`):
   * Per-scan polynomial gain shifts `s(cam, col, ch)` applied to white margins across Cameras 1–5.
3. **5-Camera Stitching** (`stitch_cameras`):
   * Assembly of the 5 staggered sensor segments at 4 boundary joins (`doc_joins`).
4. **IT8 Color Profile Matrix** (`apply_it8_transform_u16`):
   * Converts sensor color space to calibrated sRGB.
5. **Initial Seam Blending** (`blend_seam_color_variation_u16` / `blend_seam_color_artwork_u16`):
   * Feathers analog gain differences across the 4 camera joins so the initial scan (`Default Preset` and `Auto-Adjusted`) is seamless right off the bed.

### Architectural Separation: Pipeline vs. Post-Processing
* **In `Scan Setup`**:
  * **`Blend Seam` toggle** and mode (**`Linework`** / **`Artwork`**): Directly configure the initial scan pipeline.
  * **`Advanced Seam Tuning`** accordion: Collapses the internal tuning parameters (`Blend Width: ±128 px` and `Strength: 85%`) out of view for everyday scanning.
* **In `AI & Enhance`**:
  * **Removed redundant `Analog Seam Color Harmonization`**: The backend previously ran the same color-feathering algorithm a second time on top of already-blended scan pixels, causing unnecessary blur.
  * `AI & Enhance` is strictly reserved for true post-processing corrections:
    1. **`4-Seam Micro-Inpaint & Step Healing`**: Inpaints physical/mechanical step discontinuities along the seam line.
    2. **`Background Paper Clean & Whiten`**: Neutralizes aged paper cast and drafting vellum yellowing.
    3. **`Shadow & Chroma Denoise`**: Cleans sensor noise in dark zones and Camera 4/5 thermal chroma drift.
    4. **`Linework Crispness & Sharpen`**: Unsharp masking for blueprint vectors and pencil linework.
    5. **`Photocraft / Generative AI Webhook`**: External generative restoration models.

### Dynamic Cross-Feed Width & Orientation Consolidation
* **UI Pattern**: `Paper Size / Auto-Width [ Feed Width xx ]`
* The cross-feed width is computed directly from paper dimension presets and feed orientation (`portrait` vs. `landscape` clamped to the 42" bed).
* The standalone `Width (in)` field was eliminated; the numeric input remains hidden unless `Custom Width (Inches)` is selected.
* The redundant orientation header text was removed, leaving the `Portrait` / `Landscape` toggle cleanly docked under Paper Size.

### Two-Card Separation in `Scan Setup`
The `Scan Setup` tab is divided into two distinct logical cards:
1. **Card 1: `Scan Configuration` (Everyday Scan Operations)**
   * Scan Source (`Live Hardware`, `Offline Capture .rec`, Demo modes)
   * Paper Size / Auto-Width with dynamic feed width reporting
   * Feed Orientation (`Portrait` / `Landscape`)
   * Side-by-side row: `Resolution (DPI)` and `Color Mode` (`Color RGB 24-bit` / `Grayscale 8-bit`)
2. **Card 2: `Sensor Temp & Color Drift` (Hardware Calibration & Optical Tuning)**
   * **`Colour / Temp Drift Check` Toggle & Compact Action Dropdown**: Checkbox enables/disables margin drift analysis (`drift_policy: "disabled"`); right dropdown selects the reaction: **`Ask`** (`"prompt"`) vs. **`Auto-Correct`** (`"auto_adjust"`).
   * Active Calibration Preset selector with rollback/rename/delete actions.
   * `Blend Seam` toggle with concise `Linework` / `Artwork` mode dropdown.
   * Collapsible `⚙ Advanced Seam Tuning` accordion hiding raw pixel sliders (`Blend Width: ±128 px`, `Strength: 85%`).
   * Live sensor thermal telemetry and per-camera drift metrics bars.

### UI Folder Tab Visual Identity
* The left sidebar tabs (`Scan Setup` and `AI & Enhance`) are styled as physical folder tabs with seamless background continuity into the card below (no dividing line).
* `AI & Enhance` tab and panel maintain a distinct, slightly lighter indigo-slate tint (`rgba(30, 42, 68, 0.95)` / `0.65` inactive) with violet highlights so operators always know when they are working in post-processing mode versus scan acquisition.
