import {byId, element, fact, setText} from "./ui.js";

const modes = {ethernet: "Ethernet", station: "Wi-Fi station", ap: "Wi-Fi access point", bridge: "Ethernet + Wi-Fi bridge"};
const methods = {auto: "DHCP client", manual: "Static address", shared: "Local DHCP server"};
const types = {ethernet: "Ethernet", wifi: "Wi-Fi", bridge: "Bridge"};
const recoveryStates = {
  active: ["Active", "The Pi has an additional console address on the wired connection."],
  native: ["Already assigned", "The wired connection already uses this address."],
  waiting: ["Waiting for Ethernet", "The Pi is waiting for a usable wired connection."],
  conflict: ["Address conflict", "The Pi detected a conflict with the recovery address."],
  disabled: ["Disabled", "Wired recovery is turned off."],
  error: ["Unavailable", "The Pi could not verify the recovery address."],
};

function selectOptions(select, values, labels, preferred = select.value) {
  const choices = values.map(value => [value, Object.hasOwn(labels, value) ? labels[value] : value]);
  if (preferred && !values.includes(preferred)) choices.push([preferred, preferred + " (unavailable)"]);
  if (choices.length !== select.options.length || choices.some(([value, label], i) =>
    select.options[i].value !== value || select.options[i].text !== label)) {
    select.replaceChildren(...choices.map(([value, label]) => {
      const option = new Option(label, value);
      option.disabled = !values.includes(value);
      return option;
    }));
  }
  select.value = preferred || values[0] || "";
}

function ipv4(value) {
  const octets = value.split(".");
  return octets.length === 4 && octets.every(part => /^(0|[1-9]\d{0,2})$/.test(part) && Number(part) <= 255);
}

function dotted(number) {
  return [24, 16, 8, 0].map(shift => (number >>> shift) & 255).join(".");
}

function targetNetwork(value) {
  const parts = value.trim().split("/");
  if (parts.length !== 2 || !ipv4(parts[0]) || !/^([1-9]|[12]\d|3[0-2])$/.test(parts[1])) return null;
  const host = parts[0];
  const bits = Number(parts[1]);
  const number = host.split(".").reduce((total, part) => total * 256 + Number(part), 0);
  const network = (number & (0xffffffff << (32 - bits))) >>> 0;
  return {host, number, network, bits, subnet: dotted(network) + "/" + bits};
}

function consoleUrl(host) {
  const url = new URL(location.origin);
  url.hostname = host;
  url.hash = "network";
  return url.href;
}

function suggestedApAddress(interfaces, selected, recovery) {
  const addresses = interfaces.filter(row => row.name !== selected).flatMap(row => row.addresses || []);
  if (recovery?.address) addresses.push(recovery.address);
  const subnets = addresses.map(targetNetwork).filter(Boolean);
  for (let i = 0; i < 256; i++) {
    const candidate = targetNetwork("10.42." + i + ".1/24");
    if (subnets.every(subnet => candidate.network + 256 <= subnet.network ||
        subnet.network + 2 ** (32 - subnet.bits) <= candidate.network)) return candidate.host + "/24";
  }
  return ""; // NetworkManager can select another shared subnet on activation.
}

function activeAccessPoint(adapter, devices, recovery) {
  const owner = adapter.controller ? devices.find(row => row.name === adapter.controller) : adapter;
  const addresses = (owner?.addresses || []).filter(address =>
    !(recovery?.state === "active" && owner.name === recovery.interface &&
      address.split("/")[0] === recovery.address?.split("/")[0]));
  const primary = addresses.includes(owner?.configured_primary) ? owner.configured_primary : addresses[0];
  const host = targetNetwork(primary || "")?.host;
  const group = element("div", undefined, "network-guidance");
  group.append(element("h3", adapter.ssid || adapter.name));
  group.append(element("p", host
    ? "Join this Wi-Fi network and open the Pi’s console." : "Waiting for a primary IPv4 address.", "field-help"));
  if (host) {
    const link = element("a", consoleUrl(host), "text-link mono");
    link.href = consoleUrl(host);
    group.append(link);
  }
  return group;
}

function clientAddress(target, interfaces) {
  if (target.bits === 32) return "";
  const used = new Set([target.host]);
  for (const row of interfaces) {
    for (const address of row.addresses || []) used.add(address.split("/")[0]);
  }
  const first = target.network + (target.bits === 31 ? 0 : 1);
  const last = target.network + 2 ** (32 - target.bits) - (target.bits === 31 ? 1 : 2);
  let candidate = target.number - target.number % 256 + 10;
  if (candidate < first || candidate > last || used.has(dotted(candidate))) {
    candidate = first;
    while (candidate <= last && used.has(dotted(candidate))) candidate++;
  }
  return candidate <= last ? dotted(candidate) + "/" + target.bits : "";
}

