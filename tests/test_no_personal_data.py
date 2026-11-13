"""Tracked files must not contain personal names, local user paths or the old workspace name."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BS = chr(92)
# Bracketed letters keep this file from matching its own pattern.
PATTERN = re.compile(
    r"(?i)r[o]ok|vio[l]et|janosick[a]|osic[j]a|(?<!third-)part[y]|C:" + re.escape(BS) + "Users" + re.escape(BS) + r"[^" + re.escape(BS) + r"{]+" + re.escape(BS))
# Narrow allow-list for legitimate third-party words: (relative path, matched text).
ALLOW = set()


def _tracked():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_no_personal_data_in_tracked_files():
    hits = []
    for rel in _tracked():
        path = ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            for m in PATTERN.finditer(line):
                if (rel, m.group(0)) not in ALLOW:
                    hits.append(f"{rel}:{n}: {m.group(0)!r} in {line.strip()[:100]}")
    assert not hits, "personal data found:\n" + "\n".join(hits)
