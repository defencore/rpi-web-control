import {byId, setText, element, button, capacity, destinationLabel, fact, copyCommand, isRecordingId} from "./ui.js";

function cleanupDialog({api, act, busy, notice}, reload) {
  const dialog = byId("cleanup-dialog");
  const form = byId("cleanup-form");
  const input = byId("cleanup-confirmation");
  const submit = byId("cleanup-delete");
  const cancel = byId("cleanup-cancel");
  let revision = 0;
  let reviewed = null;
  let submitting = false;

  function controls() {
    submit.disabled = !reviewed || submitting || busy() || input.value !== reviewed.confirmation;
    cancel.disabled = submitting;
    input.disabled = submitting;
  }

  function dismiss() {
    revision++;
    reviewed = null;
    if (dialog.open) dialog.close();
  }

  function explain(message) {
    reviewed = null;
    form.hidden = true;
    setText("cleanup-message", message);
    controls();
  }

  async function review(selector) {
    if (dialog.open || busy() || submitting) return;
    const request = ++revision;
    input.value = "";
    byId("cleanup-details").hidden = true;
    setText("cleanup-summary", "");
    setText("cleanup-title", selector.session ? "Delete this recording?" : "Delete all eligible recordings in this folder?");
    explain("Checking the drive and its recordings…");
    dialog.showModal();
    try {
      const plan = await api("recordings/cleanup", {...selector, action: "plan"});
      if (request !== revision || !dialog.open) return;
      if (!["nvme", "usb"].includes(plan.medium) || plan.target_id !== selector.target_id ||
          !plan.revision || !plan.confirmation || !Number.isSafeInteger(plan.count) || plan.count < 0) {
        throw new Error("The drive or recording selection could not be verified. Refresh the list and review it again.");
      }
      setText("cleanup-target", (plan.medium === "usb" ? "USB" : "NVMe") + " · " + plan.device);
      setText("cleanup-directory", plan.directory);
      setText("cleanup-scope", selector.session || "All eligible completed recordings in this folder");
      byId("cleanup-details").hidden = false;
      const count = plan.count.toLocaleString("en-US") + (plan.count === 1 ? " recording" : " recordings");
      const size = capacity(plan.bytes) + " (" + plan.bytes.toLocaleString("en-US") + " bytes)";
      setText("cleanup-summary", count + " · " + size + (plan.skipped ? " · " + plan.skipped + " skipped" : ""));
      if (!plan.count) {
        explain("No eligible recordings to delete.");
        return;
      }
      reviewed = {...selector, revision: plan.revision, confirmation: plan.confirmation};
      setText("cleanup-message", "Deletion is permanent.");
      setText("cleanup-challenge", "Type " + plan.confirmation + " to confirm");
      submit.textContent = selector.session ? "Delete recording" : "Delete " + count;
      form.hidden = false;
      controls();
    } catch (error) {
      if (request === revision && dialog.open) explain(error.message);
    }
  }

  input.addEventListener("input", controls);
  cancel.addEventListener("click", dismiss);
  dialog.addEventListener("cancel", event => {
    if (submitting) event.preventDefault();
    else dismiss();
  });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (!reviewed || submitting || busy() || input.value !== reviewed.confirmation) return;
    const request = revision;
    const body = {...reviewed, action: "delete", confirmation: input.value};
    act(async () => {
      submitting = true;
      reviewed = null;
      controls();
      setText("cleanup-message", "Deleting the reviewed recordings…");
      try {
        const result = await api("recordings/cleanup", body);
        if (request !== revision) return;
        dismiss();
        const count = result.removed_count + (result.removed_count === 1 ? " recording" : " recordings");
        notice("Deleted " + count + " (" + capacity(result.removed_bytes) + ").");
        await reload();
      } catch (error) {
        if (request === revision && dialog.open) explain(error.message + " Refresh the list before reviewing again.");
      } finally {
        submitting = false;
        controls();
      }
    });
  });
  return {review, dismiss};
}

