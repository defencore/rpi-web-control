import {byId, setText, element, button, capacity, destinationLabel, isRecordingId} from "./ui.js";
import {setupNetworkPanel} from "./network.js";
import {setupDevicePanels, renderSensorProperties} from "./device.js";

const pageLinks = [...byId("main-nav").querySelectorAll("[data-page]")];
const defaults = {
  timer: {interval_s: 60, duration_s: 5},
  gpio: {pin: 17, contact: "NO", debounce_ms: 30},
  sensor: {path: "/sys/class/thermal/thermal_zone0/temp", comparison: "above", threshold: 60000, hysteresis: 2000, poll_ms: 250},
  uart: {device: "/dev/serial0", baud: 115200, start_command: "START", stop_command: "STOP"},
};
const gpioStopDefaults = {
  gpio: {pin: 27, contact: "NC", debounce_ms: 30},
  uart: {device: "/dev/ttyUSB0", baud: 115200, command: "STOP"},
};
const contactChoices = [["NO", "Normally open · closes on press"], ["NC", "Normally closed · opens on press"]];
// Physical pin numbers on the standard Raspberry Pi 40-pin header, indexed by BCM line.
const headerPins = [27, 28, 3, 5, 7, 29, 31, 26, 24, 21, 19, 23, 32, 33,
  8, 10, 36, 11, 12, 35, 38, 40, 15, 16, 18, 22, 37, 13];
const integerFormat = new Intl.NumberFormat("en-US");
const fpsFormat = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});
const recordingSizeFormat = new Intl.NumberFormat("en-US", {maximumSignificantDigits: 4});
const clockFormat = new Intl.DateTimeFormat("en-GB", {
  year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  second: "2-digit", timeZoneName: "short",
});
const clockToleranceMs = 5000;
let clockDismissed = false;
const dirtyForms = new Set();
let snapshot = null;
let capabilities = null;
let page = "monitor";
let polling = 0;
let refreshId = 0;
let authenticated = false;
let deviceOnline = false;
let actionPending = false;
let editingRule = null;
let lastRuleList = null;
let lastRoots = null;
let nextStreamAttempt = 0;
let stoppedSession = null;

const network = setupNetworkPanel({api, act, notice, busy: () => actionPending});
const device = setupDevicePanels({api, act, notice, busy: () => actionPending, chooseDestination: saveDestination});

function saveDestination(path) {
  const field = byId("storage-form").elements.directory;
  const draft = field.value;
  return act(async () => {
    await api("config", {storage: {directory: path}});
    if (field.value === draft) dirtyForms.delete("storage-form");
  }, "Recording destination saved.");
}

function notice(message = "", error = false, target = "feedback") {
  setText(target, message);
  byId(target).classList.toggle("error", error);
  if (target === "feedback") {
    byId("feedback-bar").hidden = !message;
    byId("feedback-bar").classList.toggle("error", error);
  }
}

async function api(path, data) {
  const options = {credentials: "same-origin", cache: "no-store"};
  if (data !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(data);
  }
  const response = await fetch("/api/" + path, options);
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "login") signInScreen();
    throw new Error(payload.error || ("The device returned HTTP " + response.status + "."));
  }
  return payload;
}

function signInScreen() {
  const wasSignedIn = authenticated;
  authenticated = false;
  snapshot = null;
  stoppedSession = null;
  capabilities = null;
  refreshId++;
  clearTimeout(polling);
  stopStream();
  clockDismissed = false;
  byId("clock-notice").hidden = true;
  byId("workspace").hidden = true;
  byId("nav-toggle").setAttribute("aria-expanded", "false");
  byId("login-view").hidden = false;
  byId("retry-connection").hidden = true;
  setText("entry-heading", "Operator sign-in");
  setText("entry-help", "Enter the access token for this device.");
  byId("entry-help").hidden = false;
  byId("login-form").hidden = false;
  document.title = "Sign in · Capture Control";
  dirtyForms.clear();
  device.reset();
  byId("trigger-form").hidden = true;
  byId("saved-rules").hidden = false;
  editingRule = null;
  network.reset();
  notice(wasSignedIn ? "Sign in to continue." : "", false, "login-message");
  byId("access-token").focus();
}

function connection(online) {
  deviceOnline = online;
  setText("connection-status", online ? "Connected" : "Reconnecting");
  byId("connection-lamp").className = "lamp " + (online ? "good" : "bad");
  byId("offline-message").hidden = online;
  if (!online) {
    byId("clock-notice").hidden = true;
    renderMonitor();
  }
}

function clockGap(ms) {
  let seconds = Math.round(Math.abs(ms) / 1000);
  const parts = [];
  for (const [length, unit] of [[86400, "d"], [3600, "h"], [60, "min"], [1, "s"]]) {
    const count = Math.floor(seconds / length);
    if (count) parts.push(count + " " + unit);
    seconds %= length;
    if (parts.length === 2) break;
  }
  return parts.join(" ");
}

function compareClocks(data, elapsedMs) {
  const piMs = data.device_realtime_ms;
  // The Pi sampled its clock somewhere within this request's round trip.
  const browserMs = Date.now() - elapsedMs / 2;
  const gapMs = piMs - browserMs;
  const banner = byId("clock-notice");
  banner.hidden = clockDismissed || !Number.isSafeInteger(piMs) || Math.abs(gapMs) <= clockToleranceMs + elapsedMs / 2;
  if (banner.hidden) return;
  setText("clock-heading", "Pi clock is about " + clockGap(gapMs) + (gapMs > 0 ? " ahead of" : " behind") + " this browser");
  setText("clock-pi", clockFormat.format(piMs));
  setText("clock-browser", clockFormat.format(browserMs));
  const recording = data.camera.recording || data.recording_sources.length > 0;
  byId("clock-set").disabled = actionPending || recording;
  byId("clock-lock").hidden = !recording;
}

