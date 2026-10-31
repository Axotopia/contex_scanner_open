// Contex HD Ultra i4250s — Scanner Studio Frontend Controller

// --- Activity HUD -----------------------------------------------------------
// Several operations can be in flight at once (a scan while an export runs), so
// the HUD is reference-counted by owner key: each owner shows/updates/hides its
// own entry, and the newest entry is what the overlay displays.  A finishing
// export can no longer blank the HUD, or the pipeline beam, of a running scan.
const activeActivities = new Map();
let hudTimer = null;
let hudStartedAt = 0;

function _latestActivity() {
  let last = null;
  for (const entry of activeActivities.values()) last = entry;
  return last;
}

function _renderActivityHUD() {
  const overlay = document.getElementById("processing-overlay");
  const pipeBar = document.getElementById("pipeline-bar");
  if (!overlay) return;
  const current = _latestActivity();
  if (!current) {
    overlay.style.display = "none";
    if (hudTimer) {
      clearInterval(hudTimer);
      hudTimer = null;
    }
    if (pipeBar) pipeBar.classList.remove("is-processing");
    return;
  }
  const subEl = document.getElementById("processing-hud-subtitle");
  const iconEl = document.getElementById("processing-icon");
  const timerEl = document.getElementById("processing-hud-timer");
  // The overlay shows the live activity line only (no redundant "Processing…" heading).
  if (subEl) subEl.textContent = current.subtitle || current.title || "Working…";
  if (iconEl) iconEl.textContent = current.icon || "⚡";
  overlay.style.display = "flex";
  if (pipeBar) pipeBar.classList.add("is-processing");
  if (!hudTimer) {
    hudStartedAt = Date.now();
    if (timerEl) timerEl.textContent = "0s";
    hudTimer = setInterval(() => {
      const el = document.getElementById("processing-hud-timer");
      if (el) el.textContent = `${Math.floor((Date.now() - hudStartedAt) / 1000)}s`;
    }, 500);
  }
}

function showActivityHUD(key, title, subtitle, icon) {
  activeActivities.delete(key);
  activeActivities.set(key, { title, subtitle, icon });
  _renderActivityHUD();
}

function updateActivityHUD(key, title, subtitle) {
  const entry = activeActivities.get(key);
  if (!entry) return;
  if (title) entry.title = title;
  if (subtitle) entry.subtitle = subtitle;
  if (key === activeActivities.keys().next().value) _renderActivityHUD();
}

function hideActivityHUD(key) {
  activeActivities.delete(key);
  _renderActivityHUD();
}

// UI Tab and Accordion Helpers
function switchSidebarTab(tabName) {
  const tabs = ["capture", "enhance"];
  if (!tabs.includes(tabName)) tabName = "capture";
  tabs.forEach((t) => {
    const btn = document.getElementById("sbtn-" + t);
    const content = document.getElementById("tab-content-" + t);
    const on = t === tabName;
    if (btn) {
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.setAttribute("tabindex", on ? "0" : "-1");
    }
    if (content) {
      content.classList.toggle("active", on);
      content.hidden = !on;
    }
  });
}

function toggleAccordion(boxId) {
  const box = document.getElementById(boxId);
  if (!box) return;
  const body = box.querySelector(".accordion-body");
  const arrow = box.querySelector(".accordion-arrow");
  const header = box.querySelector(".accordion-header");
  if (!body) return;
  const show = !!body.hidden;          // hidden -> this click opens it
  body.hidden = !show;
  body.style.display = show ? "flex" : "none";
  if (arrow) arrow.textContent = show ? "\u25be" : "\u25b8";
  if (header) header.setAttribute("aria-expanded", show ? "true" : "false");
}

// Contex HD Ultra i4250s — Scanner Studio Frontend Controller

let appState = {
  settings: {},
  settingsLoaded: false,
  presets: [],
  availableRecs: [],
  paperSizes: [],
  orientation: "portrait",
  currentJob: null,
  viewerMode: "preset", // "preset" | "auto_adjust" | "ai_enhanced" | "split" | "raw" | "seams"
  splitPercent: 50,
  zoom: 1.0,
  panX: 0,
  panY: 0,
  isDragging: false,
  pendingCalibrationAfterScan: false,
  dragStartX: 0,
  dragStartY: 0,
  images: {
    raw: null,
    preset: null,
    auto_adjust: null,
    ai_enhanced: null,
  },
};

document.addEventListener("DOMContentLoaded", () => {
  initCanvasEvents();
  initDriftZoom();
  connectWebSocket();
  onSourceChange();
  // Paint once after layout has settled: at DOMContentLoaded the canvas container
  // can still measure 0x0, which would size the backing store from the fallback.
  requestAnimationFrame(() => renderCanvas());
  updateViewerTabs();
  window.addEventListener("load", () => renderCanvas());
  refreshStatus();
});

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    appState.settings = data.settings || {};
    appState.settingsLoaded = true;
    appState.presets = data.presets || [];
    appState.availableRecs = data.available_rec_files || [];
    appState.paperSizes = data.paper_sizes || [];

    updateHardwareHeader(data.hardware || {});
    populatePresetsDropdown();
    populateRecsDropdown();
    syncSettingsForm();
    syncScanForm();

    if (!appState.currentJob && data.recent_scans && data.recent_scans.length > 0) {
      loadJobIntoUI(data.recent_scans[0], false);
    }
  } catch (err) {
    console.error("Failed to fetch status:", err);
  }
}

function setPillText(el, text, className) {
  // Device strings are untrusted: build the dot node, then append text only.
  if (!el) return;
  el.className = className;
  el.textContent = "";
  const dot = document.createElement("span");
  dot.className = "pill-dot";
  el.appendChild(dot);
  el.appendChild(document.createTextNode(text));
}

function updateHardwareHeader(hw) {
  const ipEl = document.getElementById("hw-ip");
  if (ipEl) ipEl.textContent = `IP: ${hw.host || appState.settings.scanner_ip || "0.0.0.0"}`;
  const modelEl = document.getElementById("hw-model");
  if (modelEl) modelEl.textContent = `Model: ${hw.model || "HD Ultra i4250s"}`;
  const snEl = document.getElementById("hw-serial");
  if (snEl) snEl.textContent = `S/N: ${hw.serial || "UNKNOWN"}`;

  const linkEl = document.getElementById("hw-link");
  if (linkEl) {
    if (hw.preflight && hw.preflight.gigabit_ok === false) {
      setPillText(linkEl, "⚠ 100 Mbps Link (Drop Risk)", "pill pill-danger");
    } else {
      setPillText(linkEl, "1 Gbps Link Ready", "pill pill-ok");
    }
  }

  const mediaEl = document.getElementById("hw-media");
  if (mediaEl) {
    if (hw.connected) {
      setPillText(mediaEl, `Scanner: ${hw.media_state || "Connected"} (${hw.media_hex || "0x30"})`,
        hw.ready ? "pill pill-ok" : "pill pill-warn");
    } else {
      setPillText(mediaEl, "Offline · Demo & .rec Ready", "pill pill-neutral");
    }
  }
}

