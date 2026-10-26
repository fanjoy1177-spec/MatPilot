"""Local operator configuration. Model calls cannot change execution commands."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("MATPILOT_DATA_DIR", "matpilot-data")).resolve())
    backend: str = "demo"
    command: list[str] = field(default_factory=lambda: ["mattergen-generate"])
    cwd: str | None = None
    batch_timeout_seconds: int = 1800
    max_samples: int = 32
    seed_supported: bool = True
    wsl_distribution: str = "Ubuntu"
    wsl_user: str = ""
    checkpoint_paths: dict[str, str] = field(default_factory=dict)
    config_path: str | None = None
    real_compute_enabled: bool = False
    templates: dict = field(default_factory=dict)
    diffcsp: dict = field(default_factory=dict)
    model_identities: dict = field(default_factory=dict)
    candidate_collections: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The v1.1 first-release cap also applies to restored worker snapshots.
        if type(self.max_samples) is not int or not 1 <= self.max_samples <= 32:
            raise ValueError("max_samples must be an integer from 1 to 32")
        if not isinstance(self.candidate_collections, dict):
            raise ValueError("candidate_collections must be an operator-controlled registry")
        for key, entry in self.candidate_collections.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key)
                    or not isinstance(entry, dict) or set(entry) != {"manifest_path", "manifest_sha256"}
                    or not isinstance(entry["manifest_path"], str) or not entry["manifest_path"].strip()
                    or not isinstance(entry["manifest_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["manifest_sha256"])):
                raise ValueError("Candidate collections need an ID, manifest_path and lowercase SHA256")

    @classmethod
    def load(cls, filename: str | None = None) -> "Settings":
        filename = filename or os.environ.get("MATPILOT_CONFIG")
        data = {}
        if filename:
            location = Path(filename).expanduser().resolve()
            data = json.loads(location.read_text(encoding="utf-8"))
            data["config_path"] = str(location)
            if "data_dir" in data and not Path(data["data_dir"]).is_absolute():
                data["data_dir"] = str(location.parent / data["data_dir"])
        if "data_dir" in data:
            data["data_dir"] = Path(data["data_dir"]).expanduser().resolve()
        for entry in data.get("templates", {}).values():
            if filename and isinstance(entry, dict) and "path" in entry and not Path(entry["path"]).is_absolute():
                entry["path"] = str(location.parent / entry["path"])
        collections = data.get("candidate_collections", {})
        if isinstance(collections, dict):
            for entry in collections.values():
                if (filename and isinstance(entry, dict) and isinstance(entry.get("manifest_path"), str)
                        and not Path(entry["manifest_path"]).is_absolute()):
                    entry["manifest_path"] = str((location.parent / entry["manifest_path"]).resolve())
        settings = cls(**data)
        if type(settings.real_compute_enabled) is not bool or not all(isinstance(x, dict) for x in (settings.templates, settings.diffcsp, settings.model_identities)):
            raise ValueError("Invalid operator policy or model registry")
        if settings.backend not in {"demo", "native", "wsl"}:
            raise ValueError("backend must be demo, native or wsl")
        if not settings.command or not all(isinstance(x, str) and x for x in settings.command):
            raise ValueError("command must be a nonempty argument array")
        if settings.batch_timeout_seconds < 1:
            raise ValueError("invalid execution limits")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def snapshot(self) -> dict:
        from dataclasses import asdict
        value = asdict(self)
        value["data_dir"] = str(self.data_dir)
        return value
