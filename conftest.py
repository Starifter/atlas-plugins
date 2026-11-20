"""The libraries each plugin's manifest declares, for that plugin's tests.

A plugin installed from this marketplace has its `python_dependencies`
installed with it, into a folder of its own (Atlas's
`docs/spec/plugin-dependencies.md`). The tests get the same: what Atlas's
environment lacks is installed once into `.test-libs/<plugin>/py<major>.<minor>/`
by the installer Atlas itself uses, and appended to the import path, never
prepended - so a library Atlas's environment has is always Atlas's copy, as it
is in a real install.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
LIBS = ROOT / ".test-libs"


def satisfied(requirement: str) -> bool:
    """Whether the import path already holds a distribution this requirement
    accepts - Atlas's environment, or a folder installed on an earlier run."""
    from packaging.requirements import Requirement

    wanted = Requirement(requirement)
    try:
        found = version(wanted.name)
    except PackageNotFoundError:
        return False
    return wanted.specifier.contains(found, prereleases=True)


def install_missing(plugin: str, requirements: list[str]) -> None:
    """Make every requirement importable, installing what is not."""
    from atlas.plugins.dependencies import installer_argv

    target = LIBS / plugin / f"py{sys.version_info.major}.{sys.version_info.minor}"
    entry = str(target)
    if target.is_dir() and entry not in sys.path:
        sys.path.append(entry)
        importlib.invalidate_caches()
    missing = [r for r in requirements if not satisfied(r)]
    if not missing:
        return
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    chosen = installer_argv(target, missing)
    if chosen is None:
        needs = ", ".join(missing)
        raise pytest.UsageError(f"{plugin} needs {needs}, and neither uv nor pip is here")
    installer, argv = chosen
    run = subprocess.run(argv, capture_output=True, text=True, check=False)
    if run.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        said = run.stderr.strip().splitlines()[-1:] or [f"exit {run.returncode}"]
        raise pytest.UsageError(f"{plugin}: {installer} could not install {missing}: {said[0]}")
    if entry not in sys.path:
        sys.path.append(entry)
    importlib.invalidate_caches()


def pytest_configure(config: pytest.Config) -> None:
    """Before any test module imports its plugin."""
    from atlas.plugins import read_manifest
    from atlas.plugins.dependencies import parse_requirements

    for path in sorted(ROOT.glob("*/PLUGIN.md")):
        manifest = read_manifest(path, source="dir")
        if not manifest.python_dependencies or not (path.parent / "tests").is_dir():
            continue
        wanted, _ = parse_requirements(manifest.python_dependencies)
        install_missing(manifest.name, wanted)