function populatePresetsDropdown() {
  const sel = document.getElementById("scan-preset");
  sel.innerHTML = "";
  for (const p of appState.presets) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.name} (dE ${p.dE76_mean ?? "—"})`;
    if (p.id === appState.settings.active_preset_id) {
      opt.selected = true;
    }
    sel.appendChild(opt);
  }
}

function populateRecsDropdown() {
  const sel = document.getElementById("scan-rec-path");
  sel.innerHTML = "";
  if (appState.availableRecs.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No .rec files found in analysis/";
    sel.appendChild(opt);
    return;
  }
  for (const r of appState.availableRecs) {
    const opt = document.createElement("option");
    opt.value = r.path;
    opt.textContent = `${r.display} (${r.size_mb} MB)`;
    sel.appendChild(opt);
  }
}

const PAPER_WIDTH_FALLBACK = {
  auto: 42.0,
  full_42: 42.0,
  arch_e: 36.0,
  arch_e1: 30.0,
  arch_d: 24.0,
  arch_c: 18.0,
  iso_a0: 33.11,
  iso_a1: 23.39,
  iso_a2: 16.54,
  ansi_e: 34.0,
  ansi_d: 22.0,
  ansi_c: 17.0,
  ansi_b: 11.0,
  ansi_a: 8.5,
};

const MAX_SCAN_WIDTH_IN = 42.0;

function paperInfo(id) {
  return (appState.paperSizes || []).find((p) => p.id === id) || null;
}

function fmtInches(v) {
  return Number(v).toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function onSourceChange() {
  const src = document.getElementById("scan-source").value;
  document.getElementById("rec-file-field").style.display = src === "rec_file" ? "flex" : "none";
  document.getElementById("demo-drift-field").style.display = src.startsWith("demo_") ? "flex" : "none";
  onPaperSizeChange();
}

function setSelectValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const v = String(value);
  const has = Array.from(el.options).some((o) => o.value === v);
  el.value = has ? v : (el.options[0] ? el.options[0].value : "");
}

function syncScanForm() {
  const s = appState.settings || {};
  setSelectValue("scan-source", s.scan_source || "live");
  setSelectValue("scan-paper-size", s.scan_paper_size || "full_42");
  const widthEl = document.getElementById("scan-width");
  if ((s.scan_paper_size || "full_42") === "custom" && s.scan_width_inches) {
    widthEl.value = String(s.scan_width_inches);
  }
  setSelectValue("scan-dpi", s.scan_dpi || 300);
  setSelectValue("scan-color-mode", s.scan_color_mode || "color");
  setSelectValue("scan-feather-mode", s.seam_feather_mode || "classic");
  appState.orientation = s.scan_orientation === "landscape" ? "landscape" : "portrait";
  onSourceChange();
}

async function persistScanSettings() {
  // Hold the last-used scan form across scans and restarts; a fresh settings
  // file still starts from the server-side defaults.
  const payload = {
    scan_source: document.getElementById("scan-source").value,
    scan_paper_size: document.getElementById("scan-paper-size").value,
    scan_orientation: appState.orientation,
    scan_width_inches: Math.max(
      4,
      Math.min(42, parseFloat(document.getElementById("scan-width").value || "42")),
    ),
    scan_dpi: parseInt(document.getElementById("scan-dpi").value || "300", 10),
    scan_color_mode: document.getElementById("scan-color-mode").value,
    seam_feather_mode: document.getElementById("scan-feather-mode").value || "classic",
    active_preset_id:
      document.getElementById("scan-preset").value || appState.settings.active_preset_id,
  };
  Object.assign(appState.settings, payload);
  try {
    await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (_) {
    /* offline: keep the local state, the next save retries */
  }
}

function setOrientation(orientation) {
  const next = orientation === "landscape" ? "landscape" : "portrait";
  if (next === appState.orientation) return;
  appState.orientation = next;
  onPaperSizeChange();
  persistScanSettings();
}

function onPaperSizeChange() {
  const ps = document.getElementById("scan-paper-size").value;
  const src = document.getElementById("scan-source").value;
  const widthInput = document.getElementById("scan-width");
  const demoPaperField = document.getElementById("demo-paper-width-field");
  const orientationField = document.getElementById("orientation-field");
  const hint = document.getElementById("paper-width-hint");
  const info = paperInfo(ps);
  const hasDims = !!(
    info && info.long_in && ps !== "auto" && ps !== "custom"
  );
  const canOrient = hasDims && Number(info.long_in) > Number(info.width_in);
  setShown(orientationField, true, true);
  document.querySelectorAll(".seg-btn").forEach((b) => {
    b.disabled = !canOrient;
    b.classList.toggle("active", b.dataset.orientation === appState.orientation);
  });

  if (ps === "custom") {
    widthInput.disabled = false;
    hint.textContent = "Custom cross-feed width (4.0–42.0 in); the scanner feeds to the end of the sheet. Orientation is not used for a custom width.";
  } else if (ps === "auto") {
    widthInput.disabled = true;
    widthInput.value = "42.0";
    hint.textContent = "Auto-detect chooses the window width after the scan; orientation is not used.";
  } else {
    widthInput.disabled = true;
    const portraitIn = info ? Number(info.width_in) : (PAPER_WIDTH_FALLBACK[ps] || 42.0);
    const longIn = info && info.long_in ? Number(info.long_in) : portraitIn;
    const landscapeIn = Math.min(MAX_SCAN_WIDTH_IN, longIn);
    const width = appState.orientation === "landscape" && canOrient ? landscapeIn : portraitIn;
    widthInput.value = fmtInches(width);
    if (canOrient) {
      hint.textContent =
        `Cross-feed width: ${fmtInches(width)} in · portrait ${fmtInches(portraitIn)} in / ` +
        `landscape ${fmtInches(landscapeIn)} in` +
        (appState.orientation === "landscape" && longIn > MAX_SCAN_WIDTH_IN
          ? " (landscape clamped to the 42 in bed)"
          : "");
    } else {
      hint.textContent = `Cross-feed width: ${fmtInches(width)} in · full sensor width, orientation is not used.`;
    }
  }
  setShown(demoPaperField, ps === "auto" && src.startsWith("demo_"), true);
}

// Show/hide a container without discarding the layout the markup declares
// (the seam-blend control groups are flex columns with a gap).
function setShown(el, shown, flex) {
  if (!el) return;
  el.style.display = shown ? (flex ? "flex" : "block") : "none";
}

function toggleExternalAi() {
  const checked = document.getElementById("ai-external").checked;
  setShown(document.getElementById("external-ai-box"), checked, false);
}

function toggleScanSeamBlendControls() {
  appState.scanSeamBlendUserSet = true;
  const enabled = document.getElementById("scan-seam-blend").checked;
  setShown(document.getElementById("scan-seam-blend-controls"), enabled, true);
}

function onScanBlendAdjustChange() {
  const w = parseInt(document.getElementById("scan-blend-width").value || "128", 10);
  const s = parseInt(document.getElementById("scan-blend-strength").value || "85", 10);
  document.getElementById("scan-blend-width-lbl").textContent = `±${w} px`;
  document.getElementById("scan-blend-str-lbl").textContent = `${s}%`;
}

function toggleAiSeamBlendControls() {
  const enabled = document.getElementById("ai-blend-seam-colors").checked;
  setShown(document.getElementById("ai-seam-blend-controls"), enabled, true);
}

function toggleAiMicroFeatherControls() {
  const enabled = document.getElementById("ai-heal-seams").checked;
  setShown(document.getElementById("ai-micro-feather-controls"), enabled, true);
}

function onAiBlendAdjustChange() {
  const w = parseInt(document.getElementById("ai-blend-width").value || "128", 10);
  const s = parseInt(document.getElementById("ai-blend-strength").value || "85", 10);
  const r = parseInt(document.getElementById("ai-seam-radius").value || "6", 10);
  document.getElementById("ai-blend-width-lbl").textContent = `±${w} px`;
  document.getElementById("ai-blend-str-lbl").textContent = `${s}%`;
  document.getElementById("ai-seam-radius-lbl").textContent = `±${r} px`;
}

function setPipelineStep(activeStep, statusText) {
  // The chip stepper was replaced by a single status strip; activeStep now only
  // selects the status dot colour (>=5 means the job finished successfully).
  const statusEl = document.getElementById("pipeline-status-text");
  if (statusEl && statusText) {
    const dotColor = activeStep === 5 ? "#10b981" : (activeStep >= 2 ? "#3b82f6" : "#94a3b8");
    statusEl.textContent = "";
    const dot = document.createElement("span");
    dot.className = "pill-dot";
    dot.style.background = dotColor;
    dot.style.boxShadow = `0 0 6px ${dotColor}`;
    statusEl.appendChild(dot);
    // Status messages carry server/device text (file paths, commands) — never
    // interpolate them as markup.  The bar clips long text, so mirror it in title.
    statusEl.appendChild(document.createTextNode(String(statusText)));
    statusEl.title = String(statusText);
  }
}

async function paperAction(action) {
  setPipelineStep(1, `Executing paper command: ${action}...`);
  try {
    const res = await fetch(`/api/paper/${action}`, { method: "POST" });
    const data = await res.json();
    setPipelineStep(1, data.message || `Paper ${action}: ${data.ok ? "OK" : "Done"}`);
    refreshStatus();
  } catch (err) {
    setPipelineStep(1, `Paper command error: ${err.message}`);
  }
}

async function ejectCompletedScan() {
  const btn = document.getElementById("btn-eject-front");
  if (btn.disabled) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏏ Ejecting...";
  try {
    await paperAction("eject_front");
  } finally {
    btn.textContent = label;
    btn.disabled = false;
  }
}

let scanActivityTimer = null;
let scanActivityStartedAt = 0;

function startScanActivity(text) {
  const box = document.getElementById("scan-activity");
  if (!box) return;
  scanActivityStartedAt = Date.now();
  document.getElementById("scan-activity-text").textContent = text;
  document.getElementById("scan-activity-elapsed").textContent = "0s";
  box.style.display = "flex";
  if (scanActivityTimer) clearInterval(scanActivityTimer);
  scanActivityTimer = setInterval(() => {
    const el = document.getElementById("scan-activity-elapsed");
    if (el) {
      el.textContent = `${Math.floor((Date.now() - scanActivityStartedAt) / 1000)}s`;
    }
  }, 500);
  showActivityHUD("scan", "Processing 5-Camera Scan…", text, "⚡");
}

function updateScanActivity(text) {
  const el = document.getElementById("scan-activity-text");
  if (el) el.textContent = text;
  updateActivityHUD("scan", "Processing 5-Camera Scan…", text);
}

function stopScanActivity() {
  const box = document.getElementById("scan-activity");
  if (box) box.style.display = "none";
  if (scanActivityTimer) {
    clearInterval(scanActivityTimer);
    scanActivityTimer = null;
  }
  hideActivityHUD("scan");
}

function showCancelButton() {
  const btn = document.getElementById("btn-cancel-scan");
  if (!btn) return;
  btn.style.display = "block";
  btn.disabled = false;
  btn.textContent = "✕ Cancel Scan";
}

function hideCancelButton() {
  const btn = document.getElementById("btn-cancel-scan");
  if (!btn) return;
  btn.style.display = "none";
  btn.disabled = false;
  btn.textContent = "✕ Cancel Scan";
}

async function cancelActiveScan() {
  const btn = document.getElementById("btn-cancel-scan");
  if (!btn || btn.style.display === "none") return;
  btn.disabled = true;
  btn.textContent = "⏳ Cancelling...";
  try {
    const res = await fetch("/api/scan/cancel", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      setPipelineStep(1, `Cancel requested for ${data.scan_id || "scan"} — stopping at the next safe point...`);
    } else {
      setPipelineStep(1, data.message || "No scan in progress");
      btn.disabled = false;
      btn.textContent = "✕ Cancel Scan";
    }
  } catch (err) {
    setPipelineStep(1, `Cancel failed: ${err.message}`);
    btn.disabled = false;
    btn.textContent = "✕ Cancel Scan";
  }
}

let idleStartBtnHTML = null;

async function startScan() {
  const calibrateAfter = !!appState.pendingCalibrationAfterScan;
  appState.pendingCalibrationAfterScan = false;
  const btn = document.getElementById("btn-start-scan");
  if (idleStartBtnHTML === null) idleStartBtnHTML = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "⏳ Scanning & Processing 5 Cameras...";
  showCancelButton();
  startScanActivity("Preparing scan…");
  await persistOutputSettings();
  await persistScanSettings();
  setPipelineStep(1, "Initiating scan sequence...");

  const payload = {
    source_mode: document.getElementById("scan-source").value,
    rec_path: document.getElementById("scan-rec-path").value || null,
    paper_size: document.getElementById("scan-paper-size").value || "full_42",
    orientation: appState.orientation,
    width_inches: parseFloat(document.getElementById("scan-width").value || "42.0"),
    dpi: parseInt(document.getElementById("scan-dpi").value || "300", 10),
    color_mode: document.getElementById("scan-color-mode").value,
    preset_id: document.getElementById("scan-preset").value,
    simulate_thermal_drift: document.getElementById("simulate-drift").checked,
    seam_color_blend: document.getElementById("scan-seam-blend").checked,
    seam_blend_width_px: parseInt(document.getElementById("scan-blend-width").value || "128", 10),
    seam_blend_strength: parseFloat(document.getElementById("scan-blend-strength").value || "85") / 100.0,
    seam_feather_mode: document.getElementById("scan-feather-mode").value || "classic",
    demo_paper_width_inches: parseFloat(document.getElementById("demo-paper-width").value || "36.0"),
    filename_prefix: document.getElementById("export-prefix").value.trim() || "scan",
  };

  try {
    setPipelineStep(2, "Collecting 16-bit 5-camera stream (81,840 B/row)...");
    const res = await fetch("/api/scan/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errData = await res.json();
      throw new Error(errData.detail || "Scan failed");
    }
    const job = await res.json();
    if (job.status === "cancelled") {
      setPipelineStep(1, `Scan ${job.scan_id} cancelled. Use Recover or Eject Front if media is still loaded.`);
      hideCancelButton();
      refreshStatus();
      return;
    }
    if (job.next_sequence_number) {
      appState.settings.next_sequence_number = job.next_sequence_number;
      document.getElementById("export-next-seq").value = job.next_sequence_number;
      updateNextFilenamePreview();
    }
    await loadJobIntoUI(job, !calibrateAfter);
    if (calibrateAfter) {
      setPipelineStep(4, "Sheet scan complete — verify the magenta IT8 grid, then approve.");
      updateScanActivity("Sheet scan complete — verify the IT8 grid in the wizard.");
      openModal("wizard-modal");
      loadWizardPreview();
    }
  } catch (err) {
    setPipelineStep(1, `Scan failed: ${err.message}`);
    alert(`Scan Error: ${err.message}`);
  } finally {
    btn.disabled = false;
    // Restore the redesigned SVG + label instead of replacing it with plain text.
    btn.innerHTML = idleStartBtnHTML !== null ? idleStartBtnHTML : "Initiate Scan";
    hideCancelButton();
    stopScanActivity();
  }
}

async function loadJobIntoUI(job, promptOnDrift = true) {
  if (job.status === "cancelled") {
    hideCancelButton();
    setPipelineStep(1, `Scan ${job.scan_id} cancelled`);
    return;
  }
  appState.currentJob = job;
  document.getElementById("btn-ai-enhance").disabled = false;
  document.getElementById("btn-export").disabled = false;

  const ejectBtn = document.getElementById("btn-eject-front");
  const isLiveScan = String(job.source || "").startsWith("live://");
  ejectBtn.disabled = !isLiveScan;
  ejectBtn.title = isLiveScan
    ? "Move the scanned sheet out the front for retrieval or rescan"
    : "Only available after a live hardware scan";

  updateDriftCard(job.drift_report || {});
  updateTelemetryFooter(job);
  populateSeamInspector(job);

  // Preload preview images
  const cacheBust = `t=${Date.now()}`;
  const kinds = ["raw", "preset", "auto_adjust"];
  if (job.previews && job.previews.ai_enhanced) {
    kinds.push("ai_enhanced");
  } else {
    appState.images.ai_enhanced = null;
  }

  await Promise.all(
    kinds.map((k) => {
      const url = job.previews && job.previews[k] ? `${job.previews[k]}?${cacheBust}` : null;
      return url ? loadImagePromise(url).then((im) => (appState.images[k] = im)).catch(() => null) : Promise.resolve();
    })
  );

  resetZoom();

  // The scan may not be able to produce every render view (AI before
  // post-processing, Auto-Adjusted when the drift check is off).
  const available = Array.isArray(job.available_renders) ? job.available_renders : ["preset"];
  if (RENDER_VIEWS.indexOf(appState.viewerMode) >= 0 && available.indexOf(appState.viewerMode) < 0) {
    appState.viewerMode = "preset";
  }
  updateViewerTabs();

  maybeAutoEnhance(job);

  if (job.status === "awaiting_drift_decision") {
    setPipelineStep(3, `Thermal / Color Drift (${job.drift_report.verdict}) — awaiting your review`);
    if (promptOnDrift) {
      openDriftComparisonModal(job);
    }
  } else {
    setPipelineStep(5, `Scan ${job.scan_id} stitched & ready (${job.active_render_mode})`);
    // After a job lands, point the user at AI/enhancement; the always-visible
    // output dock already owns autosave, naming and manual export.
    if (job.status === "ready") switchSidebarTab("enhance");
  }
}

function loadImagePromise(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });
}

function updateDriftCard(drift) {
  const badge = document.getElementById("drift-badge");
  const rec = document.getElementById("drift-recommendation");
  const box = document.getElementById("drift-metrics-box");

  const v = drift.verdict || "UNKNOWN";
  badge.textContent = v;
  if (v === "OK") badge.className = "pill pill-ok";
  else if (v === "WARN" || v === "INSUFFICIENT_WHITE") badge.className = "pill pill-warn";
  else badge.className = "pill pill-danger";

  rec.textContent = drift.recommendation || "";
  box.style.display = "block";

  document.getElementById("m-stored-spread").textContent =
    drift.stored_spread != null ? drift.stored_spread.toFixed(4) : "N/A";
  document.getElementById("m-self-spread").textContent =
    drift.self_spread != null ? drift.self_spread.toFixed(4) : "N/A";
  document.getElementById("m-level-dev").textContent =
    drift.stored_level_dev_max != null ? `${(drift.stored_level_dev_max * 100).toFixed(1)}%` : "N/A";

  const barsEl = document.getElementById("cam-gain-bars");
  barsEl.innerHTML = "";
  const gains = drift.camera_gains || {};
  for (const cam of ["cam1", "cam2", "cam3", "cam4", "cam5"]) {
    const g = gains[cam] || [1, 1, 1];
    const meanG = g[1] || 1.0;
    const pct = Math.min(100, Math.max(10, Math.round((meanG / 1.4) * 100)));
    const row = document.createElement("div");
    row.className = "cam-bar-row";
    row.innerHTML = `
      <span style="width:42px;">${cam.toUpperCase()}</span>
      <div class="cam-bar-track"><div class="cam-bar-fill" style="width:${pct}%"></div></div>
      <span style="width:52px;text-align:right;">R×${meanG.toFixed(3)}</span>
    `;
    barsEl.appendChild(row);
  }
}

// --- Drift preview zoom/pan -------------------------------------------------
// Both comparison panes show the same region of their own render so the user can
// inspect the same detail in A and B: one {scale, tx, ty} drives every image.
const driftView = { scale: 1, tx: 0, ty: 0 };
const DRIFT_ZOOM_MIN = 1;
const DRIFT_ZOOM_MAX = 12;

function driftPanes() {
  return Array.from(document.querySelectorAll("#drift-modal .compare-img-wrap"));
}

function driftApplyView() {
  const { scale, tx, ty } = driftView;
  document.querySelectorAll("#drift-modal .drift-zoom-img").forEach((img) => {
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  });
  const label = document.getElementById("drift-zoom-label");
  if (label) label.textContent = `${Math.round(scale * 100)}%`;
  driftPanes().forEach((w) => w.classList.toggle("is-zoomed", scale > 1.001));
}

function driftClampPan() {
  const pane = driftPanes()[0];
  if (!pane) return;
  // Keep part of the image under the cursor: never pan the content fully away.
  const maxX = Math.max(0, (driftView.scale - 1) * pane.clientWidth);
  const maxY = Math.max(0, (driftView.scale - 1) * pane.clientHeight);
  driftView.tx = Math.min(maxX, Math.max(-maxX, driftView.tx));
  driftView.ty = Math.min(maxY, Math.max(-maxY, driftView.ty));
}

function driftZoomAt(px, py, factor) {
  const s1 = driftView.scale;
  const s2 = Math.min(DRIFT_ZOOM_MAX, Math.max(DRIFT_ZOOM_MIN, s1 * factor));
  if (Math.abs(s2 - s1) < 1e-4) return;
  driftView.tx = px - (px - driftView.tx) * (s2 / s1);
  driftView.ty = py - (py - driftView.ty) * (s2 / s1);
  driftView.scale = s2;
  if (s2 <= DRIFT_ZOOM_MIN + 1e-4) {
    driftView.scale = 1;
    driftView.tx = 0;
    driftView.ty = 0;
  } else {
    driftClampPan();
  }
  driftApplyView();
}

function driftZoomBy(factor) {
  const pane = driftPanes()[0];
  if (!pane) return;
  driftZoomAt(pane.clientWidth / 2, pane.clientHeight / 2, factor);
}

function driftZoomReset() {
  driftView.scale = 1;
  driftView.tx = 0;
  driftView.ty = 0;
  driftApplyView();
}

function initDriftZoom() {
  const grid = document.getElementById("drift-comparison-grid");
  if (!grid || grid.dataset.zoomReady === "1") return;
  grid.dataset.zoomReady = "1";
  let drag = null;

  grid.addEventListener("wheel", (event) => {
    const pane = event.target.closest(".compare-img-wrap");
    if (!pane) return;
    event.preventDefault();
    const rect = pane.getBoundingClientRect();
    driftZoomAt(event.clientX - rect.left, event.clientY - rect.top,
                event.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });

  grid.addEventListener("pointerdown", (event) => {
    const pane = event.target.closest(".compare-img-wrap");
    if (!pane || event.button !== 0 || driftView.scale <= 1.001) return;
    drag = { x: event.clientX - driftView.tx, y: event.clientY - driftView.ty };
    pane.classList.add("is-panning");
    try { pane.setPointerCapture(event.pointerId); } catch (_) { /* older engines */ }
    event.preventDefault();
  });

  grid.addEventListener("pointermove", (event) => {
    if (!drag) return;
    driftView.tx = event.clientX - drag.x;
    driftView.ty = event.clientY - drag.y;
    driftClampPan();
    driftApplyView();
  });

  const endDrag = () => {
    drag = null;
    driftPanes().forEach((w) => w.classList.remove("is-panning"));
  };
  grid.addEventListener("pointerup", endDrag);
  grid.addEventListener("pointercancel", endDrag);
  grid.addEventListener("pointerleave", () => { if (drag) endDrag(); });

  grid.addEventListener("dblclick", (event) => {
    if (event.target.closest(".compare-img-wrap")) driftZoomReset();
  });
}

function openDriftComparisonModal(job) {
  const drift = job.drift_report || {};
  document.getElementById("drift-modal-title").textContent =
    drift.verdict === "INSUFFICIENT_WHITE"
      ? "ℹ Full-Bleed Scan (Insufficient White Margins for Auto-Fit)"
      : `⚠ Camera Operating Temperature / Color Shift (${drift.verdict})`;
  document.getElementById("drift-modal-subtitle").textContent = drift.recommendation || "";

  document.getElementById("modal-spread-preset").textContent =
    drift.stored_spread != null ? `Chroma Spread: ${drift.stored_spread.toFixed(4)}` : "Stored Preset";
  document.getElementById("modal-spread-auto").textContent =
    drift.self_spread != null ? `Chroma Spread: ${drift.self_spread.toFixed(4)}` : "Auto-Adjusted";

  const cacheBust = `t=${Date.now()}`;
  if (job.previews) {
    document.getElementById("modal-img-preset").src = `${job.previews.preset}?${cacheBust}`;
    document.getElementById("modal-img-auto").src = `${job.previews.auto_adjust}?${cacheBust}`;
  }
  driftZoomReset();
  openModal("drift-modal");
}

async function resolveDrift(choice) {
  if (!appState.currentJob) return;
  const promote = document.getElementById("drift-promote-check").checked;
  const newName = document.getElementById("drift-new-preset-name").value.trim() || null;

  closeModal("drift-modal");
  setPipelineStep(4, `Applying ${choice} calibration and stitching 42.0" composite…`);
  showActivityHUD("drift", `Applying ${choice} Calibration…`, 'Re-stitching 42.0" composite with adjusted camera gains', "🔬");

  try {
    const res = await fetch(`/api/scans/${appState.currentJob.scan_id}/drift-decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        choice,
        promote_to_preset: promote,
        save_as_new_preset_name: newName,
      }),
    });
    const updatedJob = await res.json();
    await refreshStatus();
    await loadJobIntoUI(updatedJob, false);
  } catch (err) {
    alert(`Failed to apply calibration choice: ${err.message}`);
  } finally {
    hideActivityHUD("drift");
  }
}


let wizPreviewImg = null;
let wizPreviewJobId = null;

function wizardOffsets() {
  return {
    dx: parseInt(document.getElementById("wiz-it8-dx")?.value || "0", 10),
    dy: parseInt(document.getElementById("wiz-it8-dy")?.value || "0", 10),
  };
}

function loadWizardPreview() {
  const job = appState.currentJob;
  const sheet = document.getElementById("wiz-sheet-canvas");
  const chart = document.getElementById("wiz-it8-canvas");
  if (!sheet || !chart) return;
  const sctx = sheet.getContext("2d");
  const cctx = chart.getContext("2d");
  sctx.fillStyle = "#0b1220";
  sctx.fillRect(0, 0, sheet.width, sheet.height);
  cctx.fillStyle = "#0b1220";
  cctx.fillRect(0, 0, chart.width, chart.height);
  if (!job) {
    sctx.fillStyle = "#94a3b8";
    sctx.font = "16px sans-serif";
    sctx.fillText("No scan loaded — scan the calibration sheet first.", 16, 34);
    cctx.fillStyle = "#94a3b8";
    cctx.font = "16px sans-serif";
    cctx.fillText("No scan loaded.", 16, 34);
    return;
  }
  if (wizPreviewJobId !== job.scan_id || !wizPreviewImg) {
    wizPreviewJobId = job.scan_id;
    wizPreviewImg = null;
    clearWizardApproval();
    const url = job.previews && job.previews.raw;
    if (!url) {
      sctx.fillStyle = "#94a3b8";
      sctx.font = "16px sans-serif";
      sctx.fillText("This job has no raw preview. Re-scan the sheet.", 16, 34);
      return;
    }
    const img = new Image();
    img.onload = () => {
      wizPreviewImg = img;
      drawWizardPreview();
    };
    img.onerror = () => {
      sctx.fillStyle = "#f87171";
      sctx.font = "16px sans-serif";
      sctx.fillText("Could not load the raw preview.", 16, 34);
    };
    img.src = url;
    return;
  }
  drawWizardPreview();
}

function drawWizardPreview() {
  const job = appState.currentJob;
  const img = wizPreviewImg;
  const sheet = document.getElementById("wiz-sheet-canvas");
  const chart = document.getElementById("wiz-it8-canvas");
  if (!job || !img || !sheet || !chart) return;

  const dpi = job.dpi || 300;
  const scale = dpi / 300.0;
  const { dx, dy } = wizardOffsets();
  const dxLbl = document.getElementById("wiz-it8-dx-lbl");
  const dyLbl = document.getElementById("wiz-it8-dy-lbl");
  if (dxLbl) dxLbl.textContent = String(dx);
  if (dyLbl) dyLbl.textContent = String(dy);

  const docW = job.document_width || (job.width_inches * dpi) || 12600;
  const docH = job.rows || 7250;
  const pxPerDocX = img.naturalWidth / docW;
  const pxPerDocY = img.naturalHeight / docH;

  const geom = {
    white: [2795 * scale, 3545 * scale],
    black: [3861 * scale, 4766 * scale],
    mosaic: [
      (581 + dx) * scale,
      (2264 + dx) * scale,
      (1011 + dy) * scale,
      (1942 + dy) * scale,
    ],
  };

  // Overview
  const sctx = sheet.getContext("2d");
  sctx.clearRect(0, 0, sheet.width, sheet.height);
  sctx.drawImage(img, 0, 0, sheet.width, sheet.height);
  const toSX = (docX) => (docX * pxPerDocX) * (sheet.width / img.naturalWidth);
  const toSY = (docY) => (docY * pxPerDocY) * (sheet.height / img.naturalHeight);
  sctx.lineWidth = 3;
  sctx.strokeStyle = "#22c55e";
  sctx.strokeRect(0, toSY(geom.white[0]), sheet.width - 1, toSY(geom.white[1]) - toSY(geom.white[0]));
  sctx.strokeStyle = "#ef4444";
  sctx.strokeRect(0, toSY(geom.black[0]), sheet.width - 1, toSY(geom.black[1]) - toSY(geom.black[0]));
  sctx.strokeStyle = "#22d3ee";
  sctx.lineWidth = 4;
  sctx.strokeRect(
    toSX(geom.mosaic[0]), toSY(geom.mosaic[2]),
    toSX(geom.mosaic[1]) - toSX(geom.mosaic[0]),
    toSY(geom.mosaic[3]) - toSY(geom.mosaic[2])
  );
  sctx.fillStyle = "rgba(15,23,42,0.75)";
  sctx.fillRect(8, 8, 280, 26);
  sctx.fillStyle = "#e2e8f0";
  sctx.font = "14px monospace";
  sctx.fillText(`dpi ${dpi} · X ${dx} · Y ${dy} px (300 dpi)`, 16, 26);

  // Zoomed IT8 chart
  const cctx = chart.getContext("2d");
  cctx.clearRect(0, 0, chart.width, chart.height);
  // Show a small amount of context around the 22x12 sample bbox.  The
  // template is drawn through the same coordinate transform, so it stays
  // correctly registered to the patch grid.
  const bboxX0 = geom.mosaic[0] * pxPerDocX;
  const bboxX1 = geom.mosaic[1] * pxPerDocX;
  const bboxY0 = geom.mosaic[2] * pxPerDocY;
  const bboxY1 = geom.mosaic[3] * pxPerDocY;
  const mx = Math.max(10, (bboxX1 - bboxX0) * 0.10);
  const my = Math.max(10, (bboxY1 - bboxY0) * 0.10);
  const srcX0 = Math.max(0, bboxX0 - mx);
  const srcX1 = Math.min(img.naturalWidth, bboxX1 + mx);
  const srcY0 = Math.max(0, bboxY0 - my);
  const srcY1 = Math.min(img.naturalHeight, bboxY1 + my);
  const cropW = Math.max(1, srcX1 - srcX0);
  const cropH = Math.max(1, srcY1 - srcY0);
  const cw = chart.width;
  const ch = Math.max(180, Math.round(cw * cropH / cropW));
  if (chart.height !== ch) chart.height = ch;
  cctx.drawImage(img, srcX0, srcY0, cropW, cropH, 0, 0, cw, ch);

  const toCX = (docX) => {
    const previewX = docX * pxPerDocX;
    return ((previewX - srcX0) / (srcX1 - srcX0)) * cw;
  };
  const toCY = (docY) => {
    const previewY = docY * pxPerDocY;
    return ((previewY - srcY0) / (srcY1 - srcY0)) * ch;
  };

  const x0 = geom.mosaic[0], x1 = geom.mosaic[1];
  const y0 = geom.mosaic[2], y1 = geom.mosaic[3];
  const nx = 22, ny = 12, pad = 0.30;
  cctx.strokeStyle = "#22d3ee";
  cctx.lineWidth = 2;
  cctx.strokeRect(toCX(x0) + 1, toCY(y0) + 1, toCX(x1) - toCX(x0) - 2, toCY(y1) - toCY(y0) - 2);
  cctx.strokeStyle = "#ff2bd1";
  cctx.lineWidth = 2;
  for (let r = 0; r < ny; r += 1) {
    for (let c = 0; c < nx; c += 1) {
      const xa = x0 + (c + pad) * (x1 - x0 + 1) / nx;
      const xb = x0 + (c + 1 - pad) * (x1 - x0 + 1) / nx;
      const ya = y0 + (r + pad) * (y1 - y0 + 1) / ny;
      const yb = y0 + (r + 1 - pad) * (y1 - y0 + 1) / ny;
      cctx.strokeRect(toCX(xa), toCY(ya), Math.max(2, toCX(xb) - toCX(xa)), Math.max(2, toCY(yb) - toCY(ya)));
    }
  }
  cctx.fillStyle = "rgba(15,23,42,0.75)";
  cctx.fillRect(8, 8, 370, 26);
  cctx.fillStyle = "#e2e8f0";
  cctx.font = "14px monospace";
  cctx.fillText("IT8 patch centres — adjust X/Y until centred", 16, 26);
}

function clearWizardApproval() {
  const cb = document.getElementById("wiz-placement-approved");
  if (cb) cb.checked = false;
}

function onWizardAutoAlignChange() {
  const auto = document.getElementById("wiz-auto-align");
  const on = !auto || auto.checked;
  const dx = document.getElementById("wiz-it8-dx");
  const dy = document.getElementById("wiz-it8-dy");
  [dx, dy].forEach((el) => { if (el) el.disabled = on; });
  const hint = document.getElementById("wiz-auto-align-hint");
  if (hint) {
    hint.textContent = on
      ? "Auto-align is on: the X/Y sliders are only a starting hint. Clear it to place the grid by hand."
      : "Auto-align is off: nudge X/Y until the magenta grid sits inside every patch, then tick the verification box.";
  }
  clearWizardApproval();
  drawWizardPreview();
}

function onWizardOffsetChange() {
  clearWizardApproval();
  drawWizardPreview();
}

function startSheetScanAndCalibrate() {
  const ok = window.confirm(
    "Load the Contex Official Calibration Sheet at the scan position, full 42-inch width.\n\n" +
    "This will scan the sheet at 300 dpi, then open the IT8 preview so you can verify/adjust the " +
    "magenta patch grid before the calibration is run."
  );
  if (!ok) return;

  appState.pendingCalibrationAfterScan = true;
  const dxEl = document.getElementById("wiz-it8-dx");
  const dyEl = document.getElementById("wiz-it8-dy");
  if (dxEl) dxEl.value = "0";
  if (dyEl) dyEl.value = "0";
  clearWizardApproval();
  closeModal("wizard-modal");
  document.getElementById("scan-source").value = "live";
  document.getElementById("scan-paper-size").value = "full_42";
  document.getElementById("scan-width").value = "42";
  document.getElementById("scan-dpi").value = "300";
  document.getElementById("scan-color-mode").value = "color";
  const presetSel = document.getElementById("scan-preset");
  if ([...presetSel.options].some((o) => o.value === "default-it8")) {
    presetSel.value = "default-it8";
  }
  setOrientation("landscape");
  startScan();
}

async function saveWizardDebugCapture() {
  const job = appState.currentJob;
  const note = document.getElementById("wiz-debug-note");
  if (!job) {
    alert("No scan is loaded.");
    return;
  }
  note.textContent = "Saving reusable raw and visual copies…";
  const formats = ["rec", "tiff", "png"];
  if (document.getElementById("wiz-debug-npy")?.checked) formats.push("npy");
  try {
    const res = await fetch(`/api/scans/${job.scan_id}/save-debug-capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ formats }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Save failed");
    const files = data.files || {};
    const parts = Object.entries(files).map(([k, v]) => `${k}: ${v}`);
    note.innerHTML = `Saved.<br/>${parts.join("<br/>")}`;
  } catch (err) {
    note.textContent = `Save failed: ${err.message}`;
  }
}

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fitLine(rep) {
  if (rep.it8_fitted) {
    return `IT8: <strong>gamma ${escapeHtml(String(rep.gamma))}</strong> · mean dE76 ${escapeHtml(String(rep.dE76_mean))}` +
      ` · median ${escapeHtml(String(rep.dE76_median))}` +
      (rep.patches_total ? ` · ${rep.patches_under_3}/${rep.patches_total} patches under dE 3` : "");
  }
  return `IT8: not applied — ${escapeHtml(rep.it8_note || "no usable mosaic")}`;
}

