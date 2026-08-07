#!/usr/bin/env python3
"""Build deterministic, allowlisted Signal Room private and public bundles."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil

# Release builds invoke fixed tool argv and never a shell.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import tomllib
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "pyproject.toml"
LOCK = ROOT / "requirements.lock"
FRONTEND = ROOT / "frontend"
GENERATED_EXCLUDES = {"node_modules", "coverage", "playwright-report", "test-results"}


def write_text_lf(destination: Path, value: str, *, encoding: str = "utf-8") -> None:
    destination.write_text(value, encoding=encoding, newline="\n")


def configure_reproducible_environment(epoch: int) -> None:
    if epoch < 0:
        raise RuntimeError("source date epoch cannot be negative")
    if epoch:
        os.environ["SOURCE_DATE_EPOCH"] = str(epoch)


def run(*command: str, cwd: Path = ROOT) -> None:
    executable = shutil.which(command[0]) or command[0]
    # Callers supply internal build commands from this module only.
    subprocess.run(  # noqa: S603  # nosec B603
        (executable, *command[1:]), cwd=cwd, check=True
    )


def copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise RuntimeError(f"required build directory does not exist: {source}")
    shutil.copytree(source, destination)


def source_sha() -> str:
    roots = [
        ROOT / "backend",
        ROOT / "config",
        ROOT / "deploy",
        ROOT / "frontend/src",
        ROOT / "frontend/scripts",
    ]
    files = [
        PACKAGE,
        LOCK,
        ROOT / "requirements-dev.lock",
        FRONTEND / "package.json",
        FRONTEND / "package-lock.json",
        FRONTEND / "index.html",
        FRONTEND / "demo.html",
        FRONTEND / "vite.config.ts",
        ROOT / "scripts/build-release.py",
    ]
    for base in roots:
        files.extend(item for item in base.rglob("*") if item.is_file())
    files.extend(item for item in (FRONTEND / "dist-private").rglob("*") if item.is_file())
    files.extend(item for item in (FRONTEND / "dist-demo").rglob("*") if item.is_file())
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix()):
        if any(part in GENERATED_EXCLUDES for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def python_components() -> list[dict[str, str]]:
    components: list[dict[str, str]] = []
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        name, version = match.groups()
        normalized = name.lower().replace("_", "-")
        components.append(
            {
                "type": "library",
                "name": normalized,
                "version": version,
                "purl": f"pkg:pypi/{normalized}@{version}",
                "bom-ref": f"pkg:pypi/{normalized}@{version}",
            }
        )
    return components


def node_components() -> list[dict[str, str]]:
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    components: list[dict[str, str]] = []
    for location, metadata in lock.get("packages", {}).items():
        if not location.startswith("node_modules/") or "version" not in metadata:
            continue
        name = location.rsplit("node_modules/", 1)[-1]
        version = str(metadata["version"])
        purl = f"pkg:npm/{quote(name, safe='@/')}@{version}"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": purl,
                "bom-ref": purl,
            }
        )
    return components


def write_sbom(destination: Path, version: str, build_sha: str, kind: str) -> None:
    application_ref = f"pkg:pypi/signal-room@{version}"
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"signal-room:{kind}:{build_sha}")
    components = node_components()
    if kind == "private":
        components.extend(python_components())
        components.append(
            {
                "type": "application",
                "name": "signal-room",
                "version": version,
                "purl": application_ref,
                "bom-ref": application_ref,
            }
        )
    unique = {component["bom-ref"]: component for component in components}
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": f"signal-room-{kind}",
                "version": version,
            },
            "properties": [{"name": "signal-room:build-sha", "value": build_sha}],
        },
        "components": [unique[key] for key in sorted(unique)],
    }
    write_text_lf(destination, json.dumps(bom, indent=2, sort_keys=True) + "\n")


def write_manifest(bundle: Path) -> None:
    lines: list[str] = []
    for path in sorted(bundle.rglob("*"), key=lambda item: item.relative_to(bundle).as_posix()):
        if path.is_symlink():
            raise RuntimeError(f"release bundles cannot contain symlinks: {path}")
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(bundle).as_posix()
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    write_text_lf(bundle / "SHA256SUMS", "\n".join(lines) + "\n")


def deterministic_archive(bundle: Path, destination: Path, epoch: int) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in [bundle, *sorted(bundle.rglob("*"))]:
                    relative = Path(bundle.name) / path.relative_to(bundle)
                    info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    info.mtime = epoch
                    info.mode = 0o755 if path.is_dir() or path.suffix == ".sh" else 0o644
                    if path.is_file():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def build_private(bundle: Path, version: str, build_sha: str, work: Path) -> None:
    (bundle / "wheels").mkdir(parents=True)
    run(
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--disable-pip-version-check",
        "--no-build-isolation",
        "--no-deps",
        "--wheel-dir",
        str(bundle / "wheels"),
        str(ROOT),
    )
    wheels = list((bundle / "wheels").glob("signal_room-*.whl"))
    if len(wheels) != 1 or "py3-none-any" not in wheels[0].name:
        raise RuntimeError("application build did not produce one universal wheel")

    wheelhouse = bundle / "wheelhouse"
    wheelhouse.mkdir()
    run(
        sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--require-hashes",
        "--only-binary=:all:",
        "--platform",
        "manylinux_2_34_x86_64",
        "--platform",
        "manylinux_2_28_x86_64",
        "--platform",
        "manylinux2014_x86_64",
        "--implementation",
        "cp",
        "--python-version",
        "313",
        "--abi",
        "cp313",
        "--abi",
        "abi3",
        "--abi",
        "none",
        "--dest",
        str(wheelhouse),
        "-r",
        str(LOCK),
    )

    copy_tree(FRONTEND / "dist-private", bundle / "web")
    copy_tree(ROOT / "backend/signal_room/migrations", bundle / "migrations")
    deploy = bundle / "deploy"
    deploy.mkdir()
    for source in sorted((ROOT / "deploy").rglob("*")):
        if source.is_file():
            relative = source.relative_to(ROOT / "deploy")
            target = deploy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    schema = work / "config-schema.json"
    env = {
        **os.environ,
        "SIGNAL_ROOM_ENVIRONMENT": "test",
        "SIGNAL_ROOM_RUNTIME_ROLE": "maintenance",
        "SIGNAL_ROOM_PUBLIC_ORIGIN": "https://signal.noorfamily.uk",
        "SIGNAL_ROOM_BUILD_SHA": build_sha,
    }
    # The configuration schema command is a fixed Python module invocation.
    subprocess.run(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-m",
            "signal_room.cli",
            "validate-config",
            "--schema-output",
            str(schema),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    shutil.copy2(schema, bundle / "config-schema.json")
    shutil.copy2(LOCK, bundle / "requirements.lock")
    write_sbom(bundle / "sbom.cdx.json", version, build_sha, "private")


def build_public(bundle: Path, version: str, build_sha: str) -> None:
    copy_tree(FRONTEND / "dist-demo", bundle / "site")
    write_sbom(bundle / "sbom.cdx.json", version, build_sha, "public")


def metadata(bundle: Path, version: str, build_sha: str, kind: str) -> None:
    write_text_lf(bundle / "VERSION", version + "\n")
    write_text_lf(bundle / "BUILD_SHA", build_sha + "\n")
    write_text_lf(bundle / "BUNDLE_KIND", kind + "\n")
    if kind == "private":
        write_text_lf(
            bundle / "RELEASE_ENV", f"SIGNAL_ROOM_BUILD_SHA={build_sha}\n", encoding="ascii"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    parser.add_argument("--build-sha", default="")
    parser.add_argument(
        "--source-date-epoch", type=int, default=int(os.getenv("SOURCE_DATE_EPOCH", "0"))
    )
    parser.add_argument("--no-archives", action="store_true")
    arguments = parser.parse_args()
    configure_reproducible_environment(arguments.source_date_epoch)
    output = arguments.output.resolve()
    if output == ROOT or ROOT not in output.parents:
        raise RuntimeError("release output must be a dedicated directory inside the repository")
    output.mkdir(parents=True, exist_ok=True)

    project = tomllib.loads(PACKAGE.read_text(encoding="utf-8"))["project"]
    version = str(project["version"])
    run("npm", "run", "build", cwd=FRONTEND)
    run("node", "scripts/check-demo-privacy.mjs", cwd=FRONTEND)
    build_sha = arguments.build_sha or source_sha()
    if not re.fullmatch(r"[0-9a-f]{40,64}", build_sha):
        raise RuntimeError("build SHA must be 40-64 lowercase hexadecimal characters")

    suffix = build_sha[:12]
    names = {
        "private": f"signal-room-private-{version}-{suffix}",
        "public": f"signal-room-public-{version}-{suffix}",
    }
    with tempfile.TemporaryDirectory(prefix="signal-room-release-", dir=output) as temporary:
        work = Path(temporary)
        bundles = {kind: work / name for kind, name in names.items()}
        for bundle in bundles.values():
            bundle.mkdir()
        build_private(bundles["private"], version, build_sha, work)
        build_public(bundles["public"], version, build_sha)
        for kind, bundle in bundles.items():
            metadata(bundle, version, build_sha, kind)
            write_manifest(bundle)
            run(sys.executable, str(ROOT / "deploy/verify-release.py"), str(bundle))
            target = output / names[kind]
            if target.exists():
                shutil.rmtree(target)
            shutil.move(bundle, target)
            if not arguments.no_archives:
                archive = output / f"{names[kind]}.tar.gz"
                archive.unlink(missing_ok=True)
                deterministic_archive(target, archive, arguments.source_date_epoch)
                archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                write_text_lf(
                    archive.with_suffix(archive.suffix + ".sha256"),
                    f"{archive_digest}  {archive.name}\n",
                    encoding="ascii",
                )
            print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
