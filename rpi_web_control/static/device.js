import {byId, setText, element, button, fact, capacity, copyCommand} from "./ui.js";
import {setupStoragePanels} from "./storage.js";

const numeric = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});

function pair(values, unit) {
  if (!Array.isArray(values) || values.length !== 2) return null;
  return values.join(" × ") + " " + unit;
}

export function renderSensorProperties(capabilities) {
  const props = capabilities.properties || {};
  const fields = [
    ["Model", capabilities.available ? capabilities.model : "Unavailable"],
    ["Pixel array", pair(props.pixel_array_size, "px")],
    ["Pixel pitch", pair(props.unit_cell_size_nm, "nm")],
    ["Mount rotation", Number.isFinite(props.rotation_degrees) ? props.rotation_degrees + "°" : null],
    ["Location", props.location],
  ];
  byId("sensor-properties").replaceChildren(...fields.filter(([, value]) => value != null)
    .map(([name, value]) => fact(name, value)));
}

function receiverFamily(host) {
  if (!host) return 4;
  try {
    if (/^[\d.]+$/.test(host) && new URL("http://" + host).hostname === host) return 4;
    const ipv6 = /^([a-f\d:.]+)(?:%[\w.-]+)?$/i.exec(host);
    if (ipv6) {
      const canonical = new URL("http://[" + ipv6[1] + "]").hostname;
      return canonical.startsWith("[::ffff:") ? 4 : 6;
    }
  } catch { /* Invalid numeric address. */ }
  return null;
}

function receiverCommand(family, port) {
  const bind = family === 6 ? " address=::" : "";
  return "gst-launch-1.0 -v udpsrc" + bind + " port=" + port +
    ' buffer-size=1048576 caps="application/x-rtp,media=video,encoding-name=JPEG,clock-rate=90000,payload=26"' +
    " ! rtpjpegdepay ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream" +
    " ! jpegdec ! autovideosink sync=false";
}