async function runSheetCalibrationWizard() {
  if (!appState.currentJob) {
    alert(
      "This wizard calibrates a scan that is already loaded — it does not start the scanner.\n\n" +
      "No scan is loaded. Load the calibration sheet and use 'Scan Sheet & Calibrate (300 dpi)', " +
      "or scan it with the scan card (full 42 in, 300 dpi, colour), then open this wizard on that job."
    );
    return;
  }
  const autoAlignEl = document.getElementById("wiz-auto-align");
  const autoAlign = !autoAlignEl || autoAlignEl.checked;
  const approved = document.getElementById("wiz-placement-approved");
  if (!autoAlign && (!approved || !approved.checked)) {
    alert(
      "Auto-align is off, so the sampling grid is exactly what the X/Y sliders say.\n\n" +
      "Nudge X/Y until the magenta 22×12 grid sits inside the IT8 patches, then tick " +
      "'I verified the magenta 22×12 grid is inside the IT8 patches' — or switch auto-align back on."
    );
    return;
  }
  const sheetMode = document.getElementById("wiz-sheet-mode").value;
  const presetName = document.getElementById("wiz-preset-name").value.trim() || null;
  const presetId = presetName
    ? "sheet-" + presetName.toLowerCase().replace(/[^a-z0-9]+/g, "-")
    : null;

  const resBox = document.getElementById("wiz-result-box");
  resBox.style.display = "block";
  resBox.className = "wiz-result";
  resBox.textContent = autoAlign
    ? "Auto-aligning the 22×12 grid, then fitting per-camera W, B, and 264 IT8 patches…"
    : "Fitting per-camera W, B, and 264 IT8 patches…";

  try {
    const res = await fetch(`/api/scans/${appState.currentJob.scan_id}/calibrate-sheet`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sheet_mode: sheetMode,
        save_as_preset_id: presetId,
        save_as_preset_name: presetName,
        it8_dx_300px: wizardOffsets().dx,
        it8_dy_300px: wizardOffsets().dy,
        auto_align: autoAlign,
      }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Calibration failed");
    }
    const data = await res.json();
    const rep = data.calibration_report || {};
    const fitted = !!rep.it8_fitted;
    // The banner must not claim success when the IT8 mosaic was rejected.
    resBox.className = fitted ? "wiz-result" : "wiz-result is-error";
    const align = rep.it8_auto_align || null;
    const warnings = (rep.warnings || [])
      // The auto-align summary is already shown on its own line.
      .filter((w) => !(align && align.auto && String(w).startsWith("IT8 grid auto-aligned")))
      .map((w) => `<div class="wiz-warning">⚠ ${escapeHtml(w)}</div>`)
      .join("");
    const alignLine = align && align.auto
      ? align.found === false
        ? "Grid auto-align: <strong>not found</strong> — used the X/Y sliders."
        : `Grid auto-align: <strong>X ${rep.it8_dx_300px} / Y ${rep.it8_dy_300px} px</strong> @ 300 dpi ` +
          `(dE76 ${align.dE76_mean} here vs ${align.dE76_mean_at_zero} at the nominal window)`
      : "";
    resBox.innerHTML = `
      <strong>${fitted ? "✓ Official sheet calibration applied" : "⚠ Sheet calibration applied — IT8 fit rejected"}</strong><br/>
      New preset: "${escapeHtml(data.preset.name)}"<br/>
      Mode: ${escapeHtml(String(rep.sheet_mode))} · DPI: ${escapeHtml(String(rep.dpi || "?"))} · White point K: ${escapeHtml(String(rep.K))} · Cameras fitted: ${escapeHtml(String(rep.cameras_fitted))}<br/>
      Sheet reference bands: ${rep.sheet_bands_detected ? "✓ detected" : "⚠ not confirmed"}${
        rep.it8_reference && String(rep.it8_reference).indexOf("approximate") === 0
          ? ' · reference: <span class="wiz-warning">approximate fallback table</span>'
          : ""
      }<br/>
      ${fitLine(rep)}
      ${alignLine ? `<br/>${alignLine}` : ""}
      ${warnings}
    `;
    // Keep the preview in step with what the backend actually sampled.
    if (align && align.found !== false && (rep.it8_dx_300px || rep.it8_dy_300px)) {
      const dxEl = document.getElementById("wiz-it8-dx");
      const dyEl = document.getElementById("wiz-it8-dy");
      // Show exactly what the backend sampled; the input handler redraws the grid.
      if (dxEl) { dxEl.value = String(rep.it8_dx_300px || 0); dxEl.dispatchEvent(new Event("input")); }
      if (dyEl) { dyEl.value = String(rep.it8_dy_300px || 0); dyEl.dispatchEvent(new Event("input")); }
    }
    await refreshStatus();
    await loadJobIntoUI(data.scan, false);
  } catch (err) {
    resBox.className = "wiz-result is-error";
    resBox.textContent = `Error: ${err.message}`;
  }
}

