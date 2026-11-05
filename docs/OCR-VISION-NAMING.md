# OCR, Vision & Descriptive Filenames

> **PARTLY SUPERSEDED — the architecture below still holds; the controls do not.**
>
> The module, the stdio bridge and the MCP tools are unchanged. What changed is
> the operator surface and the default policy:
>
> - There is **no document-type selector** any more. Every scan takes one path:
>   the module looks for a title block and falls back to a visual description.
>   "Image-only (skip OCR)" and "Architectural drawings" no longer exist as
>   settings; `skip_ocr` and `architectural` are derived.
> - **No full-page Tesseract pass for large format**, so *Single final PDF* is
>   policy rather than a switch: one descriptive file, no `.ocr.md`/`.ocr.json`
>   sidecars left beside it.
> - Filenames are `<Prefix>_<YYMMDDHHMM>_<NNNN>_<Slug>.<ext>`. The timestamp is
>   always present and is composed by the app, not by the module
>   (`timestamp_prefix=false` is passed down).
>
> Sections **Single final PDF**, **Image-only mode**, **Architectural drawings**
> and **Autosave naming rules** describe the previous behaviour. See
> [README § Naming, OCR & Descriptive Filenames](../README.md#naming-ocr--descriptive-filenames)
> for what ships now.

Open.NEXT reads scans with the **shared `ocr-md` module** that
`ocr-pdf-watcher` already runs in production. There is one OCR engine, one
vision fallback and one naming implementation; the app and the folder watcher
cannot drift apart.

```
┌──────────────────────┐        MCP (JSON-RPC over stdio)        ┌────────────────────────┐
│  Open.NEXT            │ ──────────────────────────────────────▶ │  ocr-md MCP server     │
│  ctx/ocr_bridge.py    │ ◀────────────────────────────────────── │  ocr_md/mcp_server.py  │
└──────────┬───────────┘                                          └───────────┬────────────┘
           │                                                                   │
  /api/scans/{id}/ocr                                               ocr_md/service.py
  MCP: scan_ocr / scan_describe                                     ocr_md/engine.py (OCRmyPDF,
  autosave background pass                                          ocr_md/vision.py (local VLM)
                                                                    ocr_md/naming.py
                                                                    ocr_md/ingest.py
┌──────────────────────┐   direct import (same package)
│ ocr-pdf-watcher      │ ───────────────────────────────────────▶ ocr_md.*
│ watcher.py           │
└──────────────────────┘
```

## What was adopted from ocr-pdf-watcher

| Piece | Source | Why it is reused |
|---|---|---|
| OCRmyPDF engine, deskew, prior-OCR policy (`force/skip/redo`) | `ocr_md/engine.py` | Battle-tested on real scans; never keeps a bad scanner text layer |
| Confidence-scored per-page orientation | `ocr_md/orient.py`, `confidence.py` | OSD misreads sparse large-format sheets; this does not |
| Text-layer triage (skip OCR when a good layer exists) | `ocr_md/service.py` | Saves a full OCR pass on born-digital PDFs |
| Markdown + provenance manifest (`.ocr.md`, `.ocr.json`) | `ocr_md/service.py` | Same artefacts for the app, the watcher and the KB |
| MCP stdio transport (no SDK, stdout clean) | `ocr_md/mcp_server.py` | Runs in the watcher venv; Open.NEXT needs no new dependency |
| Line-box JSON protocol + salvage parser | `ocr_md/engine.py` (Nemotron) | Reused for the optional box-placement mode |
| Invisible text injection (`render_mode=3`) | `ocr_md/engine.py` (GLM/Nemotron) | Keeps the scan bitmap untouched while making it searchable |

New in this integration:

* `ocr_md/vision.py` — local Ollama / OpenAI-compatible VLM client, a
  transcription engine for image-only pages, and structured descriptions.
* `ocr_md/naming.py` — deterministic date/type/entity/subject extraction with
  optional model refinement.
* `ocr_md/ingest.py` — one call that ties OCR, vision, description and naming
  together and can copy/rename the artefacts.
* `ctx/ocr_bridge.py` — stdlib-only MCP client with command discovery,
  restart and timeouts.

## Install

The OCR module lives in the watcher checkout and is installable:

```bash
# into the watcher venv (it already has the OCR dependencies)
cd /path/to/ocr-pdf-watcher
python -m pip install -e .

# local vision model (once)
ollama pull qwen2.5vl:3b
```

Open.NEXT discovers it automatically in this order:

1. `settings.ocr_command` (Settings → OCR, or `app_settings.json`);
2. `OPENNEXT_OCR_COMMAND` (shell-like, e.g.
   `"/path/venv/bin/python -m ocr_md.mcp_server"`);
3. `OPENNEXT_OCR_PYTHON` (or the app's interpreter) with `ocr_md` importable;
4. a sibling `~/Documents/ocr-pdf-watcher` checkout (`.venv` + repo root);
5. `/opt/ocr-pdf-watcher`;
6. `python3 -m ocr_md.mcp_server` on `PATH`.

Each candidate is probed once with `python -c "import ocr_md.mcp_server"`.
If nothing is found the app keeps working; only OCR/naming is disabled, and
`/api/ocr/status` explains why.

## Using it

### Autosave (zero-click)

Turn on **OCR & name** in the Output & Autosave dock (off by default: OCR on a
42-inch sheet is a background job, not an instant one). After each autosaved PDF
the app runs, in a background thread:

1. OCR (skipped when the PDF already has a good text layer);
2. vision fallback when the page has fewer than 30 words;
3. a structured description (document type, title, tags);
4. a descriptive filename.

`Apply Name` controls what happens:

| Mode | Result |
|---|---|
| `rename` (default when cleanup is on) | one `2026-10-14_Tax-Invoice_Acme.pdf`; the numbered export is removed |
| `copy` | keeps `scan_0087.pdf`, writes a descriptive copy |
| `none` | keeps the filename, writes `scan_0087.ocr.json` with the suggestion |

### Single final PDF

With **Single final PDF** on (default), the app collapses the run to exactly one
file:

1. the OCR artefact becomes `<descriptive-name>.pdf` (no `.ocr` suffix);
2. **title, subject and keywords are embedded in the PDF metadata** — the
   description travels with the file;
3. `.ocr.md` and `.ocr.json` are deleted;
4. the numbered `arch_0001_Auto.pdf` is deleted after the new PDF is verified;
5. `<number>.meta.json` is kept (small, and the app uses it for numbering and
   collision checks) and repointed to the final PDF.

Only PDF is affected; if you also export TIFF/JPG they are left alone. Turn the
toggle off to keep the numbered original and the sidecars.

### Autosave and naming

Auto-naming runs after autosave, so the two are linked: enabling **OCR & name**
turns autosave on, and turning autosave off turns OCR & name off. The backend
enforces the same invariant, so a stale settings file cannot leave naming on
with no autosave.

### Image-only mode (known artwork / drawings)

Turn on **Image-only (skip OCR)** when you already know the scan has no
meaningful text. The app then skips the Tesseract/OCRmyPDF pass entirely
(the 40–60 s cost on a 42-inch sheet) and keeps the rest of the pipeline:

| Also set | Result |
|---|---|
| `Image-only Pages = auto/always` | vision transcribes the page into a searchable PDF, plus description and name |
| `Image-only Pages = never` | no vision transcription; a description is still taken so the name is meaningful |
| `Name Mode = off` | no naming and no description: a true pass-through |

It is per settings, not permanent: toggle it off for the next batch of
text documents. API equivalent: `{"image_only": true}`; MCP:
`scan_ocr(scan_id, image_only=True)`.

### Architectural drawings (title block)

Turn on **Architectural drawings** when the scan is a building sheet. The app
then:

1. reads the OCR words with their boxes (no extra OCR pass);
2. clusters them and scores the blocks by title-block vocabulary
   (`PROJECT`, `SHEET`, `DWG`, `TITLE`, `SCALE`, `REV`, `DATE`) and proximity
   to the right/bottom edge;
3. extracts **project**, **sheet title**, **sheet number**, revision, date and
   scale from the winning block;
4. names the file as
   `2026-07-03_Riverside-Community-Centre_A-102_Ground-Floor-Plan.pdf`;
5. if the block is unreadable (image-only sheet), renders only the block — or
   the conventional bottom-right / right-strip / bottom-strip crops — at high
   DPI and asks the vision model to read it.

The block is appended to the Markdown as `## Title block`, and the manifest
gets a `title_block` section with the bbox, confidence, keywords and fields.
If nothing convincing is found (or the crop is a registration stamp, not a
title block), the generic heuristic is used instead — the file is never named
from a stamp.

API: `{"architectural": true}`; MCP: `scan_ocr(scan_id, architectural=True)`.
The module also exposes `ocr_title_block(path)` and
`python -m ocr_md.cli --title-block sheet.pdf` for inspection.

Limitations: detection assumes a conventional right-edge or bottom title
block; a rotated block or one placed mid-sheet may be missed. Ambiguous
`03/07/2026` dates still follow the invoice convention (month-first) unless the
page says otherwise.

### On demand

```bash
# status: module, tools and vision model
curl -s localhost:8000/api/ocr/status | jq

# run now (synchronous)
curl -s -X POST localhost:8000/api/scans/scan_0087/ocr \
  -H 'Content-Type: application/json' \
  -d '{"apply":"copy","naming":"heuristic","vision":"auto","image_only":false}' | jq

# background
curl -s -X POST localhost:8000/api/scans/scan_0087/ocr \
  -H 'Content-Type: application/json' -d '{"wait":false}'
```

MCP tools (available on the scanner MCP server):

| Tool | Purpose |
|---|---|
| `ocr_status` | Is the module reachable; tools; vision model |
| `scan_ocr(scan_id, naming, apply, vision, force, image_only, architectural, prompt)` | OCR + describe + name a scan's PDF; `image_only` skips Tesseract, `architectural` reads the title block, cleanup keeps one PDF, `prompt` overrides the Settings description prompt for this pass |
| `scan_describe(path, prompt)` | Vision description of any local PDF/image |
| `scan_suggest_filename(path)` | Name from an existing text layer |

### Vision prompt (custom instructions)

The description pass uses the module's `DESCRIBE_PROMPT` unless the operator
overrides it in **Settings → Auto Describe — Vision Prompt**. The saved value
lives in `app_settings.json` as `ocr_vision_prompt`; empty means "use the
module default", so improvements to the shared default keep flowing. The
editor shows the effective prompt (custom text, or the module default reported
by `/api/ocr/status` as `vision.describe_prompt`) and a **Reset to default**
button clears the override.

The prompt is sent to the module's `ocr_ingest` / `ocr_describe` call as
`prompt` and applies only to the description that feeds naming — not the
title-block reader and not the text-model refinement. The reply must remain
JSON with `doc_type`, `title`, `description`, `visible_text` and `tags`:
naming reads those keys.

```bash
# one-off override, without touching the saved setting
curl -s -X POST localhost:8080/api/scans/scan_0087/ocr \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Describe only the room layout and visible labels."}' | jq
```

### Naming rules

`2026-10-14_Tax-Invoice_Acme-Construction_Kitchen-Renovation.pdf`

* **date** — first plausible date in the text (ISO, `14 October 2026`,
  `03/07/2026`), else the filename, else the scan's mtime. Technical
  drawings/surveys read ambiguous `dd/mm/yyyy` dates day-first; invoices read
  them month-first unless the page says `dd/mm`.
* **type** — a specific heading wins (`TAX INVOICE` beats a passing "invoice");
  drawing hints (`SHEET`, `DWG NO`, `SCALE`, `TITLE BLOCK`) map to `Plan`.
* **entity** — `From:`/`Vendor:`/`Issued by:` lines, else a legal suffix
  (`Ltd`, `Pty`, `Inc`, `LLC`, `GmbH`, …); the suffix is dropped from the slug.
* **subject** — `Title:`/`Subject:`/`Project:`/`Re:`, else the vision title,
  else the first meaningful line.
* The stem is ASCII, separator-normalised, ≤ 96 characters, and collision-safe.

`naming: "llm"` asks the vision provider's text model to fill gaps; failures
fall back to the heuristic result, never to an error.

## Limits and privacy

* OCR is local (OCRmyPDF/Tesseract). Vision defaults to local Ollama; choosing
  `provider: openai` uploads page images to that endpoint.
* `vision.max_pages` (default 4) bounds the fallback; longer documents keep
  their first OCR result.
* Transcription placement is uniform, not per-line, unless `boxes: true` — the
  text is selectable/searchable but its on-page coordinates are approximate.
* `image_only` skips the text pass, not the artefacts: `.ocr.md`/`.ocr.pdf`/
  `.ocr.json` are still written (the Markdown will say no text was detected
  unless vision transcribed the page).
* Names are suggestions from text; verify unusual documents. The original scan
  file is never renamed or deleted by the module.
* Measured on this workstation: ~50 s to OCR a 42 × 24 in / 300 dpi sheet, and
  ~5 s per image-only page for the local vision transcription. Autosave runs it
  on a background thread; the on-demand API blocks for that duration.

## Tests

```bash
# module
cd /path/to/ocr-pdf-watcher && .venv/bin/python -m unittest test_ocr_enrich test_ocr_md_tool

# app side
cd Scanner && .venv/bin/python -m unittest tests.test_ocr_bridge
```
