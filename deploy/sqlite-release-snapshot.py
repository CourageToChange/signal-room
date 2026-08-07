#!/usr/bin/env python3
"""Create or restore a verified SQLite snapshot during a stopped-service release switch."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


class SnapshotError(RuntimeError):
    pass


def _regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotError(f"{label} does not exist: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise SnapshotError(f"{label} is not a regular file: {path}")


def _connect_read_only(path: Path) -> sqlite3.Connection:
    encoded = quote(path.resolve(strict=True).as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _checks(connection: sqlite3.Connection) -> None:
    for pragma in ("quick_check", "integrity_check"):
        rows = connection.execute(f"PRAGMA {pragma}").fetchall()
        if rows != [("ok",)]:
            raise SnapshotError(f"SQLite {pragma} failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SnapshotError("SQLite foreign_key_check failed")


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup(source: Path, destination: Path) -> None:
    _regular_file(source, label="source database")
    if destination.exists() or destination.is_symlink():
        raise SnapshotError(f"snapshot destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.partial.{os.getpid()}")
    if partial.exists() or partial.is_symlink():
        raise SnapshotError(f"snapshot partial path already exists: {partial}")
    source_connection = _connect_read_only(source)
    try:
        target_connection = sqlite3.connect(partial)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
            _checks(target_connection)
        finally:
            target_connection.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source_connection.close()
    os.chmod(partial, 0o600)
    _fsync_file(partial)
    os.replace(partial, destination)
    _fsync_directory(destination.parent)


def restore(snapshot: Path, destination: Path) -> None:
    if os.name != "posix":
        raise SnapshotError("database restore requires a POSIX host")
    import grp
    import pwd

    _regular_file(snapshot, label="rollback snapshot")
    with closing(_connect_read_only(snapshot)) as source_connection:
        _checks(source_connection)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise SnapshotError(f"database destination is unsafe: {destination}")
    try:
        parent_metadata = destination.parent.lstat()
    except FileNotFoundError as error:
        raise SnapshotError("database destination directory does not exist") from error
    if not stat.S_ISDIR(parent_metadata.st_mode) or destination.parent.is_symlink():
        raise SnapshotError("database destination directory is unsafe")
    partial = destination.with_name(f".{destination.name}.rollback.{os.getpid()}")
    if partial.exists() or partial.is_symlink():
        raise SnapshotError(f"rollback partial path already exists: {partial}")
    try:
        shutil.copyfile(snapshot, partial)
        with closing(_connect_read_only(partial)) as copied_connection:
            _checks(copied_connection)
        os.chmod(partial, 0o600)
        try:
            owner = pwd.getpwnam("signal-room-core").pw_uid
            group = grp.getgrnam("signal-room-core").gr_gid
        except KeyError as error:
            raise SnapshotError("signal-room-core service identity is unavailable") from error
        os.chown(partial, owner, group)
        _fsync_file(partial)
        for suffix in ("-wal", "-shm"):
            candidate = Path(f"{destination}{suffix}")
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                raise SnapshotError(f"database sidecar is unsafe: {candidate}")
            candidate.unlink(missing_ok=True)
        os.replace(partial, destination)
        _fsync_file(destination)
        _fsync_directory(destination.parent)
        with closing(_connect_read_only(destination)) as restored_connection:
            _checks(restored_connection)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.operation == "backup":
            backup(arguments.source, arguments.destination)
        else:
            restore(arguments.source, arguments.destination)
    except (OSError, sqlite3.Error, SnapshotError) as error:
        print(f"SQLite release snapshot failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
