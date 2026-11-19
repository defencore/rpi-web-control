import {byId, setText, element, button, capacity} from "./ui.js";
import {setupNetworkPanel} from "./network.js";
import {setupDevicePanels, renderSensorProperties} from "./device.js";

const pages = {monitor: "Monitor", recordings: "Recordings", live: "Live stream", camera: "Camera", storage: "Storage", triggers: "Triggers", network: "Network"};
const ruleNames = {timer: "Timer", gpio: "GPIO contact", sensor: "Sensor threshold", uart: "UART command"};
const defaults = {
  timer: {interval_s: 60, duration_s: 5},
  gpio: {pin: 17, contact: "NO", debounce_ms: 30},
  sensor: {path: "/sys/class/thermal/thermal_zone0/temp", comparison: "above", threshold: 60000, hysteresis: 2000, poll_ms: 250},
  uart: {device: "/dev/serial0", baud: 115200, start_command: "START", stop_command: "STOP"},
};
const integerFormat = new Intl.NumberFormat("en-US");
const fpsFormat = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});
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
let actionPending = false;
let editingRule = null;
let lastRuleList = null;
let lastRoots = null;
let nextStreamAttempt = 0;

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
  network.reset();
  notice(wasSignedIn ? "Sign in to continue." : "", false, "login-message");
  byId("access-token").focus();
}

function connection(online) {
  setText("connection-status", online ? "Connected" : "Reconnecting");
  byId("connection-lamp").className = "lamp " + (online ? "good" : "bad");
  byId("offline-message").hidden = online;
  if (!online) byId("clock-notice").hidden = true;
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
  const controls = [...document.querySelectorAll("#workspace button:not([disabled])")];
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
  page = Object.hasOwn(pages, hash) ? hash : "monitor";
  byId("nav-toggle").setAttribute("aria-expanded", "false");
  for (const [id, name] of Object.entries(pages)) {
    byId("page-" + id).hidden = id !== page;
    const link = document.querySelector('[data-page="' + id + '"]');
    if (id === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
    if (id === page) {
      setText("page-title", name);
      document.title = name + " · Capture Control";
    }
  }
  notice();
  updateStream();
  if (authenticated) device.open(page);
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
  const viewing = authenticated && enabled && page === "monitor" && !document.hidden;
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
    setText("stage-title", !available ? "Camera unavailable" : enabled ? "Waiting for frames" : "Preview is off");
    setText("stage-help", !available ? (capabilities.error || "Check the camera connection.") : enabled ? "The preview will resume when frames arrive." : "Enable preview to see the camera feed.");
  }
}

