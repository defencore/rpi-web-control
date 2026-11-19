export const byId = id => document.getElementById(id);

export function setText(id, value) {
  const node = byId(id);
  if (node.textContent !== String(value)) node.textContent = value;
}

export function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function button(text, onClick, className = "button secondary small") {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

export async function copyCommand(code, notice) {
  try {
    await navigator.clipboard.writeText(code.textContent);
    notice("Command copied.");
  } catch {
    const range = document.createRange();
    range.selectNodeContents(code);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    notice("Command selected. Use your browser’s Copy action.");
  }
}

export function fact(label, value) {
  const group = element("div");
  group.append(element("dt", label), element("dd", value));
  return group;
}

const sizes = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});

export function capacity(bytes) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const i = Math.min(units.length - 1, Math.max(0, Math.floor(Math.log2(bytes || 1) / 10)));
  return sizes.format(bytes / (1024 ** i)) + " " + units[i];
}

export function destinationLabel(paths) {
  for (const [medium, label] of [["nvme", "PCIe NVMe"], ["usb", "USB drive"]]) {
    const root = paths[medium];
    if (root && (paths.current === root || paths.current.startsWith(root + "/"))) return label;
  }
  return paths.current === paths.default ? "Default recording directory" : "Custom directory";
}
