#!/usr/bin/env python3
"""Verify a Signal Room release bundle without trusting bundle contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
BUILD_SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
PRIVATE_TOP_LEVEL = {
    "BUILD_SHA",
    "BUNDLE_KIND",
    "RELEASE_ENV",
    "SHA256SUMS",
    "VERSION",
    "config-schema.json",
    "deploy",
    "migrations",
    "requirements.lock",
    "sbom.cdx.json",
    "web",
    "wheelhouse",
    "wheels",
}
PUBLIC_TOP_LEVEL = {
    "BUILD_SHA",
    "BUNDLE_KIND",
    "SHA256SUMS",
    "VERSION",
    "sbom.cdx.json",
    "site",
}
DEMO_FORBIDDEN = (
    re.compile(rb"192\.168\.", re.I),
    re.compile(rb"10\.\d{1,3}\.\d{1,3}\.", re.I),
    re.compile(rb"172\.(?:1[6-9]|2\d|3[01])\.", re.I),
    re.compile(rb"PVEAPIToken=", re.I),
    re.compile(rb"Cf-Access-Jwt-Assertion", re.I),
    re.compile(rb"noorfamily\.uk", re.I),
    re.compile(rb"/api/v1/", re.I),
    re.compile(rb"EventSource\s*\(", re.I),
)


class VerificationError(RuntimeError):
    pass


def _safe_relative(value: str) -> Path:
    posix = PurePosixPath(value)
    if (
        posix.is_absolute()
        or not posix.parts
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise VerificationError(f"unsafe manifest path: {value!r}")
    if "\\" in value:
        raise VerificationError(f"manifest paths must use forward slashes: {value!r}")
    return Path(*posix.parts)


def _manifest(root: Path) -> dict[Path, str]:
    path = root / "SHA256SUMS"
    if not path.is_file() or path.is_symlink():
        raise VerificationError("SHA256SUMS is missing or unsafe")
    expected: dict[Path, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = MANIFEST_LINE.fullmatch(line)
        if not match:
            raise VerificationError(f"malformed manifest line {number}")
        relative = _safe_relative(match.group(2))
        if relative in expected:
            raise VerificationError(f"duplicate manifest path: {relative.as_posix()}")
        expected[relative] = match.group(1)
    if not expected:
        raise VerificationError("manifest is empty")
    return expected


def _files(root: Path) -> set[Path]:
    files: set[Path] = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise VerificationError(f"bundle contains a symlink: {item.relative_to(root)}")
        if item.is_file() and item.name != "SHA256SUMS":
            files.add(item.relative_to(root))
    return files


def _require_private(root: Path, files: set[Path]) -> None:
    required = {
        Path("VERSION"),
        Path("BUILD_SHA"),
        Path("BUNDLE_KIND"),
        Path("RELEASE_ENV"),
        Path("sbom.cdx.json"),
        Path("config-schema.json"),
        Path("requirements.lock"),
        Path("web/index.html"),
        Path("deploy/install-release.sh"),
        Path("deploy/cloudflared.service"),
        Path("deploy/nftables.example.conf"),
        Path("deploy/soak-monitor.py"),
        Path("deploy/sqlite-release-snapshot.py"),
        Path("deploy/verify-release.py"),
        Path("deploy/wait-for-core.py"),
    }
    missing = required - files
    if missing:
        raise VerificationError(f"private bundle is missing: {sorted(map(str, missing))}")
    if not any(path.parts[:1] == ("wheelhouse",) and path.suffix == ".whl" for path in files):
        raise VerificationError("private bundle has no offline wheelhouse")
    if not any(
        path.parts[:1] == ("wheels",)
        and path.name.startswith("signal_room-")
        and path.suffix == ".whl"
        for path in files
    ):
        raise VerificationError("private bundle has no application wheel")
    if not any(path.parts[:1] == ("migrations",) and path.suffix == ".sql" for path in files):
        raise VerificationError("private bundle has no migrations")
    if any(path.suffix in {".sqlite3", ".env", ".log"} for path in files):
        raise VerificationError("private bundle contains runtime data or environment files")


def _validate_identity(root: Path, kind: str) -> tuple[str, str]:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    build_sha = (root / "BUILD_SHA").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise VerificationError("VERSION is not a strict semantic release version")
    if not BUILD_SHA_PATTERN.fullmatch(build_sha):
        raise VerificationError("BUILD_SHA is not a full hexadecimal commit identity")
    expected_name = f"signal-room-{kind}-{version}-{build_sha[:12]}"
    if root.name != expected_name:
        raise VerificationError(f"bundle directory must be named {expected_name!r}")
    try:
        sbom = json.loads((root / "sbom.cdx.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise VerificationError("SBOM metadata is malformed") from error
    if not isinstance(sbom, dict):
        raise VerificationError("SBOM metadata is malformed")
    metadata = sbom.get("metadata")
    if not isinstance(metadata, dict):
        raise VerificationError("SBOM metadata is malformed")
    component = metadata.get("component")
    property_items = metadata.get("properties", [])
    if not isinstance(component, dict) or not isinstance(property_items, list):
        raise VerificationError("SBOM metadata is malformed")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("value"), str)
        for item in property_items
    ):
        raise VerificationError("SBOM metadata is malformed")
    property_names = [item["name"] for item in property_items]
    if len(property_names) != len(set(property_names)):
        raise VerificationError("SBOM metadata contains duplicate properties")
    properties = {item["name"]: item["value"] for item in property_items}
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        raise VerificationError("SBOM is not CycloneDX 1.6")
    if (
        component.get("type") != "application"
        or component.get("name") != f"signal-room-{kind}"
        or component.get("version") != version
    ):
        raise VerificationError("SBOM release identity does not match VERSION")
    if properties.get("signal-room:build-sha") != build_sha:
        raise VerificationError("SBOM build identity does not match BUILD_SHA")
    if kind == "private":
        expected_environment = f"SIGNAL_ROOM_BUILD_SHA={build_sha}\n".encode("ascii")
        if (root / "RELEASE_ENV").read_bytes() != expected_environment:
            raise VerificationError("RELEASE_ENV build identity does not match BUILD_SHA")
    return version, build_sha


def _require_public(root: Path, files: set[Path]) -> None:
    required = {
        Path("VERSION"),
        Path("BUILD_SHA"),
        Path("BUNDLE_KIND"),
        Path("sbom.cdx.json"),
        Path("site/index.html"),
        Path("site/_headers"),
        Path("site/og-signal-room.png"),
    }
    missing = required - files
    if missing:
        raise VerificationError(f"public bundle is missing: {sorted(map(str, missing))}")
    headers = (root / "site/_headers").read_text(encoding="utf-8")
    if "connect-src 'none'" not in headers:
        raise VerificationError("public demo CSP does not disable network connections")
    binary_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2"}
    for relative in sorted(files):
        if relative.suffix.lower() in binary_suffixes:
            continue
        content = (root / relative).read_bytes()
        for pattern in DEMO_FORBIDDEN:
            if pattern.search(content):
                raise VerificationError(
                    f"public bundle contains forbidden pattern {pattern.pattern!r} in {relative}"
                )


def verify(root: Path) -> str:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise VerificationError("bundle path must be a directory")
    expected = _manifest(root)
    actual = _files(root)
    if actual != set(expected):
        missing = set(expected) - actual
        extra = actual - set(expected)
        raise VerificationError(
            f"manifest file set mismatch; missing={sorted(map(str, missing))}, "
            f"extra={sorted(map(str, extra))}"
        )
    for relative, wanted in sorted(expected.items(), key=lambda item: item[0].as_posix()):
        digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if digest != wanted:
            raise VerificationError(f"checksum mismatch: {relative.as_posix()}")

    kind = (root / "BUNDLE_KIND").read_text(encoding="utf-8").strip()
    allowed = PRIVATE_TOP_LEVEL if kind == "private" else PUBLIC_TOP_LEVEL
    if kind not in {"private", "public"}:
        raise VerificationError(f"unknown bundle kind: {kind!r}")
    version, _ = _validate_identity(root, kind)
    unexpected = {path.parts[0] for path in actual} - allowed
    if unexpected:
        raise VerificationError(f"bundle contains non-allowlisted paths: {sorted(unexpected)}")
    if kind == "private":
        _require_private(root, actual)
        application_wheels = [
            path
            for path in actual
            if len(path.parts) == 2
            and path.parts[0] == "wheels"
            and path.name.startswith("signal_room-")
            and path.suffix == ".whl"
        ]
        if application_wheels != [Path(f"wheels/signal_room-{version}-py3-none-any.whl")]:
            raise VerificationError("application wheel version does not match VERSION")
    else:
        _require_public(root, actual)
    return kind


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    arguments = parser.parse_args()
    try:
        kind = verify(arguments.bundle)
    except (OSError, UnicodeError, VerificationError) as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {kind} release bundle: {arguments.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
