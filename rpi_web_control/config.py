"""Persistent settings and recording-directory policy."""

import json
import os
import tempfile
from pathlib import Path

from .camera import DEFAULTS as DEFAULT_CAMERA, validate as validate_camera
from .storage import resolve_directory
from .triggers import validate_rules


class Settings:
    def __init__(self, state_dir, roots, *, managed_root=None, managed_roots=()):
        self.path = Path(state_dir) / "config.json"
        self.managed_roots = ((managed_root,) if managed_root is not None else ()) + tuple(managed_roots)
        self.roots = tuple(Path(root) if Path(root) in self.managed_roots else Path(root).resolve() for root in roots)
        if not self.roots:
            raise ValueError("Configure at least one recording root.")
        self.roots += tuple(root for root in self.managed_roots if root not in self.roots)
        self.storage_identity = None
        self.defaults = {
            "camera": dict(DEFAULT_CAMERA),
            "storage": {"directory": str(self.roots[0])},
            "triggers": [],
        }

    def directory(self, value):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError("Select an absolute recording directory.")
        try:
            path = resolve_directory(Path(value), self.managed_roots)
        except (OSError, RuntimeError) as exc:
            raise ValueError("The recording directory cannot be resolved.") from exc
        root = next((r for r in self.roots if path.is_relative_to(r)), None)
        if root is None:
            raise ValueError("The recording directory must be inside an allowed storage root.")
        return path

    def merge(self, base, patch, *, restoring=False):
        if not isinstance(patch, dict) or not patch or set(patch) - self.defaults.keys():
            raise ValueError("Specify camera, storage or triggers.")
        result = dict(base)
        if "camera" in patch:
            if not isinstance(patch["camera"], dict):
                raise ValueError("Camera settings must be an object.")
            result["camera"] = {**base["camera"], **patch["camera"]}
            validate_camera(result["camera"])
        if "storage" in patch:
            storage = patch["storage"]
            if not isinstance(storage, dict) or set(storage) != {"directory"}:
                raise ValueError("Storage settings require a directory.")
            value = storage["directory"]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("Select an absolute recording directory.")
            # A retired root must not prevent the operator from opening the console
            # and selecting another volume. Every recording rechecks the live policy.
            result["storage"] = {"directory": value if restoring else str(self.directory(value))}
        if "triggers" in patch:
            result["triggers"] = validate_rules(patch["triggers"])
        return result

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.defaults.copy()
        if not isinstance(value, dict):
            raise ValueError("The saved configuration must be an object.")
        self.storage_identity = value.pop("_storage_identity", None)
        if self.storage_identity is not None and not isinstance(self.storage_identity, dict):
            raise ValueError("Invalid saved storage identity.")
        return self.merge(self.defaults, value, restoring=True)

    def save(self, value, *, storage_identity):
        on_disk = dict(value)
        if storage_identity is not None:
            on_disk["_storage_identity"] = storage_identity
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(on_disk, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, self.path)
            self.storage_identity = storage_identity
        finally:
            temporary.unlink(missing_ok=True)
