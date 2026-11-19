import {byId, element, setText} from "./ui.js";

const securityNames = {
  "wpa2-personal": "WPA2-Personal", "wpa3-personal": "WPA3-Personal", open: "Open",
  enterprise: "Enterprise", wep: "WEP", "wpa1-personal": "WPA-Personal",
  "enhanced-open": "Enhanced Open", unsupported: "Unknown security",
};
const readyStates = ["connected", "disconnected", "failed"];

export function validateWifiFields(ssid, password, security = "wpa2-personal") {
  const name = ssid.value;
  const badName = new TextEncoder().encode(name).length > 32 || /[\p{C}\p{Z}]/u.test(name.replaceAll(" ", ""));
  ssid.setCustomValidity(!ssid.disabled && badName ? "Choose a network name of up to 32 UTF-8 bytes of printable text." : "");
  password.setCustomValidity(!password.disabled && password.value && security === "wpa2-personal" &&
    !/^([ -~]{8,63}|[a-fA-F0-9]{64})$/.test(password.value)
    ? "Use an 8–63 character ASCII passphrase or a 64-digit hexadecimal key." : "");
}

export function setupStation({api, act, busy, apply, consoleUrl}) {
  const form = byId("network-station-form");
  const fields = byId("station-fields");
  const radios = byId("station-interface");
  const picker = byId("station-network");
  const ssid = byId("station-ssid");
  const password = byId("station-password");
  const security = byId("station-security");
  const hidden = byId("station-hidden");
  const scan = byId("station-scan");
  const connect = byId("station-connect");
  let adapter = null;
  let results = [];
  let scanId = 0;
  let scanning = false;
  let status = {};
  let joinResult = "";

  function selected() {
    if (picker.value === "manual") return {ssid: ssid.value, security: security.value, supported: true};
    return picker.value ? results[Number(picker.value)] : null;
  }

  function inputs() {
    const manual = picker.value === "manual";
    const network = selected();
    byId("station-manual").hidden = ssid.disabled = security.disabled = !manual;
    byId("station-hidden-field").hidden = hidden.disabled = !manual;
    byId("station-password-field").hidden = password.disabled = !network?.supported || network.security === "open";
    validateWifiFields(ssid, password, network?.security);
    fields.disabled = Boolean(status.pending) || busy() || !adapter;
    const ready = readyStates.includes(adapter?.state);
    scan.disabled = fields.disabled || scanning || !ready;
    connect.disabled = fields.disabled || scanning || !network?.supported || !ready;
  }

  function clearScan() {
    scanId++;
    scanning = false;
    results = [];
    picker.replaceChildren(new Option("Choose a network", ""), new Option("Enter network name manually…", "manual"));
    scan.textContent = "Scan networks";
    setText("station-scan-status", "");
    setText("station-result", "");
    joinResult = "";
    ssid.value = password.value = "";
    security.value = "wpa2-personal";
    hidden.checked = false;
  }

  function update(next) {
    status = next;
    const all = (status.interfaces || []).filter(row => row.type === "wifi" && row.physical);
    const off = status.wifi_radio?.enabled === false;
    const blocked = status.wifi_radio?.hardware_enabled === false;
    const choices = off || blocked || !status.available ? [] : all.filter(row => row.managed && !row.controller &&
      (readyStates.includes(row.state) || ["connecting", "disconnecting"].includes(row.state)));
    const radio = choices.find(row => row.name === radios.value) ||
      choices.find(row => row.mode === "station") || choices.find(row => row.state === "disconnected") || choices[0];
    if (choices.length !== radios.options.length || choices.some((row, i) => row.name !== radios.options[i].value))
      radios.replaceChildren(...choices.map(row => new Option(row.name, row.name)));
    radios.value = radio?.name || "";
    if (adapter?.name !== radio?.name || (adapter?.mode === "ap") !== (radio?.mode === "ap")) clearScan();
    adapter = radio;
    byId("station-interface-field").hidden = choices.length < 2;
    fields.hidden = !adapter;
    let message = "Wi-Fi gets its address via DHCP. You can keep using the Pi’s Ethernet address.";
    if (!all.length) message = status.message && !status.available ? status.message : "No physical Wi-Fi adapter detected.";
    else if (blocked) message = "Wi-Fi is hardware-blocked. Check the adapter’s radio switch.";
    else if (off) message = "Wi-Fi is off. Choose Enable Wi-Fi below to scan for networks.";
    else if (!adapter) message = all.every(row => row.controller)
      ? "The Wi-Fi adapter is a bridge port. Use a separate Wi-Fi adapter to keep the wired bridge."
      : "Waiting for a managed Wi-Fi interface. Check the radio state under Network interfaces.";
    else if (adapter.mode === "ap") {
      message = adapter.name + " is hosting an access point. Scanning this radio may briefly interrupt Wi-Fi traffic. " +
        (choices.some(row => row.name !== adapter.name && row.mode !== "ap" && readyStates.includes(row.state))
          ? "Select another Wi-Fi interface to scan independently. "
          : "A second Wi-Fi adapter can scan independently. ") +
        "To join a router manually, enter its network name over Ethernet. Connecting this radio to the router disconnects its access point clients.";
    } else if (adapter.state === "failed") message = "The last Wi-Fi connection failed. Scan again or enter the router’s network name manually.";
    else if (["connecting", "disconnecting"].includes(adapter.state)) message = adapter.name + " is " + adapter.state + ". Wait for the radio to finish before starting another operation.";
    setText("station-status", message);
    if (joinResult && !status.pending) {
      setText("station-result", "");
      joinResult = "";
    }
    inputs();
  }

  function showConnected(devices) {
    const group = byId("station-connected");
    const joined = devices.filter(row => row.type === "wifi" && row.mode === "station" && row.state === "connected");
    group.hidden = !joined.length;
    group.replaceChildren(...joined.map(row => {
      const info = element("div", undefined, "network-guidance");
      info.append(element("h3", (row.ssid || "Wi-Fi station") + " · " + row.name));
      for (const cidr of row.addresses || []) {
        const link = element("a", undefined, "text-link mono");
        link.href = consoleUrl(cidr.split("/")[0]);
        link.textContent = link.href;
        info.append(link, document.createElement("br"));
      }
      return info;
    }));
  }

  radios.addEventListener("change", () => update(status));
  picker.addEventListener("change", () => { password.value = ""; inputs(); });
  security.addEventListener("change", () => { password.value = ""; inputs(); });
  form.addEventListener("input", inputs);

  scan.addEventListener("click", async () => {
    if (scan.disabled || !adapter || busy() || status.pending) return;
    const name = adapter.name;
    const activeAp = adapter.mode === "ap";
    if (activeAp && !window.confirm("Scan for networks on " + name + " while it hosts the access point? " +
        "Wi-Fi traffic may pause briefly. The access point and DHCP settings will be kept.")) return;
    const id = ++scanId;
    scanning = true;
    inputs();
    setText("station-scan-status", "Scanning for nearby networks on " + name + "…");
    try {
      const request = {action: "scan_wifi", interface: name};
      if (activeAp) request.allow_ap_scan = true;
      const result = await api("network", request);
      if (id !== scanId) return;
      if (result.interface !== name || !Array.isArray(result.networks))
        throw new Error("The device returned an incomplete Wi-Fi scan for " + name + ". Try again.");
      const prior = selected();
      const manual = picker.value === "manual";
      results = result.networks;
      picker.replaceChildren(new Option("Choose a network", ""), ...results.map((network, i) => {
        const label = securityNames[network.security] || "Unknown security";
        const option = new Option(network.ssid + " · " + label + " · " + network.strength + "%" +
          (network.supported ? "" : " · unsupported"), String(i));
        option.disabled = !network.supported;
        return option;
      }), new Option("Enter network name manually…", "manual"));
      const match = results.findIndex(row => prior && row.supported && row.ssid === prior.ssid && row.security === prior.security);
      picker.value = manual ? "manual" : match >= 0 ? String(match) : "";
      if (!manual && match < 0) password.value = "";
      scan.textContent = "Scan again";
      let note;
      if (!results.length) {
        note = result.access_points_seen > 0
          ? "The adapter reported access points, but none had a selectable network name."
          : "No networks found on " + name + ".";
        note += activeAp ? " Try a second Wi-Fi adapter or enter the router’s name manually over Ethernet."
          : " Check that the router is in range, scan again or enter its name manually.";
      } else if (result.previous_results) {
        note = "Showing " + results.length + " previously seen " + (results.length === 1 ? "network" : "networks") +
          ". Scan again to check which are in range.";
      } else {
        note = result.truncated ? "Showing the strongest " + results.length + " networks." :
          results.length === 1 ? "1 network found." : results.length + " networks found.";
      }
      setText("station-scan-status", (result.cached && !result.previous_results ? "Recent scan. " : "") + note);
    } catch (error) {
      if (id === scanId) setText("station-scan-status", error.message +
        (results.length ? " The list is from the previous scan." : ""));
    } finally {
      if (id === scanId) { scanning = false; inputs(); }
    }
  });

  form.addEventListener("submit", event => {
    event.preventDefault();
    inputs();
    if (connect.disabled || !adapter || !form.reportValidity()) return;
    const network = selected();
    const payload = {action: "apply", mode: "station", interface: adapter.name,
      ssid: network.ssid, security: network.security, password: password.disabled ? "" : password.value,
      hidden: picker.value === "manual" && hidden.checked, ipv4_method: "auto", address: "", gateway: "", dns: []};
    password.value = "";
    act(async () => {
      setText("station-result", "Connecting to “" + payload.ssid + "”…");
      try {
        await apply(payload);
        joinResult = "Open the Wi-Fi console address, then choose Keep settings within 120 seconds.";
        setText("station-result", joinResult);
      } catch (error) {
        setText("station-result", error.message);
      }
    });
  });

  return {update, showConnected, reset() {clearScan(); adapter = null;}};
}