function schedule() {
  clearTimeout(polling);
  if (authenticated || byId("login-form").hidden) {
    const testing = page === "camera" && snapshot?.camera.mode_test?.state === "running";
    polling = setTimeout(refresh, document.hidden ? 12000 : testing ? 1000 : 2800);
  }
}

async function loadCapabilities() {
  try {
    const result = await api("capabilities");
    if (!authenticated) return;
    capabilities = result;
    renderSensorProperties(result);
    renderModes();
    render();
  } catch (error) {
    if (authenticated) setText("mode-help", error.message);
  }
}

async function refresh() {
  const ticket = ++refreshId;
  clearTimeout(polling);
  const started = performance.now();
  try {
    const data = await api("status");
    if (ticket !== refreshId) return;
    const first = !authenticated;
    const finishedTest = snapshot?.camera.mode_test?.state === "running" && data.camera.mode_test?.state !== "running";
    if (data.camera.recording || data.camera.last_error || data.storage?.error ||
        data.config.storage.directory !== snapshot?.config.storage.directory) {
      stoppedSession = null;
    } else if (snapshot?.camera.recording && snapshot.camera.session === data.camera.session && !data.recording_sources.length) {
      stoppedSession = data.camera.session;
    }
    authenticated = true;
    snapshot = data;
    byId("logout").hidden = !data.token_required;
    setText("access-mode", data.token_required ? "Token access" : "Direct access");
    byId("login-view").hidden = true;
    byId("workspace").hidden = false;
    connection(true);
    compareClocks(data, performance.now() - started);
    if (first) {
      navigate();
      loadCapabilities();
    } else if (finishedTest && capabilities?.available === false) {
      loadCapabilities();
    }
    render();
  } catch (error) {
    if (ticket !== refreshId) return;
    if (authenticated) {
      connection(false);
    } else {
      setText("entry-heading", "Device unavailable");
      setText("entry-help", "Check the connection to the device. Retrying automatically.");
      byId("entry-help").hidden = false;
      byId("retry-connection").hidden = false;
      notice(error.message, true, "login-message");
    }
  } finally {
    if (ticket === refreshId) schedule();
  }
}

async function act(work, message) {
  if (actionPending) return;
  actionPending = true;
  const controls = [...document.querySelectorAll("#workspace button:enabled:not(#nav-toggle, #dismiss-feedback)")];
  controls.forEach(control => { control.disabled = true; });
  notice();
  try {
    await work();
    if (authenticated && message) notice(message);
  } catch (error) {
    if (authenticated) notice(error.message, true);
  } finally {
    actionPending = false;
    controls.forEach(control => { control.disabled = false; });
    if (authenticated) await refresh();
  }
}

function navigate() {
  const hash = window.location.hash.slice(1);
  const [requested, detail, ...rest] = hash.split("/");
  const sessionId = requested === "recordings" && !rest.length && isRecordingId(detail) ? detail : null;
  const cameraTask = hash === "camera/tests" || hash === "camera/profile";
  const validRoute = hash === requested || Boolean(sessionId) || cameraTask;
  page = validRoute && pageLinks.some(link => link.dataset.page === requested) ? requested : "monitor";
  byId("nav-toggle").setAttribute("aria-expanded", "false");
  for (const link of pageLinks) {
    const active = link.dataset.page === page;
    byId("page-" + link.dataset.page).hidden = !active;
    if (active) {
      link.setAttribute("aria-current", "page");
      setText("page-title", link.textContent);
      document.title = link.textContent + " · Capture Control";
    } else link.removeAttribute("aria-current");
  }
  if (page === "camera") renderCameraSection(hash);
  renderStoppedRecording();
  notice();
  updateStream();
  if (authenticated) device.open(page, sessionId);
  if (page === "camera" && authenticated && capabilities?.available === false) loadCapabilities();
}

function stopStream() {
  const image = byId("stream");
  image.removeAttribute("src");
  image.hidden = true;
}

function updateStream() {
  const enabled = Boolean(snapshot?.camera.preview);
  const available = capabilities?.available !== false;
  const viewing = authenticated && deviceOnline && enabled && page === "monitor" && !document.hidden;
  const image = byId("stream");
  if (viewing && !image.hasAttribute("src") && Date.now() >= nextStreamAttempt) {
    image.src = "/api/stream.mjpg?t=" + Date.now();
    image.hidden = false;
  } else if (!viewing && image.hasAttribute("src")) {
    stopStream();
  }
  byId("stage-empty").hidden = !image.hidden;
  byId("fullscreen").disabled = actionPending || image.hidden || !byId("stage").requestFullscreen;
  if (!viewing || image.hidden) {
    if (!deviceOnline) {
      setText("stage-title", "Pi disconnected");
      setText("stage-help", "Waiting for the connection to resume.");
    } else {
      setText("stage-title", !available ? "Camera unavailable" : enabled ? "Waiting for frames" : "Preview is off");
      setText("stage-help", !available ? (capabilities.error || "Check the camera connection.") : enabled ? "The preview will resume when frames arrive." : "You can record with the preview off.");
    }
  }
}

function timeRemaining(seconds) {
  const minutes = Math.floor(seconds / 60);
  if (!minutes) return "<1 min";
  if (minutes < 60) return minutes + " min";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + " h " + (minutes % 60) + " min";
  return Math.floor(hours / 24) + " d " + (hours % 24) + " h";
}

