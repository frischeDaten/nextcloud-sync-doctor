"""Read the desktop client's sync journal (SQLite) without disturbing it.

The journal is the only persisted record of what the client believes about every
file. At startup the client also keeps an incremental "blacklist" of items that
failed to sync - this is exactly the data the GUI's activity list never shows.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field

SNAPSHOT_PREFIX = "journal-"


class JournalError(RuntimeError):
    pass


@dataclass
class JournalRow:
    path: str
    type: int = 0
    fileid: str = ""
    filesize: int = 0
    modtime: int = 0
    # Despite the column name, `metadata.md5` holds the server ETag string.
    etag: str = ""
    content_checksum: str = ""
    remote_perm: str = ""

    @property
    def is_dir(self) -> bool:
        return self.type == 2

    @property
    def is_dehydrated(self) -> bool:
        """type 4+ means a Cloud Files API placeholder (content not local)."""
        return self.type >= 4

    @property
    def numeric_fileid(self) -> str:
        """The versions API needs the numeric prefix of the file id."""
        digits = ""
        for ch in self.fileid:
            if ch.isdigit():
                digits += ch
            else:
                break
        return str(int(digits)) if digits else ""


@dataclass
class BlacklistRow:
    path: str
    errorstring: str = ""
    retrycount: int = 0
    errorcategory: str = ""
    lasttrytime: int = 0
    ignore_duration: int = 0
    rename_target: str = ""
    request_id: str = ""


@dataclass
class ConflictRow:
    path: str
    base_path: str = ""
    base_etag: str = ""
    base_modtime: int = 0
    base_fileid: str = ""
    case_conflict: bool = False


# SyncFileItem::Status / SyncFileItem::NormalError enum, as stored in the journal.
ERROR_CATEGORY_NAMES = {
    0: "NoError",
    1: "NormalError",
    2: "ServerError",
    3: "InsufficientRemoteStorage",
    4: "FatalError",
}


@dataclass
class Journal:
    db_path: str
    snapshot_path: str
    rows: dict[str, JournalRow] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)
    blacklist: list[BlacklistRow] = field(default_factory=list)
    conflicts: list[ConflictRow] = field(default_factory=list)
    case_conflicts: list[ConflictRow] = field(default_factory=list)
    last_sync: int = 0
    stats: dict[str, int] = field(default_factory=dict)

    def get(self, rel_path: str) -> JournalRow | None:
        return self.rows.get(rel_path)


def snapshot(db_path: str, dest_dir: str) -> str:
    """Copy db + wal + shm so we never touch the live database."""
    if not os.path.isfile(db_path):
        raise JournalError(f"Sync journal not found: {db_path}")
    os.makedirs(dest_dir, exist_ok=True)
    stamp = f"{SNAPSHOT_PREFIX}{int(time.time()*1000)}"
    base = os.path.join(dest_dir, stamp + ".db")
    shutil.copy2(db_path, base)
    for suffix in ("-wal", "-shm"):
        src = db_path + suffix
        if os.path.isfile(src):
            try:
                shutil.copy2(src, base + suffix)
            except OSError:
                pass
    return base


def load(db_path: str, dest_dir: str, include_rows: bool = True, keep_snapshot: bool = False) -> Journal:
    snap = snapshot(db_path, dest_dir)
    uri = f"file:{snap}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=1")
        journal = Journal(db_path=db_path, snapshot_path=snap)

        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        journal.stats["tables"] = len(tables)

        if "metadata" in tables:
            if include_rows:
                for row in con.execute(
                    "SELECT path, type, fileid, filesize, modtime, md5, contentChecksum, remotePerm FROM metadata"
                ):
                    jr = JournalRow(
                        path=row["path"],
                        type=row["type"] or 0,
                        fileid=row["fileid"] or "",
                        filesize=row["filesize"] or 0,
                        modtime=row["modtime"] or 0,
                        etag=row["md5"] or "",
                        content_checksum=row["contentChecksum"] or "",
                        remote_perm=row["remotePerm"] or "",
                    )
                    journal.rows[jr.path] = jr
                    if jr.is_dir:
                        journal.dirs.append(jr.path)
            else:
                journal.stats["metadata"] = con.execute("SELECT count(*) FROM metadata").fetchone()[0]
                journal.dirs = [
                    r[0]
                    for r in con.execute("SELECT path FROM metadata WHERE type=2")
                ]

        if "blacklist" in tables:
            for row in con.execute(
                "SELECT path, lastTryEtag, lastTryModtime, retrycount, errorstring, "
                "lastTryTime, ignoreDuration, renameTarget, errorCategory, requestId FROM blacklist"
            ):
                journal.blacklist.append(
                    BlacklistRow(
                        path=row["path"] or "",
                        errorstring=row["errorstring"] or "",
                        retrycount=row["retrycount"] or 0,
                        errorcategory=ERROR_CATEGORY_NAMES.get(row["errorCategory"], str(row["errorCategory"])),
                        lasttrytime=row["lastTryTime"] or 0,
                        ignore_duration=row["ignoreDuration"] or 0,
                        rename_target=row["renameTarget"] or "",
                        request_id=row["requestId"] or "",
                    )
                )

        for table, target, is_case in (
            ("conflicts", journal.conflicts, False),
            ("caseconflicts", journal.case_conflicts, True),
        ):
            if table not in tables:
                continue
            try:
                for row in con.execute(
                    f"SELECT path, basePath, baseEtag, baseModtime, baseFileId FROM {table}"
                ):
                    target.append(
                        ConflictRow(
                            path=row["path"] or "",
                            base_path=row["basePath"] or "",
                            base_etag=row["baseEtag"] or "",
                            base_modtime=row["baseModtime"] or 0,
                            base_fileid=row["baseFileId"] or "",
                            case_conflict=is_case,
                        )
                    )
            except sqlite3.Error:
                pass

        if "key_value_store" in tables:
            for row in con.execute("SELECT key, value FROM key_value_store"):
                if row["key"] == "last_sync":
                    try:
                        journal.last_sync = int(row["value"])
                    except (TypeError, ValueError):
                        pass

        return journal
    finally:
        con.close()
        if not keep_snapshot:
            remove_snapshot(snap)


def remove_snapshot(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def cleanup_snapshots(dest_dir: str, keep_days: int = 1) -> None:
    """Delete snapshot sets left behind by an interrupted run."""
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return
    bases: dict[str, list[str]] = {}
    for name in names:
        if not name.startswith(SNAPSHOT_PREFIX):
            continue
        base = name
        for suffix in ("-wal", "-shm"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        bases.setdefault(base, []).append(name)
    cutoff = time.time() - keep_days * 86400
    for base, files in bases.items():
        path = os.path.join(dest_dir, base)
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            pass
        for name in files:
            try:
                os.remove(os.path.join(dest_dir, name))
            except OSError:
                pass