async function runAiEnhance(autoRun = false) {
  if (!appState.currentJob) return;
  const btn = document.getElementById("btn-ai-enhance");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-top-color:#fff;border-color:rgba(255,255,255,0.3);width:13px;height:13px;display:inline-block;margin-right:6px;"></span> Running Post-Processing…';
  setPipelineStep(5, autoRun ? "Auto AI Enhance: running post-processing…" : "Running AI & restoration filters…");
  showActivityHUD("enhance", autoRun ? "Auto AI Enhance…" : "AI Post-Processing In Progress…",
                  "Equalizing analog seams, micro-inpainting & shadow denoise", "✨");

  const useExt = document.getElementById("ai-external").checked;
  const payload = {
    blend_seam_colors: document.getElementById("ai-blend-seam-colors").checked,
    seam_blend_width_px: parseInt(document.getElementById("ai-blend-width").value || "128", 10),
    seam_blend_strength: parseFloat(document.getElementById("ai-blend-strength").value || "85") / 100.0,
    seam_feather_mode: appState.settings.seam_feather_mode || "classic",
    heal_seams: document.getElementById("ai-heal-seams").checked,
    seam_radius_px: parseInt(document.getElementById("ai-seam-radius").value || "6", 10),
    clean_background: document.getElementById("ai-clean-bg").checked,
    background_strength: 0.65,
    denoise_shadows: document.getElementById("ai-denoise").checked,
    denoise_strength: 0.55,
    sharpen_detail: document.getElementById("ai-sharpen").checked,
    sharpen_amount: 0.45,
    external_provider: useExt ? "photocraft_webhook" : "none",
    external_prompt: document.getElementById("ai-prompt").value,
  };

  try {
    const res = await fetch(`/api/scans/${appState.currentJob.scan_id}/enhance`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Enhancement failed");
    }
    const job = await res.json();
    await loadJobIntoUI(job, false);
    setViewerMode("ai_enhanced");
    updateViewerTabs();
  } catch (err) {
    // An automatic run must not pop a modal at the user mid-batch; log it instead.
    if (autoRun) console.warn(`Auto AI Enhance failed: ${err.message}`);
    else alert(`Post-processing error: ${err.message}`);
  } finally {
    hideActivityHUD("enhance");
    btn.disabled = false;
    btn.innerHTML = "✨ Run Post-Processing";
  }
}