function renderRecordingStorage(cam, storageError) {
  const volume = snapshot.storage;
  const dirs = snapshot.recording_directories || {current: snapshot.config.storage.directory};
  const medium = destinationLabel(dirs);
  const hasCapacity = deviceOnline && Number.isFinite(volume?.free_bytes) && volume.total_bytes > 0;
  const free = hasCapacity ? capacity(volume.free_bytes) + " free" : "Capacity unavailable";
  setText("toolbar-destination", deviceOnline ? medium + (hasCapacity ? " · " + free : "") : "Recording destination");
  byId("toolbar-destination").title = dirs.current;
  setText("monitor-destination", medium);
  setText("record-location", dirs.current);
  setText("monitor-space", free);
  const usage = byId("disk-usage");
  usage.hidden = !hasCapacity;
  if (hasCapacity) usage.value = (1 - volume.free_bytes / volume.total_bytes) * 100;
  byId("storage-alert").hidden = !deviceOnline || !storageError;
  setText("storage-alert-reason", storageError);
  const metrics = byId("recording-metrics");
  metrics.hidden = !deviceOnline || !cam.recording || Boolean(storageError);
  if (metrics.hidden) return;
  const bytes = cam.recording_video_bytes;
  const rate = cam.recording_video_bytes_per_second;
  const hasRate = Number.isFinite(rate) && rate > 0;
  setText("monitor-written", Number.isFinite(bytes) ? capacity(bytes) : "—");
  setText("monitor-rate", hasRate ? recordingSizeFormat.format(rate / 1e6) + " MB/s" : "—");
  setText("monitor-remaining", hasCapacity && hasRate ? timeRemaining(volume.free_bytes / rate) : "—");
}

function renderStoppedRecording() {
  const id = stoppedSession?.split("/").pop();
  const show = deviceOnline && isRecordingId(id) && window.location.hash !== "#recordings/" + id && !snapshot?.recording_sources.length;
  byId("recording-stopped").hidden = !show;
  if (show) byId("view-stopped-recording").href = "#recordings/" + id;
}

function renderMonitor() {
  if (!snapshot) return;
  const cam = snapshot.camera;
  const settings = cam.settings || snapshot.config.camera;
  const owners = snapshot.recording_sources || [];
  const manual = owners.includes("manual");
  const triggers = owners.filter(source => source.startsWith("trigger:"));
  const stopping = cam.recording || owners.length > 0;
  const record = byId("record");
  const camAvailable = capabilities?.available !== false;
  const storageError = snapshot.storage?.error || (snapshot.storage?.free_bytes === 0 ? "The recording destination is full." : "");
  const testing = cam.mode_test?.state === "running";
  record.disabled = actionPending || !deviceOnline || (!stopping && (!camAvailable || testing || Boolean(storageError)));
  record.classList.toggle("active", stopping);
  record.querySelector("use").setAttribute("href", stopping ? "#i-stop" : "#i-record");
  record.querySelector("span").textContent = triggers.length ? "Stop & pause triggers" : stopping ? "Stop recording" : "Start recording";
  byId("trigger-pause-notice").hidden = !deviceOnline || !snapshot.triggers_paused;
  byId("resume-triggers").disabled = actionPending || !deviceOnline;
  byId("trigger-pause").hidden = snapshot.triggers_paused || !snapshot.config.triggers.some(rule => rule.enabled);
  byId("trigger-pause").disabled = actionPending || !deviceOnline;
  byId("manual-hold").hidden = !deviceOnline || !triggers.length || (!manual && !cam.recording);
  byId("manual-hold").disabled = actionPending;
  setText("manual-hold", manual ? "Return control to triggers" : "Keep recording after triggers stop");
  setText("device-name", capabilities?.model || (cam.backend === "simulated" ? "Simulated camera" : "Capture device"));
  const preview = byId("preview");
  preview.disabled = actionPending || !deviceOnline || testing || (!camAvailable && !cam.preview);
  preview.querySelector("span").textContent = cam.preview ? "Stop preview" : "Start preview";
  setText("stream-state", !deviceOnline ? "Unknown" : cam.preview ? "Enabled" : "Off");
  byId("stream-state").classList.toggle("live", deviceOnline && cam.preview);
  setText("actual-fps", cam.active && Number.isFinite(cam.actual_fps) ? fpsFormat.format(cam.actual_fps) + " fps" : "—");
  setText("target-fps", fpsFormat.format(settings.fps) + " fps");
  const mode = cam.sensor_mode;
  setText("active-sensor-mode", mode ? mode.width + " × " + mode.height + " · " + mode.bit_depth + "-bit" : "—");
  setText("resolution", settings.width + " × " + settings.height);
  setText("osd-status", cam.osd_effective ? "Enabled" : "Off");
  setText("frame-count-label", cam.recording ? "Frames in this clip" : "Frames in last clip");
  setText("frame-count", cam.session && Number.isFinite(cam.frames) ? integerFormat.format(cam.frames) : "—");
  setText("dropped-count", Number.isFinite(cam.dropped_frames) ? integerFormat.format(cam.dropped_frames) : "—");
  let state = camAvailable ? "Idle" : "Camera unavailable";
  if (!deviceOnline) state = "Status unknown";
  else if (cam.recording) state = "Recording";
  else if (owners.length) state = "Waiting to resume";
  else if (testing) state = "Testing camera";
  setText("record-state", state);
  byId("record-state").classList.toggle("recording", deviceOnline && cam.recording);
  byId("record-state").classList.toggle("waiting", deviceOnline && !cam.recording && owners.length > 0);
  renderRecordingStorage(cam, storageError);
  renderStoppedRecording();
  const labels = triggers.map(source => "“" + source.slice(8) + "”");
  if (manual) labels.push("manual control");
  setText("record-sources", triggers.length ? "Controlled by " + labels.join(", ") : "");
  byId("record-sources").hidden = !deviceOnline || !triggers.length;
  setText("camera-error", cam.last_error || capabilities?.error || "");
  const rules = snapshot.config.triggers;
  const active = (snapshot.triggers || []).filter(rule => rule.active).length;
  const errors = (snapshot.triggers || []).filter(rule => rule.state === "error").length;
  const enabledCount = rules.filter(rule => rule.enabled !== false).length;
  const triggerState = [enabledCount + " enabled", ...(active ? [active + " active"] : []), ...(errors ? [errors + " with errors"] : [])].join(" · ");
  setText("trigger-summary", snapshot.triggers_paused ? "Paused" : enabledCount ? triggerState : "Manual recording only");
  updateStream();
}

