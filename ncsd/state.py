"""Local, durable state: dismissals, settings, audit trail and backups."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field

from . import config

DISMISSALS = "dismissals.json"
AUDIT = "audit.jsonl"
SETTINGS = "settings.json"
BACKUPS = "backups"

DEFAULT_SETTINGS = {
    "include_junk": False,
    "backup_remote_before_overwrite": True,
    "remote_backup_size_limit_mb": 256,
    "hash_size_limit_mb": 64,
    "deep_scan_workers": 8,
}


def _atomic_write_json(path: str, data) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


@dataclass
class Store:
    data_dir: str = field(default_factory=config.tool_data_dir)

    def __post_init__(self):
        os.makedirs(self.data_dir, exist_ok=True)

    # -- settings ----------------------------------------------------------
    @property
    def settings(self) -> dict:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(_read_json(os.path.join(self.data_dir, SETTINGS), {}) or {})
        return merged

    def update_settings(self, values: dict) -> dict:
        current = self.settings
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                current[key] = value
        _atomic_write_json(os.path.join(self.data_dir, SETTINGS), current)
        return current

    # -- dismissals --------------------------------------------------------
    def dismissals(self) -> dict:
        return _read_json(os.path.join(self.data_dir, DISMISSALS), {}) or {}

    def dismiss(self, keys: list[str], note: str = "", days: int = 0) -> int:
        data = self.dismissals()
        now = time.time()
        until = now + days * 86400 if days else 0
        for key in keys:
            data[key] = {"at": now, "note": note, "until": until}
        _atomic_write_json(os.path.join(self.data_dir, DISMISSALS), data)
        return len(keys)

    def undismiss(self, keys: list[str] | None = None) -> int:
        data = self.dismissals()
        if keys is None:
            count = len(data)
            data = {}
        else:
            count = 0
            for key in keys:
                if key in data:
                    del data[key]
                    count += 1
        _atomic_write_json(os.path.join(self.data_dir, DISMISSALS), data)
        return count

    def is_dismissed(self, key: str) -> bool:
        entry = self.dismissals().get(key)
        if not entry:
            return False
        until = entry.get("until") or 0
        return not until or until > time.time()

    # -- audit -------------------------------------------------------------
    def audit(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("at", time.time())
        path = os.path.join(self.data_dir, AUDIT)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def recent_audit(self, limit: int = 100) -> list[dict]:
        path = os.path.join(self.data_dir, AUDIT)
        if not os.path.isfile(path):
            return []
        out = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out[-limit:][::-1]

    # -- backups -----------------------------------------------------------
    def new_backup_dir(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.data_dir, BACKUPS, f"{stamp}-{os.getpid()}")
        n = 1
        while os.path.exists(path):
            path = os.path.join(self.data_dir, BACKUPS, f"{stamp}-{os.getpid()}-{n}")
            n += 1
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def resolve_backup_path(backup_root: str, rel_path: str) -> str:
        rel = rel_path.replace("/", os.sep)
        full = os.path.normpath(os.path.join(backup_root, rel))
        root = os.path.normpath(backup_root)
        if not full.startswith(root + os.sep):
            raise ValueError("refusing to write outside the backup folder")
        return full

    def backup_local_file(self, local_path: str, rel_path: str) -> str:
        dest = self.resolve_backup_path(self.new_backup_dir(), rel_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(local_path):
            shutil.copytree(local_path, dest)
        else:
            shutil.copy2(local_path, dest)
        return dest

    def backups_size(self) -> tuple[int, int]:
        root = os.path.join(self.data_dir, BACKUPS)
        total = 0
        count = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                    count += 1
                except OSError:
                    continue
        return count, total

    def prune_backups(self, keep_days: int = 30) -> int:
        root = os.path.join(self.data_dir, BACKUPS)
        if not os.path.isdir(root):
            return 0
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for name in os.listdir(root):
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed
