"""Resolution actions.

Every action is planned first (dry run) and then executed. Local files are never
deleted outright: they are moved to the backup folder. Server overwrites back up
the previous server version first. Each execution is appended to an audit log.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from . import names
from .dav import Dav, DavError
from .state import Store


class ActionError(RuntimeError):
    pass


@dataclass
class Plan:
    action: str
    path: str
    summary: str
    steps: list[str] = field(default_factory=list)
    destructive: bool = False
    warning: str = ""
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "path": self.path,
            "summary": self.summary,
            "steps": self.steps,
            "destructive": self.destructive,
            "warning": self.warning,
            "params": self.params,
        }


@dataclass
class Outcome:
    ok: bool
    action: str
    path: str
    message: str
    backup: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "path": self.path,
            "message": self.message,
            "backup": self.backup,
            "details": self.details,
        }


ACTION_LABELS = {
    "keep_local": "Keep the local version",
    "keep_server": "Keep the server version",
    "keep_both": "Keep both (server wins the original name)",
    "keep_both_local_name": "Keep both (local wins the original name)",
    "rename_remote": "Rename on the server",
    "rename_local": "Rename locally",
    "download_as": "Download the server copy under another name",
    "download_remote": "Download from the server",
    "upload_local": "Upload the local copy",
    "delete_remote": "Delete on the server (moves to trash)",
    "delete_local": "Delete locally (kept in backup)",
    "restore_version": "Download an earlier server version into the sync folder",
    "restore_version_in_place": "Roll the server file back to an earlier version",
    "ignore": "Add to the client's ignore list",
    "dismiss": "Hide from this list",
}

# These move or compare file *content*; they make no sense for a folder.
FILE_ONLY_ACTIONS = {
    "keep_local", "keep_server", "keep_both", "keep_both_local_name",
    "download_remote", "upload_local", "download_as",
    "restore_version", "restore_version_in_place",
}


def _glob_escape(path: str) -> str:
    """Escape a literal path for the client's gitignore-style exclude file."""
    out = []
    for ch in path:
        if ch in "*?[":
            out.append(f"[{ch}]")
        elif ch == "]":
            out.append("[]]")
        else:
            out.append(ch)
    return "".join(out)


