"""Reuse the desktop client's own stored app password.

The Nextcloud client stores one credential per account. Reading it means the
doctor never has to ask the user for a password, and it always uses exactly the
token the client itself would use.

* Windows: Credential Manager (read-only).
* macOS: the login keychain through the `security` command line tool. The client
  (QtKeychain) stores the app password as a generic password with service
  `Nextcloud` and account `<user>_app-password:<server url>[:<id>]`. macOS asks
  the user once before a terminal app may read it; "Always Allow" makes it
  silent afterwards. `security` is a system binary, so this keeps the "standard
  library only" rule.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from dataclasses import dataclass

from . import platform

try:  # the module itself is Windows-only, the package is not
    import ctypes.wintypes as wt
except Exception:  # pragma: no cover - non-Windows platforms
    wt = None

CRED_TYPE_GENERIC = 1
MAC_KEYCHAIN_SERVICE = "Nextcloud"


class AuthError(RuntimeError):
    pass


if wt is not None:

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD),
            ("Type", wt.DWORD),
            ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR),
            ("LastWritten", wt.FILETIME),
            ("CredentialBlobSize", wt.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wt.DWORD),
            ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wt.LPWSTR),
            ("UserName", wt.LPWSTR),
        ]

else:  # pragma: no cover
    _CREDENTIAL = None


_ALLOWED = os.environ.get("NCSD_ALLOW_CREDENTIAL_REUSE", "1") != "0"


# ---------------------------------------------------------------------------
# Windows Credential Manager
# ---------------------------------------------------------------------------

def _advapi():
    if wt is None:
        raise AuthError("Windows Credential Manager is only available on Windows.")
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    lib.CredReadW.restype = wt.BOOL
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredEnumerateW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)))]
    lib.CredEnumerateW.restype = wt.BOOL
    return lib


def _blob_to_secret(blob: bytes) -> str:
    # QtKeychain stores the password as UTF-8 bytes; older paths stored UTF-16LE.
    if blob and len(blob) % 2 == 0 and b"\x00" in blob:
        try:
            text = blob.decode("utf-16-le")
            if text.isprintable():
                return text.rstrip("\x00")
        except UnicodeDecodeError:
            pass
    text = blob.decode("utf-8", "replace")
    return text.rstrip("\x00")


def _read(target: str) -> str | None:
    lib = _advapi()
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not lib.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        blob = ctypes.string_at(ctypes.cast(cred.CredentialBlob, ctypes.c_void_p), cred.CredentialBlobSize)
        return _blob_to_secret(blob)
    finally:
        lib.CredFree(ptr)


def list_targets() -> list[str]:
    lib = _advapi()
    count = wt.DWORD(0)
    arr = ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))()
    if not lib.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(arr)):
        return []
    out: list[str] = []
    try:
        for i in range(count.value):
            entry = arr[i]
            if not entry:
                continue
            name = entry.contents.TargetName
            if name:
                out.append(str(name))
    finally:
        # CredFree takes the array pointer exactly once.
        lib.CredFree(arr)
    return out


# ---------------------------------------------------------------------------
# macOS login keychain
# ---------------------------------------------------------------------------

_SECURITY = "/usr/bin/security"
_ACCT_ATTR_RE = re.compile(r'"acct"<blob>="(.*)"')


def _security(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_SECURITY, *args], capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError:
        raise AuthError(f"{_SECURITY} not found; cannot read the macOS keychain.") from None
    except subprocess.SubprocessError as exc:
        raise AuthError(f"Could not query the macOS keychain: {exc}") from None


def keychain_accounts(service: str = MAC_KEYCHAIN_SERVICE) -> list[str]:
    """Account names of every generic password stored under `service`.

    The keychain database file keeps its attribute table in plain text: the
    creation and modification dates as ASCII, then the service name and the account
    name, each length-prefixed and NUL-terminated. That table is not secret, so this
    never asks for a password - unlike `security find-generic-password`, which would.
    Secrets stay where they belong: only `keychain_password` reads them, through
    `security`.

    `~/Library/Keychains` and `/Library/Keychains` are the user's search list (the
    login keychain); the system keychain is not part of it.
    """
    roots = [
        os.path.expanduser("~/Library/Keychains"),
        "/Library/Keychains",
    ]
    out: list[str] = []
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".keychain-db"):
                    out.extend(_accounts_in_keychain(os.path.join(dirpath, name), service))
    return out


_TIMEDATE_RE = re.compile(rb"\d{14}Z\x00")
_ZERO4 = bytes(4)


def _read_field(data: bytes, pos: int, max_len: int, length_bytes: int) -> tuple[bytes, int] | None:
    """A length-prefixed NUL-terminated attribute, or None if `pos` does not hold one."""
    if pos + length_bytes >= len(data):
        return None
    length = int.from_bytes(data[pos:pos + length_bytes], "big")
    end = pos + length_bytes + length
    if not (0 < length <= max_len) or end >= len(data) or data[end] != 0:
        return None
    return data[pos + length_bytes:end], end + 1


def _accounts_in_keychain(path: str, service: str) -> list[str]:
    """(service, account) attribute pairs of the generic passwords in one keychain file."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    pairs: list[tuple[bytes, bytes]] = []
    pos = 0
    while True:
        m = _TIMEDATE_RE.search(data, pos)
        if not m:
            break
        # Two timedates (created, modified) in a row start a record.
        second = _TIMEDATE_RE.match(data, m.end())
        if not second:
            pos = m.start() + 1
            continue
        # The service name follows the timedates, but records carry one to two extra
        # zeroed attributes first; try each offset and keep the one that ends in NUL.
        base = second.end()
        first = None
        for skip in (0, 4, 8):
            first = _read_field(data, base + skip, 64, 4)
            if first is not None:
                break
        if first is None:
            pos = second.start() + 1
            continue
        svce, pos = first
        # the account follows the empty attribute: 4 zero bytes, 2-byte length, text, NUL
        if data[pos:pos + 4] != _ZERO4:
            pos = second.start() + 1
            continue
        account = _read_field(data, pos + 4, 1024, 2)
        if account is None:
            pos = second.start() + 1
            continue
        acct, pos = account
        pairs.append((svce, acct))
    wanted = service.encode("utf-8")
    return [acct.decode("utf-8", "replace") for svce, acct in pairs if svce == wanted]


