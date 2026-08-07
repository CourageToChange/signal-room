#!/usr/bin/env python3
"""Bound systemd startup until the trusted core Unix socket is ready."""

from __future__ import annotations

import argparse
import stat
import sys
import time
from pathlib import Path


class CoreReadinessError(RuntimeError):
    pass


def wait_for_sockets(
    paths: tuple[Path, ...], timeout_seconds: float, interval_seconds: float
) -> None:
    if not paths:
        raise CoreReadinessError("at least one core socket is required")
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise CoreReadinessError("readiness timing must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        ready = 0
        for path in paths:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise CoreReadinessError("core socket path must not be a symlink")
            if not stat.S_ISSOCK(metadata.st_mode):
                raise CoreReadinessError("core socket path is not a Unix socket")
            ready += 1
        if ready == len(paths):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CoreReadinessError("core socket did not become ready before the deadline")
        time.sleep(min(interval_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sockets", nargs="+", type=Path)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=0.05)
    arguments = parser.parse_args()
    try:
        wait_for_sockets(tuple(arguments.sockets), arguments.timeout, arguments.interval)
    except (OSError, CoreReadinessError) as error:
        print(f"Signal Room core readiness failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