class Resolver:
    def __init__(self, account, folder, dav: Dav, store: Store):
        self.account = account
        self.folder = folder
        self.dav = dav
        self.store = store

    # -- helpers -----------------------------------------------------------
    def local_path(self, rel_path: str) -> str:
        root = self.folder.local_root
        full = names.local_full_path(root, rel_path)
        norm_root = os.path.normpath(root)
        norm_full = os.path.normpath(full)
        if not (norm_full == norm_root or norm_full.startswith(norm_root + os.sep)):
            raise ActionError(f"Path escapes the sync folder: {rel_path}")
        if ".." in rel_path.replace("\\", "/").split("/"):
            raise ActionError(f"Refusing to act on a path containing '..': {rel_path}")
        return norm_full

    def _remote_stat(self, rel_path: str):
        try:
            return self.dav.stat(rel_path)
        except DavError as exc:
            if exc.status == 404:
                return None
            raise ActionError(str(exc)) from None

    def _conflict_copy_name(self, rel_path: str) -> str:
        base = names.basename(rel_path)
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, ext = base, ""
        stamp = time.strftime("%Y-%m-%d %H%M%S")
        # Deliberately NOT "(conflicted copy ...)": the client refuses to upload
        # those, so a "keep both" copy would stay local-only forever.
        suffix = f" (local copy {stamp})"
        return stem + suffix + ("." + ext if ext else "")

    def _rel_with_new_name(self, rel_path: str, new_name: str) -> str:
        parent = names.parent_rel(rel_path)
        return f"{parent}/{new_name}" if parent else new_name

    def _validate_new_name(self, new_name: str, rel_path: str) -> str:
        new_name = new_name.strip().replace("\\", "/").split("/")[-1]
        if not new_name:
            raise ActionError("The new name is empty")
        problems = names.windows_problems(new_name)
        if problems:
            raise ActionError(f"'{new_name}' is still not valid on Windows: {'; '.join(problems)}")
        target_rel = self._rel_with_new_name(rel_path, new_name)
        if target_rel == rel_path:
            raise ActionError("The new name is identical to the current name")
        full = self.local_path(target_rel)
        if os.path.exists(full):
            raise ActionError(f"A local file named '{new_name}' already exists")
        if self._remote_stat(target_rel) is not None:
            raise ActionError(f"A file named '{new_name}' already exists on the server")
        return new_name

    def _backup_remote(self, rel_path: str, item) -> str:
        settings = self.store.settings
        if not settings.get("backup_remote_before_overwrite", True):
            return ""
        limit = int(settings.get("remote_backup_size_limit_mb", 256)) * 1024 * 1024
        if item is not None and item.size and item.size > limit:
            return ""
        dest = Store.resolve_backup_path(self.store.new_backup_dir(), rel_path + ".server-version")
        self.dav.download(rel_path, dest, expected_size=item.size if item else None)
        return dest

    def _backup_local(self, rel_path: str) -> str:
        full = self.local_path(rel_path)
        if not os.path.exists(full):
            return ""
        return self.store.backup_local_file(full, rel_path)

    def _set_mtime(self, path: str, mtime: float) -> None:
        if mtime and mtime > 0:
            try:
                os.utime(path, (mtime, mtime))
            except OSError:
                pass

    # -- planning ----------------------------------------------------------
    def plan(self, action: str, rel_path: str, params: dict | None = None) -> Plan:
        params = params or {}
        full = self.local_path(rel_path)
        local_exists = os.path.exists(full)
        local_is_dir = os.path.isdir(full)
        remote = self._remote_stat(rel_path)
        remote_exists = remote is not None
        remote_is_dir = bool(remote and remote.is_dir)
        is_dir = local_is_dir or remote_is_dir
        name = names.basename(rel_path)

        if is_dir and action in FILE_ONLY_ACTIONS:
            raise ActionError(
                f"'{name}' is a folder - that action only works on files. "
                "Use rename, delete, ignore or hide instead."
            )

        if action == "keep_local":
            if not local_exists:
                raise ActionError("The local file does not exist any more")
            steps = []
            if remote_exists:
                steps.append(f"Back up the current server version of '{name}' locally")
                steps.append(f"Upload the local file ({_human_size(os.path.getsize(full))}) and overwrite the server copy")
            else:
                steps.append("Upload the local file (it does not exist on the server)")
            return Plan(action, rel_path, f"Replace the server copy of '{name}' with your local file",
                        steps, destructive=remote_exists, params=params)

        if action in ("keep_server", "download_remote"):
            if not remote_exists:
                raise ActionError("The file does not exist on the server")
            steps = []
            if local_exists:
                steps.append(f"Back up the local file ({_human_size(os.path.getsize(full))})")
                steps.append("Overwrite the local file with the server version")
            else:
                steps.append(f"Download the server file ({_human_size(remote.size)}) into the sync folder")
            steps.append("Keep the server modification time so the client sees the file as up to date")
            return Plan(action, rel_path, f"Replace your local '{name}' with the server version",
                        steps, destructive=local_exists, params=params)

        if action in ("keep_both", "keep_both_local_name"):
            if not remote_exists and not local_exists:
                raise ActionError("Neither version exists any more")
            copy_name = self._conflict_copy_name(rel_path)
            if action == "keep_both":
                if not remote_exists:
                    raise ActionError("There is no server version to keep - use 'keep_local' instead")
                steps = [f"Rename your local file to '{copy_name}'",
                         "Download the server version into the original name",
                         "Both copies will be synced"]
                return Plan(action, rel_path, f"Keep both: server version under '{name}', your version as '{copy_name}'",
                            steps, params={**params, "copy_name": copy_name})
            steps = [f"Rename your local file to '{copy_name}'",
                     f"Download the server version as '{copy_name}' instead of the original name"]
            return Plan(action, rel_path, f"Keep both: your version keeps the name, the server copy becomes '{copy_name}'",
                        steps, params={**params, "copy_name": copy_name})

        if action == "rename_remote":
            new_name = params.get("new_name") or ""
            proposed = self._validate_new_name(new_name or names.suggest_safe_name(name), rel_path)
            steps = [f"Rename '{name}' to '{proposed}' on the server",
                     "Rename the local file too, if it exists, so nothing is re-uploaded"]
            if not local_exists:
                steps = [f"Rename '{name}' to '{proposed}' on the server",
                         "The client will then download it under the new name"]
            return Plan(action, rel_path, f"Rename on the server to '{proposed}'", steps,
                        params={**params, "new_name": proposed})

        if action == "rename_local":
            new_name = params.get("new_name") or ""
            proposed = self._validate_new_name(new_name or names.suggest_safe_name(name), rel_path)
            steps = [f"Rename the local file to '{proposed}'",
                     "The client will upload it under the new name",
                     f"The server copy of '{name}' stays and will be downloaded again"]
            return Plan(action, rel_path, f"Rename the local file to '{proposed}'", steps,
                        params={**params, "new_name": proposed})

        if action == "download_as":
            if not remote_exists:
                raise ActionError("The file does not exist on the server")
            new_name = params.get("new_name") or names.suggest_safe_name(name)
            problems = names.windows_problems(new_name)
            if problems:
                raise ActionError(f"'{new_name}' is not valid on Windows: {'; '.join(problems)}")
            target_rel = self._rel_with_new_name(rel_path, new_name)
            if os.path.exists(self.local_path(target_rel)):
                raise ActionError(f"'{new_name}' already exists locally")
            steps = [f"Download the server file into the sync folder as '{new_name}'",
                     "The server copy keeps its original name - expect a second copy on the server"]
            return Plan(action, rel_path, f"Download the server copy as '{new_name}'", steps,
                        params={**params, "new_name": new_name})

        if action == "upload_local":
            if not local_exists:
                raise ActionError("The local file does not exist any more")
            return Plan(action, rel_path, f"Upload '{name}' to the server",
                        ["Upload the local file"], params=params)

        if action == "delete_remote":
            if not remote_exists:
                raise ActionError("The file does not exist on the server")
            return Plan(action, rel_path, f"Delete '{name}' on the server",
                        ["Move the file to the server's trash bin (recoverable for the configured retention period)"],
                        destructive=True, params=params)

        if action == "delete_local":
            if not local_exists:
                raise ActionError("The local file does not exist any more")
            steps = ["Copy the local file to the doctor's backup folder", "Remove it from the sync folder"]
            if remote_exists:
                steps.append("The client will download the server version again on the next sync")
            return Plan(action, rel_path, f"Remove your local copy of '{name}'",
                        steps, destructive=True, params=params)

        if action == "restore_version":
            version = params.get("version") or ""
            if not remote or not remote.fileid:
                raise ActionError("No server file id available for version restore")
            if not version:
                raise ActionError("No version selected")
            steps = [f"Download server version {version}"]
            if local_exists:
                steps.insert(0, "Back up the current local file")
            steps.append("Write it into the sync folder so the client uploads it as the new current version")
            return Plan(action, rel_path, f"Restore server version {version} locally", steps, params=params)

        if action == "restore_version_in_place":
            version = params.get("version") or ""
            if not remote or not remote.fileid:
                raise ActionError("No server file id available for version restore")
            if not version:
                raise ActionError("No version selected")
            steps = [
                f"Roll the server file back to version {version} (the server keeps the current content as a version)",
                "Download the rolled-back version locally",
            ]
            return Plan(action, rel_path, f"Roll the server file back to version {version}", steps,
                        destructive=True, params=params)

        if action == "ignore":
            pattern = _glob_escape(rel_path)
            return Plan(
                action, rel_path,
                f"Add '{rel_path}' to the folder's ignore list",
                [
                    f"Append '{pattern}' to .sync-exclude.lst in the sync folder",
                    "The file stays on the server but will never be downloaded here",
                    "The client re-reads the exclude file at the start of the next sync",
                ],
                params={**params, "pattern": pattern},
            )

        if action == "dismiss":
            return Plan(action, rel_path, "Hide this entry from the list", [], params=params)

        raise ActionError(f"Unknown action '{action}'")

    # -- execution ---------------------------------------------------------
    def apply(self, action: str, rel_path: str, params: dict | None = None) -> Outcome:
        params = params or {}
        plan = self.plan(action, rel_path, params)
        full = self.local_path(rel_path)
        item = self._remote_stat(rel_path)
        backup = ""
        details: dict = {}

        try:
            if action in ("keep_local", "upload_local"):
                backup = self._backup_remote(rel_path, item) if (action == "keep_local" and item) else ""
                mtime = os.path.getmtime(full)
                self.dav.upload(rel_path, full, mtime=mtime)
                uploaded = self._remote_stat(rel_path)
                details = {"server_size": uploaded.size if uploaded else None, "local_size": os.path.getsize(full)}
                message = f"Uploaded '{names.basename(rel_path)}' to the server"

            elif action in ("keep_server", "download_remote"):
                backup = self._backup_local(rel_path) if os.path.exists(full) else ""
                self.dav.download(rel_path, full, expected_size=item.size if item else None)
                self._set_mtime(full, item.mtime if item else 0)
                details = {"local_size": os.path.getsize(full), "server_size": item.size if item else None}
                message = f"Replaced the local copy of '{names.basename(rel_path)}' with the server version"

            elif action in ("keep_both", "keep_both_local_name"):
                copy_name = plan.params["copy_name"]
                copy_rel = self._rel_with_new_name(rel_path, copy_name)
                copy_full = self.local_path(copy_rel)
                if action == "keep_both":
                    if os.path.exists(full):
                        shutil.move(full, copy_full)
                    self.dav.download(rel_path, full, expected_size=item.size if item else None)
                    self._set_mtime(full, item.mtime if item else 0)
                    message = f"Kept both versions: '{names.basename(rel_path)}' is now the server copy, '{copy_name}' is yours"
                else:
                    self.dav.download(rel_path, copy_full, expected_size=item.size if item else None)
                    self._set_mtime(copy_full, item.mtime if item else 0)
                    message = f"Kept both versions: your file stays, the server copy is '{copy_name}'"
                details = {"copy_name": copy_name}

            elif action == "rename_remote":
                new_name = plan.params["new_name"]
                target_rel = self._rel_with_new_name(rel_path, new_name)
                self.dav.move(rel_path, target_rel, overwrite=False)
                moved = self._remote_stat(target_rel)
                if os.path.exists(full):
                    target_full = self.local_path(target_rel)
                    shutil.move(full, target_full)
                    if moved and moved.mtime:
                        self._set_mtime(target_full, moved.mtime)
                    details["local_renamed"] = True
                details["new_path"] = target_rel
                message = f"Renamed on the server to '{new_name}'"

            elif action == "rename_local":
                new_name = plan.params["new_name"]
                target_rel = self._rel_with_new_name(rel_path, new_name)
                target_full = self.local_path(target_rel)
                shutil.move(full, target_full)
                details["new_path"] = target_rel
                message = f"Renamed locally to '{new_name}'"

            elif action == "download_as":
                new_name = plan.params["new_name"]
                target_rel = self._rel_with_new_name(rel_path, new_name)
                target_full = self.local_path(target_rel)
                self.dav.download(rel_path, target_full, expected_size=item.size if item else None)
                self._set_mtime(target_full, item.mtime if item else 0)
                details["new_path"] = target_rel
                message = f"Downloaded the server copy as '{new_name}'"

            elif action == "delete_remote":
                self.dav.delete(rel_path)
                message = f"Deleted '{names.basename(rel_path)}' on the server (moved to trash)"

            elif action == "delete_local":
                backup = self._backup_local(rel_path)
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                if item is not None:
                    details["note"] = "the client will download the server copy again"
                message = f"Removed the local copy of '{names.basename(rel_path)}'"

            elif action == "restore_version":
                version = params["version"]
                if os.path.exists(full):
                    backup = self._backup_local(rel_path)
                self.dav.download_version(item.fileid, version, full)
                now = time.time()
                self._set_mtime(full, now)
                details = {"version": version}
                message = f"Restored server version {version} of '{names.basename(rel_path)}' into the sync folder"

            elif action == "restore_version_in_place":
                version = params["version"]
                if os.path.exists(full):
                    backup = self._backup_local(rel_path)
                self.dav.restore_version(item.fileid, version)
                refreshed = self._remote_stat(rel_path)
                self.dav.download(rel_path, full, expected_size=refreshed.size if refreshed else None)
                self._set_mtime(full, refreshed.mtime if refreshed else 0)
                details = {"version": version}
                message = f"Server file rolled back to version {version}"

            elif action == "ignore":
                pattern = plan.params["pattern"]
                if pattern.startswith("]"):
                    raise ActionError("Refusing to write an exclude pattern starting with ']' "
                                      "(that tells the client to delete local files)")
                exclude_path = os.path.join(self.folder.local_root, ".sync-exclude.lst")
                try:
                    with open(exclude_path, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.read().splitlines()
                except OSError:
                    lines = []
                if pattern not in lines:
                    lines.append(pattern)
                    with open(exclude_path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write("\n".join(lines) + "\n")
                details = {"exclude_file": exclude_path, "pattern": pattern}
                message = f"Added '{pattern}' to the ignore list"

            elif action == "dismiss":
                self.store.dismiss([params.get("key") or rel_path], note=params.get("note", ""))
                message = "Hidden from the list"

            else:
                raise ActionError(f"Unknown action '{action}'")

        except ActionError:
            raise
        except DavError as exc:
            raise ActionError(str(exc)) from None

        self.store.audit(
            {
                "action": action,
                "path": rel_path,
                "params": {k: v for k, v in params.items() if k != "key"},
                "message": message,
                "backup": backup,
                "ok": True,
            }
        )
        return Outcome(ok=True, action=action, path=rel_path, message=message, backup=backup, details=details)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
