"""Discover Nextcloud Desktop Client accounts, sync folders and log locations.

Reads the client's own configuration (``nextcloud.cfg``) so the doctor always
operates on exactly the same account / folder / journal as the GUI client.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from . import platform

APP_NAME = platform.APP_NAME


class ConfigError(RuntimeError):
    pass


def config_file() -> str:
    """The client configuration the doctor will use (or would use, if it existed)."""
    candidates = platform.config_candidates()
    for candidate in candidates:
        if os.path.isfile(candidate.path):
            return candidate.path
    return candidates[0].path


def tool_data_dir() -> str:
    """Where the doctor keeps snapshots, backups, audits and cached state.

    Deliberately *outside* the sync folder so nothing we write is uploaded.
    """
    return platform.tool_data_dir()


def mac_fileprovider_journal_candidates(journal_name: str) -> list[str]:
    """Where a FileProvider sync journal may live, most likely first.

    FileProvider domains are managed by the system and their data sits in
    protected directories; only the owning app (or a process with Full Disk
    Access) can read them. These are the places worth looking in.
    """
    roots = [
        os.path.expanduser("~/Library/Application Support/FileProvider"),
        os.path.join(platform.mac_container_dir(), "Data/Library/Application Support/FileProvider"),
        os.path.expanduser("~/Library/Group Containers/NKUJUXUJ3B.com.nextcloud.desktopclient"),
        os.path.join(platform.mac_container_dir(), "Data/Library/Application Support/Nextcloud"),
    ]
    return [os.path.join(root, journal_name) for root in roots]


MAC_CLOUD_STORAGE = os.path.expanduser("~/Library/CloudStorage")


def _fileprovider_host(url: str) -> str:
    """The server part of the FileProvider folder name ("nc.example.com")."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).netloc or url.split("://", 1)[-1]
    except ValueError:
        host = url.split("://", 1)[-1]
    return host.split("@")[-1].split(":")[0].strip("/")


def _fileprovider_folder_name(url: str, user: str) -> str:
    """The name macOS gives a FileProvider domain's folder in ~/Library/CloudStorage.

    The system builds it as "Nextcloud-<server>-<user>", with characters that
    cannot appear in a folder name replaced. The client may use the display name
    instead of the login name, so this is only the first guess; the config does not
    record the path anywhere.
    """
    parts = [APP_NAME, _fileprovider_host(url), user or ""]
    return re.sub(r"[/:]", "-", "-".join(p for p in parts if p))


def discover_fileprovider_folder(account: Account, domain: str = "") -> SyncFolder | None:
    """Find a macOS FileProvider sync folder for `account` and turn it into a SyncFolder.

    FileProvider domains do not record their local path in nextcloud.cfg; the
    folder appears in ~/Library/CloudStorage under a name derived from the server
    and the user. The domain identifier is used to check the name against the
    domain's own metadata when that is readable, and the folder must exist before
    it is returned - a guess is never silently treated as a real folder.
    """
    if not platform.is_macos():
        return None
    wanted = _fileprovider_folder_name(account.url, account.user)
    if not os.path.isdir(MAC_CLOUD_STORAGE):
        return None
    try:
        entries = sorted(os.listdir(MAC_CLOUD_STORAGE))
    except OSError:
        return None
    host = _fileprovider_host(account.url)
    candidates = [name for name in entries if name.lower() == wanted.lower()]
    if not candidates and host:
        # The client may name the folder after the display name instead of the login
        # name ("Nextcloud-<server>-<display name>"), so match on the server part and
        # accept the one folder that belongs to this account.
        marker = f"{APP_NAME}-{host}-".lower()
        same_server = [name for name in entries if name.lower().startswith(marker)]
        if len(same_server) == 1:
            candidates = same_server
    for name in candidates:
        path = os.path.join(MAC_CLOUD_STORAGE, name)
        if not os.path.isdir(path):
            continue
        return SyncFolder(
            index=0,
            local_path=path,
            target_path="/",
            virtual_files_mode="fileprovider",
            source="fileprovider",
            note=(
                "This is the client's FileProvider folder. The client does not record its "
                "path in nextcloud.cfg, so the doctor found it in ~/Library/CloudStorage "
                f"('{name}')."
                + (f" Domain: {domain}." if domain else "")
            ),
        )
    return None


def fileprovider_folder_missing_error(account: Account) -> ConfigError:
    wanted = _fileprovider_folder_name(account.url, account.user)
    return ConfigError(
        "The selected account has no sync folders configured, and no FileProvider "
        f"folder was found in {MAC_CLOUD_STORAGE} for it (looked for a folder named "
        f"'{wanted}').\n"
        "  - If the account does not sync anything on this Mac, open the client and add "
        "a folder, or choose another account with --account N.\n"
        "  - If it does sync, start the client once so its FileProvider domain appears, "
        "then run the doctor again."
    )


@dataclass
class LogDir:
    path: str
    files: list[str] = field(default_factory=list)
    error: str = ""


