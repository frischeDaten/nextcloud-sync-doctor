"""Reuse the desktop client's own stored app password from Windows Credential Manager.

The Nextcloud client stores one credential per account. Reading it means the
doctor never has to ask the user for a password, and it always uses exactly the
token the client itself would use.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

try:  # the module itself is Windows-only, the package is not
    import ctypes.wintypes as wt
except Exception:  # pragma: no cover - non-Windows platforms
    wt = None

CRED_TYPE_GENERIC = 1


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


@dataclass
class Credential:
    token: str
    target: str
    user: str = ""
    source: str = "credential-manager"

    def basic_auth(self, user: str) -> tuple[str, str]:
        return (user, self.token)


def find_account_credential(account, explicit_password: str | None = None) -> Credential:
    """Locate the token for ``account`` (``ncsd.config.Account``)."""
    if explicit_password:
        return Credential(token=explicit_password, target="", user=account.user, source="cli")

    if not _ALLOWED or os.name != "nt":
        raise AuthError(
            "Credential reuse disabled. Pass an app password explicitly "
            "(Nextcloud → Settings → Security → App passwords)."
        )

    host = account.url.split("://", 1)[-1].rstrip("/")
    user = account.user or ""
    targets = [t for t in list_targets() if t.lower().startswith("nextcloud")]

    # Rank: app-password entry for this user/host, then any entry for this host.
    def score(target: str) -> int:
        low = target.lower()
        s = 0
        if host.lower() in low:
            s += 10
        if user and user.lower() in low:
            s += 5
        if "app-password" in low:
            s += 3
        return s

    candidates = sorted(targets, key=score, reverse=True)
    for target in candidates:
        if score(target) <= 0:
            continue
        token = _read(target)
        if token:
            return Credential(token=token, target=target, user=user)

    raise AuthError(
        "No stored Nextcloud credential found. Open the client once so it can store "
        "its token, or pass --password <app-password>."
    )