function recordingsPanel(options, isVisible) {
  const {api, busy, notice} = options;
  let directories = null;
  let session = null;
  let recording = false;
  let recordingHeld = false;
  function catalog(id) {
    return {list: byId(id), ticket: 0, loaded: false, loading: false, failed: false, message: "",
      cursor: null, targetId: null, cleanupAllowed: false};
  }
  const folder = catalog("clips-list");
  const lookup = catalog("clips-exact-list");
  const filter = byId("clips-filter");
  let exactId = "";
  let view = folder;
  const more = byId("clips-more");
  const source = byId("clips-source");
  const clean = byId("clips-clean");
  const cleanup = cleanupDialog(options, reloadFolder);

  function deletionControls() {
    const section = byId("clips-cleanup");
    section.hidden = !view.cleanupAllowed || Boolean(filter.value.trim());
    if (section.hidden) section.open = false;
    setText("clips-delete-lock", recordingHeld ? "Stop all recording sources before deleting recordings." : "");
    for (const control of [clean, ...view.list.querySelectorAll(".clip-delete")]) {
      control.hidden = !view.cleanupAllowed;
      control.disabled = !view.cleanupAllowed || !view.targetId || recordingHeld || busy();
      control.title = recordingHeld ? "Stop all recording sources before deleting recordings." : "";
    }
  }

  function review(sessionName) {
    if (!view.cleanupAllowed || !view.targetId || recordingHeld || !isVisible() || (!sessionName && filter.value.trim())) return;
    const selector = {source: source.value, target_id: view.targetId};
    if (sessionName) selector.session = sessionName;
    cleanup.review(selector);
  }

  function downloadLink(file, className) {
    if (typeof file?.url !== "string") return null;
    let url;
    try { url = new URL(file.url, window.location.origin); } catch { return null; }
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/recordings/")) return null;
    const link = element("a", undefined, className);
    link.href = url.href;
    link.download = file.name;
    return link;
  }

  function recordingRow(session) {
    const row = element("article", undefined, "clip-row");
    const date = new Date(session.started_at);
    const header = element("div", undefined, "clip-heading");
    const info = element("div");
    const title = Number.isNaN(date.getTime()) ? "Start time unavailable" : date.toLocaleString("en-US", {dateStyle: "medium", timeStyle: "long"});
    info.append(element("h3", title));
    info.append(element("p", "Recording ID · " + session.name, "clip-id"));
    const summary = [];
    const format = session.files.some(file => file.name === "video.mjpeg") ? "Motion JPEG (MJPEG)"
      : session.files.some(file => file.name === "video.y4m") ? "YUV420 (Y4M)" : "";
    if (format) summary.push(format);
    row.dataset.search = [session.name, session.started_at, title, format, date.toLocaleDateString("en-CA")].join(" ").toLowerCase();
    if (Number.isSafeInteger(session.frames)) summary.push(session.frames.toLocaleString("en-US") + " frames");
    if (session.files.length && session.files.every(file => Number.isSafeInteger(file.size_bytes))) {
      summary.push(capacity(session.files.reduce((total, file) => total + file.size_bytes, 0)));
    }
    info.append(element("p", summary.join(" · "), "field-help"));
    header.append(info);
    row.append(header);
    const archive = downloadLink(session.archive, "button primary small clip-archive");
    if (archive) {
      archive.textContent = "Download ZIP";
      archive.setAttribute("aria-label", "Download ZIP for " + session.name);
      header.append(archive);
    }
    const links = element("div", undefined, "clip-links");
    for (const file of session.files) {
      const link = downloadLink(file, "clip-link");
      if (!link) continue;
      link.append(element("span", file.name), element("small", capacity(file.size_bytes)));
      link.setAttribute("aria-label", "Download " + file.name + " from " + session.name);
      links.append(link);
    }
    const details = element("details", undefined, "clip-details");
    details.append(element("summary", "Files and actions"), links);
    details.open = !archive;
    const remove = button("Delete recording…", () => review(session.name), "button secondary small danger clip-delete");
    remove.setAttribute("aria-label", "Delete recording " + session.name);
    details.append(remove);
    row.append(details);
    return row;
  }

  function reset(catalog) {
    catalog.ticket++;
    catalog.list.replaceChildren();
    catalog.loaded = catalog.loading = catalog.failed = catalog.cleanupAllowed = false;
    catalog.targetId = catalog.cursor = null;
    catalog.message = "";
  }

  function displayResults() {
    folder.list.hidden = view !== folder;
    lookup.list.hidden = view !== lookup;
    const query = filter.value.trim().toLowerCase();
    const words = query.split(/\s+/);
    const rows = view.list.querySelectorAll(".clip-row");
    let matches = 0;
    for (const row of rows) {
      row.hidden = view === folder && !words.every(word => row.dataset.search.includes(word));
      if (!row.hidden) matches++;
    }
    let count = "";
    if (view.loaded) {
      if (view === lookup) {
        if (matches) count = "1 matching recording.";
        else if (!view.list.querySelector(".danger")) count = "No completed recording with this ID in this folder. Try another folder.";
      }
      else if (query) count = matches + " of " + rows.length + " loaded recordings match." +
        (view.cursor ? " Load older recordings to search more." : "");
      else if (rows.length) count = rows.length + (rows.length === 1 ? " recording" : " recordings") + (view.cursor ? " loaded." : " in this folder.");
      else count = view.cursor ? "No completed recordings on this page. Load older recordings to continue."
        : view.list.querySelector(".danger") ? "No readable completed recordings in this folder." : "No completed recordings in this folder.";
    }
    setText("clips-count", count);
    setText("clips-message", view.message || "");
    byId("clips-message").classList.toggle("danger", Boolean(view.failed));
    byId("clips-clear").hidden = !filter.value;
    byId("clips-refresh").disabled = more.disabled = Boolean(view.loading);
    more.hidden = !view.cursor;
    deletionControls();
  }

  async function load(append = false) {
    if (!directories || !isVisible() || (append && (!view.cursor || view.loading))) return;
    const current = view;
    if (!append) {
      cleanup.dismiss();
      reset(current);
    }
    const request = ++current.ticket;
    const query = new URLSearchParams({source: source.value});
    if (current === lookup) {
      query.set("session", exactId);
      if (folder.targetId) query.set("target_id", folder.targetId);
    }
    if (append) {
      query.set("cursor", current.cursor);
      query.set("target_id", current.targetId);
    }
    setText("clips-directory", directories[source.value] || "");
    byId("clips-in-progress").hidden = !recording || directories[source.value] !== directories.current;
    current.loading = true;
    current.failed = false;
    current.message = current === lookup ? "Looking up this recording…" : "Loading recordings…";
    displayResults();
    try {
      const result = await api("recordings?" + query.toString());
      if (request !== current.ticket) return;
      current.loaded = true;
      current.targetId = result.target_id;
      current.cleanupAllowed = result.cleanup_allowed === true;
      current.list.append(...result.sessions.map(recordingRow));
      if (result.unreadable_sessions?.length) {
        const folders = result.unreadable_sessions.join(", ");
        current.list.append(element("p", "The drive reported an I/O error reading " + folders +
          ". Check the drive in Recording drives, then refresh this list.", "danger"));
      }
      current.cursor = result.next_cursor;
      current.message = "";
    } catch (error) {
      if (request !== current.ticket) return;
      current.failed = true;
      current.message = "Could not load recordings. " + error.message;
    } finally {
      if (request === current.ticket) {
        current.loading = false;
        if (current === view) displayResults();
      }
    }
  }

  function applyFilter() {
    const value = filter.value.trim();
    const sessionId = isRecordingId(value) ? value : "";
    if (sessionId !== exactId) {
      cleanup.dismiss();
      reset(lookup);
      exactId = sessionId;
      view = exactId ? lookup : folder;
    }
    if (!view.loaded && !view.loading) load();
    displayResults();
  }

  function clearDeepLink() {
    if (location.hash.startsWith("#recordings/")) history.replaceState(null, "", "#recordings");
  }

  function reloadFolder() {
    reset(folder);
    reset(lookup);
    return load();
  }

  byId("clips-refresh").addEventListener("click", reloadFolder);
  clean.addEventListener("click", () => review());
  byId("copy-mp4-command").addEventListener("click", () => copyCommand(byId("mp4-command"), notice));
  for (const id of ["browse-active", "monitor-browse"]) {
    byId(id).addEventListener("click", () => { source.value = "current"; });
  }
  filter.addEventListener("input", () => { clearDeepLink(); applyFilter(); });
  byId("clips-clear").addEventListener("click", () => {
    filter.value = "";
    clearDeepLink();
    applyFilter();
    filter.focus();
  });
  source.addEventListener("change", () => { clearDeepLink(); reloadFolder(); });
  more.addEventListener("click", () => load(true));
  return {
    open(sessionId) {
      if (sessionId) source.value = "current";
      filter.value = exactId = sessionId || "";
      view = exactId ? lookup : folder;
      reloadFolder();
    },
    leave: cleanup.dismiss,
    update(snapshot) {
      const next = snapshot.recording_directories || {current: snapshot.config.storage.directory, nvme: null};
      const previous = source.value;
      const path = directories?.[previous];
      if (!directories || ["current", "nvme", "usb", "default"].some(key => next[key] !== directories[key])) {
        const names = {current: "Current destination · " + destinationLabel(next),
          nvme: "PCIe NVMe", usb: "USB drive", default: "Default recording folder"};
        const seen = new Set();
        const options = [];
        for (const key of ["current", "nvme", "usb", "default"]) {
          if (!next[key] || seen.has(next[key])) continue;
          seen.add(next[key]);
          options.push(new Option(names[key], key));
        }
        source.replaceChildren(...options);
        source.value = options.some(option => option.value === previous) ? previous
          : options.find(option => next[option.value] === path)?.value || "current";
      }
      const camera = snapshot.camera;
      const held = Boolean(camera.recording || snapshot.recording_sources?.length);
      if (!recordingHeld && held) cleanup.dismiss();
      recordingHeld = held;
      deletionControls();
      const changed = !directories || previous !== source.value || path !== next[source.value] ||
        (next[source.value] === next.current && !camera.recording && (recording || session !== camera.session));
      directories = next;
      recording = camera.recording;
      session = camera.session;
      byId("clips-in-progress").hidden = !recording || next[source.value] !== next.current;
      if (changed) reloadFolder();
    },
    clear() {
      cleanup.dismiss();
      recordingHeld = recording = false;
      directories = session = null;
      source.value = "current";
      filter.value = exactId = "";
      view = folder;
      reset(folder);
      reset(lookup);
      byId("clips-in-progress").hidden = true;
      setText("clips-directory", "");
      displayResults();
    },
  };
}

