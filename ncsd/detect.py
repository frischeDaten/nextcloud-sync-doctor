"""Find the files that are actually failing to sync.

Three independent sources, merged:

1. the client's own log files - this is the *only* place the client records the
   errors it shows in the activity list (the list itself is memory only),
2. the sync journal - the persisted blacklist and conflict tables,
3. an optional deep server scan - finds Windows-incompatible names that the
   client merely skipped once and never mentioned again.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import config, journal, names
from .dav import Dav, DavError, RemoteItem
from .model import KIND_ORDER, SEVERITY_ORDER, FileSide, Issue, preview_kind

LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(\d{3})")
ACTIVITY_RE = re.compile(
    r'Error successfully added to the notification list:\s+OCC::ActivityListModel::ErrorType::(\w+)\s+'
    r'"(.*?)"\s+"(.*?)"\s+OCC::SyncResult::(\w+)\s+OCC::SyncFileItem::(\w+)'
)
PROPAGATION_RE = re.compile(
    r'Could not complete propagation of "(.*?)" by .*? with status OCC::SyncFileItem::(\w+) and error: "(.*)"'
)
LOCAL_ERROR_RE = re.compile(r'inserted error item "(.*?)"')

# SyncFileItem::Status -> (issue kind, human explanation)
STATUS_KIND = {
    "FileNameInvalid": "filename_invalid",
    "FileNameInvalidOnServer": "filename_invalid",
    "FileNameClash": "case_conflict",
    "FileNameLengthExceeded": "path_too_long",
    "Conflict": "conflict",
    "FileIgnored": "ignored",
    "FatalError": "sync_error",
    "NormalError": "sync_error",
    "SoftError": "sync_error",
    "ServerError": "sync_error",
    "BlacklistedError": "sync_error",
    "InsufficientRemoteStorage": "sync_error",
    "FileAccessError": "sync_error",
    "FileReadError": "sync_error",
    "FileWriteError": "sync_error",
    "FileLocked": "sync_error",
    "FileLockedServer": "sync_error",
    "DirectoryMissing": "sync_error",
    "FileTooLarge": "sync_error",
    "FileSizeMismatch": "sync_error",
    "DetailError": "sync_error",
    "NoStatus": "sync_error",
}

JUNK_ACTIONS = ["delete_remote", "dismiss"]
IGNORED_ACTIONS = ["delete_remote", "ignore", "dismiss"]
INVALID_NAME_ACTIONS = ["rename_remote", "download_as", "delete_remote", "ignore", "dismiss"]
BOTH_ACTIONS = [
    "keep_local", "keep_server", "keep_both", "rename_local", "rename_remote",
    "delete_local", "delete_remote", "open_local", "open_remote", "ignore", "dismiss",
]
LOCAL_ONLY_ACTIONS = ["upload_local", "rename_local", "delete_local", "open_local", "ignore", "dismiss"]
REMOTE_ONLY_ACTIONS = [
    "download_remote", "rename_remote", "download_as",
    "delete_remote", "open_remote", "ignore", "dismiss",
]
# Content transfer and previews make no sense for a folder; renaming, deleting
# and opening it do.
DIR_ALLOWED_ACTIONS = {
    "rename_remote", "rename_local", "delete_remote", "delete_local",
    "open_local", "open_remote", "ignore", "dismiss",
}


@dataclass
class LogEntry:
    path: str
    status: str
    error: str
    timestamp: float
    occurrences: int
    activity_type: str = ""
    run_id: int = 0
    stale: bool = False


@dataclass
class LogScan:
    """What the logs say, plus where the client's sync runs begin and end."""

    entries: dict[str, LogEntry]
    latest_run: int = 0
    latest_completed_run: int = -1
    in_progress: bool = False


@dataclass
class ScanOptions:
    include_junk: bool = False
    deep: bool = False
    scope: str = ""
    include_logs: bool = True
    include_journal: bool = True
    include_conflict_copies: bool = True
    max_remote_stat: int = 400
    log_max_age_days: float = 7.0
    max_rename_probe: int = 25


