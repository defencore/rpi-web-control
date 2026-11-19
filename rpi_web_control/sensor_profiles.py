"""Built-in CSI profiles and administrator-provisioned overlay definitions."""

import json
import re

IMX577_SPEC = "imx577,4lane,link-frequency=750000000"
# These IDs and definitions were reserved when vendor catalogs were introduced.
LEGACY_PROFILES = {
    "auto": ("Firmware auto-detection", None),
    "ov5647": ("Camera Module 1 (OV5647)", "ov5647"),
    "imx219": ("Camera Module 2 (IMX219 / IMX355)", "imx219"),
    "imx708": ("Camera Module 3 (IMX708)", "imx708"),
    "imx477": ("High Quality Camera (IMX477)", "imx477"),
    "imx296": ("Global Shutter Camera (IMX296)", "imx296"),
    "ov9281": ("OV9281", "ov9281"),
    "imx378": ("IMX378", "imx378"),
    "imx290-37mhz": ("IMX290/327 (imx290 overlay, 37.125 MHz clock)", "imx290,clock-frequency=37125000"),
    "imx290-74mhz": ("IMX290/327 (imx290 overlay, 74.25 MHz clock)", "imx290,clock-frequency=74250000"),
}

# Append new presets in release order. Earlier presets keep their assigned fallback IDs.
ADDITIONAL_PROFILES = {
    "imx577": ("IMX577 (4 lanes, 750 MHz link)", IMX577_SPEC),
    "imx500": ("Raspberry Pi AI Camera (IMX500)", "imx500"),
    "imx519": ("Arducam 16MP IMX519 (autofocus)", "imx519"),
    "imx519-fixed": ("Arducam 16MP IMX519 (fixed focus)", "imx519,vcm=off"),
    "arducam-64mp": ("Arducam 64MP Hawkeye", "arducam-64mp"),
    "ov64a40-360mhz": ("Arducam 64MP OwlSight (360 MHz link)", "ov64a40,link-frequency=360000000"),
    "ov64a40-456mhz": ("Arducam 64MP OwlSight (456 MHz link)", "ov64a40,link-frequency=456000000"),
    "arducam-pivariety": ("Arducam Pivariety", "arducam-pivariety"),
    "imx258": ("Sony IMX258", "imx258"),
    "imx283": ("Sony IMX283 (4 lanes)", "imx283"),
    "imx327": ("Sony IMX327 (default 37.125 MHz clock)", "imx327"),
    "imx327-74mhz": ("Sony IMX327 (74.25 MHz clock)", "imx327,clock-frequency=74250000"),
    "imx335": ("Sony IMX335 (overlay defaults)", "imx335"),
    "imx335-4lane": ("Sony IMX335 (4 lanes)", "imx335,4lane"),
    "imx415": ("Sony IMX415 (overlay defaults)", "imx415"),
    "imx462": ("Sony IMX462 (default 37.125 MHz clock)", "imx462"),
    "imx462-74mhz": ("Sony IMX462 (74.25 MHz clock)", "imx462,clock-frequency=74250000"),
    "ov2311": ("OmniVision OV2311", "ov2311"),
    "ov7251": ("OmniVision OV7251", "ov7251"),
}

ID = re.compile(r"[a-z][a-z0-9-]{0,47}")
OVERLAY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}")
PARAM = re.compile(r"([a-zA-Z0-9][a-zA-Z0-9_-]*)(?:=[a-zA-Z0-9_+.:-]+)?")


def overlay_definition(spec):
    overlay, *params = spec.split(",")
    return (overlay, *sorted(params))


def _preset_id(preferred, profiles):
    if preferred not in profiles:
        return preferred
    # IMX577 shipped before the general fallback naming convention.
    base = "imx577-4lane-750mhz" if preferred == "imx577" else preferred[:40] + "-builtin"
    name = base
    number = 2
    while name in profiles:
        suffix = f"-{number}"
        name = base[:48 - len(suffix)] + suffix
        number += 1
    return name


def _catalog(entries):
    result = dict(LEGACY_PROFILES)
    imx577_definition = overlay_definition(IMX577_SPEC)
    definitions = {overlay_definition(spec) for _, spec in result.values() if spec}
    for row in entries:
        if not isinstance(row, dict) or set(row) - {"id", "label", "overlay", "params"}:
            raise ValueError("Each entry accepts id, label, overlay and optional params.")
        name, label, overlay = (row.get(k) for k in ("id", "label", "overlay"))
        if not isinstance(name, str) or not ID.fullmatch(name) or name in result or name == "restore":
            raise ValueError("Expected a unique lowercase profile ID distinct from the reserved IDs and restore.")
        if (not isinstance(label, str) or not label.isascii() or not label.isprintable()
                or not label.strip() or len(label) > 80):
            raise ValueError(f"{name}: label must be 1 to 80 printable ASCII characters.")
        if not isinstance(overlay, str) or not OVERLAY.fullmatch(overlay):
            raise ValueError(f"{name}: overlay must be a bare firmware overlay name.")
        params = row.get("params", [])
        if not isinstance(params, list) or len(params) > 16:
            raise ValueError(f"{name}: params must be a list with up to 16 entries.")
        keys = {"cam0", "cam1"}
        for token in params:
            match = PARAM.fullmatch(token) if isinstance(token, str) else None
            if not match or match[1] in keys:
                raise ValueError(f"{name}: use unique parameter names, flags or key=value tokens; the CSI port is separate.")
            keys.add(match[1])
        spec = ",".join([overlay, *params])
        if len("dtoverlay=" + spec + ",cam0") > 98:
            raise ValueError(f"{name}: dtoverlay with its connector would exceed 98 characters.")
        definition = overlay_definition(spec)
        if definition in definitions:
            raise ValueError(f"{name}: duplicate overlay and parameters.")
        definitions.add(definition)
        result[name] = (label, IMX577_SPEC if definition == imx577_definition else spec)
    for preferred, (label, spec) in ADDITIONAL_PROFILES.items():
        definition = overlay_definition(spec)
        if definition not in definitions:
            result[_preset_id(preferred, result)] = (label, spec)
            definitions.add(definition)
    return result


PROFILES = _catalog([])


def parse_profiles(raw):
    entries = json.loads(raw)
    if not isinstance(entries, list) or len(entries) > 64:
        raise ValueError("Expected a JSON list with up to 64 sensor profiles.")
    return _catalog(entries)