function drivePanel(medium, label, {api, act, busy, chooseDestination}, refreshPage, inventory) {
  const id = part => medium + "-" + part;
  const control = part => byId(id(part));
  const write = (part, value) => setText(id(part), value);
  const markup = byId("drive-setup-template").content.cloneNode(true);
  for (const field of markup.querySelectorAll("[data-part]")) field.id = id(field.dataset.part);
  control("setup").append(markup);
  write("disk-label", label + " disk");
  write("refresh", "Refresh " + label);
  const picker = control("disk");
  const erase = control("erase");
  const confirmation = control("confirm");
  let latest = null;
  let disk = null;
  let recording = false;
  let healthyDirectory = null;
  let ticket = 0;
  let loading = false;
  let applying = false;

  function controls() {
    const blocked = !latest?.available || applying || loading || recording || latest?.busy || busy();
    picker.disabled = applying || loading || busy();
    control("refresh").disabled = applying || loading || busy();
    for (const mount of control("volumes").querySelectorAll("button")) mount.disabled = blocked;
    confirmation.disabled = blocked;
    control("prepare").disabled = blocked || !disk?.can_prepare || confirmation.value !== disk.device;
    const selected = latest?.ready && healthyDirectory === latest.directory;
    control("use").disabled = blocked || !latest?.ready || selected;
    write("use", selected ? label + " selected" : "Use " + label);
    write("activity", applying ? "Setting up the " + label + " disk…" : latest?.busy ? label + " setup is in progress."
      : recording ? "Finish the active recording before changing storage." : "");
  }

  function displayDisk() {
    const previous = disk;
    disk = latest?.devices?.find(item => item.device === picker.value) || null;
    if (!disk || disk.device !== previous?.device || disk.revision !== previous?.revision) {
      erase.open = false;
      confirmation.value = "";
    }
    control("target").hidden = !disk;
    control("ready").hidden = !latest?.ready;
    if (latest?.ready) write("path", latest.directory);
    if (disk) {
      control("facts").replaceChildren(fact("Device", disk.device), fact("Capacity", capacity(disk.size_bytes)),
        ...(disk.serial ? [fact("Serial number", disk.serial)] : []));
      const rows = disk.volumes.map(volume => {
        const row = element("div", undefined, "volume-row");
        const parts = [volume.device, volume.filesystem || "No filesystem"];
        if (Number.isFinite(volume.size_bytes)) parts.push(capacity(volume.size_bytes));
        row.append(element("strong", parts.join(" · ")));
        if (volume.uuid) row.append(element("p", "UUID " + volume.uuid, "clip-id"));
        if (volume.mountpoints?.length) row.append(element("p", volume.mountpoints.join(", "), "file-path"));
        if (volume.can_mount) {
          row.append(button("Mount existing volume", () => apply({action: "mount", volume: volume.device})));
        } else if (volume.reason) {
          row.append(element("p", volume.reason, "field-help"));
        }
        return row;
      });
      control("volumes").replaceChildren(...rows);
      write("reason", disk.reason || "");
      erase.hidden = !disk.can_prepare;
      write("warning", "This erases every partition and file on " + disk.device +
        " and creates a new ext4 recording volume.");
      write("confirm-label", "Type " + disk.device + " to confirm erasure");
    }
    controls();
  }

  function show(result) {
    latest = result;
    const disks = result?.devices || [];
    inventory.managed(medium, result?.available && result.ready ? result.directory : null);
    control("setup-shortcut").hidden = !result?.available || result.ready ||
      !disks.some(item => item.can_prepare || item.volumes.some(volume => volume.can_mount));
    const selected = picker.value;
    picker.replaceChildren(...disks.map(item => new Option([item.model, item.device].filter(Boolean).join(" · "), item.device)));
    if (disks.some(item => item.device === selected)) picker.value = selected;
    const message = result?.error || (!result?.available ? label + " setup is unavailable."
      : !disks.length ? "No " + label + " disks detected." : result.simulated ? "Simulated " + label + " disk." : "");
    write("message", message);
    displayDisk();
  }

  async function load() {
    if (applying) return;
    const request = ++ticket;
    loading = true;
    write("message", "Checking " + label + " disks…");
    controls();
    try {
      const result = await api(medium);
      if (request === ticket) show(result);
    } catch (error) {
      if (request === ticket) show({available: false, error: error.message});
    } finally {
      if (request === ticket) {
        loading = false;
        controls();
      }
    }
  }

  function apply(operation) {
    if (!disk || !latest?.available || loading || applying || recording || latest.busy || busy()) return;
    const target = disk.device;
    const body = {...operation, device: target, revision: disk.revision};
    if (body.action === "prepare") {
      if (!disk.can_prepare || confirmation.value !== target) return;
      body.confirmation = confirmation.value;
    }
    const request = ++ticket;
    act(async () => {
      applying = true;
      confirmation.value = "";
      write("result", "");
      controls();
      try {
        const result = await api(medium, body);
        if (request !== ticket) return;
        show(result);
        write("result", result.ready ? target + " is ready. Select it as the destination to save new recordings there."
          : result.error || "The " + label + " recording directory is unavailable.");
      } catch (error) {
        if (request !== ticket) return;
        write("result", error.message);
        erase.open = false;
      } finally {
        if (request === ticket) {
          applying = false;
          controls();
          await refreshPage();
        }
      }
    });
  }

  picker.addEventListener("change", () => { write("result", ""); displayDisk(); });
  confirmation.addEventListener("input", controls);
  erase.addEventListener("toggle", () => { if (!erase.open) { confirmation.value = ""; controls(); } });
  control("setup").addEventListener("toggle", event => {
    if (!event.currentTarget.open) erase.open = false;
  });
  control("refresh").addEventListener("click", load);
  control("open-setup").addEventListener("click", () => {
    const setup = control("setup");
    setup.open = true;
    setup.querySelector("summary").focus();
  });
  control("use").addEventListener("click", () => {
    if (latest?.ready) chooseDestination(latest.directory);
  });
  control("erase-form").addEventListener("submit", event => {
    event.preventDefault();
    apply({action: "prepare"});
  });
  return {
    load,
    update(snapshot) {
      recording = Boolean(snapshot.camera.recording || snapshot.recording_sources?.length);
      healthyDirectory = snapshot.storage?.error ? null : snapshot.config.storage.directory;
      controls();
    },
    clear() {
      ticket++;
      loading = applying = recording = false;
      latest = disk = healthyDirectory = null;
      show(null);
      write("result", "");
    },
  };
}

