"""Run the whole suite against a generic test config in a temporary bridge home (never the developer's own)."""
import os
import shutil
import tempfile
from pathlib import Path

_HOME = Path(tempfile.mkdtemp(prefix="tb-test-home-"))
shutil.copy(Path(__file__).parent / "fixtures" / "test-config.toml", _HOME / "config.toml")
os.environ["TELEGRAM_BRIDGE_HOME"] = str(_HOME)