function livePanel({api, act, notice, busy}) {
  let current = null;
  let browserHost = "";
  const hostInput = byId("live-host");
  const portInput = byId("live-port");
  const fpsInput = byId("live-max-fps");
  const command = byId("receiver-command");
  function updateCommand() {
    const active = current?.enabled;
    const family = receiverFamily(active ? current.host : hostInput.value.trim() || browserHost);
    const port = active ? current.port : portInput.valueAsNumber;
    const error = !family ? "Enter a valid IPv4 or IPv6 receiver address." : "";
    hostInput.setCustomValidity(error);
    const validPort = active || portInput.validity.valid;
    setText("receiver-command", error || (validPort ? receiverCommand(family, port) : "Enter a valid UDP port."));
    byId("copy-command").disabled = !family || !validPort || busy();
  }

  hostInput.addEventListener("input", updateCommand);
  portInput.addEventListener("input", updateCommand);
  byId("live-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!current) return;
    const body = current.enabled ? {enabled: false} : {enabled: true, port: portInput.valueAsNumber};
    if (body.enabled && hostInput.value.trim()) body.host = hostInput.value.trim();
    if (body.enabled && fpsInput.value !== "") body.max_fps = fpsInput.valueAsNumber;
    act(() => api("live", body));
  });
  byId("copy-command").addEventListener("click", () => copyCommand(command, notice));

  const url = new URL(window.location.href);
  setText("console-url", url.protocol + "//" + url.hostname + ":" + (url.port || (url.protocol === "https:" ? "443" : "80")));
  if (["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) {
    setText("loopback-help", "This is a local console address. On another computer, use the device’s LAN address and HTTPS port.");
  }

  return function update(snapshot, caps) {
    const cam = snapshot.camera;
    const wasActive = Boolean(current?.enabled);
    current = cam.live;
    const active = Boolean(current?.enabled);
    if (active !== wasActive) byId("live-player").open = !active;
    const mode = cam.settings || snapshot.config.camera;
    const supportedSize = [mode.width, mode.height].every(n => n <= 2040 && n % 8 === 0);
    browserHost = snapshot.client_address || "";
    hostInput.placeholder = browserHost || "Automatic";
    hostInput.disabled = active;
    portInput.disabled = active;
    fpsInput.disabled = active;
    byId("live-receiver-fields").hidden = active;
    byId("live-destination").hidden = !active;
    byId("live-active-limit").hidden = !active;
    if (active) {
      const host = current.host.includes(":") ? "[" + current.host + "]" : current.host;
      setText("live-endpoint", host + ":" + current.port);
      setText("live-active-max-fps", current.max_fps ? numeric.format(current.max_fps) + " fps" : "Camera rate");
    }
    setText("live-mode", mode.width + " × " + mode.height + " at " + numeric.format(mode.fps) + " fps");
    setText("live-state", active ? "Sending" : "Off");
    byId("live-state").classList.toggle("live", active);
    let limit = "";
    if (!current) limit = "The service has no live sender available.";
    else if (caps?.available === false) limit = caps.error || "No camera is available.";
    else if (cam.mode_test?.state === "running") limit = "Finish the sensor mode test before starting the live stream.";
    else if (!supportedSize) limit = "For RTP/JPEG, choose dimensions divisible by 8, up to 2040 × 2040.";
    setText("live-limit", limit);
    const toggle = byId("live-toggle");
    toggle.disabled = busy() || (!active && (!current || !supportedSize || caps?.available === false || cam.mode_test?.state === "running"));
    toggle.textContent = active ? "Stop live stream" : "Start live stream";
    toggle.setAttribute("aria-pressed", String(active));
    for (const [id, value, unit] of [
      ["live-capture-fps", cam.actual_fps, " fps"], ["live-fps", current?.sent_fps, " fps"],
      ["live-bitrate", current?.bitrate_mbps, " Mbit/s"], ["live-drops", current?.dropped_frames, ""],
      ["live-frames", current?.sent_frames, ""], ["live-queue-drops", current?.queue_drops, ""],
      ["live-congestion-drops", current?.congestion_drops, ""], ["live-retries", current?.send_retries, ""],
      ["live-rate-limited", current?.rate_limited_frames, ""],
    ]) {
      setText(id, Number.isFinite(value) ? numeric.format(value) + unit : "—");
    }
    setText("live-error", current?.last_error || "");
    byId("live-statistics").hidden = !current?.host;
    byId("live-rates").hidden = !active;
    setText("live-stats-title", active ? "Stream performance" : "Last stream");
    let pressure = "";
    if (active && current.congestion_drops > 0) {
      pressure = "The network was busy and the Pi dropped frames. If sender drops keep rising, stop the stream and lower its FPS limit.";
    } else if (active && current.queue_drops > 0) {
      pressure = "The sender replaced queued frames to keep up with capture. If sender drops keep rising, try a lower stream FPS limit.";
    }
    setText("live-pressure", pressure);
    updateCommand();
  };
}

function diskCard(disk) {
  const card = element("article", undefined, "disk");
  const head = element("div", undefined, "disk-head");
  head.append(element("h3", [disk.kind, disk.label].filter(Boolean).join(" · ") || "Storage device"));
  if (disk.system) head.append(element("span", "System volume", "tag"));
  if (disk.raw) head.append(element("span", "Raw flash", "tag"));
  card.append(head);
  const volume = element("dl", undefined, "fact-grid");
  volume.append(fact("Device", disk.device || "—"), fact("Mount point", disk.mountpoint || "Unmounted"));
  if (disk.filesystem) volume.append(fact("Filesystem", disk.filesystem));
  if (Number.isFinite(disk.total_bytes)) {
    const space = Number.isFinite(disk.free_bytes) ? capacity(disk.free_bytes) + " free of " : "";
    volume.append(fact("Capacity", space + capacity(disk.total_bytes)));
  }
  card.append(volume);
  if (disk.read_only || disk.raw || !disk.mountpoint || !disk.roots?.length) {
    const reason = disk.raw ? "Recording requires a mounted writable filesystem."
      : disk.read_only ? "Mounted read-only."
      : !disk.mountpoint ? "Mount this device to use it for recordings."
      : "No allowed recording root on this mount.";
    card.append(element("p", reason, "field-help"));
  }
  return card;
}

