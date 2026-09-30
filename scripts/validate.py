"""What this marketplace promises about every entry, checked.

Runs on every change in CI and by hand before a pull request. It reads each
top-level directory the way Atlas's own discovery does - the manifest is
parsed and nothing is imported - so passing here means `/plugins market
refresh` will list the entry and `/plugins install` will take it. It does not
run the plugin: what a plugin does once consented to is the reader's call, and
this script says nothing about it.

Usage: python scripts/validate.py [root]
"""

from __future__ import annotations

import sys
from pathlib import Path

from atlas.errors import IncompatibleSDKError
from atlas.plugins.discovery import (
    MANIFEST_FILENAME,
    MODULE_FILENAME,
    check_compatibility,
    read_manifest,
)
from atlas.sdk import SDK_VERSION

SKIPPED = {".git", ".github", "scripts"}
"""Directories that are the repository's and not a plugin's."""


def problems(directory: Path) -> list[str]:
    found: list[str] = []
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return [f"no {MANIFEST_FILENAME}"]
    if not (directory / MODULE_FILENAME).is_file():
        found.append(f"no {MODULE_FILENAME}")
    manifest = read_manifest(manifest_path, source="marketplace", rank=9)
    if manifest.error:
        return [*found, manifest.error]
    if manifest.name != directory.name:
        found.append(f"manifest says name: {manifest.name!r}, directory is {directory.name!r}")
    if not manifest.description:
        found.append("no description - the Discover tab shows it")
    if not manifest.version:
        found.append("no version - an update has to be visible as one")
    if not manifest.requires_atlas_sdk:
        found.append("no requires_atlas_sdk - say which SDK the plugin was written against")
    else:
        try:
            check_compatibility(manifest)
        except IncompatibleSDKError as exc:
            found.append(f"{exc} (this SDK is {SDK_VERSION})")
    if manifest.autoload:
        found.append("autoload: true - Atlas honours it only for the plugins it ships")
    found.extend(f"warning: {warning}" for warning in manifest.warnings)
    return found


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    if (root / MANIFEST_FILENAME).is_file():
        print(f"{root} is itself a plugin - a marketplace holds plugins one level down")
        return 1
    entries = sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and path.name not in SKIPPED and not path.name.startswith(".")
    )
    if not entries:
        print("no plugins found")
        return 1
    failed = 0
    for directory in entries:
        found = problems(directory)
        errors = [line for line in found if not line.startswith("warning: ")]
        status = "ok" if not errors else "FAIL"
        print(f"{directory.name:<24} {status}")
        for line in found:
            print(f"    {line}")
        failed += bool(errors)
    print(f"\n{len(entries)} plugin(s), {failed} failed, SDK {SDK_VERSION}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
