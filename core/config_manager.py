import json
import os
import shutil
import threading
from datetime import datetime
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
SECRETS_PATH = os.path.join(BASE_DIR, "config", "secrets.json")


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into nested dicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class ConfigManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._config: dict = {}
        self._secrets: dict = {}
        self._plugin_change_callbacks: dict[str, list] = {}
        self.load()

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self):
        with self._lock:
            self._config = self._load_json(CONFIG_PATH)
            self._secrets = self._load_json(SECRETS_PATH)

    def _load_json(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] Warning: could not read {path}: {e}")
            return {}

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get(self, *keys, default=None) -> Any:
        """Dot-path get: get('display', 'brightness') -> config['display']['brightness']"""
        with self._lock:
            node = self._config
            for k in keys:
                if not isinstance(node, dict) or k not in node:
                    return default
                node = node[k]
            return node

    def get_plugin_config(self, plugin_id: str) -> dict:
        with self._lock:
            return dict(self._config.get("plugins", {}).get(plugin_id, {}))

    def get_secret(self, key: str, default=None) -> Any:
        with self._lock:
            return self._secrets.get(key, default)

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._config)

    def get_all_secrets_masked(self) -> dict:
        """Return secrets dict with non-empty values masked for web display."""
        with self._lock:
            masked = {}
            for k, v in self._secrets.items():
                masked[k] = "********" if v else ""
            return masked

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_config(self, new_config: dict):
        """Replace entire config atomically. Caller must supply complete config."""
        with self._lock:
            self._atomic_write(CONFIG_PATH, new_config)
            self._config = new_config

    def save_config_section(self, *keys, value: Any):
        """Set a nested key and save. save_config_section('display', 'brightness', value=80)"""
        with self._lock:
            node = self._config
            for k in keys[:-1]:
                node = node.setdefault(k, {})
            node[keys[-1]] = value
            self._atomic_write(CONFIG_PATH, self._config)

    def save_plugin_config(self, plugin_id: str, data: dict):
        with self._lock:
            self._config.setdefault("plugins", {})[plugin_id] = data
            self._atomic_write(CONFIG_PATH, self._config)
        self._fire_plugin_change(plugin_id, data)

    def save_secrets(self, new_secrets: dict):
        with self._lock:
            # Preserve existing non-empty values for masked (blank) inputs
            for k, v in new_secrets.items():
                if v and v != "********":
                    self._secrets[k] = v
            self._atomic_write(SECRETS_PATH, self._secrets)

    def _atomic_write(self, path: str, data: dict):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def create_backup(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(BASE_DIR, "config", "backups")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f"config_{stamp}.json")
        with self._lock:
            shutil.copy2(CONFIG_PATH, backup_path)
        return backup_path

    def list_backups(self) -> list[str]:
        backup_dir = os.path.join(BASE_DIR, "config", "backups")
        if not os.path.exists(backup_dir):
            return []
        files = sorted(
            [f for f in os.listdir(backup_dir) if f.endswith(".json")],
            reverse=True,
        )
        return files

    def restore_backup(self, filename: str) -> bool:
        backup_dir = os.path.join(BASE_DIR, "config", "backups")
        src = os.path.join(backup_dir, filename)
        # Guard against path traversal
        if not os.path.commonpath([src, backup_dir]) == backup_dir:
            return False
        if not os.path.exists(src):
            return False
        with self._lock:
            shutil.copy2(src, CONFIG_PATH)
            self._config = self._load_json(CONFIG_PATH)
        return True

    # ------------------------------------------------------------------
    # Plugin change callbacks
    # ------------------------------------------------------------------

    def register_plugin_change(self, plugin_id: str, callback):
        self._plugin_change_callbacks.setdefault(plugin_id, []).append(callback)

    def _fire_plugin_change(self, plugin_id: str, data: dict):
        for cb in self._plugin_change_callbacks.get(plugin_id, []):
            try:
                cb(data)
            except Exception as e:
                print(f"[config] Plugin change callback error ({plugin_id}): {e}")


# Module-level singleton
_instance: ConfigManager | None = None


def get_config() -> ConfigManager:
    global _instance
    if _instance is None:
        _instance = ConfigManager()
    return _instance