function inventoryPanel({api, chooseDestination, busy}) {
  let ticket = 0;
  let status = null;
  const managedPaths = {nvme: null, usb: null};
  const choices = byId("storage-choices");
  function update(snapshot) {
    status = snapshot;
    for (const choice of choices.querySelectorAll("button")) {
      const path = choice.dataset.directory;
      choice.parentElement.hidden = path === managedPaths.nvme || path === managedPaths.usb;
      const active = path === snapshot?.config.storage.directory && !snapshot.storage?.error;
      choice.disabled = !snapshot || active || busy() || snapshot.camera.recording || snapshot.recording_sources.length > 0;
    }
  }

  function showChoices(devices) {
    const paths = new Set();
    choices.replaceChildren();
    for (const disk of devices) {
      if (disk.read_only || disk.raw || !disk.mountpoint) continue;
      for (const path of disk.roots || []) {
        if (paths.has(path)) continue;
        paths.add(path);
        const label = disk.system ? "system drive" : (disk.kind || "volume");
        const row = element("div", undefined, "destination-row");
        const choice = button("Record to " + label, () => chooseDestination(path));
        choice.dataset.directory = path;
        row.append(choice, element("span", path, "file-path"));
        choices.append(row);
      }
    }
    update(status);
  }

  async function load() {
    const request = ++ticket;
    const refresh = byId("storage-refresh");
    refresh.disabled = true;
    setText("device-storage-message", "Discovering devices…");
    try {
      const result = await api("storage");
      if (request !== ticket) return;
      const devices = result.devices || [];
      byId("disk-list").replaceChildren(...devices.map(diskCard));
      showChoices(devices);
      setText("device-storage-message", result.message || (devices.length ? "" : "No storage devices reported."));
    } catch (error) {
      if (request !== ticket) return;
      byId("disk-list").replaceChildren();
      choices.replaceChildren();
      setText("device-storage-message", error.message);
    } finally {
      if (request === ticket) refresh.disabled = false;
    }
  }
  byId("storage-refresh").addEventListener("click", load);
  return {
    load, update,
    managed(medium, path) {
      managedPaths[medium] = path;
      update(status);
    },
    clear() { ticket++; status = null; managedPaths.nvme = managedPaths.usb = null; choices.replaceChildren(); byId("disk-list").replaceChildren(); },
  };
}

