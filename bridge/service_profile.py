"""One default account and service profile per operating-system user."""
import os
import sys
from pathlib import Path


def default_config():
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path.home() / ".local/share"
    return root / "Biandengbao/config.json"


def same_config(one, two):
    return os.path.normcase(str(Path(one).resolve())) == os.path.normcase(str(Path(two).resolve()))
