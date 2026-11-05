# Changelog — Open.NEXT Contex Studio

All notable changes to the **Open.NEXT** Contex HD Ultra studio, driver, and MCP server are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] — 2026-11-05

Version 2.0.0 is a major milestone release introducing memory-bounded disk streaming for extra-long sheets, native Vision-Language LLM (VLM) auto-describe, physical CIS optical channel registration, hardware safety guards, desktop packaging, and a modernized dark-mode studio WebUI.

### Added

#### Memory-Bounded Spooling & Banded Long-Document Scanning (`ctx/banded.py`)
- **Direct Disk Spooling (`ScannerEngine.run_live_scan(spool_path=...)`)**: Streams incoming high-speed UDP wire records directly to disk as a `.spool` or `.rec` container instead of buffering in host RAM.
- **Row-Banded Processing**: Processes scans in configurable row bands (default 4,000 rows $\approx 20\text{ in}$ at 200 DPI) with a 200-row Hann-tapered cross-fade overlap.
- **Strict RAM Ceilings**: Bounds peak RAM consumption to a single row band ($\sim 2\text{–}3\text{ GB}$) regardless of sheet length. Measured benchmark on a 30 ft capture (5.9 GB wire records): processed in 19 bands at **9.9 GB peak / 57 s**, preventing the 30.5 GB RAM consumption and OOM crash of single-pass processing.
- **Single-Pass Memory Guard**: Automatically checks host available memory (`MemAvailable × 0.6 ÷ 9`) and halts acquisition before stitching if a single-pass scan would trigger an OOM kill.
- **Smart Single-File Auto Selection**: Automatically selects JPEG for documents under libjpeg's 65,535-row ceiling ($\approx 218\text{ in}$ at 300 DPI, $\approx 327\text{ in}$ at 200 DPI) and switches to TIFF beyond it.
- **Multi-Part JPEG Splitting**: Sheets exceeding 65,535 rows are split into sequential `_partN.jpg` files (60,000 rows each) accompanied by a coordinate mapping manifest (`<scan>.meta.json`).
- **One-Feed Capture Preservation (`.rec`)**: Working spools are reframed into `<output>/captures/<scan_id>.rec` on disk (`long_keep_capture`, enabled by default). Long sheets (20–30 ft) are fed through physical rollers only **once**; crop, DPI, band size, and seam equalization can be re-run in minutes via `python -m ctx longscan --from-rec CAPTURE.rec` or `source_mode="rec_file"`.
- **Bounded Disk Retention**: Automatically prunes older `.rec` captures to keep the newest `long_capture_keep` (default 1), with proactive warnings when disk free space drops below 20 GB.

