from __future__ import annotations

import hashlib
from pathlib import Path

import aiosqlite
import pytest
from signal_room.migrate import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationError,
    applied_schema_version,
    database_checks,
    discover_migrations,
    migrate_database,
    verified_backup,
    verify_schema,
)


def test_migration_discovery_is_ordered_checksummed_and_strict(tmp_path: Path) -> None:
    migrations = discover_migrations()
    assert [item.version for item in migrations] == [1, 2, 3, 4]
    assert all(
        item.checksum == hashlib.sha256(item.sql.encode()).hexdigest() for item in migrations
    )

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "bad-name.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="filename"):
        discover_migrations(invalid)

    gap = tmp_path / "gap"
    gap.mkdir()
    (gap / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="sequence"):
        discover_migrations(gap)


async def test_applied_and_verified_schema_states(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    assert await applied_schema_version(missing) == 0
    with pytest.raises(MigrationError, match="not ready"):
        await verify_schema(missing)

    legacy = tmp_path / "legacy.sqlite3"
    connection = await aiosqlite.connect(legacy)
    await connection.execute("CREATE TABLE legacy(value TEXT)")
    await connection.commit()
    await connection.close()
    assert await applied_schema_version(legacy) == 0
    with pytest.raises(MigrationError, match="not ready"):
        await verify_schema(legacy)

    assert await migrate_database(missing) == CURRENT_SCHEMA_VERSION
    assert await verify_schema(missing) == CURRENT_SCHEMA_VERSION
    assert await migrate_database(missing) == CURRENT_SCHEMA_VERSION


async def test_existing_database_gets_verified_pre_migration_backup(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = await aiosqlite.connect(path)
    await connection.execute("CREATE TABLE legacy(value TEXT)")
    await connection.execute("INSERT INTO legacy VALUES ('preserved')")
    await connection.commit()
    await connection.close()
    backup_dir = tmp_path / "preflight"
    await migrate_database(path, backup_dir)
    backups = list(backup_dir.glob("*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].with_suffix(".sqlite3.sha256").is_file()
    backup = await aiosqlite.connect(backups[0])
    assert (await (await backup.execute("SELECT value FROM legacy")).fetchone())[0] == "preserved"
    await backup.close()


async def test_migration_rejects_future_and_tampered_history(tmp_path: Path) -> None:
    future = tmp_path / "future.sqlite3"
    await migrate_database(future)
    connection = await aiosqlite.connect(future)
    await connection.execute(
        "INSERT INTO schema_migrations VALUES (5, '005_future.sql', 'future', '2026-01-01')"
    )
    await connection.commit()
    await connection.close()
    with pytest.raises(MigrationError, match="newer"):
        await migrate_database(future)
    with pytest.raises(MigrationError, match="newer"):
        await verify_schema(future)

    tampered = tmp_path / "tampered.sqlite3"
    await migrate_database(tampered)
    connection = await aiosqlite.connect(tampered)
    await connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
    await connection.commit()
    await connection.close()
    with pytest.raises(MigrationError, match="checksum"):
        await migrate_database(tampered)


async def test_failed_migration_restores_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    await migrate_database(path)
    connection = await aiosqlite.connect(path)
    await connection.execute("DELETE FROM schema_migrations WHERE version>=3")
    await connection.execute("PRAGMA user_version=2")
    await connection.execute("INSERT INTO runtime_state(key, value) VALUES ('sentinel', 'kept')")
    await connection.commit()
    await connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await connection.close()
    original = discover_migrations()
    broken = [
        *original[:2],
        Migration(
            version=3, name=original[2].name, sql="NOT VALID SQL;", checksum=original[2].checksum
        ),
    ]
    monkeypatch.setattr("signal_room.migrate.discover_migrations", lambda: broken)
    backup_dir = tmp_path / "rollback-backups"
    with pytest.raises(MigrationError, match="restored"):
        await migrate_database(path, backup_dir)
    assert await applied_schema_version(path) == 2
    restored = await aiosqlite.connect(path)
    assert (
        await (
            await restored.execute("SELECT value FROM runtime_state WHERE key='sentinel'")
        ).fetchone()
    )[0] == "kept"
    await restored.close()
    assert list(backup_dir.glob("*.sqlite3"))


async def test_failed_first_migration_removes_unpublished_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "new-failure.sqlite3"
    originals = discover_migrations()
    broken = [
        Migration(
            version=1, name=originals[0].name, sql="INVALID SQL", checksum=originals[0].checksum
        ),
        *originals[1:],
    ]
    monkeypatch.setattr("signal_room.migrate.discover_migrations", lambda: broken)
    with pytest.raises(MigrationError, match="restored"):
        await migrate_database(path)
    assert not path.exists()


async def test_database_checks_and_verified_backup_detect_foreign_key_failures(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.sqlite3"
    source = await aiosqlite.connect(source_path)
    await source.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
    await source.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
    await source.commit()
    await database_checks(source, full=False)
    await database_checks(source, full=True)
    destination = tmp_path / "nested" / "copy.sqlite3"
    assert await verified_backup(source, destination) == destination
    assert destination.is_file()
    assert destination.with_suffix(".sqlite3.sha256").is_file()

    await source.execute("PRAGMA foreign_keys=OFF")
    await source.execute("INSERT INTO child VALUES (999)")
    await source.commit()
    with pytest.raises(MigrationError, match="foreign_key_check"):
        await database_checks(source, full=False)
    await source.close()


class InterruptedSource:
    async def backup(self, target: aiosqlite.Connection) -> None:
        await target.execute("CREATE TABLE partial(value TEXT)")
        raise OSError("simulated interruption")


async def test_interrupted_backup_never_leaves_a_publishable_partial(tmp_path: Path) -> None:
    destination = tmp_path / "daily" / "copy.sqlite3"
    with pytest.raises(OSError, match="interruption"):
        await verified_backup(InterruptedSource(), destination)  # type: ignore[arg-type]
    assert not destination.exists()
    assert not destination.with_name("copy.sqlite3.partial").exists()


async def test_corrupt_database_fails_integrity_checks(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"this is not a SQLite database")
    connection = await aiosqlite.connect(path)
    try:
        with pytest.raises(Exception, match="database|encrypted|malformed"):
            await database_checks(connection, full=True)
    finally:
        await connection.close()
