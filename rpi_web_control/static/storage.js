import {byId, setText, element, button, capacity, fact, copyCommand} from "./ui.js";

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
    setText("cleanup-title", selector.session ? "Delete this recording?" : "Clean this recording directory?");
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
      setText("cleanup-scope", selector.session || "All eligible completed recordings in this directory");
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
      submit.textContent = selector.session ? "Delete recording" : "Clean recordings";
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
  let cursor = null;
  let targetId = null;
  let cleanupAllowed = false;
  let recordingHeld = false;
  let ticket = 0;
  const list = byId("clips-list");
  const more = byId("clips-more");
  const source = byId("clips-source");
  const clean = byId("clips-clean");
  const cleanup = cleanupDialog(options, () => load());

  function deletionControls() {
    for (const control of [clean, ...list.querySelectorAll(".clip-delete")]) {
      control.hidden = !cleanupAllowed;
      control.disabled = !cleanupAllowed || !targetId || recordingHeld || busy();
      control.title = recordingHeld ? "Stop all recording sources before deleting recordings." : "";
    }
  }

  function review(sessionName) {
    if (!cleanupAllowed || !targetId || recordingHeld || !isVisible()) return;
    const selector = {source: source.value, target_id: targetId};
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
    row.append(element("h3", Number.isNaN(date.getTime()) ? session.name : date.toLocaleString("en-US")));
    const frames = Number.isSafeInteger(session.frames) ? " · " + session.frames.toLocaleString("en-US") + " frames" : "";
    row.append(element("p", session.name + frames, "clip-id"));
    const archive = downloadLink(session.archive, "button primary small clip-archive");
    if (archive) {
      archive.textContent = "Download recording (.zip)";
      archive.setAttribute("aria-label", "Download recording (.zip) for " + session.name);
      row.append(archive);
    }
    const links = element("div", undefined, "clip-links");
    for (const file of session.files) {
      const link = downloadLink(file, "clip-link");
      if (!link) continue;
      link.append(element("span", file.name), element("small", capacity(file.size_bytes)));
      link.setAttribute("aria-label", "Download " + file.name + " from " + session.name);
      links.append(link);
    }
    if (archive && links.childElementCount) {
      const details = element("details", undefined, "clip-details");
      details.append(element("summary", "Individual files"), links);
      row.append(details);
    } else {
      row.append(links);
    }
    const remove = button("Delete recording…", () => review(session.name), "button secondary small danger clip-delete");
    remove.setAttribute("aria-label", "Delete recording " + session.name);
    row.append(remove);
    return row;
  }

  async function load(append = false) {
    if (!directories || !isVisible() || (append && !cursor)) return;
    const request = ++ticket;
    const query = new URLSearchParams({source: source.value});
    if (append) {
      query.set("cursor", cursor);
      query.set("target_id", targetId);
    }
    byId("clips-refresh").disabled = more.disabled = true;
    if (!append) {
      cleanup.dismiss();
      cleanupAllowed = false;
      deletionControls();
      list.replaceChildren();
      more.hidden = true;
      targetId = cursor = null;
      setText("clips-directory", directories[source.value] || "");
    }
    setText("clips-message", "Loading recordings…");
    try {
      const result = await api("recordings?" + query.toString());
      if (request !== ticket) return;
      setText("clips-directory", result.directory);
      targetId = result.target_id;
      cleanupAllowed = result.cleanup_allowed === true;
      list.append(...result.sessions.map(recordingRow));
      deletionControls();
      cursor = result.next_cursor;
      more.hidden = !cursor;
      setText("clips-message", list.childElementCount ? "" : cursor
        ? "No completed recordings on this page. Load older recordings to continue."
        : "No completed recordings in this directory.");
    } catch (error) {
      if (request === ticket) setText("clips-message", error.message);
    } finally {
      if (request === ticket) byId("clips-refresh").disabled = more.disabled = false;
    }
  }

  byId("clips-refresh").addEventListener("click", () => load());
  clean.addEventListener("click", () => review());
  byId("copy-mp4-command").addEventListener("click", () => copyCommand(byId("mp4-command"), notice));
  byId("browse-active").addEventListener("click", () => { source.value = "current"; });
  source.addEventListener("change", () => load());
  more.addEventListener("click", () => load(true));
  return {
    load, leave: cleanup.dismiss,
    update(snapshot) {
      const next = snapshot.recording_directories || {current: snapshot.config.storage.directory, nvme: null};
      const previous = source.value;
      const path = directories?.[previous];
      if (!directories || ["current", "nvme", "usb", "default"].some(key => next[key] !== directories[key])) {
        const currentMedium = next.current === next.nvme ? " · PCIe NVMe" : next.current === next.usb ? " · USB"
          : next.current === next.default ? " · Default" : "";
        const names = {current: "Current recording directory" + currentMedium,
          nvme: "PCIe NVMe recording directory", usb: "USB recording directory", default: "Default recording directory"};
        const seen = new Set();
        const options = [];
        for (const key of ["current", "nvme", "usb", "default"]) {
          if (!(key in next) || seen.has(next[key])) continue;
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
        (!camera.recording && (recording || session !== camera.session));
      directories = next;
      recording = camera.recording;
      session = camera.session;
      if (changed) load();
    },
    clear() {
      cleanup.dismiss();
      cleanupAllowed = recordingHeld = false;
      deletionControls();
      recording = false;
      directories = cursor = targetId = session = null;
      source.value = "current";
      ticket++;
      list.replaceChildren();
      more.hidden = true;
      setText("clips-directory", "");
      setText("clips-message", "");
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
    control("use").disabled = blocked || !latest?.ready || healthyDirectory === latest.directory;
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
        write("result", result.ready ? target + " is ready. Choose Record to " + label + " to use it."
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

  function refreshPage() {
    if (page === "recordings") return recordings.load();
    if (page === "storage") return Promise.all([...drives.map(drive => drive.load()), inventory.load()]);
  }

  return {
    open(nextPage) {
      if (page === "recordings" && nextPage !== page) recordings.leave();
      page = nextPage;
      refreshPage();
    },
    update(snapshot) {
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
