"""Discover Nextcloud Desktop Client accounts, sync folders and log locations.

Reads the client's own configuration (``nextcloud.cfg``) so the doctor always
operates on exactly the same account / folder / journal as the GUI client.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable

APP_NAME = "Nextcloud"


class ConfigError(RuntimeError):
    pass


def appdata_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), APP_NAME)


def localappdata_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME)


def config_file() -> str:
    return os.path.join(appdata_dir(), "nextcloud.cfg")


def log_dir() -> str:
    return os.path.join(appdata_dir(), "logs")


def tool_data_dir() -> str:
    """Where the doctor keeps snapshots, backups, audits and cached state.

    Deliberately *outside* the sync folder so nothing we write is uploaded.
    """
    d = os.environ.get("NCSD_DATA_DIR") or os.path.join(localappdata_dir(), "sync-doctor")
    os.makedirs(d, exist_ok=True)
    return d


_VARIANT_RE = re.compile(r"^@(Variant|ByteArray|DateTime|Invalid)\(.*\)$", re.S)


def _qt_unescape(value: str) -> str:
    """QSettings escapes newlines/tabs inside values; undo the common cases."""
    if "\\" not in value:
        return value
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def read_ini(path: str) -> dict[str, dict[str, str]]:
    """Minimal QSettings-INI reader (keys may contain literal backslashes)."""
    if not os.path.isfile(path):
        raise ConfigError(f"Nextcloud client config not found: {path}")
    sections: dict[str, dict[str, str]] = {}
    current = ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n").rstrip("\r")
            if not line or line.lstrip().startswith(";"):
                continue
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped[1:-1]
                sections.setdefault(current, {})
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            sections.setdefault(current, {})[_qt_unescape(key.strip())] = _qt_unescape(value)
    return sections


@dataclass
class SyncFolder:
    index: int
    local_path: str
    target_path: str = "/"
    journal_name: str = ""
    virtual_files_mode: str = ""
    paused: bool = False
    ignore_hidden: bool = False

    @property
    def journal_path(self) -> str:
        if not self.journal_name:
            return ""
        return os.path.join(self.local_path, self.journal_name)

    @property
    def local_root(self) -> str:
        return os.path.normpath(self.local_path)

    @property
    def exists(self) -> bool:
        return os.path.isdir(self.local_root)


@dataclass
class Account:
    index: int
    url: str
    user: str
    display_name: str = ""
    server_version: str = ""
    folders: list[SyncFolder] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    @property
    def dav_root(self) -> str:
        return f"{self.base_url}/remote.php/dav/files/{_quote(self.user)}"

    @property
    def label(self) -> str:
        who = self.display_name or self.user
        return f"{who} ({self.url})"


def _quote(part: str) -> str:
    from urllib.parse import quote

    return quote(part, safe="")


@dataclass
class ClientConfig:
    path: str
    accounts: list[Account]
    client_version: str = ""
    is_vfs_enabled: bool = False

    def account(self, index: int = 0) -> Account:
        if not self.accounts:
            raise ConfigError("No Nextcloud account configured in nextcloud.cfg")
        if index < 0 or index >= len(self.accounts):
            raise ConfigError(f"Account index {index} out of range (have {len(self.accounts)})")
        return self.accounts[index]

    def all_folders(self) -> Iterable[tuple[Account, SyncFolder]]:
        for acc in self.accounts:
            for folder in acc.folders:
                yield acc, folder


def load_config(path: str | None = None) -> ClientConfig:
    path = path or config_file()
    ini = read_ini(path)
    general = ini.get("General", {})
    accounts_section = ini.get("Accounts", {})

    accounts: dict[int, Account] = {}
    folder_re = re.compile(r"^(\d+)\\(?:FoldersWithPlaceholders|Folders)\\(\d+)\\(.+)$")

    # First pass: account level keys.
    for key, value in accounts_section.items():
        m = re.match(r"^(\d+)\\(.+)$", key)
        if not m:
            continue
        idx = int(m.group(1))
        prop = m.group(2)
        if folder_re.match(key):
            continue
        acc = accounts.setdefault(idx, Account(index=idx, url="", user=""))
        if prop == "url":
            acc.url = value
        elif prop in ("dav_user", "webflow_user", "user"):
            if value and not value.startswith("@") and not acc.user:
                acc.user = value
        elif prop == "displayName":
            acc.display_name = value
        elif prop == "serverVersion":
            acc.server_version = value

    # Second pass: folders.
    folders: dict[tuple[int, int], SyncFolder] = {}
    for key, value in accounts_section.items():
        m = folder_re.match(key)
        if not m:
            continue
        acc_idx, folder_idx, prop = int(m.group(1)), int(m.group(2)), m.group(3)
        sf = folders.get((acc_idx, folder_idx))
        if sf is None:
            sf = SyncFolder(index=folder_idx, local_path="")
            folders[(acc_idx, folder_idx)] = sf
            if acc_idx in accounts:
                accounts[acc_idx].folders.append(sf)
        if prop == "localPath":
            sf.local_path = value.replace("/", os.sep) if os.sep == "\\" else value
        elif prop == "targetPath":
            sf.target_path = value or "/"
        elif prop == "journalPath":
            sf.journal_name = value
        elif prop == "virtualFilesMode":
            sf.virtual_files_mode = value
        elif prop == "paused":
            sf.paused = value.strip().lower() == "true"
        elif prop == "ignoreHiddenFiles":
            sf.ignore_hidden = value.strip().lower() == "true"

    ordered = [accounts[i] for i in sorted(accounts)]
    for acc in ordered:
        acc.folders.sort(key=lambda f: f.index)

    return ClientConfig(
        path=path,
        accounts=ordered,
        client_version=general.get("clientVersion", ""),
        is_vfs_enabled=general.get("isVfsEnabled", "false").strip().lower() == "true",
    )


def recent_logs(limit: int = 40) -> list[str]:
    """Client log files, newest last (they rotate quickly, hence the gz files)."""
    d = log_dir()
    if not os.path.isdir(d):
        return []
    files = [
        os.path.join(d, name)
        for name in os.listdir(d)
        if "nextcloud.log" in name.lower()
    ]
    files.sort(key=lambda p: os.path.getmtime(p))
    return files[-limit:]