function renderMonitor() {
  if (!snapshot) return;
  const cam = snapshot.camera;
  const settings = cam.settings || snapshot.config.camera;
  const owners = snapshot.recording_sources || [];
  const manual = owners.includes("manual");
  const record = byId("record");
  const camAvailable = capabilities?.available !== false;
  record.disabled = actionPending || (!camAvailable && !manual);
  record.classList.toggle("active", manual);
  record.querySelector("use").setAttribute("href", manual ? "#i-stop" : "#i-record");
  record.querySelector("span").textContent = manual ? (owners.length > 1 ? "Release manual hold" : "Stop recording") : cam.recording ? "Hold recording" : "Start recording";
  record.setAttribute("aria-pressed", String(manual));
  setText("device-name", capabilities?.model || (cam.backend === "simulated" ? "Simulated camera" : "Capture device"));
  const preview = byId("preview");
  preview.disabled = actionPending || cam.mode_test?.state === "running" || (!camAvailable && !cam.preview);
  preview.setAttribute("aria-pressed", String(cam.preview));
  preview.querySelector("span").textContent = cam.preview ? "Disable preview" : "Enable preview";
  setText("stream-state", cam.preview ? "Enabled" : "Off");
  byId("stream-state").classList.toggle("live", cam.preview);
  setText("actual-fps", cam.active && Number.isFinite(cam.actual_fps) ? fpsFormat.format(cam.actual_fps) : "—");
  setText("target-fps", fpsFormat.format(settings.fps) + " fps");
  const mode = cam.sensor_mode;
  setText("active-sensor-mode", mode ? mode.width + " × " + mode.height + " · " + mode.bit_depth + "-bit" : "—");
  setText("resolution", settings.width + " × " + settings.height);
  setText("osd-status", cam.osd_effective ? "Enabled" : "Off");
  setText("frame-count", Number.isFinite(cam.frames) ? integerFormat.format(cam.frames) : "—");
  setText("dropped-count", Number.isFinite(cam.dropped_frames) ? integerFormat.format(cam.dropped_frames) : "—");
  setText("record-state", cam.recording ? "Recording" : owners.length ? "Waiting to resume" : "Idle");
  byId("record-state").classList.toggle("recording", cam.recording);
  byId("record-state").classList.toggle("waiting", !cam.recording && owners.length > 0);
  const volume = snapshot.storage;
  const hasCapacity = Number.isFinite(volume?.free_bytes) && volume.total_bytes > 0;
  setText("monitor-space", hasCapacity ? capacity(volume.free_bytes) + " free" : "Capacity unavailable");
  const usage = byId("disk-usage");
  usage.hidden = !hasCapacity;
  if (hasCapacity) usage.value = (1 - volume.free_bytes / volume.total_bytes) * 100;
  setText("record-location", cam.recording && cam.session ? cam.session : snapshot.config.storage.directory);
  setText("record-sources", owners.length ? "Held by " + owners.join(", ") : "");
  setText("camera-error", cam.last_error || snapshot.storage?.error || capabilities?.error || "");
  const rules = snapshot.config.triggers;
  const active = (snapshot.triggers || []).filter(rule => rule.active).length;
  const errors = (snapshot.triggers || []).filter(rule => rule.state === "error").length;
  setText("trigger-count", rules.length + (rules.length === 1 ? " rule" : " rules"));
  setText("trigger-summary", rules.length ? [rules.filter(rule => rule.enabled !== false).length + " enabled", active + " active", ...(errors ? [errors + " in error"] : [])].join(" · ") : "Manual recording only.");
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

function describeFormat() {
  const fields = byId("camera-form").elements;
  const fps = fields.fps.valueAsNumber;
  const raw = fps > 60;
  const bytes = fields.width.valueAsNumber * fields.height.valueAsNumber * 1.5 * fps;
  const bandwidth = raw && bytes > 0 && Number.isFinite(bytes)
    ? " Frame data requires " + fpsFormat.format(bytes / (1024 ** 2)) + " MiB/s." : "";
  setText("capture-format", raw ? "Raw YUV420 recording. Timestamp overlay is disabled above 60 fps." + bandwidth
    : "Motion JPEG recording with a separate timestamp for each saved frame.");
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
  setText("mode-help", modes.length ? "Select a mode to fill in its resolution, sensor bit depth and maximum frame rate. Adjust the target rate if needed, then save." : capabilities?.error || "No sensor modes reported.");
  byId("modes").replaceChildren(...modes.map(mode => {
    const rate = Math.floor(mode.max_fps * 100) / 100;
    const label = mode.width + " × " + mode.height + " · " + fpsFormat.format(rate) + " fps" +
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
      describeFormat();
      form.elements.fps.focus();
    });
  }));
}

function renderModeTest() {
  const cam = snapshot.camera;
  const test = cam.mode_test;
  const running = test?.state === "running";
  const inUse = cam.preview || cam.live?.enabled || cam.recording || snapshot.recording_sources.length > 0;
  byId("mode-test-start").hidden = running;
  byId("mode-test-start").disabled = actionPending || inUse || !test;
  byId("mode-test-cancel").hidden = !running;
  byId("mode-test-cancel").disabled = actionPending;
  byId("mode-test-selection").disabled = actionPending || running;
  byId("camera-form").querySelector('[type="submit"]').disabled = actionPending || running || cam.recording || cam.live?.enabled;
  setText("mode-test-lock", inUse && !running ? "Stop preview, streaming and any recording sources before running a test." : "");
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
    ? "Unsaved directory. Recordings still use " + directory + "." : "");
  const roots = snapshot.storage_roots || [];
  if (JSON.stringify(roots) === lastRoots) return;
  lastRoots = JSON.stringify(roots);
  byId("storage-options").replaceChildren(...roots.map(root => new Option(root, root)));
  byId("storage-roots").replaceChildren(...roots.map(root => element("li", root)));
}

