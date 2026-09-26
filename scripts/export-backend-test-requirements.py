#!/usr/bin/env python3
"""Split backend/uv.lock into hash-pinned requirement files for the test job.

    python scripts/export-backend-test-requirements.py OUT_DIR

Writes ``pypi.txt`` (everything installable from PyPI) and ``testpypi.txt``
(only sqlbot-xpack, which is published on test.pypi.org). Hashes are kept, so a
same-named package on another index can never be installed. torch and its GPU
companions are dropped: the test suite does not load embedding models.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
SKIPPED = re.compile(r"^(torch|triton|nvidia-[a-z0-9-]+)(==|\s|;)", re.IGNORECASE)
TEST_PYPI = re.compile(r"^sqlbot-xpack(==|\s|;)", re.IGNORECASE)


def requirement_blocks(text: str) -> list[str]:
    """One block per requirement: the pin line plus its ``--hash`` continuation lines."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and blocks:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return ["\n".join(block) for block in blocks]


def main(out_dir: str) -> int:
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-emit-project", "--all-groups"],
        cwd=BACKEND, check=True, capture_output=True, text=True,
    ).stdout
    pypi, test_pypi = [], []
    for block in requirement_blocks(exported):
        if SKIPPED.match(block):
            continue
        (test_pypi if TEST_PYPI.match(block) else pypi).append(block)
    if len(test_pypi) != 1:
        raise SystemExit("expected exactly one sqlbot-xpack requirement in the lock")
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "pypi.txt").write_text("\n".join(pypi) + "\n", encoding="utf-8")
    (target / "testpypi.txt").write_text("\n".join(test_pypi) + "\n", encoding="utf-8")
    print(f"{len(pypi)} PyPI requirements, {len(test_pypi)} test.pypi requirement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
