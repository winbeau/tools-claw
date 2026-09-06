import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("BEAUCLAW_CONFIG_DIR", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "beauclaw"))


def data_dir() -> Path:
    return Path(os.environ.get("BEAUCLAW_DATA_DIR", Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "beauclaw"))
