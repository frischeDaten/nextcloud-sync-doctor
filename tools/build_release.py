"""Build the downloadable ZIP bundle.

The bundle is the source tree minus repository plumbing, wrapped in a single
top-level folder so extracting it does not scatter files. It runs directly:
double-click the .cmd, or `python -m ncsd serve`.

    python tools/build_release.py [--out dist]

The archive is reproducible: fixed timestamps and permissions, so rebuilding
the same commit yields the same bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files (and folders) that belong in the bundle.
INCLUDE_FILES = [
    "Nextcloud Sync Doctor.cmd",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
]
INCLUDE_DIRS = ["ncsd", "docs"]
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_PARTS = {"__pycache__", ".git", "dist", "build", ".github", ".idea", ".vscode"}

# 1980-01-01 00:00:00, the earliest a ZIP can represent.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
FIXED_MODE = 0o644


def package_version() -> str:
    sys.path.insert(0, str(ROOT))
    try:
        import ncsd

        return ncsd.__version__
    finally:
        sys.path.pop(0)


def tracked_files() -> list[Path] | None:
    """Prefer git's view of the tree so untracked junk never sneaks in."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return None
    names = [name for name in out.split("\0") if name]
    return [ROOT / name for name in names]


def collect() -> list[Path]:
    tracked = tracked_files()
    if tracked is None:
        candidates = [ROOT / name for name in INCLUDE_FILES]
        for dirname in INCLUDE_DIRS:
            candidates.extend(sorted((ROOT / dirname).rglob("*")))
    else:
        candidates = tracked

    selected: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if EXCLUDE_PARTS.intersection(rel.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        rel_posix = rel.as_posix()
        if rel_posix in INCLUDE_FILES or any(rel_posix.startswith(d + "/") for d in INCLUDE_DIRS):
            selected.append(path)
    return sorted(selected, key=lambda p: p.relative_to(ROOT).as_posix())


def build(out_dir: Path) -> Path:
    version = package_version()
    folder = f"nextcloud-sync-doctor-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{folder}.zip"
    files = collect()
    if not files:
        raise SystemExit("nothing to package")

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{folder}/{rel}", date_time=FIXED_DATE)
            info.external_attr = (0o100000 | FIXED_MODE) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    size = target.stat().st_size
    print(f"{target}  ({size / 1024:.0f} KiB, {len(files)} files)")
    print(f"sha256  {digest}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    args = parser.parse_args()
    build(Path(args.out) if os.path.isabs(args.out) else ROOT / args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