function fillSettings(formId, settings) {
  if (dirtyForms.has(formId)) return;
  const form = byId(formId);
  for (const [name, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(name);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = value;
    else if (input.value !== String(value)) input.value = value;
  }
}

function renderCameraSection(hash) {
  const links = [...byId("camera-sections").querySelectorAll("a")];
  const active = links.find(link => link.hash.slice(1) === hash) || links[0];
  for (const link of links) {
    const current = link === active;
    byId(link.getAttribute("aria-controls")).hidden = !current;
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function renderCameraSettings() {
  const form = byId("camera-form");
  const fields = form.elements;
  const fps = fields.fps.valueAsNumber;
  const raw = fields.fps.validity.valid && fps > 60;
  const hasEstimate = raw && fields.width.validity.valid && fields.height.validity.valid;
  byId("camera-overlay").hidden = raw;
  byId("capture-estimates").hidden = !hasEstimate;
  setText("capture-format", !fields.fps.validity.valid ? "—" : raw ? "Uncompressed YUV420 (Y4M)" : "Motion JPEG (MJPEG)");
  if (hasEstimate) {
    const bytesPerSecond = fields.width.valueAsNumber * fields.height.valueAsNumber * 1.5 * fps;
    setText("capture-rate", recordingSizeFormat.format(bytesPerSecond / 1e6) + " MB/s");
    setText("capture-space", recordingSizeFormat.format(bytesPerSecond * 60 / 1e9) + " GB/min");
  }
  let help = "Enter a target frame rate from 1 to 1000 fps.";
  if (raw) {
    help = hasEstimate ? "Estimates cover frame data at the target rate. Allow extra space for the frame index and headers."
      : "Enter valid output dimensions to estimate write rate and recording size.";
    help += " Above 60 fps, sensor time is saved in frames.csv without an image overlay.";
  } else if (fields.fps.validity.valid) {
    help = "JPEG size varies with the scene. Check Average video rate on Capture monitor during a sample recording. Each frame’s sensor time is saved in frames.csv.";
  }
  setText("capture-format-help", help);
  const cam = snapshot?.camera;
  let lock = "";
  if (cam?.mode_test?.state === "running") lock = "Finish the sensor test to save settings.";
  else if (cam?.recording || snapshot?.recording_sources.length) lock = "Stop recording before saving settings.";
  else if (cam?.live?.enabled) lock = "Stop the live stream before saving settings.";
  form.querySelector('[type="submit"]').disabled = actionPending || Boolean(lock);
  setText("camera-save-state", lock || (dirtyForms.has("camera-form") ? "Unsaved changes" : ""));
}

function updateSensorDepthOptions() {
  const select = byId("camera-form").elements.sensor_bit_depth;
  const selected = dirtyForms.has("camera-form") ? Number(select.value) : snapshot.config.camera.sensor_bit_depth || 0;
  const detected = (capabilities?.modes || []).map(mode => mode.bit_depth);
  const depths = [...new Set([selected, ...detected])].filter(depth => Number.isInteger(depth) && depth > 0);
  const values = [0, ...depths.sort((a, b) => a - b)];
  if (values.length !== select.options.length || values.some((depth, i) => select.options[i].value !== String(depth))) {
    select.replaceChildren(...values.map(depth => new Option(depth ? depth + "-bit" : "Automatic", depth)));
  }
  select.value = String(selected);
}

function modeKey(mode) {
  return mode.width + "x" + mode.height + ":" + mode.bit_depth;
}

function renderModes() {
  const modes = capabilities?.modes || [];
  const selection = byId("mode-test-selection");
  const selected = selection.value;
  selection.replaceChildren(new Option("All detected modes", ""));
  setText("mode-help", modes.length ? "A preset fills in the dimensions, sensor bit depth and advertised maximum frame rate. You can lower the target rate before saving." : capabilities?.error || "No sensor modes reported.");
  byId("modes").replaceChildren(...modes.map(mode => {
    const rate = Math.floor(mode.max_fps * 100) / 100;
    const label = mode.width + " × " + mode.height + " · up to " + fpsFormat.format(rate) + " fps" +
      (mode.bit_depth ? " · " + mode.bit_depth + "-bit" : "");
    const key = modeKey(mode);
    selection.add(new Option(label, key, false, key === selected));
    return button(label, () => {
      const form = byId("camera-form");
      form.elements.width.value = mode.width;
      form.elements.height.value = mode.height;
      form.elements.fps.value = rate;
      form.elements.sensor_bit_depth.value = String(mode.bit_depth || 0);
      dirtyForms.add("camera-form");
      renderCameraSettings();
      byId("camera-presets").open = false;
      form.elements.fps.focus();
    });
  }));
}

function renderModeTest() {
  const cam = snapshot.camera;
  const test = cam.mode_test;
  const running = test?.state === "running";
  const activeUses = [];
  if (cam.preview) activeUses.push("the browser preview");
  if (cam.live?.enabled) activeUses.push("the network stream");
  if (cam.recording || snapshot.recording_sources.length) activeUses.push("the recording");
  byId("mode-test-start").hidden = running;
  byId("mode-test-start").disabled = actionPending || activeUses.length > 0 || !test;
  byId("mode-test-cancel").hidden = !running;
  byId("mode-test-cancel").disabled = actionPending;
  byId("mode-test-selection").disabled = actionPending || running;
  setText("mode-test-lock", activeUses.length && !running ? "Stop " + activeUses.join(" and ") + " before testing the sensor." : "");
  const results = test?.results || [];
  const sensorLabel = mode => mode.width + " × " + mode.height + " · " + mode.bit_depth + "-bit";
  let status = "";
  if (running) {
    status = test.current ? "Testing " + sensorLabel(test.current) + " (" + (results.length + 1) + " of " + test.total + ")." : "Discovering sensor modes…";
  } else if (test?.state === "cancelled") {
    status = "Test cancelled. " + results.length + " of " + test.total + " modes tested.";
  } else if (test?.state === "completed") {
    status = results.length === 1 ? "Test complete. 1 mode passed." : "Test complete. " + results.length + " modes passed.";
  } else if (test?.state === "failed") {
    const failed = results.filter(row => row.status === "failed").length;
    status = results.length ? "Test finished. " + failed + " of " + test.total + " failed." : "Could not measure any sensor modes.";
  }
  if (test?.error) status += " " + test.error;
  if (cam.backend === "simulated" && status) status = "Simulated camera. " + status;
  setText("mode-test-status", status);
  byId("mode-test-results").hidden = !results.length;
  const rows = [];
  for (const item of results) {
    const row = element("tr");
    row.append(element("td", sensorLabel(item.mode)), element("td", fpsFormat.format(item.mode.max_fps)),
      element("td", Number.isFinite(item.achieved_fps) ? fpsFormat.format(item.achieved_fps) : "—"),
      element("td", item.status === "passed" ? "Passed" : "Failed", item.status === "failed" ? "danger" : ""));
    rows.push(row);
    if (item.status === "failed") {
      const detail = element("tr");
      const cell = element("td", item.error || "", "danger");
      const counts = [["Frames sampled", item.frames], ["Timestamp gaps", item.timestamp_gaps],
        ["Capture queue drops", item.queue_drops]]
        .filter(([, count]) => Number.isInteger(count) && count > 0)
        .map(([label, count]) => label + " " + integerFormat.format(count));
      if (counts.length) cell.append(element("div", counts.join(" · ")));
      cell.colSpan = 4;
      detail.append(cell);
      rows.push(detail);
    }
  }
  byId("mode-test-rows").replaceChildren(...rows);
}

function renderRoots() {
  const volume = snapshot.storage;
  const locked = snapshot.camera.recording || snapshot.recording_sources.length > 0;
  byId("storage-form").querySelector('[type="submit"]').disabled = actionPending || locked;
  setText("storage-lock", locked ? "Finish the active recording before saving a new destination." : "");
  const directory = snapshot.config.storage.directory;
  setText("storage-active", directory);
  setText("storage-capacity", Number.isFinite(volume?.free_bytes) && Number.isFinite(volume?.total_bytes)
    ? capacity(volume.free_bytes) + " free of " + capacity(volume.total_bytes) : (volume?.error || "Capacity unavailable."));
  setText("storage-draft", byId("storage-form").elements.directory.value !== directory
    ? "Unsaved folder. New recordings still go to " + directory + "." : "");
  const roots = snapshot.storage_roots || [];
  if (JSON.stringify(roots) === lastRoots) return;
  lastRoots = JSON.stringify(roots);
  byId("storage-options").replaceChildren(...roots.map(root => new Option(root, root)));
  byId("storage-roots").replaceChildren(...roots.map(root => element("li", root)));
}

function gpioBehavior(rule) {
  const pin = "GPIO" + rule.pin;
  const active = rule.contact === "NC" ? "open" : "closed";
  const idle = rule.contact === "NC" ? "closed" : "open";
  if (rule.toggle_on_press)
    return pin + " (normally " + idle + ") · Press once to start, release, then press again to stop.";
  if (!rule.stop)
    return pin + " · Records while " + active + ". Stops when " + idle + ".";
  const start = pin + " · Starts when the contact " + (rule.contact === "NC" ? "opens" : "closes") + ". ";
  const end = rule.stop.type === "gpio"
    ? "GPIO" + rule.stop.pin + (rule.stop.contact === "NC" ? " opens" : " closes")
    : rule.stop.device + " receives “" + rule.stop.command + "”";
  return start + "Stops when " + end + (rule.stop_on_return ? " or the start contact returns to " + idle : "") + ".";
}

function ruleBehavior(rule) {
  if (rule.type === "gpio") return gpioBehavior(rule);
  if (rule.type === "timer") return "Records for " + rule.duration_s + " s every " + rule.interval_s + " s.";
  if (rule.type === "uart") return rule.device + " · “" + rule.start_command + "” starts recording. “" + rule.stop_command + "” stops it.";
  const above = rule.comparison === "above";
  const releases = above ? rule.threshold - rule.hysteresis : rule.threshold + rule.hysteresis;
  return rule.path + " · Starts at " + rule.threshold + (above ? " or above. Stops below " : " or below. Stops above ") + releases + ".";
}

function renderRules() {
  const rules = snapshot.config.triggers;
  const statuses = snapshot.triggers || [];
  const owners = snapshot.recording_sources || [];
  const key = JSON.stringify([rules, statuses, owners]);
  if (key === lastRuleList) return;
  lastRuleList = key;
  const list = byId("trigger-list");
  if (!rules.length) {
    list.replaceChildren(element("p", "Recording is manual. Add a rule to record from a button, timer, sensor or serial command.", "field-help"));
    return;
  }
  const focused = document.activeElement?.dataset.editRule;
  const states = {disabled: "Disabled", paused: "Paused", ready: "Ready", starting: "Starting", error: "Input error"};
  list.replaceChildren(...rules.map(rule => {
    const status = statuses.find(item => item.id === rule.id);
    const holding = owners.includes("trigger:" + rule.id);
    const state = status?.state || (rule.enabled === false ? "disabled" : "starting");
    const row = element("div", undefined, "rule-row");
    const info = element("div");
    info.append(element("strong", rule.id), element("p", holding ? "Holding recording" : states[state] || state, holding ? "rule-holding" : "subtle"),
      element("p", ruleBehavior(rule)));
    if (status?.error) info.append(element("p", status.error, "danger"));
    const actions = element("div", undefined, "rule-actions");
    if (holding) actions.append(button("Disable rule", () => {
      if (!window.confirm("Disable “" + rule.id + "” and release its recording request? Other recording sources keep their requests.")) return;
      act(async () => {
        const current = (await api("status")).config.triggers;
        if (JSON.stringify(rule) !== JSON.stringify(current.find(item => item.id === rule.id)))
          throw new Error("This rule changed on the device. Review its current settings and try again.");
        await api("config", {triggers: current.map(item => item.id === rule.id ? {...item, enabled: false} : item)});
      }, "Rule disabled.");
    }));
    const edit = button("Edit", () => editRule(rule));
    edit.dataset.editRule = rule.id;
    edit.setAttribute("aria-label", "Edit " + rule.id);
    actions.append(edit);
    row.append(info, actions);
    return row;
  }));
  if (focused) focusRule(focused);
}

function triggerField(name, title, value, options = {}) {
  const label = element("label", title);
  let input;
  if (options.choices) {
    input = element("select");
    options.choices.forEach(([key, text]) => input.add(new Option(text, key)));
  } else {
    input = element("input");
    input.type = options.text ? "text" : "number";
    input.required = true;
    if (!options.text) input.step = options.step || "1";
    if (options.min !== undefined) input.min = options.min;
    if (options.max !== undefined) input.max = options.max;
  }
  input.name = name;
  input.value = value;
  label.append(input);
  return label;
}

function gpioFields(rule) {
  const saved = rule.stop;
  const selector = triggerField("stop_type", "Recording behavior", rule.toggle_on_press ? "toggle" : saved?.type || "level", {choices: [
    ["level", "Hold to record"], ["toggle", "Press once to start, press again to stop"],
    ["gpio", "Stop with a second contact"], ["uart", "Stop with a serial command"],
  ]});
  selector.classList.add("full-field");
  const type = selector.querySelector("select");
  function field(kind, name, label, options) {
    const data = saved?.type === kind ? saved : gpioStopDefaults[kind];
    return triggerField("stop_" + name, label, data[name], options);
  }
  const gpio = [
    field("gpio", "pin", "Stop BCM GPIO pin", {min: 0, max: 27}),
    field("gpio", "contact", "Stop contact at rest", {choices: contactChoices}),
  ];
  const stopDebounce = field("gpio", "debounce_ms", "Stop debounce time (ms)", {min: 0, max: 1000});
  const timing = element("details", undefined, "rule-timing full-field");
  const timings = element("div", undefined, "field-pair");
  timings.append(triggerField("debounce_ms", "Start debounce time (ms)", rule.debounce_ms, {min: 0, max: 1000}), stopDebounce);
  timing.append(element("summary", "Contact timing"), timings);
  timing.open = rule.debounce_ms !== defaults.gpio.debounce_ms ||
    (saved?.type === "gpio" && saved.debounce_ms !== gpioStopDefaults.gpio.debounce_ms);
  const uart = [
    field("uart", "device", "Stop serial port", {text: true}),
    field("uart", "baud", "Stop baud rate", {min: 1}),
    field("uart", "command", "Stop command", {text: true}),
  ];
  const onReturn = element("input");
  onReturn.type = "checkbox";
  onReturn.name = "stop_on_return";
  onReturn.checked = rule.stop_on_return === true;
  const returnLabel = element("label", undefined, "check-line full-field");
  returnLabel.append(onReturn, "Also stop when the start contact returns to normal");
  function showSelection() {
    returnLabel.hidden = onReturn.disabled = !["gpio", "uart"].includes(type.value);
    for (const [kind, fields] of [["gpio", [...gpio, stopDebounce]], ["uart", uart]]) {
      for (const label of fields) {
        label.hidden = type.value !== kind;
        label.querySelector("input, select").disabled = label.hidden;
      }
    }
  }
  type.addEventListener("change", showSelection);
  showSelection();
  const hint = element("div", undefined, "field-help gpio-rule-help full-field");
  hint.id = "gpio-rule-help";
  return [selector, triggerField("pin", "Start BCM GPIO pin", rule.pin, {min: 0, max: 27}),
    triggerField("contact", "Start contact at rest", rule.contact, {choices: contactChoices}),
    ...gpio, ...uart, returnLabel, hint, timing];
}

function gpioRuleFromForm(fields) {
  const rule = {pin: fields.pin.valueAsNumber, contact: fields.contact.value, debounce_ms: fields.debounce_ms.valueAsNumber};
  const mode = fields.stop_type.value;
  if (mode === "toggle") rule.toggle_on_press = true;
  if (mode === "gpio" || mode === "uart") {
    rule.stop = {type: mode};
    for (const name of Object.keys(gpioStopDefaults[mode])) {
      const input = fields.namedItem("stop_" + name);
      rule.stop[name] = input.type === "number" ? input.valueAsNumber : input.value;
    }
    if (fields.stop_on_return.checked) rule.stop_on_return = true;
  }
  return rule;
}

function describeGpioRule() {
  const fields = byId("trigger-form").elements;
  if (fields.type.value !== "gpio") return;
  const rule = gpioRuleFromForm(fields);
  const physical = headerPins[rule.pin];
  const hint = byId("gpio-rule-help");
  if (!physical || (rule.stop?.type === "gpio" && !headerPins[rule.stop.pin])) {
    hint.textContent = "Enter a BCM GPIO number from 0 to 27.";
    return;
  }
  let wiring = "On the standard 40-pin header, wire the button between pin " + physical + " (GPIO" + rule.pin + ") and pin 9 (GND).";
  if (rule.stop?.type === "gpio")
    wiring += " Wire the stop contact between pin " + headerPins[rule.stop.pin] + " (GPIO" + rule.stop.pin + ") and pin 14 (GND).";
  const armed = rule.stop || rule.toggle_on_press
    ? " If pressed when enabled, release the button before the first press."
    : " If already " + (rule.contact === "NC" ? "open" : "closed") + " when enabled, this contact starts recording.";
  hint.replaceChildren(element("p", wiring), element("p", gpioBehavior(rule) + armed));
}

function validateRuleFields() {
  const fields = byId("trigger-form").elements;
  if (fields.type.value === "timer") {
    fields.duration_s.setCustomValidity(fields.duration_s.valueAsNumber >= fields.interval_s.valueAsNumber
      ? "Use a recording duration shorter than the repeat interval." : "");
  } else if (fields.type.value === "gpio") {
    fields.stop_pin.setCustomValidity(fields.stop_type.value === "gpio" && fields.stop_pin.valueAsNumber === fields.pin.valueAsNumber
      ? "Choose a different GPIO pin for the stop contact." : "");
    describeGpioRule();
  } else if (fields.type.value === "uart") {
    fields.stop_command.setCustomValidity(fields.stop_command.value === fields.start_command.value
      ? "Choose different serial commands for starting and stopping." : "");
  }
}

function ruleFields(type, data) {
  const field = (name, label, opts) => triggerField(name, label, data[name], opts);
  let fields;
  switch (type) {
    case "timer":
      fields = [field("interval_s", "Repeat interval (s)", {min: 0.1, step: "any"}), field("duration_s", "Recording duration (s)", {min: 0.1, step: "any"})];
      break;
    case "gpio":
      fields = gpioFields(data);
      break;
    case "sensor":
      fields = [field("path", "Sensor file (sysfs path)", {text: true}), field("comparison", "Start recording", {choices: [["above", "At or above the threshold"], ["below", "At or below the threshold"]]}), field("threshold", "Threshold (sensor units)", {step: "any"}), field("hysteresis", "Reset margin (hysteresis)", {min: 0, step: "any"}), field("poll_ms", "Read interval (ms)", {min: 20, max: 60000})];
      break;
    case "uart":
      fields = [field("device", "Serial port", {text: true}), field("baud", "Baud rate", {min: 1}), field("start_command", "Start command", {text: true}), field("stop_command", "Stop command", {text: true})];
      break;
  }
  byId("rule-fields").replaceChildren(...fields);
  validateRuleFields();
}

function editRule(rule = null) {
  editingRule = rule;
  const form = byId("trigger-form");
  form.hidden = false;
  byId("saved-rules").hidden = true;
  byId("remove-rule").hidden = !rule;
  setText("rule-error", "");
  if (rule) form.elements.id.value = rule.id;
  else {
    const ids = new Set(snapshot.config.triggers.map(item => item.id));
    let number = 1;
    while (ids.has("rule-" + number)) number++;
    form.elements.id.value = "rule-" + number;
  }
  form.elements.type.value = rule?.type || "gpio";
  form.elements.enabled.checked = rule?.enabled !== false;
  setText("rule-title", rule ? "Edit “" + rule.id + "”" : "New recording rule");
  ruleFields(form.elements.type.value, rule || defaults.gpio);
  form.elements.type.focus();
}

function focusRule(id) {
  const edit = id && byId("trigger-list").querySelector('[data-edit-rule="' + CSS.escape(id) + '"]');
  (edit || byId("add-trigger")).focus();
}

function closeRuleEditor(id) {
  byId("trigger-form").hidden = true;
  byId("saved-rules").hidden = false;
  editingRule = null;
  if (page === "triggers" && authenticated) focusRule(id);
}

function render() {
  renderMonitor();
  updateSensorDepthOptions();
  fillSettings("camera-form", snapshot.config.camera);
  renderModeTest();
  renderCameraSettings();
  fillSettings("storage-form", snapshot.config.storage);
  renderRoots();
  renderRules();
  network.update(snapshot.network);
  device.update(snapshot, capabilities);
}

byId("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button");
  submit.disabled = true;
  try {
    await api("login", {token: byId("access-token").value});
    byId("access-token").value = "";
    byId("login-form").hidden = true;
    byId("entry-help").hidden = true;
    setText("entry-heading", "Connecting to device…");
    notice("", false, "login-message");
    await refresh();
  } catch (error) {
    notice(error.message, true, "login-message");
  } finally {
    submit.disabled = false;
  }
});

byId("retry-connection").addEventListener("click", refresh);
byId("clock-set").addEventListener("click", () => act(
  () => api("clock", {browser_realtime_ms: Date.now(), confirm: true}), "Pi time updated from this browser."
));
byId("clock-dismiss").addEventListener("click", () => {
  clockDismissed = true;
  byId("clock-notice").hidden = true;
  byId("page-title").focus({preventScroll: true});
});
byId("logout").addEventListener("click", () => act(async () => {
  await api("logout", {});
  signInScreen();
  notice("", false, "login-message");
}));
byId("record").addEventListener("click", () => {
  if (snapshot.recording_sources.some(source => source.startsWith("trigger:"))) {
    act(() => api("triggers/pause", {paused: true, stop_manual: true}));
  } else {
    act(() => api("recording", {enabled: !snapshot.camera.recording && !snapshot.recording_sources.includes("manual")}));
  }
});
byId("trigger-pause").addEventListener("click", () => act(() => api("triggers/pause", {paused: true})));
byId("resume-triggers").addEventListener("click", () => act(() => api("triggers/pause", {paused: false})));
byId("manual-hold").addEventListener("click", () => act(() => api("recording", {enabled: !snapshot.recording_sources.includes("manual")})));
byId("preview").addEventListener("click", () => act(() => api("preview", {enabled: !snapshot.camera.preview})));
byId("mode-test-start").addEventListener("click", () => {
  const key = byId("mode-test-selection").value;
  const chosen = capabilities?.modes?.find(mode => modeKey(mode) === key);
  const body = {action: "start"};
  if (chosen) body.mode = {width: chosen.width, height: chosen.height, bit_depth: chosen.bit_depth};
  act(() => api("camera/mode-test", body));
});
byId("mode-test-cancel").addEventListener("click", () => act(() => api("camera/mode-test", {action: "cancel"})));
byId("fullscreen").addEventListener("click", () => byId("stage").requestFullscreen().catch(error => notice(error.message, true)));
byId("stream").addEventListener("error", () => {
  stopStream();
  nextStreamAttempt = Date.now() + 2000;
  updateStream();
});

for (const id of ["camera-form", "storage-form"]) {
  byId(id).addEventListener("input", () => {
    dirtyForms.add(id);
    if (id === "camera-form") renderCameraSettings();
    else if (snapshot) renderRoots();
  });
  byId(id).addEventListener("submit", event => {
    event.preventDefault();
    const fields = event.currentTarget.elements;
    if (id === "storage-form") {
      saveDestination(fields.directory.value);
      return;
    }
    const cameraDraft = () => ({
      width: fields.width.valueAsNumber,
      height: fields.height.valueAsNumber,
      fps: fields.fps.valueAsNumber,
      sensor_bit_depth: Number(fields.sensor_bit_depth.value),
      osd: fields.osd.checked,
    });
    const camera = cameraDraft();
    act(async () => {
      await api("config", {camera});
      const current = cameraDraft();
      if (Object.keys(camera).every(key => camera[key] === current[key])) dirtyForms.delete(id);
    }, "Video settings saved.");
  });
}

byId("add-trigger").addEventListener("click", () => editRule());
byId("cancel-rule").addEventListener("click", () => closeRuleEditor(editingRule?.id));
byId("remove-rule").addEventListener("click", () => {
  if (editingRule && window.confirm("Delete the recording rule “" + editingRule.id + "”?")) saveRuleChange(null);
});
byId("trigger-form").elements.type.addEventListener("change", event => {
  const type = event.target.value;
  ruleFields(type, editingRule?.type === type ? editingRule : defaults[type]);
});
byId("rule-fields").addEventListener("input", validateRuleFields);
byId("trigger-form").addEventListener("invalid", event => {
  const details = event.target.closest("details");
  if (details) details.open = true;
}, true);
byId("trigger-form").addEventListener("submit", event => {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  const type = fields.type.value;
  const rule = {id: fields.id.value, type, enabled: fields.enabled.checked};
  if (type === "gpio") Object.assign(rule, gpioRuleFromForm(fields));
  else for (const name of Object.keys(defaults[type])) {
    const input = fields.namedItem(name);
    rule[name] = input.type === "number" ? input.valueAsNumber : input.value;
  }
  saveRuleChange(rule);
});

async function saveRuleChange(rule) {
  if (actionPending) return;
  let saved = false;
  setText("rule-error", "");
  byId("rule-inputs").disabled = true;
  await act(async () => {
    try {
      const current = (await api("status")).config.triggers;
      if (editingRule && JSON.stringify(editingRule) !== JSON.stringify(current.find(item => item.id === editingRule.id)))
        throw new Error("This rule changed on the Pi. Cancel and reopen it to use its latest settings.");
      if (rule && current.some(item => item.id === rule.id && item.id !== editingRule?.id))
        throw new Error("A rule already uses this name. Choose another rule name.");
      let triggers;
      if (!rule) triggers = current.filter(item => item.id !== editingRule.id);
      else if (!editingRule) triggers = [...current, rule];
      else triggers = current.map(item => item.id === editingRule.id ? rule : item);
      await api("config", {triggers});
      saved = true;
      notice(rule ? "Rule saved." : "Rule deleted.");
    } catch (error) {
      if (authenticated) {
        setText("rule-error", error.message);
        byId("rule-error").focus();
      }
    }
  });
  byId("rule-inputs").disabled = false;
  if (saved) closeRuleEditor(rule?.id);
}
const navToggle = byId("nav-toggle");
navToggle.addEventListener("click", () => {
  const open = navToggle.getAttribute("aria-expanded") === "true";
  navToggle.setAttribute("aria-expanded", String(!open));
});
function closeMenuOnPage(heading = byId("page-title")) {
  navToggle.setAttribute("aria-expanded", "false");
  heading.focus({preventScroll: true});
  window.scrollTo(0, 0);
}
byId("skip-navigation").addEventListener("click", event => {
  event.preventDefault();
  closeMenuOnPage();
});
byId("dismiss-feedback").addEventListener("click", () => {
  notice();
  byId("page-title").focus({preventScroll: true});
});
byId("main-nav").addEventListener("click", event => {
  const link = event.target.closest("a");
  if (link?.hash === window.location.hash) closeMenuOnPage();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && navToggle.getAttribute("aria-expanded") === "true" && navToggle.offsetWidth) {
    navToggle.setAttribute("aria-expanded", "false");
    navToggle.focus();
  }
});
window.addEventListener("hashchange", () => {
  if (authenticated) {
    const previous = page;
    navigate();
    const heading = previous === "camera" && page === "camera"
      ? document.querySelector("[data-camera-view]:not([hidden]) h2") : null;
    closeMenuOnPage(heading || byId("page-title"));
  }
});
document.addEventListener("visibilitychange", () => {
  updateStream();
  if (authenticated && !document.hidden) refresh();
});
refresh();
