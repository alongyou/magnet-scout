"""Shared locations for source defaults and mutable runtime data."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('MAGNET_SCOUT_DATA_DIR', str(ROOT))).expanduser().resolve()