function selectedExportFormats() {
  const formats = [];
  if (document.getElementById("fmt-tiff").checked) formats.push("tiff");
  if (document.getElementById("fmt-jpg").checked) formats.push("jpg");
  if (document.getElementById("fmt-pdf").checked) formats.push("pdf");
  return formats;
}

async function onExportFormatChange(el) {
  let formats = selectedExportFormats();
  if (formats.length === 0) {
    // Autosave needs at least one real file type; undo the last uncheck.
    if (el) el.checked = true;
    formats = selectedExportFormats();
  }
  appState.settings.export_formats = formats;
  const note = document.getElementById("export-format-note");
  if (note) {
    note.textContent = `Autosave and Save / Export will write ${formats.map((f) => f.toUpperCase()).join(", ")}.`;
  }
  await persistOutputSettings({ export_formats: formats });
}

//: Auto AI Enhance: when the workflow setting is on, run the AI chain as soon as
//: a job is ready and its AI render does not exist yet. Runs once per scan.
const autoEnhanceDone = new Set();

function maybeAutoEnhance(job) {
  if (!job || job.status !== "ready") return;
  if (!appState.settings || !appState.settings.auto_ai_enhance) return;
  const available = Array.isArray(job.available_renders) ? job.available_renders : [];
  if (available.indexOf("ai_enhanced") >= 0) return;
  if (autoEnhanceDone.has(job.scan_id)) return;
  autoEnhanceDone.add(job.scan_id);
  runAiEnhance(true);
}

