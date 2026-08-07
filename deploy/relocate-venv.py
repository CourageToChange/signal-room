#!/usr/bin/env python3
"""Relocate generated virtual-environment entrypoint shebangs safely."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def relocate_entrypoints(stage: Path, target: Path) -> list[Path]:
    stage = stage.resolve(strict=True)
    target = target.resolve(strict=False)
    if not stage.is_absolute() or not target.is_absolute() or stage == target:
        raise ValueError("stage and target must be distinct absolute paths")

    bin_directory = (stage / ".venv" / "bin").resolve(strict=True)
    old_prefix = b"#!" + os.fsencode(stage)
    new_prefix = b"#!" + os.fsencode(target)
    rewritten: list[Path] = []

    for candidate in sorted(bin_directory.iterdir()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        content = candidate.read_bytes()
        first_line, separator, remainder = content.partition(b"\n")
        if not first_line.startswith(old_prefix):
            continue
        suffix = first_line[len(old_prefix) :]
        if not suffix.startswith((b"/", b"\\")):
            continue
        candidate.write_bytes(new_prefix + suffix + separator + remainder)
        rewritten.append(candidate)

    if bin_directory / "signal-room" not in rewritten:
        raise RuntimeError("signal-room entrypoint did not contain the staging path")
    return rewritten


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: relocate-venv.py <stage> <target>", file=sys.stderr)
        return 64
    rewritten = relocate_entrypoints(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"relocated {len(rewritten)} virtual-environment entrypoints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