export function setupStoragePanels(options, inventory) {
  let page = null;
  const recordings = recordingsPanel(options, () => page === "recordings");
  const drives = [drivePanel("nvme", "NVMe", options, refreshPage, inventory),
    drivePanel("usb", "USB", options, refreshPage, inventory)];
  const refreshDrives = byId("refresh-drives");

  async function refreshPage() {
    if (page === "storage") {
      refreshDrives.disabled = true;
      try { await Promise.all([...drives.map(drive => drive.load()), inventory.load()]); }
      finally { refreshDrives.disabled = false; }
    }
  }

  refreshDrives.addEventListener("click", refreshPage);
  return {
    open(nextPage, sessionId) {
      if (page === "recordings" && nextPage !== page) recordings.leave();
      page = nextPage;
      if (page === "recordings") recordings.open(sessionId);
      else refreshPage();
    },
    update(snapshot) {
      const paths = snapshot.recording_directories || {current: snapshot.config.storage.directory};
      const unavailable = Boolean(snapshot.storage?.error);
      setText("storage-medium", destinationLabel(paths) + (unavailable ? " · Unavailable" : ""));
      byId("storage-current").classList.toggle("unavailable", unavailable);
      byId("storage-recovery").hidden = !unavailable;
      recordings.update(snapshot);
      for (const drive of drives) drive.update(snapshot);
    },
    clear() {
      page = null;
      recordings.clear();
      for (const drive of drives) drive.clear();
    },
  };
}