async function onAutoAiEnhanceChange() {
  const el = document.getElementById("ai-auto-enhance");
  if (!el) return;
  await persistSetting({ auto_ai_enhance: !!el.checked });
  if (el.checked && appState.currentJob) maybeAutoEnhance(appState.currentJob);
}

async function persistSetting(payload) {
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    appState.settings = await res.json();
    return true;
  } catch (err) {
    alert(`Could not save setting: ${err.message}`);
    return false;
  }
}

async function onDriftPolicyChange() {
  const el = document.getElementById("scan-drift-policy");
  if (!el) return;
  const ok = await persistSetting({ drift_policy: el.value });
  if (ok) {
    setPipelineStep(1, el.value === "disabled"
      ? "Colour / temperature drift check is off"
      : `Colour / temperature drift check: ${el.value === "auto_adjust" ? "auto-correct" : "ask"}`);
  }
}

async function exportCurrentScan() {
  if (!appState.currentJob) return;
  const formats = selectedExportFormats();
  if (formats.length === 0) {
    alert("Select at least one export format (TIFF, JPG or PDF).");
    return;
  }

  const exportBtn = document.getElementById("btn-export");
  if (exportBtn) {
    exportBtn.disabled = true;
    exportBtn.innerHTML = `<span class="spinner" style="border-top-color:#fff;border-color:rgba(255,255,255,0.3);width:13px;height:13px;display:inline-block;margin-right:6px;"></span> Exporting ${formats.join(", ").toUpperCase()}…`;
  }

  showActivityHUD("export", "Exporting High-Resolution Files…", `Writing ${formats.join(", ").toUpperCase()} to output directory`, "💾");
  setPipelineStep(5, `Exporting ${formats.join(", ").toUpperCase()}…`);

  const viewMode = appState.viewerMode;
  if (RENDER_VIEWS.indexOf(viewMode) < 0) {
    alert("Switch to Default Preset, Auto-Adjusted or AI Enhanced to save an image.");
    return;
  }
  const payload = {
    formats,
    color_mode: document.getElementById("scan-color-mode").value,
    tiff_16bit: !!appState.settings.tiff_16bit,
    debug_mode: !!appState.settings.debug_mode,
    filename_prefix: document.getElementById("export-prefix").value.trim() || "scan",
    render_mode: viewMode,
  };

  try {
    const res = await fetch(`/api/scans/${appState.currentJob.scan_id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const err = await res.json();
        detail = err.detail || detail;
      } catch (_) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    const data = await res.json();
    appState.currentJob = data.scan || appState.currentJob;
    if (data.next_sequence_number) {
      appState.settings.next_sequence_number = data.next_sequence_number;
      document.getElementById("export-next-seq").value = data.next_sequence_number;
      updateNextFilenamePreview();
    }
    setPipelineStep(
      5,
      `Saved as ${data.exported.base_name} (${data.exported.files.length} file(s)) in ${data.exported.output_dir}`
    );
  } catch (err) {
    alert(`Export error: ${err.message}`);
  } finally {
    hideActivityHUD("export");
    const exportBtn = document.getElementById("btn-export");
    if (exportBtn) {
      exportBtn.disabled = false;
      exportBtn.innerHTML = "💾 Save / Export Current Scan";
    }
  }
}

function updateTelemetryFooter(job) {
  const sm = job.stitch_metadata || {};
  const autoInfo = sm.auto_width_info;
  const sb = sm.seam_blend_info;
  const orientationTag = sm.orientation && sm.orientation !== "portrait"
    ? ` · ${sm.orientation === "landscape" ? "Landscape" : "Portrait"}`
    : "";
  const sizeTag = autoInfo
    ? ` · Auto-Width: ${autoInfo.matched_label}`
    : sm.paper_size && sm.paper_size !== "full_42"
    ? ` · Size: ${sm.paper_size.toUpperCase()}${orientationTag}`
    : "";
  const blendTag = sb && sb.enabled
    ? ` · Seam Blend: ±${sb.blend_width_px}px (${Math.round((sb.strength || 0.85) * 100)}%, Δ ${sb.before_rgb_step_mean} → ${sb.after_rgb_step_mean})`
    : " · Seam Blend: Off";
  document.getElementById("meta-dims").title = document.getElementById("meta-dims").textContent =
    `Document: ${sm.document_width || 12600} × ${sm.rows || job.rows} px (${sm.width_inches || 42}" × ${sm.height_inches || "—"}" @ ${job.dpi} DPI${sizeTag})`;
  document.getElementById("meta-joins").title = document.getElementById("meta-joins").textContent =
    `Seam Joins: ${(sm.joins || [2658, 5320, 7982, 10644]).join(", ")} px${blendTag}`;
  document.getElementById("meta-quality").title = document.getElementById("meta-quality").textContent =
    `Holes: ${job.holes} · Zero Cols: ${sm.zero_columns ?? 0} · Dark Cols: ${sm.dark_columns ?? 0}`;
  document.getElementById("meta-mode").textContent =
    `Active Render: ${job.active_render_mode} (${job.color_mode})`;
}

function populateSeamInspector(job) {
  const grid = document.getElementById("seam-crops-grid");
  grid.innerHTML = "";
  const crops = job.seam_crops || [];
  const sb = (job.stitch_metadata && job.stitch_metadata.seam_blend_info) || {};
  const beforeArr = sb.seams_before || [];
  const afterArr = sb.seams_after || [];
  const labels = ["Seam 1 (Cam 1 | Cam 2)", "Seam 2 (Cam 2 | Cam 3)", "Seam 3 (Cam 3 | Cam 4)", "Seam 4 (Cam 4 | Cam 5)"];
  crops.forEach((c, idx) => {
    const bStep = beforeArr[idx] ? beforeArr[idx].mean_step_u16 : null;
    const aStep = afterArr[idx] ? afterArr[idx].mean_step_u16 : null;
    const stepText =
      bStep != null && aStep != null
        ? ` · Step Δu16: ${bStep} → ${aStep}`
        : "";
    const card = document.createElement("div");
    card.className = "seam-card";
    card.innerHTML = `
      <strong>${labels[idx] || `Join ${idx + 1}`}</strong>
      <div class="muted-text">Join at x = ${c.join_x} px (1:1 crop ±150 px${stepText})</div>
      <img src="${c.url}?t=${Date.now()}" alt="Seam ${idx}" />
    `;
    grid.appendChild(card);
  });
}

// ---------------------------------------------------------------------------
// Deep-Zoom & A/B Split Canvas Renderer
// ---------------------------------------------------------------------------

//: Tabs that render a savable image (everything else is an inspection view).
const RENDER_VIEWS = ["preset", "auto_adjust", "ai_enhanced"];
const VIEW_TAB_IDS = {
  preset: "tab-preset",
  auto_adjust: "tab-auto",
  ai_enhanced: "tab-ai",
  split: "tab-split",
  raw: "tab-raw",
  seams: "tab-seams",
};
const SAVE_VIEW_LABELS = {
  preset: "Default Preset",
  auto_adjust: "Auto-Adjusted",
  ai_enhanced: "AI Enhanced",
};

function viewIsAvailable(mode) {
  const job = appState.currentJob;
  // Nothing scanned yet: no render exists, but the tabs stay selectable (the
  // canvas shows the empty-state hint and the save button stays disabled).
  if (!job) return true;
  const available = Array.isArray(job.available_renders)
    ? job.available_renders
    : RENDER_VIEWS.filter((m) => !!appState.images[m]);
  return available.indexOf(mode) >= 0;
}

//: Grey out the render tabs whose image this scan cannot produce (AI before
//: post-processing, Auto-Adjusted when the drift check is off).
function updateViewerTabs() {
  Object.keys(VIEW_TAB_IDS).forEach((mode) => {
    const tab = document.getElementById(VIEW_TAB_IDS[mode]);
    if (!tab) return;
    const renderView = RENDER_VIEWS.indexOf(mode) >= 0;
    const available = !renderView || viewIsAvailable(mode);
    const isActive = appState.viewerMode === mode;
    tab.classList.toggle("is-disabled", !available);
    tab.disabled = !available;
    tab.setAttribute("aria-disabled", available ? "false" : "true");
    if (!available) {
      tab.title = mode === "ai_enhanced"
        ? "Run AI Enhance to enable this view"
        : "Auto-Adjusted needs the colour / temperature drift check turned on in Scan Setup";
    }
    tab.classList.toggle("active", available && isActive);
  });
  updateSaveViewButton();
}

function updateSaveViewButton() {
  const btn = document.getElementById("btn-export");
  if (!btn) return;
  const mode = appState.viewerMode;
  const renderView = RENDER_VIEWS.indexOf(mode) >= 0;
  const available = renderView && !!appState.currentJob && viewIsAvailable(mode);
  btn.disabled = !available;
  if (renderView) {
    btn.innerHTML = `💾 Save “${SAVE_VIEW_LABELS[mode]}” view`;
    btn.title = available
      ? `Save the ${SAVE_VIEW_LABELS[mode]} render to the output directory`
      : "That render is not available for this scan yet";
  } else {
    btn.innerHTML = "💾 Save current view";
    btn.title = "Switch to Default Preset, Auto-Adjusted or AI Enhanced to save";
  }
}

function setViewerMode(mode) {
  if (RENDER_VIEWS.indexOf(mode) >= 0 && !viewIsAvailable(mode)) return;
  appState.viewerMode = mode;
  Object.values(VIEW_TAB_IDS).forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.classList.remove("active");
  });
  const activeTab = document.getElementById(VIEW_TAB_IDS[mode]);
  if (activeTab) activeTab.classList.add("active");

  document.getElementById("seam-inspector-view").style.display = mode === "seams" ? "block" : "none";
  document.getElementById("split-slider-control").style.display = mode === "split" ? "flex" : "none";
  updateViewerTabs();
  renderCanvas();
}

function onSplitSliderChange(val) {
  appState.splitPercent = parseFloat(val);
  renderCanvas();
}

function initCanvasEvents() {
  const container = document.getElementById("canvas-container");
  const canvas = document.getElementById("scan-canvas");

  window.addEventListener("resize", () => renderCanvas());

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 0.87;
    zoomBy(factor);
  });

  canvas.addEventListener("mousedown", (e) => {
    appState.isDragging = true;
    appState.dragStartX = e.clientX - appState.panX;
    appState.dragStartY = e.clientY - appState.panY;
  });

  window.addEventListener("mousemove", (e) => {
    if (!appState.isDragging) return;
    appState.panX = e.clientX - appState.dragStartX;
    appState.panY = e.clientY - appState.dragStartY;
    renderCanvas();
  });

  window.addEventListener("mouseup", () => {
    appState.isDragging = false;
  });

  const viewerShortcuts = {
    "+": () => zoomBy(1.25), "=": () => zoomBy(1.25),
    "-": () => zoomBy(0.8), "_": () => zoomBy(0.8),
    "0": () => resetZoom(),
    "1": () => setViewerMode("preset"),
    "2": () => setViewerMode("auto_adjust"),
    "3": () => setViewerMode("ai_enhanced"),
    "4": () => setViewerMode("split"),
    "5": () => setViewerMode("raw"),
    "6": () => setViewerMode("seams"),
  };

  function modalIsOpen() {
    return ["drift-modal", "wizard-modal", "settings-modal"].some((id) => {
      const el = document.getElementById(id);
      return !!el && el.style.display !== "none" && el.style.display !== "";
    });
  }

  window.addEventListener("keydown", (e) => {
    // Never steal browser shortcuts (Ctrl+/- zoom, Cmd+digit tab switch, Alt+…).
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = document.activeElement ? document.activeElement.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select") {
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    if (e.key === "Escape") {
      // The drift modal is a required decision: dismissing it would silently
      // leave the job undecided, so it stays open until a choice is made.
      closeModal("wizard-modal");
      closeModal("settings-modal");
      return;
    }
    // While a modal is open, viewer shortcuts would mutate the hidden canvas.
    if (modalIsOpen()) return;
    const action = viewerShortcuts[e.key];
    if (action) action();
  });
}

function zoomBy(factor) {
  appState.zoom = Math.min(12.0, Math.max(0.2, appState.zoom * factor));
  document.getElementById("zoom-label").textContent = `${Math.round(appState.zoom * 100)}%`;
  renderCanvas();
}

function resetZoom() {
  appState.zoom = 1.0;
  appState.panX = 0;
  appState.panY = 0;
  document.getElementById("zoom-label").textContent = "Fit";
  renderCanvas();
}

function renderCanvas() {
  const container = document.getElementById("canvas-container");
  const canvas = document.getElementById("scan-canvas");
  const ctx = canvas.getContext("2d");

  const cw = container.clientWidth || 900;
  const ch = container.clientHeight || 600;
  canvas.width = cw;
  canvas.height = ch;

  ctx.fillStyle = "#0a0e14";
  ctx.fillRect(0, 0, cw, ch);

  const job = appState.currentJob;
  if (!job) {
    ctx.fillStyle = "#64748b";
    ctx.font = "15px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Select a source, then click Initiate Scan to capture and stitch a 5-camera scan.", cw / 2, ch / 2);
    return;
  }

  let primaryImg = null;
  let secondaryImg = null;

  if (appState.viewerMode === "split") {
    primaryImg = appState.images.preset || appState.images.raw;
    secondaryImg = appState.images.ai_enhanced || appState.images.auto_adjust || primaryImg;
    document.getElementById("split-left-label").textContent = "Left: Default Stored Preset";
    document.getElementById("split-right-label").textContent = appState.images.ai_enhanced
      ? "Right: AI Enhanced + Calibrated"
      : "Right: Auto-Adjusted (Margin Self-Cal)";
  } else {
    primaryImg = appState.images[appState.viewerMode] || appState.images.auto_adjust || appState.images.preset;
  }

  if (!primaryImg) return;

  const fitScale = Math.min((cw - 40) / primaryImg.width, (ch - 60) / primaryImg.height);
  const scale = fitScale * appState.zoom;
  const drawW = primaryImg.width * scale;
  const drawH = primaryImg.height * scale;
  const drawX = (cw - drawW) / 2 + appState.panX;
  const drawY = (ch - drawH) / 2 + appState.panY;

  if (appState.viewerMode === "split" && secondaryImg) {
    const splitX = drawX + drawW * (appState.splitPercent / 100.0);

    // Draw Left (Default Preset)
    ctx.save();
    ctx.beginPath();
    ctx.rect(drawX, drawY, splitX - drawX, drawH);
    ctx.clip();
    ctx.drawImage(primaryImg, drawX, drawY, drawW, drawH);
    ctx.restore();

    // Draw Right (Auto-Adjusted / AI)
    ctx.save();
    ctx.beginPath();
    ctx.rect(splitX, drawY, drawX + drawW - splitX, drawH);
    ctx.clip();
    ctx.drawImage(secondaryImg, drawX, drawY, drawW, drawH);
    ctx.restore();

    // Split Divider Line
    ctx.strokeStyle = "#3b82f6";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(splitX, drawY);
    ctx.lineTo(splitX, drawY + drawH);
    ctx.stroke();
  } else {
    ctx.drawImage(primaryImg, drawX, drawY, drawW, drawH);
  }

  // Draw 4 Camera Seam Join Markers if enabled
  if (document.getElementById("show-seam-lines").checked) {
    const sm = job.stitch_metadata || {};
    const docWidth = sm.document_width || 12600;
    const joins = sm.joins || [2658, 5320, 7982, 10644];

    ctx.save();
    ctx.strokeStyle = "rgba(245, 158, 11, 0.75)";
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 5]);
    ctx.fillStyle = "#fbbf24";
    ctx.font = "11px sans-serif";
    ctx.textAlign = "center";

    // Label the seams only when the frame is wide enough for the text not to
    // collide (the viewer is narrower now that the action rail takes a column).
    const showLabels = drawW >= 760;
    joins.forEach((jx, idx) => {
      const px = drawX + (jx / docWidth) * drawW;
      ctx.beginPath();
      ctx.moveTo(px, drawY);
      ctx.lineTo(px, drawY + drawH);
      ctx.stroke();
      if (showLabels) {
        ctx.fillText(`Cam ${idx + 1}|${idx + 2} (${jx}px)`, px, Math.max(14, drawY - 6));
      }
    });
    if (!showLabels) {
      ctx.fillStyle = "rgba(203, 213, 225, 0.85)";
      ctx.textAlign = "left";
      ctx.fillText("Seam markers: cam 1|2, 2|3, 3|4, 4|5 (zoom in for positions)", drawX + 4, Math.max(14, drawY - 6));
    }
    ctx.restore();
  }
}

// ---------------------------------------------------------------------------
// Settings & Preset Helpers
// ---------------------------------------------------------------------------

function updateNextFilenamePreview() {
  const rawPrefix = (document.getElementById("export-prefix").value || "scan").trim();
  const safePrefix = rawPrefix.replace(/[^A-Za-z0-9_-]+/g, "_") || "scan";
  const seq = Math.max(1, parseInt(document.getElementById("export-next-seq").value || "1", 10));
  const padded = String(seq).padStart(4, "0");
  const el = document.getElementById("next-file-preview");
  if (!el) return;
  const valueEl = el.querySelector("strong");
  const value = `${safePrefix}_${padded}`;
  if (valueEl) {
    // The card markup owns the label; only the filename is dynamic.
    valueEl.textContent = value;
    el.title = `Next saved file: ${value}`;
  } else {
    el.textContent = `Next saved file: ${value}`;
  }
}

function currentOutputPayload(overrides = {}) {
  const prefix = (document.getElementById("export-prefix").value || "scan").trim() || "scan";
  const seq = Math.max(1, parseInt(document.getElementById("export-next-seq").value || "1", 10) || 1);
  const outputDir = (document.getElementById("export-output-dir").value || "").trim();
  const formats = selectedExportFormats();
  return {
    autosave_enabled: document.getElementById("autosave-enabled").checked,
    filename_prefix: prefix,
    next_sequence_number: seq,
    output_dir: outputDir,
    export_formats: formats,
    ...overrides,
  };
}

async function persistOutputSettings(overrides = {}) {
  if (!appState.settingsLoaded) return;  // don't overwrite saved settings with HTML placeholders
  const payload = currentOutputPayload(overrides);
  const note = document.getElementById("export-format-note");
  if (!payload.output_dir) {
    // Never let an empty field create files in the process working directory.
    const outDir = document.getElementById("export-output-dir");
    if (outDir) outDir.value = appState.settings.output_dir || "";
    if (note) note.textContent = "Output directory cannot be empty.";
    return;
  }
  Object.assign(appState.settings, payload);
  updateNextFilenamePreview();
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const err = await res.json();
        detail = err.detail || detail;
      } catch (_) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    const saved = await res.json();
    appState.settings = { ...appState.settings, ...saved };
    syncOutputDock();
  } catch (err) {
    if (note) note.textContent = `Could not save output settings: ${err.message}`;
  }
}

function syncOutputDock() {
  const s = appState.settings || {};
  const autosave = document.getElementById("autosave-enabled");
  if (autosave) autosave.checked = s.autosave_enabled !== false;

  const outDir = document.getElementById("export-output-dir");
  if (outDir && document.activeElement !== outDir) outDir.value = s.output_dir || "";

  const prefix = document.getElementById("export-prefix");
  if (prefix && document.activeElement !== prefix) prefix.value = s.filename_prefix || "scan";

  const seq = document.getElementById("export-next-seq");
  if (seq && document.activeElement !== seq) seq.value = s.next_sequence_number || 1;

  const exportFormats = Array.isArray(s.export_formats) && s.export_formats.length
    ? s.export_formats
    : ["tiff", "jpg", "pdf"];
  const fmtTiff = document.getElementById("fmt-tiff");
  const fmtJpg = document.getElementById("fmt-jpg");
  const fmtPdf = document.getElementById("fmt-pdf");
  if (fmtTiff) fmtTiff.checked = exportFormats.includes("tiff");
  if (fmtJpg) fmtJpg.checked = exportFormats.includes("jpg");
  if (fmtPdf) fmtPdf.checked = exportFormats.includes("pdf");

  const formatNote = document.getElementById("export-format-note");
  if (formatNote) {
    formatNote.textContent =
      `Autosave and Save / Export will write ${exportFormats.map((f) => f.toUpperCase()).join(", ")}.`;
  }
  updateNextFilenamePreview();
}

// -- Output directory browser ------------------------------------------------

let browseCurrentPath = "";
let browseParentPath = null;
let browseHomePath = "";

function setBrowseStatus(text, isError = false) {
  const el = document.getElementById("browse-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#f87171" : "";
}

function renderBrowseList(directories) {
  const list = document.getElementById("browse-list");
  if (!list) return;
  list.innerHTML = "";
  if (!directories.length) {
    const empty = document.createElement("div");
    empty.className = "browse-empty";
    empty.textContent = "No subfolders here. Use this folder, or go up.";
    list.appendChild(empty);
    return;
  }
  for (const d of directories) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "browse-dir";
    btn.setAttribute("role", "option");
    btn.title = d.path;

    const icon = document.createElement("span");
    icon.textContent = "📁";
    icon.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "browse-dir-name";
    name.textContent = d.name;
    const arrow = document.createElement("span");
    arrow.className = "browse-dir-arrow";
    arrow.textContent = "›";

    btn.append(icon, name, arrow);
    btn.addEventListener("click", () => loadBrowsePath(d.path));
    list.appendChild(btn);
  }
}

async function loadBrowsePath(path) {
  setBrowseStatus("Loading…");
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    const res = await fetch(`/api/fs/dirs${query}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    browseCurrentPath = data.path || "";
    browseParentPath = data.parent || null;
    browseHomePath = data.home || "";
    const input = document.getElementById("browse-path-input");
    if (input) input.value = browseCurrentPath;
    renderBrowseList(data.directories || []);
    setBrowseStatus(
      data.truncated
        ? `Showing the first ${data.directories.length} folders.`
        : `${(data.directories || []).length} subfolder(s)`
    );
  } catch (err) {
    browseCurrentPath = "";
    browseParentPath = null;
    renderBrowseList([]);
    setBrowseStatus(`Cannot open folder: ${err.message}`, true);
  }
}

function openBrowseModal() {
  const start = (document.getElementById("export-output-dir").value || "").trim()
    || appState.settings.output_dir
    || "";
  setBrowseStatus("");
  openModal("browse-modal");
  loadBrowsePath(start);
}

function browseUp() {
  if (browseParentPath) loadBrowsePath(browseParentPath);
}

function browseHome() {
  loadBrowsePath(browseHomePath || "~");
}

async function useBrowsedFolder() {
  if (!browseCurrentPath) {
    setBrowseStatus("Choose a folder first.", true);
    return;
  }
  const input = document.getElementById("export-output-dir");
  if (input) input.value = browseCurrentPath;
  await persistOutputSettings({ output_dir: browseCurrentPath });
  closeModal("browse-modal");
}

function setJpegQualityValue(value) {
  const sel = document.getElementById("set-jpg-quality");
  if (!sel) return;
  const q = Math.max(1, Math.min(100, parseInt(value, 10) || 95));
  let opt = Array.from(sel.options).find((o) => Number(o.value) === q);
  if (!opt) {
    opt = document.createElement("option");
    opt.value = String(q);
    opt.textContent = `${q}% — Custom`;
    sel.appendChild(opt);
  }
  sel.value = String(q);
}

function syncSettingsForm() {
  const s = appState.settings;
  document.getElementById("set-scanner-ip").value = s.scanner_ip || "0.0.0.0";
  document.getElementById("set-scanner-serial").value = s.scanner_serial || "UNKNOWN";
  document.getElementById("set-drift-policy").value = s.drift_policy || "prompt";
  const scanDrift = document.getElementById("scan-drift-policy");
  if (scanDrift) scanDrift.value = s.drift_policy || "prompt";
  const autoAi = document.getElementById("ai-auto-enhance");
  if (autoAi) autoAi.checked = !!s.auto_ai_enhance;
  document.getElementById("set-eject-mode").value = s.post_scan_eject || "hold";
  setJpegQualityValue(s.jpg_quality != null ? s.jpg_quality : 95);
  document.getElementById("set-auto-number").checked = s.auto_number_enabled !== false;
  document.getElementById("set-tiff-16bit").checked = !!s.tiff_16bit;
  document.getElementById("set-debug-mode").checked = !!s.debug_mode;

  const seamEnabled = s.seam_color_blend_enabled !== false;
  const seamWidth = s.seam_blend_width_px || 128;
  const seamStrengthPct = Math.round((s.seam_blend_strength != null ? s.seam_blend_strength : 0.85) * 100);
  document.getElementById("set-seam-blend-enabled").checked = seamEnabled;
  document.getElementById("set-seam-blend-width").value = seamWidth;
  document.getElementById("set-seam-blend-strength").value = seamStrengthPct;

  if (!appState.scanSeamBlendUserSet) {
    document.getElementById("scan-seam-blend").checked = seamEnabled;
  }
  document.getElementById("scan-blend-width").value = seamWidth;
  document.getElementById("scan-blend-strength").value = seamStrengthPct;
  toggleScanSeamBlendControls();
  onScanBlendAdjustChange();

  document.getElementById("ai-blend-seam-colors").checked = seamEnabled;
  document.getElementById("ai-blend-width").value = seamWidth;
  document.getElementById("ai-blend-strength").value = seamStrengthPct;
  toggleAiSeamBlendControls();
  onAiBlendAdjustChange();

  document.getElementById("set-photocraft-url").value = s.photocraft_url || "";
  document.getElementById("set-photocraft-key").value = s.photocraft_api_key || "";
  syncOutputDock();
}

async function saveSettings() {
  const payload = {
    scanner_ip: document.getElementById("set-scanner-ip").value.trim(),
    scanner_serial: document.getElementById("set-scanner-serial").value.trim(),
    drift_policy: document.getElementById("set-drift-policy").value,
    post_scan_eject: document.getElementById("set-eject-mode").value,
    seam_color_blend_enabled: document.getElementById("set-seam-blend-enabled").checked,
    seam_blend_width_px: Math.max(8, Math.min(1024, parseInt(document.getElementById("set-seam-blend-width").value || "128", 10))),
    seam_blend_strength: Math.max(0.05, Math.min(1.0, parseFloat(document.getElementById("set-seam-blend-strength").value || "85") / 100.0)),
    auto_number_enabled: document.getElementById("set-auto-number").checked,
    tiff_16bit: document.getElementById("set-tiff-16bit").checked,
    jpg_quality: Math.max(1, Math.min(100, parseInt(document.getElementById("set-jpg-quality").value || "95", 10))),
    debug_mode: document.getElementById("set-debug-mode").checked,
    photocraft_url: document.getElementById("set-photocraft-url").value.trim(),
    photocraft_api_key: document.getElementById("set-photocraft-key").value.trim(),
  };

  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  closeModal("settings-modal");
  refreshStatus();
}

async function rollbackActivePreset() {
  const presetId = document.getElementById("scan-preset").value;
  const res = await fetch(`/api/presets/${presetId}/rollback`, { method: "POST" });
  if (!res.ok) {
    alert("No previous .bak backup exists for this preset.");
    return;
  }
  alert(`Rolled back preset '${presetId}' to previous backup.`);
  refreshStatus();
}

function openModal(id) {
  document.getElementById(id).style.display = "flex";
  if (id === "wizard-modal") {
    onWizardAutoAlignChange();
    loadWizardPreview();
  }
}

function closeModal(id) {
  document.getElementById(id).style.display = "none";
}

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/events`);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "scan_started") {
        showCancelButton();
        startScanActivity("Connecting to scanner…");
        setPipelineStep(1, `Scan ${msg.scan_id || ""} started — connecting to scanner...`);
      } else if (msg.type === "scan_progress") {
        const stgMap = {
          collecting_stream: 2,
          generating_demo_stream: 2,
          analyzing_color_drift: 3,
          stitching_cameras: 4,
        };
        const stageLabels = {
          connecting: "Connecting to scanner…",
          recovering_media: "Recovering media (unload → load)…",
          arming: msg.command
            ? `Arming scanner ${msg.index || 0}/${msg.total || msg.commands || "?"} — ${msg.command}…`
            : "Arming scanner…",
          streaming: `Receiving scan data… ${(msg.records || 0).toLocaleString()} records`,
          collecting_stream: "Collecting 5-camera stream…",
          generating_demo_stream: "Generating demo stream…",
          analyzing_color_drift: "Checking colour / temperature drift…",
          stitching_cameras: "Stitching cameras…",
          cancelled: "Cancelling…",
        };
        const label = stageLabels[msg.stage] || `Scanning (${msg.stage})…`;
        updateScanActivity(label);
        setPipelineStep(stgMap[msg.stage] || 2, label);
      } else if (msg.type === "scan_completed" && msg.scan_id) {
        hideCancelButton();
        stopScanActivity();
        loadJobIntoUI(msg, true);
        refreshStatus();
      } else if (msg.type === "scan_cancelled" && msg.scan_id) {
        hideCancelButton();
        stopScanActivity();
        setPipelineStep(1, `Scan ${msg.scan_id} cancelled. Use Recover or Eject Front if media is still loaded.`);
        refreshStatus();
      } else if (msg.type === "panel_error") {
        hideCancelButton();
        stopScanActivity();
        setPipelineStep(1, `Scan error: ${msg.error || "unknown"}`);
      } else if ((msg.type === "drift_resolved" || msg.type === "ai_enhanced") && msg.scan_id) {
        loadJobIntoUI(msg, false);
        refreshStatus();
      }
    } catch (_) {}
  };
  ws.onopen = () => {
    // Events emitted while the socket was down are lost; re-read the server state.
    refreshStatus();
  };
  ws.onclose = () => {
    setTimeout(connectWebSocket, 2000);
  };
}