def normalise_name(name: str) -> str:
    """Rough identity of a file name, ignoring the characters Windows rejects.

    ``pricing_grid_*.pdf`` and ``pricing_grid_(star).pdf`` normalise to the same value,
    which is how a completed server-side rename is recognised.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    keep = "".join(ch for ch in stem.lower() if ch.isalnum())
    return f"{keep}.{ext.lower()}"


def _parse_ts(line: str) -> float:
    m = LOG_TS_RE.match(line)
    if not m:
        return 0.0
    stamp = f"{m.group(1)}.{m.group(2)}"
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f"))
    except ValueError:
        return 0.0


def _open_log(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


RUN_START_MARKER = "#### Discovery start"
RUN_END_MARKERS = ("SyncEngine finished", "Sync run took")


def parse_logs(paths: list[str]) -> LogScan:
    """Parse oldest -> newest.

    Sync runs are delimited by the engine's own markers, so every error can be
    attributed to the run it happened in. That is what separates "still failing"
    from "failed once, fifteen minutes and two syncs ago".
    """
    found: dict[str, LogEntry] = {}
    counts: dict[str, int] = {}
    run_id = 0
    latest_completed = -1
    run_open = False

    for path in paths:
        try:
            with _open_log(path) as fh:
                for line in fh:
                    if RUN_START_MARKER in line:
                        run_id += 1
                        run_open = True
                        continue
                    if any(marker in line for marker in RUN_END_MARKERS):
                        if run_open:
                            latest_completed = run_id
                            run_open = False
                        continue
                    if "Error successfully added" not in line and "Could not complete propagation" not in line:
                        continue
                    ts = _parse_ts(line)
                    m = ACTIVITY_RE.search(line)
                    if m:
                        _, rel, message, _, status = m.groups()
                    else:
                        m = PROPAGATION_RE.search(line)
                        if not m:
                            continue
                        rel, status, message = m.groups()
                    rel = rel.replace("\\", "/").strip("/")
                    if not rel:
                        continue
                    counts[rel] = counts.get(rel, 0) + 1
                    found[rel] = LogEntry(
                        path=rel,
                        status=status,
                        error=message,
                        timestamp=ts or found.get(rel, LogEntry("", "", "", 0, 0)).timestamp,
                        occurrences=counts[rel],
                        run_id=run_id,
                    )
        except OSError:
            continue

    return LogScan(
        entries=found,
        latest_run=run_id,
        latest_completed_run=latest_completed,
        in_progress=run_open,
    )


def _md5_of(path: str, chunk: int = 1024 * 256) -> str:
    digest = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return ""


def local_side(local_root: str, rel_path: str, with_hash: bool = False) -> FileSide:
    full = names.local_full_path(local_root, rel_path)
    side = FileSide(preview=preview_kind(names.basename(rel_path)))
    try:
        st = os.lstat(full)
    except (OSError, ValueError) as exc:
        side.error = exc.__class__.__name__
        return side
    side.exists = True
    side.is_dir = stat.S_ISDIR(st.st_mode)
    side.size = 0 if side.is_dir else st.st_size
    side.mtime = st.st_mtime
    if with_hash and not side.is_dir and st.st_size <= 64 * 1024 * 1024:
        side.md5 = _md5_of(full)
    return side


def remote_side(item: RemoteItem | None) -> FileSide | None:
    if item is None:
        return None
    return FileSide(
        exists=True,
        is_dir=item.is_dir,
        size=item.size,
        mtime=item.mtime,
        fileid=item.fileid,
        etag=item.etag,
        checksums=item.checksums,
        preview=preview_kind(names.basename(item.path)),
    )


def _actions_for(kind: str, local: FileSide, remote: FileSide | None, has_remote: bool) -> list[str]:
    if kind in ("junk", "ignored"):
        return JUNK_ACTIONS if has_remote else ["dismiss"]
    if kind == "filename_invalid":
        return INVALID_NAME_ACTIONS if has_remote else ["dismiss"]
    if local.exists and has_remote:
        return BOTH_ACTIONS
    if local.exists:
        return LOCAL_ONLY_ACTIONS
    if has_remote:
        return REMOTE_ONLY_ACTIONS
    return ["dismiss"]


def _issue_key(kind: str, path: str) -> str:
    return f"{kind}:{path}"


def collect(
    account,
    folder,
    journal_obj: journal.Journal | None,
    dav: Dav | None,
    options: ScanOptions,
    deep_results: list[dict] | None = None,
) -> list[Issue]:
    local_root = folder.local_root
    candidates: dict[str, Issue] = {}

    def add(rel: str, kind: str, reason: str, source: str, status: str = "", ts: float = 0.0, occurrences: int = 0):
        key = _issue_key(kind, rel)
        issue = candidates.get(key)
        if issue is None:
            issue = Issue(key=key, kind=kind, path=rel, reason=reason, status=status)
            candidates[key] = issue
        if source not in issue.sources:
            issue.sources.append(source)
        if reason and not issue.reason:
            issue.reason = reason
        if ts and ts > issue.last_seen:
            issue.last_seen = ts
        issue.occurrences = max(issue.occurrences, occurrences)
        return issue

    # --- 1. client logs ---------------------------------------------------
    if options.include_logs:
        logscan = parse_logs(config.recent_logs())
        # Errors logged before the last completed run are history: the client has
        # synced this folder since without reporting them again.
        cutoff = (logscan.latest_completed_run if logscan.in_progress
                  else logscan.latest_run)
        stale_below = cutoff if cutoff >= 0 else 0
        now = time.time()
        max_age = options.log_max_age_days * 86400 if options.log_max_age_days else 0
        for rel, entry in logscan.entries.items():
            # Logs are history: ignore lines older than the window unless the file is
            # still broken (the reality check below decides that).
            if max_age and entry.timestamp and (now - entry.timestamp) > max_age:
                continue
            entry.stale = entry.run_id < stale_below
            name = names.basename(rel)
            if entry.status == "FileIgnored" or names.is_junk_name(name):
                kind = "junk" if names.is_junk_name(name) else "ignored"
            else:
                kind = STATUS_KIND.get(entry.status, "sync_error")
                problems = names.windows_problems(name, names.local_full_path(local_root, rel))
                if problems and kind not in ("filename_invalid",):
                    kind = "filename_invalid"
            issue = add(rel, kind, entry.error, "client log", entry.status, entry.timestamp, entry.occurrences)
            if entry.stale:
                issue.details["log_stale"] = True
                issue.details["log_run"] = entry.run_id
                issue.details["latest_run"] = logscan.latest_run

    # --- 2. sync journal --------------------------------------------------
    if options.include_journal and journal_obj is not None:
        for row in journal_obj.blacklist:
            problems = names.windows_problems(names.basename(row.path), names.local_full_path(local_root, row.path))
            kind = "filename_invalid" if problems else "sync_error"
            issue = add(row.path, kind, row.errorstring or "The client blacklisted this file",
                        "sync journal", error_status_name(row.errorcategory), 0, row.retrycount)
            issue.details["blacklist"] = {
                "retries": row.retrycount,
                "category": row.errorcategory,
                "rename_target": row.rename_target,
            }
            # The journal still holds this item, so it is not merely historical.
            issue.details["journal_signal"] = True
        for row in journal_obj.conflicts:
            add(row.path, "conflict", "Local and server versions diverged", "sync journal").details["journal_signal"] = True
        for row in journal_obj.case_conflicts:
            issue = add(row.path, "case_conflict", "Differs from another server file only by capitalisation",
                        "sync journal")
            issue.details["journal_signal"] = True

        if options.include_conflict_copies:
            for rel, row in journal_obj.rows.items():
                if row.is_dir:
                    continue
                if names.is_conflict_copy(names.basename(rel)):
                    add(rel, "conflict", "Conflict copy kept by the client", "sync journal")

    # --- 3. deep server scan ---------------------------------------------
    if options.deep and deep_results:
        for entry in deep_results:
            rel = entry["path"]
            kind = entry["kind"]
            issue = add(rel, kind, entry.get("reason", ""), "server scan", "", entry.get("mtime", 0), 0)
            issue.details.setdefault("server_scan", entry)
            if entry.get("suggested_name"):
                issue.suggested_name = entry["suggested_name"]

    # --- 4. resolve both sides -------------------------------------------
    issues = sorted(
        candidates.values(),
        key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), KIND_ORDER.index(i.kind) if i.kind in KIND_ORDER else 99, i.path),
    )
    remote_budget = options.max_remote_stat
    rename_probe = [options.max_rename_probe]
    for issue in issues:
        issue.title = names.basename(issue.path)
        issue.local = local_side(local_root, issue.path)
        if not issue.suggested_name:
            taken = set()
            try:
                parent = os.path.dirname(names.local_full_path(local_root, issue.path))
                if os.path.isdir(parent):
                    taken = set(os.listdir(parent))
            except OSError:
                taken = set()
            issue.suggested_name = names.suggest_safe_name(issue.title, taken)

        has_remote = False
        remote_checked = False
        if dav is not None and remote_budget > 0:
            remote_budget -= 1
            remote_checked = True
            try:
                item = dav.stat(issue.path)
            except DavError as exc:
                issue.details["remote_error"] = str(exc)
                item = None
            if item is not None:
                issue.remote = remote_side(item)
                has_remote = True
        elif issue.kind in ("filename_invalid", "case_conflict") and issue.details.get("server_scan"):
            entry = issue.details["server_scan"]
            issue.remote = FileSide(
                exists=True,
                size=entry.get("size", 0),
                mtime=entry.get("mtime", 0.0),
                fileid=entry.get("fileid", ""),
                etag=entry.get("etag", ""),
                preview=preview_kind(issue.title),
            )
            has_remote = True
            remote_checked = True

        issue.actions = _actions_for(issue.kind, issue.local, issue.remote, has_remote)
        if issue.is_dir:
            issue.actions = [a for a in issue.actions if a in DIR_ALLOWED_ACTIONS] or ["dismiss"]
            issue.details["is_dir"] = True
        if not issue.local.exists and has_remote:
            issue.details["server_only"] = True

        # --- reality check ------------------------------------------------
        # Log lines and journal rows describe the past. An entry is history when
        #  * neither side exists any more (fixed, renamed or deleted), or
        #  * the client completed a later sync run without reporting it again.
        if remote_checked and not issue.local.exists and not has_remote:
            issue.resolved = True
            issue.resolved_note = (
                "Neither a local nor a server file exists at this path any more, "
                "so the client's message is historical."
            )
            issue.actions = ["dismiss"]
            if (
                dav is not None
                and rename_probe[0] > 0
                and issue.kind in ("filename_invalid", "case_conflict", "conflict")
            ):
                rename_probe[0] -= 1
                renamed_to = _find_renamed_sibling(dav, issue.path, names.basename(issue.path))
                if renamed_to:
                    issue.resolved_note = (
                        f"Fixed: the server file now exists as '{names.basename(renamed_to)}'."
                    )
                    issue.details["renamed_to"] = renamed_to
        elif issue.details.get("log_stale") and not issue.details.get("journal_signal"):
            issue.resolved = True
            issue.resolved_note = (
                "The client reported this in an earlier sync run and has synced this "
                "folder successfully since, without mentioning it again."
            )
            issue.details["runs_since"] = max(
                0, issue.details.get("latest_run", 0) - issue.details.get("log_run", 0)
            )
            issue.actions = ["dismiss"]

    if not options.include_junk:
        issues = [i for i in issues if i.kind != "junk"]
    return issues


def rename_keys(name: str) -> set[str]:
    """Normalised identities a renamed file could have.

    Covers the plain case (invalid characters simply dropped) and the sanitised
    spelling we suggest ourselves (``*`` -> ``(star)``, ``|`` -> ``-``, ...).
    """
    keys = {normalise_name(name)}
    try:
        keys.add(normalise_name(names.suggest_safe_name(name)))
    except Exception:
        pass
    return keys


def _find_renamed_sibling(dav: Dav, rel_path: str, original_name: str) -> str:
    """Look for a sibling whose name is the same file with Windows-safe characters."""
    parent = names.parent_rel(rel_path)
    wanted = rename_keys(original_name)
    try:
        children = dav.listdir(parent)
    except (DavError, OSError):
        return ""
    for child in children:
        if child.is_dir:
            continue
        if normalise_name(names.basename(child.path)) in wanted:
            return child.path
    return ""


def error_status_name(category: str) -> str:
    return category or ""


# ---------------------------------------------------------------------------
# Deep server scan
# ---------------------------------------------------------------------------

@dataclass
class DeepScanState:
    running: bool = False
    done: int = 0
    total: int = 0
    issues: int = 0
    errors: int = 0
    cancel: bool = False
    started: float = 0.0
    finished: float = 0.0
    message: str = ""
    results: list[dict] | None = None
    error_samples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        percent = round(100.0 * self.done / self.total, 1) if self.total else 0.0
        return {
            "running": self.running,
            "done": self.done,
            "total": self.total,
            "issues": self.issues,
            "errors": self.errors,
            "started": self.started,
            "finished": self.finished,
            "message": self.message,
            "progress": round(self.done / self.total, 3) if self.total else 0.0,
            "progress_percent": percent,
            "progress_text": f"{percent:.1f}%",
            "error_samples": self.error_samples[-20:],
        }


def deep_scan(
    account,
    folder,
    journal_obj: journal.Journal | None,
    dav: Dav,
    state: DeepScanState | None = None,
    scope: str = "",
    workers: int = 8,
    max_dirs: int = 20000,
) -> list[dict]:
    """Crawl the server tree and flag names/paths Windows cannot represent."""
    state = state or DeepScanState()
    lock = threading.Lock()
    results: list[dict] = []

    dirs: list[str] = [""]
    if journal_obj is not None:
        for d in journal_obj.dirs:
            if scope and not (d == scope or d.startswith(scope.rstrip("/") + "/")):
                continue
            dirs.append(d)
    if scope and scope not in dirs:
        dirs.append(scope)
    dirs = sorted(set(dirs))[:max_dirs]

    state.running = True
    state.started = time.time()
    state.total = len(dirs)
    state.done = 0
    state.issues = 0
    state.errors = 0
    state.message = "Scanning server folders"

    def scan_dir(rel_dir: str) -> list[dict]:
        out: list[dict] = []
        children = None
        last_error = ""
        for attempt in range(2):
            try:
                children = dav.listdir(rel_dir)
                break
            except DavError as exc:
                last_error = str(exc)
                if attempt == 0:
                    time.sleep(0.4)
        if children is None:
            with lock:
                state.errors += 1
                if len(state.error_samples) < 200:
                    state.error_samples.append({"path": rel_dir or "/", "error": last_error})
            return out
        child_names = [names.basename(c.path) for c in children]
        for group in names.case_collisions(child_names):
            primary = group[0]
            out.append(
                {
                    "kind": "case_conflict",
                    "path": (rel_dir + "/" if rel_dir else "") + primary,
                    "reason": "Server holds these names which differ only by case: "
                    + ", ".join(group),
                    "suggested_name": names.suggest_safe_name(primary, set(child_names)),
                    "variants": group,
                }
            )
        for child in children:
            child_name = names.basename(child.path)
            rel = child.path
            full = names.local_full_path(folder.local_root, rel)
            problems = names.windows_problems(child_name, full)
            if problems:
                out.append(
                    {
                        "kind": "filename_invalid",
                        "path": rel,
                        "reason": "; ".join(problems),
                        "size": child.size,
                        "mtime": child.mtime,
                        "fileid": child.fileid,
                        "etag": child.etag,
                        "is_dir": child.is_dir,
                        "suggested_name": names.suggest_safe_name(child_name, set(child_names)),
                    }
                )
            elif len(full) > 259:
                out.append(
                    {
                        "kind": "path_too_long",
                        "path": rel,
                        "reason": f"local path would be {len(full)} characters",
                        "size": child.size,
                        "mtime": child.mtime,
                        "fileid": child.fileid,
                        "etag": child.etag,
                        "suggested_name": "",
                    }
                )
        with lock:
            state.done += 1
            state.issues += len(out)
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(scan_dir, d): d for d in dirs}
        for future in as_completed(futures):
            if state.cancel:
                break
            try:
                results.extend(future.result())
            except Exception:
                with lock:
                    state.errors += 1

    deduped: dict[tuple[str, str], dict] = {}
    for item in results:
        deduped[(item["kind"], item["path"])] = item
    state.running = False
    state.finished = time.time()
    state.message = "Scan finished" if not state.cancel else "Scan cancelled"
    state.results = list(deduped.values())
    return state.results
