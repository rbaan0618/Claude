"""Default configuration for the softphone application."""

import os
import json

DEFAULT_CONFIG = {
    "sip": {
        "enabled": True,
        "server": "",
        "port": 5060,
        "local_port": 5060,
        "rport": True,
        "username": "",
        "password": "",
        "transport": "UDP",
        "display_name": "",
    },
    "audio": {
        "input_device": "",
        "output_device": "",
        "ring_device": "",
    },
    "contacts": {
        "entries": [],
    },
    "blf": {
        "entries": [],
    },
    "gui": {
        "always_on_top": False,
        "start_minimized": False,
        "theme": "dark",
    },
}

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".softphone")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_FILE = os.path.join(CONFIG_DIR, "call_history.db")


def load_config():
    """Load configuration from file, creating defaults if needed."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        config = DEFAULT_CONFIG.copy()
        _deep_update(config, saved)
        return config
    return DEFAULT_CONFIG.copy()


def save_config(config):
    """Save configuration to file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def _deep_update(base, update):
    for key, value in update.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
