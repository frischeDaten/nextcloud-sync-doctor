"""Windows filename rules and the cross-platform failure classes they cause.

This is the heart of the Mac <-> Windows problem: macOS (and Linux) allow names
that NTFS/Win32 reject, so files created on a Mac can never be materialised on a
Windows machine. The desktop client reports them once and otherwise gives up.
"""

from __future__ import annotations

import os
import re

INVALID_CHARS = '<>:"/\\|?*'
INVALID_CHAR_SET = set(INVALID_CHARS)

RESERVED_BASENAMES = {
    "con", "prn", "aux", "nul", "clock$", "$recycle.bin",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
    "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
}

# The client refuses a basename longer than 254 characters.
MAX_BASENAME = 254
# Windows' classic MAX_PATH limit; the client itself uses \\?\ paths, so this
# only breaks Explorer and other applications.
MAX_PATH = 259

# Names that are pure macOS/Windows filesystem junk. The client already ignores
# most of them - but it still reports each one as a sync error, which is what
# buries the real problems in the activity list.
JUNK_PATTERNS = [
    re.compile(r"^\._"),                 # AppleDouble resource forks
    re.compile(r"^\.DS_Store$", re.I),
    re.compile(r"^\.ds_store$", re.I),
    re.compile(r"^Thumbs\.db$", re.I),
    re.compile(r"^desktop\.ini$", re.I),
    re.compile(r"^\.Spotlight-V100$"),
    re.compile(r"^\.Trashes$"),
    re.compile(r"^\.fseventd$"),
    re.compile(r"^\.apdisk$"),
    re.compile(r"^\.TemporaryItems$"),
    re.compile(r"^\.DocumentRevisions-V100$"),
    re.compile(r"^\.Trash-\d+$"),
    re.compile(r"^~\$"),                # Office lock files
    re.compile(r"^\.~lock\."),
    re.compile(r"^\.directory$"),
    re.compile(r"\.tmp$", re.I),
]

CONFLICT_COPY_RE = re.compile(r"\(conflicted copy", re.I)
CONFLICT_SUFFIX_RE = re.compile(r"\.conflict(-\d+)?$", re.I)

# Suggest a replacement for characters Windows cannot store.
CHAR_SUGGESTIONS = {
    "*": "(star)",
    "?": "(question)",
    "<": "(lt)",
    ">": "(gt)",
    ":": "-",
    '"': "'",
    "|": "-",
    "\\": "-",
    "/": "-",
}


def is_junk_name(name: str) -> bool:
    return any(p.search(name) for p in JUNK_PATTERNS)


def is_conflict_copy(name: str) -> bool:
    return bool(CONFLICT_COPY_RE.search(name) or CONFLICT_SUFFIX_RE.search(name))


def windows_problems(name: str, full_local_path: str | None = None) -> list[str]:
    """Return a list of human readable reasons why Windows cannot store ``name``."""
    problems: list[str] = []
    if not name:
        return ["empty file name"]
    if name in (".", ".."):
        problems.append(f"'{name}' is a reserved path segment")

    bad = sorted({ch for ch in name if ch in INVALID_CHAR_SET})
    if bad:
        shown = ", ".join(repr(ch) for ch in bad)
        problems.append(f"contains character(s) {shown} which are not allowed on Windows")

    if any(ord(ch) < 32 for ch in name):
        problems.append("contains control characters")

    if name != name.rstrip(" ."):
        problems.append("ends with a space or a dot (silently stripped by Windows)")

    if name != name.lstrip(" "):
        problems.append("starts with a space")

    stem = name.split(".", 1)[0].strip().lower()
    if stem in RESERVED_BASENAMES or name.lower() in RESERVED_BASENAMES:
        problems.append(f"'{stem}' is a reserved Windows device name")

    if len(name) > MAX_BASENAME:
        problems.append(
            f"file name is {len(name)} characters long (the client refuses more than {MAX_BASENAME})"
        )

    if full_local_path and len(full_local_path) > MAX_PATH:
        problems.append(
            f"full path is {len(full_local_path)} characters long "
            f"(longer than {MAX_PATH}; Explorer and most apps will fail, the client copes)"
        )
    return problems


def suggest_safe_name(name: str, taken: set[str] | None = None) -> str:
    """Propose a Windows-safe variant of ``name`` that is unlikely to collide."""
    taken_lower = {t.lower() for t in (taken or set())}
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""

    fixed = "".join(CHAR_SUGGESTIONS.get(ch, "-" if ord(ch) < 32 else ch) for ch in stem)
    fixed = fixed.rstrip(" .") or "unnamed"
    if fixed.split(".", 1)[0].strip().lower() in RESERVED_BASENAMES:
        fixed = fixed + "_file"
    if len(fixed) > 200:
        fixed = fixed[:200]

    candidate = f"{fixed}.{ext}" if ext else fixed
    if not windows_problems(candidate) and candidate.lower() not in taken_lower:
        return candidate

    n = 2
    while True:
        candidate = f"{fixed} ({n})" + (f".{ext}" if ext else "")
        if not windows_problems(candidate) and candidate.lower() not in taken_lower:
            return candidate
        n += 1


def case_collisions(names: list[str]) -> list[list[str]]:
    """Group names that differ only by case - fatal for Windows, fine elsewhere."""
    buckets: dict[str, list[str]] = {}
    for name in names:
        buckets.setdefault(name.lower(), []).append(name)
    return [sorted(v) for v in buckets.values() if len(v) > 1]


def local_full_path(local_root: str, rel_path: str) -> str:
    return os.path.join(local_root, *rel_path.replace("\\", "/").split("/"))


def parent_rel(rel_path: str) -> str:
    rel = rel_path.replace("\\", "/").strip("/")
    if "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0]


def basename(rel_path: str) -> str:
    return rel_path.replace("\\", "/").rstrip("/").split("/")[-1]
