"""
VolSteady — Configuration Manager
Persists settings to %APPDATA%\\VolSteady\\settings.json.
Thread-safe reads/writes using a lock.
"""

import json
import os
import threading
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Default settings
DEFAULTS = {
    "enabled": True,
    "strength": 65,
    "advanced": {
        "compressor_threshold_db": -25.0,
        "compressor_ratio": 4.0,
        "compressor_attack_ms": 10.0,
        "compressor_release_ms": 200.0,
        "compressor_knee_db": 6.0,
        "makeup_gain_db": 0.0,
        "gate_threshold_db": -50.0,
        "gate_attack_ms": 0.5,
        "gate_release_ms": 50.0,
        "gate_hold_ms": 20.0,
        "limiter_ceiling_db": -1.0,
        "limiter_release_ms": 50.0,
    },
    "audio_device": "default",
    "start_minimized": True,
    "auto_start": False,
    "use_advanced": False,
}


def _get_config_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    config_dir = Path(appdata) / "VolSteady"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "settings.json"


class Config:
    """Thread-safe JSON configuration manager."""

    def __init__(self):
        self._lock = threading.Lock()
        self._path = _get_config_path()
        self._data = self._load()

    def _load(self) -> dict:
        """Load from disk, falling back to defaults for missing keys."""
        data = {}
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load config: {e}. Using defaults.")

        # Deep merge defaults
        merged = dict(DEFAULTS)
        for k, v in data.items():
            if k == "advanced" and isinstance(v, dict):
                merged["advanced"] = {**DEFAULTS["advanced"], **v}
            else:
                merged[k] = v
        return merged

    def save(self):
        """Persist current settings to disk."""
        with self._lock:
            try:
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2)
            except Exception as e:
                logger.error(f"Could not save config: {e}")

    # ─── Getters ─────────────────────────────────────────────

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def get_advanced(self) -> dict:
        with self._lock:
            return dict(self._data.get("advanced", DEFAULTS["advanced"]))

    def get_advanced_key(self, key: str, default=None):
        with self._lock:
            return self._data.get("advanced", {}).get(key, default)

    # ─── Setters ─────────────────────────────────────────────

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value
        self.save()

    def set_advanced(self, key: str, value):
        with self._lock:
            self._data.setdefault("advanced", {})[key] = value
        self.save()

    def set_advanced_bulk(self, updates: dict):
        with self._lock:
            adv = self._data.setdefault("advanced", {})
            adv.update(updates)
        self.save()

    # ─── Convenience properties ───────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self.get("enabled", True))

    @enabled.setter
    def enabled(self, v: bool):
        self.set("enabled", v)

    @property
    def strength(self) -> float:
        return float(self.get("strength", 65))

    @strength.setter
    def strength(self, v: float):
        self.set("strength", float(v))

    @property
    def start_minimized(self) -> bool:
        return bool(self.get("start_minimized", True))

    @property
    def use_advanced(self) -> bool:
        return bool(self.get("use_advanced", False))

    @use_advanced.setter
    def use_advanced(self, v: bool):
        self.set("use_advanced", v)

    @property
    def audio_device(self) -> str:
        return str(self.get("audio_device", "default"))

    @audio_device.setter
    def audio_device(self, v: str):
        self.set("audio_device", v)
