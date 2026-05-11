import yaml
from pathlib import Path

_config = None

def load(path: str = "config.yaml") -> dict:
    global _config
    if _config is None:
        with open(Path(path)) as f:
            _config = yaml.safe_load(f)
    return _config

def get(key: str, default=None):
    cfg = load()
    keys = key.split(".")
    val = cfg
    for k in keys:
        val = val.get(k, {}) if isinstance(val, dict) else default
    return val if val != {} else default