function showRecovery(recovery, interfaces) {
  const state = recovery?.state;
  const target = targetNetwork(recovery?.address || "");
  const available = (state === "active" || state === "native") && Boolean(target);
  const showCable = available || (state === "waiting" && Boolean(target));
  const [label, description] = Object.hasOwn(recoveryStates, state) ? recoveryStates[state]
    : ["Unknown", "The device has not reported wired recovery status."];
  setText("network-recovery-state", label);
  byId("network-recovery-state").classList.toggle("live", available);
  setText("network-recovery-address", [recovery?.address, recovery?.interface].filter(Boolean).join(" on "));
  setText("network-recovery-message", recovery?.message || description);
  byId("network-pending-recovery").hidden = state !== "active" || location.hostname !== target?.host;
  byId("network-recovery-connect").hidden = !showCable;
  const link = byId("network-recovery-link");
  link.hidden = !available;
  if (available) {
    link.href = consoleUrl(target.host);
    setText("network-recovery-link", link.href);
  } else {
    link.removeAttribute("href");
    setText("network-recovery-link", "");
  }
  if (showCable) {
    const client = clientAddress(target, interfaces);
    setText("network-recovery-client", client
      ? "Connect the computer to the Pi’s Ethernet port. Set the computer’s Ethernet IPv4 address to an unused address such as " + client + " and leave its gateway blank."
      : "A direct cable requires another available host address on this subnet for the computer.");
  }
}

function adapterCard(adapter, recovery) {
  const card = element("article", undefined, "network-adapter");
  const heading = element("div", undefined, "disk-head");
  heading.append(element("h3", adapter.name, "mono"), element("span", types[adapter.type] || adapter.type, "tag"));
  const state = adapter.managed ? adapter.state : "unmanaged";
  heading.append(element("span", state, "field-help"));
  card.append(heading);
  const addresses = (adapter.addresses || []).map(address =>
    recovery?.state === "active" && adapter.name === recovery.interface && address === recovery.address?.split("/")[0] + "/32"
      ? address + " (wired recovery)" : address);
  const entries = [
    ["Controller", adapter.controller],
    ["Wi-Fi", [modes[adapter.mode], adapter.ssid].filter(Boolean).join(" · ")],
    ["IPv4", addresses.join(", ")],
    ["Assignment", methods[adapter.ipv4_method]],
    ["Gateway", adapter.gateway],
    ["DNS", (adapter.dns || []).join(", ")],
  ].filter(([name, value]) => value && (name !== "Wi-Fi" || adapter.type === "wifi"));
  if (entries.length) {
    const facts = element("dl", undefined, "fact-grid");
    facts.append(...entries.map(([name, value]) => fact(name, value)));
    card.append(facts);
  }
  return card;
}

export function setupNetworkPanel({api, act, notice, busy}) {
  const form = byId("network-form");
  const fields = form.elements;
  let devices = [];
  let recovery = null;
  let inventoryKey = null;
  let pending = null;
  let applying = false;
  let deadline = 0;
  let timer = 0;

  function interfaceChoices() {
    return devices.filter(item => item.physical && item.managed && ["ethernet", "wifi"].includes(item.type)
      && !["unavailable", "unmanaged", "unknown"].includes(item.state)
      && (!item.controller || item.controller === "br-rpi"));
  }

  function apChoices() {
    return interfaceChoices().filter(row => row.type === "wifi" && row.ap && !row.controller);
  }

  function wifiControls(status) {
    const radios = devices.filter(row => row.type === "wifi" && row.physical);
    const accessPoints = radios.filter(row => row.mode === "ap" && row.state === "connected");
    const choices = apChoices();
    const hardwareOff = status.wifi_radio?.hardware_enabled === false;
    const softwareOff = status.wifi_radio?.enabled === false;
    const enable = byId("network-enable-wifi");
    const setup = byId("network-setup-ap");
    enable.hidden = !radios.length || hardwareOff || !softwareOff;
    setup.hidden = hardwareOff || softwareOff || !status.available || !choices.length;
    enable.disabled = setup.disabled = Boolean(pending) || busy();
    byId("network-wifi-actions").hidden = enable.hidden && setup.hidden;
    const picker = byId("network-ap-interface");
    const preferred = choices.find(row => row.name === picker.value) ||
      choices.find(row => row.mode === "ap") || choices.find(row => row.state === "disconnected") || choices[0];
    selectOptions(picker, choices.map(row => row.name), {}, preferred?.name || "");
    byId("network-ap-interface-field").hidden = setup.hidden || choices.length < 2;
    picker.disabled = setup.disabled;
    let message;
    if (!radios.length) message = !status.available && !devices.length && status.message
      ? status.message : "No Wi-Fi adapter detected. Connect an AP-capable USB Wi-Fi adapter or use a Pi with onboard Wi-Fi.";
    else if (hardwareOff) message = "Wi-Fi is blocked at the hardware level. Check the adapter and its hardware radio switch.";
    else if (softwareOff) message = "Wi-Fi is switched off. Enable Wi-Fi to set up an access point.";
    else if (accessPoints.length) message = "";
    else if (!setup.hidden) message = "Create a password-protected Wi-Fi network for direct access to the Pi.";
    else if (radios.every(row => !row.managed || row.state === "unmanaged"))
      message = "NetworkManager is not managing a Wi-Fi adapter. Enable management of the adapter on the Pi.";
    else if (radios.some(row => row.state === "unavailable" || row.state === "unknown"))
      message = "The Wi-Fi adapter is unavailable. Check the Wi-Fi country setting and adapter firmware on the Pi.";
    else if (radios.some(row => row.ap && row.controller))
      message = "The Wi-Fi adapter belongs to a network controller. Use the bridge settings for its access point.";
    else message = "No available Wi-Fi adapter reports access point support.";
    setText("network-wifi-message", message);
    setText("network-setup-ap", choices.length === 1 && preferred.mode === "ap" ? "Edit access point" : "Set up access point");
  }

  function guidance() {
    const mode = fields.mode.value;
    const method = fields.ipv4_method.value;
    const target = method === "auto" ? null : targetNetwork(fields.address.value);
    const link = byId("network-target");
    link.hidden = !target;
    let text;
    if (target) {
      link.href = consoleUrl(target.host);
      link.textContent = link.href;
      text = "Reconnect from the " + target.subnet + " network and open the Pi at the address below.";
    } else if (method === "auto") {
      text = "Find the Pi’s new address in your DHCP server’s leases and open the console on that address.";
    } else if (mode === "ap" && method === "shared" && !fields.address.value.trim()) {
      text = "The Pi will choose a subnet. Open the console using the gateway address assigned to your Wi-Fi client.";
    } else {
      text = "Enter the Pi’s IPv4 address and prefix to see the console address for this connection.";
    }
    if (mode === "station") text = "Join the same Wi-Fi network as the Pi. " + text;
    if (mode === "ap" || mode === "bridge") {
      const name = fields.ssid.value;
      const join = name ? "join “" + name + "”" : "join the Pi’s access point";
      text = (mode === "bridge" ? "The Pi’s IPv4 settings will move to br-rpi. Use the Ethernet port or " + join + ". "
        : join[0].toUpperCase() + join.slice(1) + " using the Wi-Fi password. ") + text;
    }
    setText("network-reconnect", text + " Choose Keep settings after reconnecting.");
  }

  function editOptions(resetMode = false, resetMethod = false) {
    const available = interfaceChoices();
    const adapter = available.find(item => item.name === fields.interface.value);
    const radios = available.filter(item => item.type === "wifi" && item.ap);
    const allowedModes = !adapter ? [] : adapter.type === "ethernet"
      ? (radios.length ? ["ethernet", "bridge"] : ["ethernet"])
      : (adapter.ap ? ["station", "ap"] : ["station"]);
    const currentMode = adapter?.controller === "br-rpi" && adapter.type === "ethernet" ? "bridge" : adapter?.mode;
    const initialMode = allowedModes.includes(currentMode) ? currentMode : allowedModes[0];
    selectOptions(fields.mode, allowedModes, modes, resetMode ? initialMode || "" : fields.mode.value);
    const mode = fields.mode.value;
    const bridge = mode === "bridge";
    const wireless = bridge || mode === "station" || mode === "ap";
    const initialRadio = radios.find(item => item.controller === "br-rpi") || radios[0];
    selectOptions(fields.wifi_interface, radios.map(item => item.name), {}, resetMode || resetMethod
      ? initialRadio?.name || "" : fields.wifi_interface.value);
    byId("network-radio").hidden = fields.wifi_interface.disabled = !bridge;
    byId("network-wireless").hidden = fields.ssid.disabled = fields.password.disabled = !wireless;
    byId("network-password-help").hidden = !wireless;

    const allowedMethods = mode === "ap" ? ["shared", "manual"]
      : mode === "station" ? ["auto", "manual"] : ["auto", "manual", "shared"];
    if (resetMode || resetMethod) {
      const source = bridge ? devices.find(item => item.type === "bridge" && item.name === "br-rpi") : adapter;
      const existing = source && mode === initialMode && (mode === currentMode || (mode === "ethernet" && !adapter.controller));
      const preferred = existing && allowedMethods.includes(source.ipv4_method) ? source.ipv4_method : allowedMethods[0];
      selectOptions(fields.ipv4_method, allowedMethods, methods, preferred);
      const primary = source?.configured_primary ?? (recovery ? "" : source?.addresses?.[0]);
      fields.address.value = existing && source.ipv4_method !== "auto" ? primary || "" : "";
      fields.gateway.value = existing && preferred === "manual" ? source.gateway || "" : "";
      const dns = preferred === "auto" ? source?.dns_override : source?.dns;
      fields.dns.value = existing && preferred !== "shared" ? (dns || []).join(", ") : "";
      const radio = bridge ? radios.find(item => item.name === fields.wifi_interface.value) : adapter;
      fields.ssid.value = wireless && (radio?.mode === mode || (bridge && radio?.mode === "ap")) ? radio.ssid || "" : "";
      fields.password.value = "";
    } else {
      selectOptions(fields.ipv4_method, allowedMethods, methods);
    }
    const method = fields.ipv4_method.value;
    byId("network-address").hidden = fields.address.disabled = method === "auto";
    const automaticSubnet = mode === "ap" && method === "shared";
    fields.address.required = !automaticSubnet && method !== "auto";
    setText("network-address-label", automaticSubnet ? "Pi IPv4 address / prefix (optional)" : "Pi IPv4 address / prefix");
    byId("network-routes").hidden = fields.dns.disabled = method === "shared";
    byId("network-gateway").hidden = fields.gateway.disabled = method !== "manual";
    setText("network-dns-label", method === "auto" ? "Override DHCP DNS servers (optional)" : "DNS servers (optional)");
    byId("network-shared-help").hidden = method !== "shared";
    if (method === "shared") {
      setText("network-shared-help", mode === "ap" ? "The Pi will assign addresses to Wi-Fi clients." :
        "The Pi will serve DHCP on the Ethernet LAN" + (bridge ? " and Wi-Fi access point" : "") +
        ". Use an isolated network where this Pi is the only DHCP server.");
    }
    byId("network-fields").disabled = applying || Boolean(pending);
    byId("network-apply").disabled = applying || Boolean(pending) || !adapter || !allowedModes.includes(mode)
      || (bridge && !radios.some(item => item.name === fields.wifi_interface.value));
    setText("network-apply", mode === "ap" ? "Start access point" : "Apply temporarily");
    byId("network-apply-hint").hidden = mode !== "ap";
    guidance();
    validate();
  }

  function countdown() {
    const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
    setText("network-timer", seconds ? "Automatic rollback in about " + seconds + " s." : "Checking the result of automatic rollback…");
    byId("network-confirm").disabled = byId("network-rollback").disabled = busy() || !seconds;
  }

  function update(status = {}, reload = false) {
    const next = status.pending || null;
    const loadActiveSettings = reload || !fields.interface.value || (pending && !next);
    byId("network-pending").hidden = !next;
    if (next) {
      const estimate = performance.now() + next.remaining_s * 1000;
      deadline = next.id === pending?.id ? Math.min(deadline, estimate) : estimate;
      countdown();
      if (!timer) timer = window.setInterval(countdown, 1000);
    } else {
      clearInterval(timer);
      timer = 0;
    }
    pending = next;
    devices = status.interfaces || [];
    recovery = status.recovery || null;
    showRecovery(recovery, devices);
    const key = JSON.stringify([devices, recovery?.interface, recovery?.address, recovery?.state]);
    if (key !== inventoryKey) {
      inventoryKey = key;
      byId("adapter-list").replaceChildren(...devices.map(adapter => adapterCard(adapter, recovery)));
      const aps = devices.filter(row => row.type === "wifi" && row.mode === "ap" && row.state === "connected");
      byId("network-active-aps").hidden = !aps.length;
      byId("network-active-aps").replaceChildren(...aps.map(ap => activeAccessPoint(ap, devices, recovery)));
    }
    const available = interfaceChoices();
    wifiControls(status);
    form.hidden = !status.available || !available.length;
    setText("network-message", status.message || (form.hidden ? "No managed Ethernet or Wi-Fi interface is available." : ""));
    const names = available.map(item => item.name);
    const selected = fields.interface.value;
    selectOptions(fields.interface, names, {}, loadActiveSettings && !names.includes(selected) ? "" : selected);
    editOptions(loadActiveSettings);
  }

  function nameservers() {
    return fields.dns.value.trim().split(/[\s,]+/).filter(Boolean);
  }

  function validate() {
    fields.ssid.setCustomValidity(!fields.ssid.disabled && fields.ssid.value &&
      (new TextEncoder().encode(fields.ssid.value).length > 32 || /[\p{C}\p{Z}]/u.test(fields.ssid.value.replaceAll(" ", "")))
      ? "Choose a network name of up to 32 UTF-8 bytes of printable text." : "");
    fields.password.setCustomValidity(!fields.password.disabled && fields.password.value &&
      !/^([ -~]{8,63}|[a-fA-F0-9]{64})$/.test(fields.password.value)
      ? "Use an 8–63 character ASCII passphrase or a 64-digit hexadecimal key." : "");
    fields.address.setCustomValidity(!fields.address.disabled && (fields.address.required || fields.address.value.trim())
      && !targetNetwork(fields.address.value)
      ? "Enter an IPv4 address with a prefix, for example 192.168.10.1/24." : "");
    fields.gateway.setCustomValidity(!fields.gateway.disabled && fields.gateway.value.trim() && !ipv4(fields.gateway.value.trim())
      ? "Enter an IPv4 gateway address." : "");
    fields.dns.setCustomValidity(!fields.dns.disabled && nameservers().some(value => !ipv4(value))
      ? "Enter IPv4 DNS addresses separated by commas or spaces." : "");
  }

  fields.interface.addEventListener("change", () => editOptions(true));
  byId("network-enable-wifi").addEventListener("click", () => {
    if (busy() || pending) return;
    act(async () => update(await api("network", {action: "enable_wifi"})), "Wi-Fi enabled.");
  });
  byId("network-setup-ap").addEventListener("click", () => {
    if (busy() || pending) return;
    const radio = apChoices().find(row => row.name === byId("network-ap-interface").value);
    if (!radio) return;
    fields.interface.value = radio.name;
    editOptions(true);
    fields.mode.value = "ap";
    editOptions(false, true);
    if (radio.mode !== "ap") {
      fields.ssid.value = "RPi-Camera";
      fields.ipv4_method.value = "shared";
      fields.address.value = suggestedApAddress(devices, radio.name, recovery);
    }
    editOptions();
    form.scrollIntoView({block: "start"});
    fields.ssid.focus({preventScroll: true});
  });
  fields.mode.addEventListener("change", () => editOptions(false, true));
  fields.ipv4_method.addEventListener("change", () => { editOptions(); validate(); });
  fields.wifi_interface.addEventListener("change", () => { fields.password.value = ""; editOptions(); });
  form.addEventListener("input", () => { validate(); guidance(); });

  form.addEventListener("submit", event => {
    event.preventDefault();
    validate();
    if (busy() || pending || byId("network-apply").disabled || !form.reportValidity()) return;
    const data = {action: "apply", interface: fields.interface.value, mode: fields.mode.value,
      ipv4_method: fields.ipv4_method.value, address: fields.address.disabled ? "" : fields.address.value.trim(),
      gateway: fields.gateway.disabled ? "" : fields.gateway.value.trim(), dns: fields.dns.disabled ? [] : nameservers()};
    if (!fields.ssid.disabled) {
      data.ssid = fields.ssid.value;
      data.password = fields.password.value;
    }
    if (!fields.wifi_interface.disabled) data.wifi_interface = fields.wifi_interface.value;
    act(async () => {
      applying = true;
      fields.password.value = "";
      editOptions();
      notice("Applying network settings. Reconnect using the instructions on the Network page.");
      try {
        update(await api("network", data));
        notice();
      } finally {
        applying = false;
        editOptions();
      }
    });
  });

  for (const action of ["confirm", "rollback"]) {
    byId("network-" + action).addEventListener("click", () => {
      if (!pending || busy()) return;
      const request = {action, id: pending.id};
      act(async () => update(await api("network", request), true), action === "confirm" ? "Network settings kept." : "Network settings rolled back.");
    });
  }

  return {update, reset() {
    form.reset();
    devices = [];
    inventoryKey = pending = null;
    [fields.interface, fields.mode, fields.wifi_interface, fields.ipv4_method].forEach(select => select.replaceChildren());
    update();
  }};
}