def keychain_password(account: str, service: str = MAC_KEYCHAIN_SERVICE) -> str | None:
    result = _security(["find-generic-password", "-s", service, "-a", account, "-w"])
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    return token or None


# ---------------------------------------------------------------------------
# Ranking (shared by both stores)
# ---------------------------------------------------------------------------

def _rank(target: str, host: str, user: str) -> int:
    """How well a stored item matches an account. The app password wins.

    The client stores two items per account: the login credentials
    ("<user>:<url>") and the app password ("<user>_app-password:<url>"). The app
    password is the one that works for WebDAV without the client's login flow, so
    it must outrank the other one, not merely tie with it.
    """
    low = target.lower()
    score = 0
    if host and host.lower() in low:
        score += 100
    if user and user.lower() in low:
        score += 10
    if "app-password" in low:
        # Only a tie-breaker: another server's app password must never win over
        # this server's login credentials.
        score += 1
    return score


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("://", 1)[-1].rstrip("/")
    return parts.netloc or url.split("://", 1)[-1].rstrip("/")


@dataclass
class Credential:
    token: str
    target: str
    user: str = ""
    source: str = "credential-manager"

    def basic_auth(self, user: str) -> tuple[str, str]:
        return (user, self.token)


def find_account_credential(account, explicit_password: str | None = None) -> Credential:
    """Locate the token for `account` (`ncsd.config.Account`)."""
    if explicit_password:
        return Credential(token=explicit_password, target="", user=account.user, source="cli")

    if not _ALLOWED:
        raise AuthError(
            "Credential reuse disabled. Pass an app password explicitly "
            "(Nextcloud -> Settings -> Security -> App passwords)."
        )

    host = _host_of(account.url)
    user = account.user or ""

    if platform.is_macos():
        accounts = [a for a in keychain_accounts() if a]
        candidates = sorted(accounts, key=lambda a: _rank(a, host, user), reverse=True)
        for acct in candidates:
            if _rank(acct, host, user) <= 0:
                continue
            token = keychain_password(acct)
            if token:
                return Credential(token=token, target=f"{MAC_KEYCHAIN_SERVICE}:{acct}",
                                  user=user, source="macos-keychain")
        raise AuthError(
            "No stored Nextcloud credential found in the macOS keychain. Open the client "
            "once so it can store its app password, or pass --password <app-password> "
            "(Nextcloud -> Settings -> Security -> App passwords)."
        )

    if not platform.is_windows():
        raise AuthError(
            "Credential reuse is only implemented for Windows and macOS. Pass an app "
            "password explicitly (Nextcloud -> Settings -> Security -> App passwords)."
        )

    targets = [t for t in list_targets() if t.lower().startswith("nextcloud")]

    # Rank: app-password entry for this user/host, then any entry for this host.
    candidates = sorted(targets, key=lambda t: _rank(t, host, user), reverse=True)
    for target in candidates:
        if _rank(target, host, user) <= 0:
            continue
        token = _read(target)
        if token:
            return Credential(token=token, target=target, user=user)

    raise AuthError(
        "No stored Nextcloud credential found. Open the client once so it can store "
        "its token, or pass --password <app-password>."
    )
