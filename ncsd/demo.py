"""Demo mode: synthetic accounts and issues, no client and no server required.

Used for the screenshots in the README, for trying the UI out, and for working on
it without a Nextcloud instance. Nothing here reads the real client configuration,
the credential store, the sync folder or the network.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time

from . import config, detect, journal, names, platform
from .actions import ACTION_LABELS, ActionError, Outcome, Plan
from .dav import RemoteItem
from .model import FileSide, Issue, preview_kind
from .state import Store

CLIENT_VERSION = "35.0.0 (build 20261012)"
SERVER_VERSION = "31.0.2.1"


def _ago(days: float = 0, hours: float = 0, minutes: float = 0) -> float:
    return time.time() - (days * 86400 + hours * 3600 + minutes * 60)


def build_context() -> tuple[config.ClientConfig, config.Account, config.SyncFolder]:
    account = config.Account(
        index=0,
        url="https://cloud.example.com",
        user="alex",
        display_name="Alex Example",
        server_version=SERVER_VERSION,
    )
    folder = config.SyncFolder(
        index=0,
        # Windows and macOS have different conventions for where a sync folder
        # lives; the fixtures follow the platform the demo runs on.
        local_path=platform.demo_sync_root(),
        target_path="/",
        journal_name=".sync_1a2b3c4d5e6f.db",
        virtual_files_mode=platform.demo_vfs_mode(),
    )
    account.folders.append(folder)
    cfg = config.ClientConfig(path="", accounts=[account], client_version=CLIENT_VERSION, is_vfs_enabled=True)
    return cfg, account, folder


def _remote(path: str, size: int, mtime: float, fileid: str, sha1: str, etag: str,
            permissions: str = "RGDNVW", content_type: str = "application/octet-stream") -> RemoteItem:
    return RemoteItem(
        path=path, is_dir=False, size=size, mtime=mtime, etag=etag, fileid=fileid,
        checksums=f"SHA1:{sha1}", permissions=permissions, content_type=content_type,
    )


BRIEF_LOCAL = b"""# Project brief

- Ship the pricing page by Friday
- Waiting for legal review

Contact: alex
"""

BRIEF_REMOTE = b"""# Project brief

- Ship the pricing page by Friday
- Legal review done, see thread
- Add the new tier table

