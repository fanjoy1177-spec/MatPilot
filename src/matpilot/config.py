"""Local operator configuration. Model calls cannot change execution commands."""
from __future__ import annotations

import json
import os
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
        settings = cls(**data)
        if type(settings.real_compute_enabled) is not bool or not all(isinstance(x, dict) for x in (settings.templates, settings.diffcsp, settings.model_identities)):
            raise ValueError("Invalid operator policy or model registry")
        if settings.backend not in {"demo", "native", "wsl"}:
            raise ValueError("backend must be demo, native or wsl")
        if not settings.command or not all(isinstance(x, str) and x for x in settings.command):
            raise ValueError("command must be a nonempty argument array")
        if not 1 <= settings.max_samples <= 1024 or settings.batch_timeout_seconds < 1:
            raise ValueError("invalid execution limits")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def snapshot(self) -> dict:
        from dataclasses import asdict
        value = asdict(self)
        value["data_dir"] = str(self.data_dir)
        return value