#### Native Vision-Language LLM (VLM) Auto-Describe (`ctx/vlm.py`)
- **Direct Multimodal Cataloging**: Zero-dependency vision cataloging eliminating the requirement for external OCR daemons or CLI bridge installations.
- **Local Ollama Auto-Detection**: Interrogates local Ollama instances (`http://localhost:11434/v1`) and detects installed vision models (`qwen2.5vl:3b`, `qwen3-vl:2b/8b`, `glm-ocr`, `llama3.2-vision`, etc.), picking the fastest available model automatically.
- **OpenAI-Compatible Multimodal Endpoint Support**: Supports remote or cloud endpoints (OpenRouter, OpenAI GPT-4o / GPT-4o-mini, vLLM, LM Studio) with custom endpoints and bearer tokens.
- **Large-Format Optimization**: Automatically downscales large-format previews to 2048 px max dimension for sub-4-second inference without VRAM saturation.
- **Structured Metadata Extraction**: Extracts `doc_type`, `title`, `description`, `visible_text` (title block / stamps), `slug`, and `tags`.
- **Automated Multi-Format Renaming**: Renames all exported formats (TIFF, JPG, PDF) to standardized descriptive filenames:
  $$\text{<Prefix>\_<YYMMDDHHMM>\_<Auto\#>\_<DocType>\_<Slug>.<ext>}$$
- **Metadata Sidecar Generation**: Writes portable `.meta.json` sidecar files containing the full VLM description, visible text, tags, and model provenance.
- **Live UI Telemetry**: Emits `scan_ocr` WebSocket events to update the browser UI, pipeline step, and filename previews live.

#### CIS Channel Registration & Repair (`ctx/registration.py`, `ctx/rescue.py`)
- **Optical RGB Row-Offset Correction**: Fixed physical CIS sensor line displacement along the paper transport axis ($R - B \approx +6\text{ px}$, $G - B \approx +3\text{ px}$ at 300 DPI) that previously caused yellow/red/blue fringing on sharp horizontal linework.
- **12,000-Row Consensus Estimation**: Samples channel cross-correlation once across 12,000 rows per sheet, eliminating inter-band offset disagreements and rainbow banding.
- **Rescue Tool (`python -m ctx rescue`)**: Per-block channel registration correction utility to repair existing captures already stitched with misaligned channels without refeeding paper.
- **Symmetric Seam Equalization**: Measures join tone steps using per-row medians and applies raised-cosine ramps across camera joins.

#### Paper Transport Safety Protocol (`ctx/engine.py`)
- **Safe Media Default (`recover=False`)**: Scans never drive transport rollers automatically if media is detected in a mid-scan state (`0x24 SCANNING`). The driver raises an actionable error and requires manual sheet recovery.
- **Roller Skew & Media Tear Prevention**: Prevents full-speed unguided `unload` motor cycles that can skew and tear long ($> 10\text{ ft}$) or delicate architectural sheets against side guides.
- **Explicit Feed Controls**: Dedicated endpoints for **Load**, **Eject Front (`0xFFFFFE`)**, **Eject Rear (`0xFFFFFF`)**, and **Recover**.

#### Modernized Studio WebUI & REST Server (`ctx/web/`, `ctx/server.py`)
- **Unified Output & Autosave Panel**: Always-visible control dock for Autosave, File Prefix, Auto-Numbering (with Reset), Auto-Describe toggle, format chips (TIFF, JPG, PDF), and an interactive folder browser modal.
- **Live Activity Beam**: Continuous animated laser activity beam on the top status strip during scanning, arming, stitching, and VLM cataloging.
- **Settings Modal Enhancements**:
  - Auto-Describe VLM configuration (endpoint URL, model selection, API key).
  - Live VLM status pill (dynamic green readiness badge).
  - Custom vision description prompt editor with default reset.
- **Multi-Render Canvas**: High-performance HTML5 canvas for 100+ MP scans with deep zoom, pan, and view tabs:
  - *Default Preset*, *Auto-Adjusted*, *AI Enhanced*, *A/B Split*, *Raw Sensor*, and *1:1 Seam Inspector*.

#### Model Context Protocol (MCP) Server (`ctx/mcp_server.py`)
- Built-in FastMCP server (`python -m ctx mcp`) exposing tools for AI orchestrators:
  - `scanner_status`, `scanner_paper_control`, `scanner_cancel_scan`, `scanner_initiate_scan`, `scanner_resolve_drift`, `scanner_calibrate_official_sheet`, `scanner_ai_enhance`, `scanner_export`, and `scan_describe`.
- Live JSON resource: `contex://status`.

#### Packaging & Desktop Integration (`packaging/`)
- `packaging/install.sh`: One-click per-user desktop launcher installer (application menu entry, SVG icon, desktop shortcut, optional autostart).
- `packaging/contex-studio`: Background session daemon managing start, stop, status, and browser launch.

#### Test Suite Expansion (`tests/`)
- Added comprehensive unit and end-to-end integration tests:
  - `tests/test_vlm.py`: VLM extraction, slug normalization, downscaling, and mocked cataloging.
  - `tests/test_channel_registration.py`: Cross-correlation offset estimation.
  - `tests/test_paper_safety.py`: Verification that `0x24` halts without moving rollers.
  - `tests/test_app_workflow.py`: End-to-end REST API, WebSocket telemetry, and export pipelines.

---

## [1.0.0] — 2026-10-28

Initial release of the open-source userspace driver and application for Contex HD Ultra scanners.

### Added
- Reverse-engineered SCSI-over-TCP/UDP communication layer (`SPECS.md`, `DESIGN.md`).
- 5-camera circular readout framing and physical bed reordering (`cam1..cam5`).
- Two-point flat-field calibration ($W_i, B_i$) and Official Contex IT8.7/2 (`R170829`) colorimetric matrix and gamma fitting.
- Operating-temperature color drift analyzer with dual-render preview and Contex calibration sheet wizard (`p/n 6779G014`).
- Optional NVIDIA CUDA acceleration via CuPy for 16-bit color transforms.
- Local AI restoration filters (seam micro-inpainting, background neutralization, shadow chroma denoising).
- Basic WebUI server and low-level CLI utilities (`ctx discover`, `ctx info`, `ctx plan`, `ctx scan`, `ctx param`, `ctx pcap`).