Contact: alex
"""

REMOTE_ITEMS: dict[str, RemoteItem] = {
    "Projects/Website/pricing_grid_*.pdf": _remote(
        "Projects/Website/pricing_grid_*.pdf", 48_214, _ago(days=6, hours=3),
        "512884", "9f2c41b7d0a5e6f31c8b4d2a7e9035b1c6d8f4a2", "e7c1a94f", "application/pdf"),
    "Projects/Website/content/brief.md": _remote(
        "Projects/Website/content/brief.md", len(BRIEF_REMOTE), _ago(hours=5),
        "512877", "3bd1e5a9c7f204b8e6d3a1c5f9027b4e8d6c2a10", "4b2d8f61", "text/markdown"),
    "Photos/2026 Rome/IMG_2041.JPG": _remote(
        "Photos/2026 Rome/IMG_2041.JPG", 2_884_112, _ago(days=41, hours=1),
        "512901", "c4e8b1d6f3a95027e8c1b4d7f6029a3e5c8d1b47", "9a3e5c8d", "image/jpeg"),
    "Shared/Finance/Q3-forecast.xlsx": _remote(
        "Shared/Finance/Q3-forecast.xlsx", 812_444, _ago(days=2, hours=7),
        "512912", "a71f3c9e5b2d8406f1c9a3e7b5d29048c6e1f7b3", "1c9a3e7b",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "Documents/._notes.txt": _remote(
        "Documents/._notes.txt", 4096, _ago(days=12), "512933", "e1b7d3a9c5f28406b1d9e3a7c5f28", "b1d9e3a7"),
    "Documents/Travel/.DS_Store": _remote(
        "Documents/Travel/.DS_Store", 6148, _ago(days=19), "512940", "7a2c8e4b6d0f13579b2d4e6a8c0f24", "2d4e6a8c"),
    "Projects/Website/assets": RemoteItem(
        path="Projects/Website/assets", is_dir=True, size=1_284_602, mtime=_ago(days=3, hours=2),
        fileid="512944", permissions="RGDNVW", etag="5c1d9b3a"),
}

VERSIONS: dict[str, list[dict]] = {
    "512877": [
        {"version": "1764001200", "size": 3_418, "mtime": _ago(hours=5), "etag": "1764001200"},
        {"version": "1763894400", "size": 3_102, "mtime": _ago(days=1, hours=2), "etag": "1763894400"},
    ],
}


def _side(path: str) -> FileSide:
    item = REMOTE_ITEMS[path]
    return FileSide(
        exists=True, size=item.size, mtime=item.mtime, fileid=item.fileid,
        etag=item.etag, checksums=item.checksums, preview=preview_kind(names.basename(path)),
    )


def _local(path: str, size: int, mtime: float) -> FileSide:
    return FileSide(exists=True, size=size, mtime=mtime, preview=preview_kind(names.basename(path)))


def build_issues() -> list[Issue]:
    invalid = "Projects/Website/pricing_grid_*.pdf"
    conflict = "Projects/Website/content/brief.md"
    case = "Photos/2026 Rome/IMG_2041.JPG"
    locked = "Shared/Finance/Q3-forecast.xlsx"

    return [
        Issue(
            key=f"filename_invalid:{invalid}",
            kind="filename_invalid",
            path=invalid,
            title=names.basename(invalid),
            reason='File names containing the character "*" are not supported on this file system.',
            status="FileNameInvalid",
            sources=["server scan", "client log"],
            occurrences=12,
            last_seen=_ago(hours=2),
            local=FileSide(preview="pdf"),
            remote=_side(invalid),
            actions=list(detect.INVALID_NAME_ACTIONS),
            suggested_name="pricing_grid_(star).pdf",
            details={"server_only": True},
        ),
        Issue(
            key=f"conflict:{conflict}",
            kind="conflict",
            path=conflict,
            title=names.basename(conflict),
            reason="Local and server versions diverged",
            status="Conflict",
            sources=["sync journal", "client log"],
            occurrences=3,
            last_seen=_ago(hours=5),
            local=_local(conflict, len(BRIEF_LOCAL), _ago(hours=9)),
            remote=_side(conflict),
            actions=list(detect.BOTH_ACTIONS),
        ),
        Issue(
            key=f"case_conflict:{case}",
            kind="case_conflict",
            path=case,
            title=names.basename(case),
            reason="Server holds two names that differ only by case: IMG_2041.JPG, img_2041.jpg",
            status="FileNameClash",
            sources=["server scan"],
            occurrences=1,
            last_seen=_ago(days=41),
            local=FileSide(preview="image"),
            remote=_side(case),
            actions=list(detect.REMOTE_ONLY_ACTIONS),
            suggested_name="IMG_2041 (2).JPG",
        ),
        Issue(
            key=f"sync_error:{locked}",
            kind="sync_error",
            path=locked,
            title=names.basename(locked),
            reason="Server error: the file is locked by another user",
            status="ServerError",
            sources=["client log"],
            occurrences=7,
            last_seen=_ago(days=2, hours=7),
            local=_local(locked, 812_444, _ago(days=2, hours=8)),
            remote=_side(locked),
            actions=list(detect.BOTH_ACTIONS),
        ),
        Issue(
            key="sync_error:Projects/Website/assets",
            kind="sync_error",
            path="Projects/Website/assets",
            title="assets",
            reason="Server error: an unexpected error occurred while creating this folder",
            status="NormalError",
            sources=["client log"],
            occurrences=2,
            last_seen=_ago(days=3, hours=2),
            local=FileSide(exists=True, is_dir=True, mtime=_ago(days=3, hours=3)),
            remote=FileSide(
                exists=True, is_dir=True, size=1_284_602, mtime=_ago(days=3, hours=2),
                fileid="512944", etag="5c1d9b3a",
            ),
            actions=["rename_local", "rename_remote", "delete_local", "delete_remote",
                     "open_local", "open_remote", "ignore", "dismiss"],
            details={"is_dir": True},
        ),
        Issue(
            key="junk:Documents/._notes.txt",
            kind="junk", path="Documents/._notes.txt", title="._notes.txt",
            reason="File is listed on the ignore list.", status="FileIgnored",
            sources=["client log"], occurrences=4, last_seen=_ago(days=12),
            local=_local("Documents/._notes.txt", 4096, _ago(days=12)),
            remote=_side("Documents/._notes.txt"),
            actions=list(detect.JUNK_ACTIONS),
        ),
        Issue(
            key="junk:Documents/Travel/.DS_Store",
            kind="junk", path="Documents/Travel/.DS_Store", title=".DS_Store",
            reason="File is listed on the ignore list.", status="FileIgnored",
            sources=["client log"], occurrences=2, last_seen=_ago(days=19),
            local=_local("Documents/Travel/.DS_Store", 6148, _ago(days=19)),
            remote=_side("Documents/Travel/.DS_Store"),
            actions=list(detect.JUNK_ACTIONS),
        ),
        Issue(
            key="junk:Documents/Budget/~$Budget 2027.xlsx",
            kind="junk", path="Documents/Budget/~$Budget 2027.xlsx", title="~$Budget 2027.xlsx",
            reason="File is listed on the ignore list.", status="FileIgnored",
            sources=["client log"], occurrences=6, last_seen=_ago(hours=3),
            local=_local("Documents/Budget/~$Budget 2027.xlsx", 165, _ago(hours=3)),
            remote=None,
            actions=["dismiss"],
        ),
        Issue(
            key="ignored:Code/app/venv/lib",
            kind="ignored", path="Code/app/venv/lib", title="lib",
            reason="File is listed on the ignore list.", status="FileIgnored",
            sources=["client log"], occurrences=1, last_seen=_ago(days=3),
            local=FileSide(exists=True, size=0, mtime=_ago(days=3)),
            remote=FileSide(exists=True, size=0, mtime=_ago(days=3)),
            actions=["delete_remote", "dismiss"],
        ),
        Issue(
            key="ignored:Notes/.embeddings.tar.gz",
            kind="ignored", path="Notes/.embeddings.tar.gz", title=".embeddings.tar.gz",
            reason="File is listed on the ignore list.", status="FileIgnored",
            sources=["client log"], occurrences=1, last_seen=_ago(days=6),
            local=_local("Notes/.embeddings.tar.gz", 9_412_800, _ago(days=6)),
            remote=None,
            actions=["dismiss"],
        ),
        # macOS only: the Finder moves deleted items into a local ".Trash" folder on
        # FileProvider volumes instead of removing them, and the client then reports
        # them as sync errors that can never be resolved from the server side.
        Issue(
            key="mac_trash:Documents/.Trash/Budget 2027.xlsx",
            kind="mac_trash",
            path="Documents/.Trash/Budget 2027.xlsx",
            title="Budget 2027.xlsx",
            reason="Not found in local storage: the item was deleted in Finder and only "
                   "its local leftover in .Trash remains.",
            status="NormalError",
            sources=["client log", "sync journal"],
            occurrences=4,
            last_seen=_ago(hours=2),
            local=_local("Documents/.Trash/Budget 2027.xlsx", 18_432, _ago(days=2)),
            remote=None,
            actions=list(detect.MAC_TRASH_ACTIONS),
            details={"journal_signal": True},
        ),
        Issue(
            key="filename_invalid:Projects/Website/old_export_*.csv",
            kind="filename_invalid",
            path="Projects/Website/old_export_*.csv",
            title="old_export_*.csv",
            reason='File names containing the character "*" are not supported on this file system.',
            status="FileNameInvalid",
            sources=["client log"],
            occurrences=2,
            last_seen=_ago(days=8),
            local=FileSide(preview="text"),
            remote=None,
            actions=["dismiss"],
            resolved=True,
            resolved_note="Fixed: the server file now exists as 'old_export_(star).csv'.",
            details={"renamed_to": "Projects/Website/old_export_(star).csv"},
        ),
    ]


class _FakeResponse:
    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._buffer.close()
        return False


class DemoDav:
    """Read-only stand-in for the WebDAV client."""

    def __init__(self):
        self.items = dict(REMOTE_ITEMS)
        self.versions_by_fileid = {k: list(v) for k, v in VERSIONS.items()}
        self.text = {"Projects/Website/content/brief.md": BRIEF_REMOTE}

    def stat(self, rel_path: str) -> RemoteItem | None:
        return self.items.get(rel_path.strip("/"))

    def listdir(self, rel_path: str) -> list[RemoteItem]:
        prefix = rel_path.strip("/")
        out = []
        for path, item in self.items.items():
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent == prefix:
                out.append(item)
        return out

    def versions(self, fileid: str) -> list[dict]:
        return self.versions_by_fileid.get(str(fileid), [])

    def open_download(self, rel_path: str):
        return _FakeResponse(self.text.get(rel_path.strip("/"), b""))

    def download_version(self, fileid: str, version: str, dest_path: str) -> int:
        raise ActionError("Demo mode: downloads are disabled.")

    def restore_version(self, fileid: str, version: str) -> None:
        raise ActionError("Demo mode: the server is read-only.")


class DemoResolver:
    """Plan-only resolver: shows what would happen, never changes anything."""

    def __init__(self, dav: DemoDav):
        self.dav = dav

    def plan(self, action: str, rel_path: str, params: dict | None = None) -> Plan:
        params = dict(params or {})
        name = names.basename(rel_path)
        label = ACTION_LABELS.get(action, action)

        if action in ("rename_remote", "rename_local", "download_as"):
            proposed = params.get("new_name") or names.suggest_safe_name(name)
            proposed = proposed.strip().replace("\\", "/").split("/")[-1]
            problems = names.windows_problems(proposed)
            if problems:
                raise ActionError(f"'{proposed}' is not valid on Windows: {'; '.join(problems)}")
            params["new_name"] = proposed
            if action == "rename_remote":
                return Plan(action, rel_path, f"Rename on the server to '{proposed}'",
                            [f"Rename '{name}' to '{proposed}' on the server",
                             "The client will then download it under the new name"], params=params)
            if action == "rename_local":
                return Plan(action, rel_path, f"Rename the local file to '{proposed}'",
                            [f"Rename the local file to '{proposed}'",
                             "The client will upload it under the new name",
                             f"The server copy of '{name}' stays and will be downloaded again"],
                            params=params)
            return Plan(action, rel_path, f"Download the server copy as '{proposed}'",
                        [f"Download the server file into the sync folder as '{proposed}'",
                         "The server copy keeps its original name"], params=params)

        if action == "keep_both":
            stamp = time.strftime("%Y-%m-%d %H%M%S")
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            copy_name = f"{stem} (local copy {stamp})" + (f".{ext}" if ext else "")
            params["copy_name"] = copy_name
            return Plan(action, rel_path,
                        f"Keep both: server version under '{name}', your version as '{copy_name}'",
                        [f"Rename your local file to '{copy_name}'",
                         "Download the server version into the original name",
                         "Both copies will be synced"], params=params)

        canned = {
            "keep_local": (f"Replace the server copy of '{name}' with your local file",
                           ["Back up the current server version locally",
                            "Upload the local file and overwrite the server copy"], True),
            "keep_server": (f"Replace your local '{name}' with the server version",
                            ["Back up the local file", "Overwrite the local file with the server version"], True),
            "download_remote": (f"Replace your local '{name}' with the server version",
                                ["Download the server file into the sync folder"], False),
            "upload_local": (f"Upload '{name}' to the server", ["Upload the local file"], False),
            "delete_remote": (f"Delete '{name}' on the server",
                              ["Move the file to the server's trash bin"], True),
            "delete_local": (f"Remove your local copy of '{name}'",
                             ["Copy the local file to the backup folder",
                              "Remove it from the sync folder"], True),
            "ignore": (f"Add '{rel_path}' to the folder's ignore list",
                       ["Append the path to .sync-exclude.lst",
                        "The file stays on the server but is never downloaded here"], False),
            "dismiss": ("Hide this entry from the list", [], False),
        }
        if action not in canned:
            raise ActionError(f"Unknown action '{action}'")
        summary, steps, destructive = canned[action]
        return Plan(action, rel_path, summary, steps, destructive=destructive, params=params)

    def apply(self, action: str, rel_path: str, params: dict | None = None) -> Outcome:
        self.plan(action, rel_path, params)  # validates the same way
        return Outcome(ok=True, action=action, path=rel_path,
                       message=f"Demo mode: '{ACTION_LABELS.get(action, action)}' was not executed.")


class _FakeRows(dict):
    def __len__(self) -> int:  # a plausible journal size for the header
        return 48_210


class DemoJournal(journal.Journal):
    def __init__(self):
        super().__init__(db_path="demo", snapshot_path="")
        self.rows = _FakeRows()
        self.last_sync = _ago(minutes=4)


class DemoSession:
    demo = True

    def __init__(self):
        self.cfg, self.account, self.folder = build_context()
        self.dav = DemoDav()
        self.resolver = DemoResolver(self.dav)
        data_dir = os.path.join(tempfile.mkdtemp(prefix="ncsd-demo-"), "data")
        self.store = Store(data_dir=data_dir)
        # Keep the complete fixture list: scan() filters noise from a working copy.
        self.all_issues = build_issues()
        self.issues = list(self.all_issues)
        self.journal_obj = DemoJournal()
        self.journal_error = ""
        self.lock = threading.Lock()
        self.deep_state = detect.DeepScanState()
        self.deep_results: list[dict] = []
        self.deep_finished_at = 0.0
        self.started_at = time.time()

        from .creds import Credential

        self.credential = Credential(token="", target="", user=self.account.user, source="demo")

    # -- Session API -------------------------------------------------------
    def refresh_journal(self) -> None:
        return None

    def scan(self, include_junk: bool | None = None, use_deep: bool = False) -> list[Issue]:
        if include_junk is None:
            include_junk = bool(self.store.settings.get("include_junk", False))
        issues = [i for i in self.all_issues if include_junk or i.severity != "noise"]
        with self.lock:
            self.issues = issues
        return issues

    def start_deep_scan(self, scope: str = "") -> bool:
        if self.deep_state.running:
            return False
        state = detect.DeepScanState()
        self.deep_state = state
        total = 4_821

        def run():
            state.running = True
            state.total = total
            state.started = time.time()
            state.message = "Scanning server folders"
            step = 41
            for done in range(step, total + step, step):
                if state.cancel:
                    break
                state.done = min(done, total)
                state.issues = 1 if state.done > total * 0.55 else 0
                time.sleep(0.05)
            state.done = total
            state.running = False
            state.finished = time.time()
            state.message = "Scan finished"
            state.results = []
            self.deep_results = []
            self.deep_finished_at = time.time()

        threading.Thread(target=run, daemon=True, name="demo-deep-scan").start()
        return True

    def find_issue(self, key: str) -> Issue | None:
        for issue in self.issues:
            if issue.key == key:
                return issue
        return None

    def local_path(self, rel_path: str) -> str:
        return names.local_full_path(self.folder.local_root, rel_path)


def build_session() -> DemoSession:
    return DemoSession()