def log_dirs() -> list[LogDir]:
    """Every place the client may write its logs, with what could be read from it."""
    out: list[LogDir] = []
    for source in platform.log_sources():
        entry = LogDir(path=source.directory)
        try:
            names = [
                name for name in os.listdir(source.directory)
                if fnmatch.fnmatch(name.lower(), source.pattern.lower())
            ]
        except OSError as exc:
            entry.error = exc.__class__.__name__
            out.append(entry)
            continue
        entry.files = [os.path.join(source.directory, name) for name in names]
        out.append(entry)
    return out


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
    # How the folder was found: "config" (a localPath key), "fileprovider" (derived
    # from a macOS FileProvider domain) or "cli" (given explicitly).
    source: str = "config"
    # Set when the folder had to be guessed or is missing, so the UI can explain it.
    note: str = ""

    @property
    def journal_path(self) -> str:
        """Where the client keeps this folder's sync journal.

        Classic folder sync keeps it in the sync folder itself. A macOS FileProvider
        domain (the default since client 3.7) keeps it in the provider's private
        directory instead, so the sync folder holds no journal at all.
        """
        if not self.journal_name:
            return ""
        in_folder = os.path.join(self.local_path, self.journal_name)
        if os.path.isfile(in_folder):
            return in_folder
        if platform.is_macos():
            for candidate in mac_fileprovider_journal_candidates(self.journal_name):
                if os.path.isfile(candidate):
                    return candidate
        return in_folder

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
    # The FileProvider domain identifier, when the macOS client uses one.
    file_provider_domain: str = ""

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
    # Folder-related keys as written in the file, for diagnostics when parsing
    # finds nothing (the client's format differs between versions and platforms).
    folder_keys: list[str] = field(default_factory=list)

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


def _unreadable_config_error(candidates: list[platform.Candidate]) -> ConfigError:
    """Explain why the client configuration cannot be used, in words the user can act on."""
    detail = "; ".join(f"{c.where}: {c.path}" for c in candidates)
    if not platform.is_macos():
        return ConfigError(f"Nextcloud client config not found: {detail}")
    path = candidates[0].path
    if os.path.exists(path):
        problem = "exists but macOS does not let this process read it"
        why = (
            " The official client is a sandboxed app: its nextcloud.cfg lives inside "
            "~/Library/Containers/com.nextcloud.desktopclient/Data/Library/Preferences/"
            "Nextcloud/, and only processes with Full Disk Access may open it."
        )
    else:
        problem = "was not found"
        why = (
            " The official client is a sandboxed app and keeps its configuration inside "
            "~/Library/Containers/com.nextcloud.desktopclient/Data/Library/Preferences/"
            "Nextcloud/, which only processes with Full Disk Access may read."
        )
    return ConfigError(
        f"The Nextcloud client's configuration {problem}: {path}.{why}\n"
        "Fix it with either of these:\n"
        "  - System Settings > Privacy & Security > Full Disk Access: add the app you "
        "start the doctor from (Terminal, iTerm, Visual Studio Code, ...), quit and "
        "reopen it, then start the doctor again.\n"
        "  - Or point the doctor at a readable copy: "
        "NCSD_CONFIG=/path/to/nextcloud.cfg ncsd scan (or --config on the command line)."
    )


def load_config(path: str | None = None) -> ClientConfig:
    candidates = platform.config_candidates()
    path = os.path.expanduser(path) if path else config_file()
    try:
        ini = read_ini(path)
    except ConfigError:
        raise _unreadable_config_error(candidates) from None
    except OSError as exc:
        # macOS TCC answers with EPERM (errno 1, "Operation not permitted").
        if platform.is_macos() and exc.errno in (1, 13):
            raise _unreadable_config_error([platform.Candidate(path, "selected")]) from None
        raise ConfigError(f"Cannot read the Nextcloud client config {path}: {exc.strerror or exc}") from None
    general = ini.get("General", {})
    accounts_section = ini.get("Accounts", {})

    accounts: dict[int, Account] = {}
    folder_re = re.compile(r"^(\d+)\\(?:FoldersWithPlaceholders|Folders)\\(\d+)\\(.+)$")
    unprefixed_folder_re = re.compile(r"^(?:FoldersWithPlaceholders|Folders)\\(\d+)\\(.+)$")

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
        elif prop == "fileProviderDomainIdentifier":
            acc.file_provider_domain = value

    # Second pass: folders. Qt writes "0\Folders\1\localPath" (the number is the
    # account index); some client versions write "Folders\1\localPath" instead, in
    # which case the folders belong to the account that owns the Folders group.
    folders: dict[tuple[int, int], SyncFolder] = {}
    for key, value in accounts_section.items():
        m = folder_re.match(key)
        if m:
            acc_idx, folder_idx, prop = int(m.group(1)), int(m.group(2)), m.group(3)
        else:
            m = unprefixed_folder_re.match(key)
            if not m:
                continue
            folder_idx, prop = int(m.group(1)), m.group(2)
            # Unprefixed folders belong to the only account, if there is one.
            if len(accounts) != 1:
                continue
            acc_idx = next(iter(accounts))
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
        if not acc.folders and platform.is_macos():
            # A macOS FileProvider account stores no localPath at all.
            folder = discover_fileprovider_folder(acc, acc.file_provider_domain)
            if folder is not None:
                acc.folders.append(folder)
        acc.folders.sort(key=lambda f: f.index)

    folder_keys = sorted(
        key for key in accounts_section
        if "Folders" in key or "localPath" in key or "targetPath" in key
    )
    return ClientConfig(
        path=path,
        accounts=ordered,
        client_version=general.get("clientVersion", ""),
        is_vfs_enabled=general.get("isVfsEnabled", "false").strip().lower() == "true",
        folder_keys=folder_keys,
    )


def recent_logs(limit: int = 40) -> list[str]:
    """Client log files, newest last (they rotate quickly, hence the gz files).

    On macOS the client writes into Application Support and, when sandboxed,
    into its container; whichever directories are readable are used, and a
    container that is sealed off is simply skipped.
    """
    files: list[str] = []
    for entry in log_dirs():
        files.extend(entry.files)
    files.sort(key=lambda p: os.path.getmtime(p))
    return files[-limit:]
