"""Data model shared by detection, actions and the web UI."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yml", ".yaml", ".ini",
    ".cfg", ".conf", ".log", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".scss", ".sh", ".bat", ".ps1", ".cmd", ".tex", ".rst", ".sql", ".php", ".rb",
    ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".toml", ".env", ".gitignore",
    ".srt", ".vtt", ".diff", ".patch",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif", ".heic"}
PDF_EXT = {".pdf"}
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".opus"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi"}


def preview_kind(name: str) -> str:
    import os

    ext = os.path.splitext(name)[1].lower()
    if ext in TEXT_EXT:
        return "text"
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    return "binary"


@dataclass
class FileSide:
    exists: bool = False
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0
    md5: str = ""
    fileid: str = ""
    etag: str = ""
    checksums: str = ""
    preview: str = "binary"
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# Issue kinds, in the order the UI groups them.
KINDS = {
    "filename_invalid": {
        "label": "Cannot be created on Windows",
        "severity": "blocking",
        "blurb": "The server has a name Windows rejects (like * or :). It will never sync until renamed.",
    },
    "case_conflict": {
        "label": "Name clash (upper/lower case)",
        "severity": "blocking",
        "blurb": "Two server files differ only by capitalisation. Windows cannot hold both.",
    },
    "path_too_long": {
        "label": "Path too long for Windows",
        "severity": "warning",
        "blurb": "The full local path exceeds the classic 259 character Windows limit.",
    },
    "conflict": {
        "label": "Both sides changed",
        "severity": "warning",
        "blurb": "Local and server versions differ, so the client kept both or stopped.",
    },
    "sync_error": {
        "label": "Sync error",
        "severity": "warning",
        "blurb": "The client reported an error for this file.",
    },
    "ignored": {
        "label": "Ignored file",
        "severity": "noise",
        "blurb": "Excluded by the client's ignore rules, but still reported as a sync error.",
    },
    "junk": {
        "label": "macOS / Windows junk",
        "severity": "noise",
        "blurb": "._* resource forks, .DS_Store, Thumbs.db and friends. Nothing is wrong.",
    },
}

SEVERITY_ORDER = {"blocking": 0, "warning": 1, "noise": 2}
KIND_ORDER = list(KINDS)


@dataclass
class Issue:
    key: str
    kind: str
    path: str
    title: str = ""
    reason: str = ""
    status: str = ""
    sources: list[str] = field(default_factory=list)
    occurrences: int = 0
    last_seen: float = 0.0
    local: FileSide = field(default_factory=FileSide)
    remote: FileSide | None = None
    actions: list[str] = field(default_factory=list)
    suggested_name: str = ""
    occurrence_paths: list[str] = field(default_factory=list)
    dismissed: bool = False
    resolved: bool = False
    resolved_note: str = ""
    details: dict = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return KINDS.get(self.kind, {}).get("severity", "warning")

    @property
    def is_dir(self) -> bool:
        return bool(
            (self.local and self.local.is_dir)
            or (self.remote and self.remote.is_dir)
        )

    @property
    def has_both(self) -> bool:
        return self.local.exists and bool(self.remote and self.remote.exists)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "kind_label": KINDS.get(self.kind, {}).get("label", self.kind),
            "severity": self.severity,
            "path": self.path,
            "title": self.title or self.path.split("/")[-1],
            "reason": self.reason,
            "status": self.status,
            "sources": self.sources,
            "occurrences": self.occurrences,
            "last_seen": self.last_seen,
            "local": self.local.to_dict(),
            "remote": self.remote.to_dict() if self.remote else None,
            "is_dir": self.is_dir,
            "actions": self.actions,
            "suggested_name": self.suggested_name,
            "occurrence_paths": self.occurrence_paths,
            "dismissed": self.dismissed,
            "resolved": self.resolved,
            "resolved_note": self.resolved_note,
            "details": self.details,
        }
