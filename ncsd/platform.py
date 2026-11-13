"""Where the doctor and the desktop client keep things, per operating system.

The Windows layout and the macOS layout differ in almost every path, and macOS
adds a sandbox: since the client is a sandboxed app, its configuration lives
inside `~/Library/Containers/com.nextcloud.desktopclient/Data` and only the
client (or a process with Full Disk Access) may read it. Every platform-specific
path and rule the rest of the package needs lives here, so `config`, `creds`
and `names` stay readable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

APP_NAME = "Nextcloud"
MAC_BUNDLE_ID = "com.nextcloud.desktopclient"
MAC_APP_PATHS = (
    "/Applications/Nextcloud.app",
    "~/Applications/Nextcloud.app",
)

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"


def platform_name() -> str:
    if WINDOWS:
        return "windows"
    if MACOS:
        return "macos"
    return sys.platform or "unknown"


def is_windows() -> bool:
    return WINDOWS


def is_macos() -> bool:
    return MACOS


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------

def mac_container_dir() -> str:
    """The sandbox container of the desktop client (`~/Library/Containers/...`)."""
    return os.path.expanduser(f"~/Library/Containers/{MAC_BUNDLE_ID}")


def _client_bundle_path() -> str:
    """The path of `Nextcloud.app` when it is installed."""
    for candidate in MAC_APP_PATHS:
        path = os.path.expanduser(candidate)
        if os.path.isdir(path):
            return path
    return ""


def is_sandboxed_client() -> bool:
    """True when the installed macOS client is a sandboxed app (the normal case).

    The official build carries the `com.apple.security.app-sandbox` entitlement.
    A self-built client does not, and keeps its settings outside the container.
    """
    if not MACOS:
        return False
    bundle = _client_bundle_path()
    if not bundle:
        return True  # assume the official build
    try:
        out = subprocess.run(
            ["codesign", "-d", "--entitlements", "-", bundle],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "com.apple.security.app-sandbox" in out


def mac_settings_root() -> str:
    """Qt's QSettings root for an organisation named "Nextcloud" on macOS."""
    return os.path.expanduser("~/Library/Preferences")


@dataclass(frozen=True)
class Candidate:
    path: str
    where: str


def config_candidates() -> list[Candidate]:
    """Possible locations of `nextcloud.cfg`, most likely first."""
    override = os.environ.get("NCSD_CONFIG")
    if override:
        return [Candidate(os.path.expanduser(override), "NCSD_CONFIG")]
    if WINDOWS:
        base = os.path.join(os.environ.get("APPDATA", ""), APP_NAME)
        return [Candidate(os.path.join(base, "nextcloud.cfg"), "APPDATA")]
    if MACOS:
        out: list[Candidate] = []
        if is_sandboxed_client():
            out.append(Candidate(
                os.path.join(mac_container_dir(), "Data/Library/Preferences", APP_NAME, "nextcloud.cfg"),
                "sandbox container",
            ))
        out.append(Candidate(
            os.path.join(mac_settings_root(), APP_NAME, "nextcloud.cfg"),
            "Library/Preferences",
        ))
        return out
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [Candidate(os.path.join(xdg, APP_NAME, "nextcloud.cfg"), "XDG_CONFIG_HOME")]


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LogSource:
    directory: str
    pattern: str
    where: str


def log_sources() -> list[LogSource]:
    """Directories that may hold client logs, in order of preference."""
    if WINDOWS:
        base = os.path.join(os.environ.get("APPDATA", ""), APP_NAME, "logs")
        return [LogSource(base, "nextcloud.log*", "APPDATA")]
    if MACOS:
        # The client logs into Application Support; the sandbox container is where
        # a sandboxed client really writes.
        app_support = os.path.expanduser("~/Library/Application Support/Nextcloud")
        return [
            LogSource(app_support, "nextcloud*.log*", "Application Support"),
            LogSource(os.path.join(mac_container_dir(), "Data/Library/Application Support/Nextcloud"),
                      "nextcloud*.log*", "sandbox container"),
            LogSource(os.path.expanduser("~/Library/Logs/Nextcloud"), "nextcloud*.log*", "Library/Logs"),
        ]
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return [LogSource(os.path.join(xdg, APP_NAME, "logs"), "nextcloud.log*", "XDG_DATA_HOME")]


# ---------------------------------------------------------------------------
# Tool data (backups, audit, snapshots, dismissals)
# ---------------------------------------------------------------------------

def tool_data_dir() -> str:
    """Where the doctor keeps its own files - deliberately outside the sync folder."""
    override = os.environ.get("NCSD_DATA_DIR")
    if override:
        path = os.path.expanduser(override)
    elif WINDOWS:
        path = os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME, "sync-doctor")
    elif MACOS:
        path = os.path.expanduser("~/Library/Application Support/Nextcloud/sync-doctor")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(xdg, APP_NAME, "sync-doctor")
    os.makedirs(path, exist_ok=True)
    return path


def demo_data_dir_hint() -> str:
    """A fictional data directory shown in demo mode (never a real path)."""
    if WINDOWS:
        return r"C:\Users\alex\AppData\Local\Nextcloud\sync-doctor"
    if MACOS:
        return "/Users/alex/Library/Application Support/Nextcloud/sync-doctor"
    return "/home/alex/.local/share/Nextcloud/sync-doctor"


def demo_sync_root() -> str:
    """A fictional sync folder for the demo fixtures, in this platform's style."""
    if WINDOWS:
        return r"C:\Users\alex\Nextcloud"
    if MACOS:
        return "/Users/alex/Library/CloudStorage/Nextcloud-cloud.example.com-Alex"
    return "/home/alex/Nextcloud"


def demo_vfs_mode() -> str:
    if WINDOWS:
        return "wincfapi"
    if MACOS:
        return "fileprovider"
    return ""


# ---------------------------------------------------------------------------
# Walking the sync folder
# ---------------------------------------------------------------------------

# Top-level folders that are large, rarely edited and slow to walk on a cloud folder
# (a FileProvider directory costs a provider round-trip). "ncsd check" and the UI's
# folder check skip them unless asked for everything.
CHECK_DEFAULT_SKIP = {
    "backup", "backups", "archive", "archives", "old", "migration", "loswebos migration",
}


# ---------------------------------------------------------------------------
# Opening things in the desktop
# ---------------------------------------------------------------------------

def reveal_command(path: str) -> list[str]:
    """A command that shows `path` in the platform's file manager."""
    if WINDOWS:
        return ["explorer", "/select,", os.path.normpath(path)]
    if MACOS:
        # -R selects the item in Finder.
        return ["open", "-R", os.path.normpath(path)]
    return ["xdg-open", os.path.dirname(os.path.normpath(path)) or "."]


# ---------------------------------------------------------------------------
# Desktop client process
# ---------------------------------------------------------------------------

def client_executable() -> str:
    if WINDOWS:
        return os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Nextcloud", "nextcloud.exe")
    if MACOS:
        return _client_bundle_path()
    return "nextcloud"


def client_process_names() -> list[str]:
    """Process names used to find the running client."""
    if WINDOWS:
        return ["nextcloud.exe"]
    if MACOS:
        return ["Nextcloud"]
    return ["nextcloud"]


def mac_fileprovider_extension_name() -> str:
    return "FileProviderExt"


def mac_client_path() -> str:
    return _client_bundle_path()