function renderRules() {
  const rules = snapshot.config.triggers;
  const statuses = snapshot.triggers || [];
  const key = JSON.stringify([rules, statuses]);
  if (key === lastRuleList) return;
  lastRuleList = key;
  const list = byId("trigger-list");
  if (!rules.length) {
    list.replaceChildren(element("p", "No rules configured. Add a rule to start recording from a hardware input or timer.", "field-help"));
    return;
  }
  list.replaceChildren(...rules.map(rule => {
    const status = statuses.find(item => item.id === rule.id);
    const state = status?.active ? "active" : status?.state || (rule.enabled === false ? "disabled" : "starting");
    const row = element("div", undefined, "rule-row");
    const info = element("div");
    info.append(element("strong", rule.id), element("p", ruleNames[rule.type] + " · " + state, "subtle"));
    if (status?.error) info.append(element("p", status.error, "danger"));
    const actions = element("div", undefined, "rule-actions");
    actions.append(button("Edit", () => editRule(rule)), button("Remove", () => {
      if (!window.confirm('Remove the recording rule "' + rule.id + '"?')) return;
      act(async () => {
        await api("config", {triggers: snapshot.config.triggers.filter(item => item.id !== rule.id)});
        if (editingRule?.id === rule.id) byId("trigger-form").hidden = true;
      }, "Rule removed.");
    }, "button secondary small danger"));
    row.append(info, actions);
    return row;
  }));
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

function ruleFields(type, data) {
  const field = (name, label, opts) => triggerField(name, label, data[name], opts);
  let fields;
  switch (type) {
    case "timer":
      fields = [field("interval_s", "Repeat interval (s)", {min: 0.1, step: "any"}), field("duration_s", "Recording duration (s)", {min: 0.1, step: "any"})];
      break;
    case "gpio":
      fields = [field("pin", "BCM GPIO pin", {min: 0, max: 27}), field("contact", "Contact type", {choices: [["NO", "Normally open"], ["NC", "Normally closed"]]}), field("debounce_ms", "Debounce time (ms)", {min: 0, max: 1000})];
      break;
    case "sensor":
      fields = [field("path", "Sensor sysfs path", {text: true}), field("comparison", "Record when", {choices: [["above", "Above threshold"], ["below", "Below threshold"]]}), field("threshold", "Threshold (sensor units)", {step: "any"}), field("hysteresis", "Hysteresis (sensor units)", {min: 0, step: "any"}), field("poll_ms", "Poll interval (ms)", {min: 20, max: 60000})];
      break;
    case "uart":
      fields = [field("device", "UART device", {text: true}), field("baud", "Baud rate", {min: 1}), field("start_command", "Start command", {text: true}), field("stop_command", "Stop command", {text: true})];
      break;
  }
  byId("rule-fields").replaceChildren(...fields);
}

function editRule(rule = null) {
  editingRule = rule;
  const form = byId("trigger-form");
  form.hidden = false;
  form.elements.id.value = rule?.id || "";
  form.elements.type.value = rule?.type || "timer";
  form.elements.enabled.checked = rule?.enabled !== false;
  setText("rule-title", rule ? "Edit rule" : "New rule");
  ruleFields(form.elements.type.value, rule || defaults.timer);
  form.elements.id.focus();
}

function render() {
  renderMonitor();
  updateSensorDepthOptions();
  fillSettings("camera-form", snapshot.config.camera);
  renderModeTest();
  describeFormat();
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
byId("record").addEventListener("click", () => act(() => api("recording", {enabled: !snapshot.recording_sources.includes("manual")})));
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
    if (id === "camera-form") describeFormat();
    else if (snapshot) renderRoots();
  });
  byId(id).addEventListener("submit", event => {
    event.preventDefault();
    const fields = event.currentTarget.elements;
    if (id === "storage-form") {
      saveDestination(fields.directory.value);
      return;
    }
    const payload = {camera: {
      width: fields.width.valueAsNumber,
      height: fields.height.valueAsNumber,
      fps: fields.fps.valueAsNumber,
      sensor_bit_depth: Number(fields.sensor_bit_depth.value),
      osd: fields.osd.checked,
    }};
    act(async () => {
      await api("config", payload);
      dirtyForms.delete(id);
    }, "Camera settings saved.");
  });
}

byId("add-trigger").addEventListener("click", () => editRule());
byId("cancel-rule").addEventListener("click", () => {
  byId("trigger-form").hidden = true;
  byId("add-trigger").focus();
});
byId("trigger-form").elements.type.addEventListener("change", event => {
  const type = event.target.value;
  ruleFields(type, editingRule?.type === type ? editingRule : defaults[type]);
});
byId("trigger-form").addEventListener("submit", event => {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  const type = fields.type.value;
  const rule = {id: fields.id.value, type, enabled: fields.enabled.checked};
  for (const name of Object.keys(defaults[type])) {
    const input = fields.namedItem(name);
    rule[name] = input.type === "number" ? input.valueAsNumber : input.value;
  }
  const existing = snapshot.config.triggers;
  if (existing.some(item => item.id === rule.id && item.id !== editingRule?.id)) {
    notice("Choose a unique rule ID.", true);
    return;
  }
  if (editingRule && JSON.stringify(editingRule) !== JSON.stringify(existing.find(item => item.id === editingRule.id))) {
    notice("This rule changed on the device. Reopen it to edit the latest settings.", true);
    return;
  }
  const triggers = editingRule ? existing.map(item => item.id === editingRule.id ? rule : item) : [...existing, rule];
  act(async () => {
    await api("config", {triggers});
    byId("trigger-form").hidden = true;
    editingRule = null;
  }, "Rule saved.");
});
const navToggle = byId("nav-toggle");
navToggle.addEventListener("click", () => {
  const open = navToggle.getAttribute("aria-expanded") === "true";
  navToggle.setAttribute("aria-expanded", String(!open));
});
function closeMenuOnPage() {
  navToggle.setAttribute("aria-expanded", "false");
  byId("page-title").focus({preventScroll: true});
  window.scrollTo(0, 0);
}
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
    navigate();
    closeMenuOnPage();
  }
});
document.addEventListener("visibilitychange", () => {
  updateStream();
  if (authenticated && !document.hidden) refresh();
});
refresh();