function bootPanel({api, act, notice, busy}) {
  let latest = null;
  let draft = null;
  let ticket = 0;
  let activity = false;
  const profile = byId("boot-profile");
  const port = byId("boot-port");
  const form = byId("boot-form");
  const byLabel = new Intl.Collator("en", {numeric: true, sensitivity: "base"});

  function controls() {
    const stale = draft && draft.revision !== latest?.revision;
    const blocked = busy() || !latest?.available || !latest.revision;
    const conflict = latest?.conflicts?.length > 0;
    profile.disabled = blocked || conflict;
    port.disabled = profile.disabled || !profile.value || profile.value === "auto";
    const selected = latest?.profiles?.find(item => item.id === profile.value);
    const directive = selected?.directives?.[port.value] || "";
    byId("boot-preview").hidden = !directive || conflict;
    setText("boot-directive", directive);
    byId("boot-apply").disabled = blocked || conflict || stale || !profile.value || profile.selectedOptions[0]?.disabled;
    byId("boot-restore").disabled = blocked || !latest?.backup_available;
    byId("boot-reboot").disabled = blocked || activity;
    const issues = [...(latest?.conflicts || [])];
    if (stale) issues.push("The boot file changed after you began editing. Reload before staging.");
    if (activity) issues.push("Stop all active recording sources before rebooting.");
    setText("boot-conflicts", issues.join(" "));
  }

  function show(data, discard = false) {
    if (discard) draft = null;
    latest = data;
    const profiles = (data?.profiles || []).slice().sort((a, b) => byLabel.compare(a.label, b.label));
    const installed = document.createElement("optgroup");
    const missing = document.createElement("optgroup");
    installed.label = "Installed overlays";
    missing.label = "Missing overlays";
    profile.replaceChildren();
    for (const item of profiles) {
      const option = new Option(item.label + (item.installed === false ? " (overlay missing)" : ""), item.id);
      option.disabled = item.installed === false;
      if (item.id === "auto") profile.append(option);
      else if (option.disabled) missing.append(option);
      else installed.append(option);
    }
    if (installed.childElementCount) profile.append(installed);
    if (missing.childElementCount) profile.append(missing);
    if (data?.selection_required_port) {
      const placeholder = new Option("Choose a profile", "");
      placeholder.disabled = true;
      profile.prepend(placeholder);
    }
    if (!draft) {
      profile.value = data?.selection_required_port ? "" : data?.profile?.id || "auto";
      port.value = data?.selection_required_port || (data?.profile?.id !== "auto" && data?.profile?.port) || "cam0";
    } else {
      profile.value = draft.profile;
      port.value = draft.port;
    }
    const configured = profiles.find(p => p.id === data?.profile?.id);
    const connector = data?.profile?.id !== "auto" && data?.profile?.port;
    const current = configured ? configured.label + (connector ? " · " + connector.toUpperCase() : "") : "Unspecified";
    const sharedPort = data?.selection_required_port?.toUpperCase();
    setText("boot-current", sharedPort ? "Multiple overlays on " + sharedPort : current);
    setText("boot-state", !data?.available ? "Unavailable" : data.pending_reboot ? "Reboot pending" : "No pending reboot");
    const guidance = sharedPort ? "Choose one profile to replace the camera overlays on " + sharedPort + "." : data?.message;
    const message = [data?.simulated ? "Simulated boot configuration." : "", guidance].filter(Boolean).join(" ");
    setText("boot-message", message);
    setText("boot-path", data?.path || "");
    setText("boot-file", data?.config_text ?? "Configuration text unavailable.");
    controls();
  }

  async function load(discard = false) {
    const request = ++ticket;
    try {
      const result = await api("firmware");
      if (request === ticket) show(result, discard);
    } catch (error) {
      if (request === ticket) show({available: false, message: error.message});
    }
  }

  async function stage(selected) {
    const revision = selected === "restore" ? latest.revision : draft?.revision || latest.revision;
    const body = {profile: selected, port: port.value, revision};
    ticket++;
    try {
      const result = await api("firmware", body);
      show(result, true);
    } catch (error) {
      await load();
      throw error;
    }
  }

  [profile, port].forEach(input => input.addEventListener("change", () => {
    if (latest) draft = {revision: draft?.revision || latest.revision, profile: profile.value, port: port.value};
    controls();
  }));
  byId("firmware-reload").addEventListener("click", () => {
    if (draft && !window.confirm("Discard the unsaved boot profile selection and reload the boot file?")) return;
    load(true);
  });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (byId("boot-apply").disabled) return;
    act(() => stage(profile.value), "Camera boot profile staged.");
  });
  byId("boot-restore").addEventListener("click", () => {
    if (!window.confirm("Restore the saved boot configuration backup? It will replace the current boot file.")) return;
    act(() => stage("restore"), "Boot configuration backup restored.");
  });
  byId("boot-reboot").addEventListener("click", () => {
    const simulation = latest?.simulated;
    const prompt = simulation ? "Simulate a device reboot and apply staged boot settings?" :
      "Reboot this Raspberry Pi now? The control service will disconnect and the device will reload its boot configuration.";
    if (!window.confirm(prompt)) return;
    act(async () => {
      const result = await api("reboot", {confirm: true, revision: latest.revision});
      if (result.simulated) await load(true);
      notice(result.simulated ? "Simulated reboot completed." : "Reboot scheduled. Reconnect when the device returns.");
    });
  });
  return {
    load,
    activity(value) { activity = Boolean(value); controls(); },
    clear() { ticket++; draft = null; show(null); },
  };
}

export function setupDevicePanels(options) {
  const live = livePanel(options);
  const inventory = inventoryPanel(options);
  const boot = bootPanel(options);
  const storage = setupStoragePanels(options, inventory);
  return {
    open(page) {
      storage.open(page);
      if (page === "camera") boot.load();
    },
    update(snapshot, capabilities) {
      live(snapshot, capabilities);
      inventory.update(snapshot);
      storage.update(snapshot);
      boot.activity(snapshot.camera.recording || snapshot.recording_sources?.length);
    },
    reset() {
      boot.clear();
      inventory.clear();
      storage.clear();
      byId("live-host").value = "";
      byId("live-port").value = "5004";
      byId("live-max-fps").value = "";
    },
  };
}
