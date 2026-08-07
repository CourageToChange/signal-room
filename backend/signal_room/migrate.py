from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2

import aiosqlite

CURRENT_SCHEMA_VERSION = 4
MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    root = directory or Path(__file__).with_name("migrations")
    migrations: list[Migration] = []
    for path in sorted(root.glob("*.sql")):
        match = MIGRATION_PATTERN.fullmatch(path.name)
        if not match:
            raise MigrationError(f"invalid migration filename: {path.name}")
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=path.name,
                sql=raw.decode("utf-8"),
                checksum=hashlib.sha256(raw).hexdigest(),
            )
        )
    versions = [item.version for item in migrations]
    if versions != list(range(1, CURRENT_SCHEMA_VERSION + 1)):
        raise MigrationError(f"migration sequence must be 1..{CURRENT_SCHEMA_VERSION}")
    return migrations


async def database_checks(connection: aiosqlite.Connection, *, full: bool) -> None:
    check_name = "integrity_check" if full else "quick_check"
    rows = await (await connection.execute(f"PRAGMA {check_name}")).fetchall()
    if [str(row[0]).lower() for row in rows] != ["ok"]:
        raise MigrationError(f"SQLite {check_name} failed")
    foreign_keys = await (await connection.execute("PRAGMA foreign_key_check")).fetchall()
    if foreign_keys:
        raise MigrationError("SQLite foreign_key_check failed")


def _fsync_file(path: Path) -> None:
    # Windows rejects fsync on a descriptor opened without write access.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def verified_backup(
    source: aiosqlite.Connection,
    destination: Path,
    *,
    full_check: bool = True,
) -> Path:
    """Create, verify, fsync, and atomically publish an SQLite backup."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        target = await aiosqlite.connect(partial)
        try:
            await source.backup(target)
            await target.commit()
            await database_checks(target, full=full_check)
        finally:
            await target.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _fsync_file(partial)
    partial.replace(destination)
    _fsync_directory(destination.parent)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = destination.with_suffix(destination.suffix + ".sha256")
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="ascii", newline="\n")
    _fsync_file(checksum)
    _fsync_directory(destination.parent)
    return destination


async def applied_schema_version(path: Path) -> int:
    if not path.is_file():
        return 0
    connection = await aiosqlite.connect(path)
    try:
        row = await (
            await connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            )
        ).fetchone()
        if row is None:
            return 0
        version_row = await (
            await connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        ).fetchone()
        return int(version_row[0] if version_row else 0)
    finally:
        await connection.close()


async def migrate_database(path: Path, backup_directory: Path | None = None) -> int:
    """Apply every pending migration with future-schema and rollback protection."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.is_file() and path.stat().st_size > 0
    connection = await aiosqlite.connect(path)
    backup: Path | None = None
    closed = False
    try:
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=5000")
        tracking_table = await (
            await connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            )
        ).fetchone()
        rows = (
            await (
                await connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                )
            ).fetchall()
            if tracking_table
            else []
        )
        applied = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
        if applied and max(applied) > CURRENT_SCHEMA_VERSION:
            raise MigrationError("database schema is newer than this release")

        migrations = discover_migrations()
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded and recorded != (migration.name, migration.checksum):
                raise MigrationError(f"migration checksum mismatch: {migration.name}")

        pending = [item for item in migrations if item.version not in applied]
        if not pending:
            await database_checks(connection, full=False)
            return CURRENT_SCHEMA_VERSION

        if existed:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            directory = backup_directory or path.parent / "pre-migration"
            backup = directory / f"{path.stem}.schema-{max(applied, default=0)}.{timestamp}.sqlite3"
            await verified_backup(connection, backup)

        if tracking_table is None:
            await connection.execute(
                """
                CREATE TABLE schema_migrations (
                  version INTEGER PRIMARY KEY,
                  name TEXT NOT NULL UNIQUE,
                  checksum TEXT NOT NULL,
                  applied_at TEXT NOT NULL
                )
                """
            )
            await connection.commit()

        try:
            for migration in pending:
                applied_at = datetime.now(UTC).isoformat()
                # Migration SQL is packaged and checksum-verified; filename metadata is
                # restricted by MIGRATION_PATTERN before this atomic script is built.
                script = (
                    "BEGIN IMMEDIATE;\n"  # noqa: S608  # nosec B608
                    + migration.sql
                    + "\nINSERT INTO schema_migrations"  # noqa: S608  # nosec B608
                    "(version, name, checksum, applied_at) VALUES ("
                    + f"{migration.version}, '{migration.name}', '{migration.checksum}', "  # noqa: S608
                    + f"'{applied_at}');\nPRAGMA user_version={migration.version};\nCOMMIT;"
                )
                await connection.executescript(script)
            await database_checks(connection, full=True)
        except Exception as error:
            try:
                await connection.rollback()
            finally:
                await connection.close()
                closed = True
            if backup is not None:
                copy2(backup, path)
                _fsync_file(path)
                _fsync_directory(path.parent)
            elif not existed:
                path.unlink(missing_ok=True)
            raise MigrationError(
                "migration failed; the pre-migration database was restored"
            ) from error
        return CURRENT_SCHEMA_VERSION
    finally:
        if not closed:
            await connection.close()


async def verify_schema(path: Path) -> int:
    version = await applied_schema_version(path)
    if version > CURRENT_SCHEMA_VERSION:
        raise MigrationError("database schema is newer than this release")
    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"database schema {version} is not ready; run the one-shot migration service"
        )
    connection = await aiosqlite.connect(path)
    try:
        await connection.execute("PRAGMA foreign_keys=ON")
        await database_checks(connection, full=False)
    finally:
        await connection.close()
    return version
