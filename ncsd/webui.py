"""Local web UI for the Sync Doctor.

Bound to 127.0.0.1 only and guarded by a random per-run token, so neither another
machine nor a random browser tab can drive it.
"""

from __future__ import annotations

import difflib
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, config, creds, detect, journal as journal_mod, names, platform
from .actions import ACTION_LABELS, ActionError, Resolver
from .dav import Dav, DavError
from .model import KINDS, preview_kind
from .state import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_TEXT_BYTES = 256 * 1024
MAX_RAW_BYTES = 512 * 1024 * 1024
VERSION_SIDECAR = "server copy"


class HttpError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Session:
    """Everything the request handlers need, shared across threads."""

    def __init__(self, cfg, account, folder, credential, dav: Dav, store: Store):
        self.cfg = cfg
        self.account = account
        self.folder = folder
        self.credential = credential
        self.dav = dav
        self.store = store
        self.resolver = Resolver(account, folder, dav, store)
        self.issues = []
        self.journal_obj = None
        self.journal_error = ""
        self.lock = threading.Lock()
        self.deep_state = detect.DeepScanState()
        self.deep_results: list[dict] = []
        self.deep_finished_at = 0.0
        self.started_at = time.time()
        self.refresh_journal()

    # -- scanning ----------------------------------------------------------
    def refresh_journal(self):
        try:
            self.journal_obj = journal_mod.load(
                self.folder.journal_path, os.path.join(self.store.data_dir, "snapshots")
            )
            self.journal_error = ""
            journal_mod.cleanup_snapshots(os.path.join(self.store.data_dir, "snapshots"))
        except journal_mod.JournalError as exc:
            self.journal_obj = None
            self.journal_error = str(exc)

    def scan(self, include_junk: bool | None = None, use_deep: bool = False) -> list:
        settings = self.store.settings
        if include_junk is None:
            include_junk = bool(settings.get("include_junk", False))
        self.refresh_journal()
        options = detect.ScanOptions(include_junk=include_junk, deep=use_deep)
        issues = detect.collect(
            self.account, self.folder, self.journal_obj, self.dav, options,
            deep_results=self.deep_results if use_deep else None,
        )
        dismissed = self.store.dismissals()
        now = time.time()
        for issue in issues:
            entry = dismissed.get(issue.key)
            if entry and (not entry.get("until") or entry["until"] > now):
                issue.dismissed = True
        with self.lock:
            self.issues = issues
        return issues

    def start_deep_scan(self, scope: str = "") -> bool:
        if self.deep_state.running:
            return False
        state = detect.DeepScanState()
        self.deep_state = state
        if self.journal_obj is not None:
            journal_obj = self.journal_obj
        else:
            try:
                journal_obj = journal_mod.load(
                    self.folder.journal_path, os.path.join(self.store.data_dir, "snapshots")
                )
            except journal_mod.JournalError:
                # The crawl can still work from the sync folder itself.
                journal_obj = None

        def run():
            try:
                results = detect.deep_scan(
                    self.account, self.folder, journal_obj, self.dav, state,
                    scope=scope, workers=int(self.store.settings.get("deep_scan_workers", 8)),
                )
                self.deep_results = results
                self.deep_finished_at = time.time()
            except Exception as exc:  # keep the UI alive
                state.running = False
                state.message = f"Deep scan failed: {exc}"

        threading.Thread(target=run, daemon=True, name="deep-scan").start()
        return True

    # -- helpers -----------------------------------------------------------
    def find_issue(self, key: str):
        for issue in self.issues:
            if issue.key == key:
                return issue
        return None

    def local_path(self, rel_path: str) -> str:
        return self.resolver.local_path(rel_path)


MAC_CLOUD_STORAGE = os.path.expanduser("~/Library/CloudStorage")


def _folder_platform_facts(folder) -> dict:
    """What the platform adds to the picture of the sync folder (empty elsewhere)."""
    facts: dict = {"platform": platform.platform_name()}
    if not platform.is_macos():
        return facts
    root = os.path.abspath(folder.local_root)
    # FileProvider domains live in ~/Library/CloudStorage; iCloud Drive is one too.
    facts["fileprovider"] = root.startswith(MAC_CLOUD_STORAGE + os.sep)
    facts["icloud"] = "/Library/Mobile Documents/" in root
    facts["dataless"] = detect.count_dataless(root)
    return facts


def _client_pids() -> list[int]:
    """Process ids of the running desktop client (and its FileProvider extension).

    Candidates are found by process name (`pgrep -x`), then confirmed by the
    executable path, so a shell whose command line merely mentions "Nextcloud"
    never counts as the client.
    """
    names = platform.client_process_names()
    if platform.is_macos():
        names.append(platform.mac_fileprovider_extension_name())
    pids: list[int] = []
    for name in names:
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (out.stdout or "").splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid in pids or not _pid_is_client(pid):
                continue
            pids.append(pid)
    return pids


def _pid_is_client(pid: int) -> bool:
    if not platform.is_macos():
        return True
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "Nextcloud.app" in (out.stdout or "")


def _client_running() -> bool:
    if platform.is_windows():
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq nextcloud.exe", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
            return "nextcloud.exe" in (out.stdout or "").lower()
        except (OSError, subprocess.SubprocessError):
            return False
    return bool(_client_pids())


def _quit_client() -> tuple[bool, str]:
    if not _client_running():
        return True, "The desktop client is not running."
    if platform.is_windows():
        try:
            subprocess.run(["taskkill", "/IM", "nextcloud.exe", "/T"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        for _ in range(10):
            time.sleep(0.5)
            if not _client_running():
                return True, "Desktop client stopped."
        try:
            subprocess.run(["taskkill", "/F", "/IM", "nextcloud.exe", "/T"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return False, "Could not stop the desktop client."
        time.sleep(1.0)
        return (not _client_running()), "Desktop client force-stopped."

    # macOS (and other POSIX systems): ask the app to quit, then insist.
    if platform.is_macos():
        try:
            subprocess.run(["osascript", "-e", 'quit app "Nextcloud"'], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        for _ in range(10):
            time.sleep(0.5)
            if not _client_running():
                return True, "Desktop client quit."
    for pid in _client_pids():
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    time.sleep(1.0)
    if not _client_running():
        return True, "Desktop client stopped."
    for pid in _client_pids():
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    time.sleep(0.5)
    return (not _client_running()), "Desktop client force-stopped."


def _start_client() -> tuple[bool, str]:
    exe = platform.client_executable()
    if platform.is_macos():
        if not exe:
            return False, "Nextcloud.app not found in /Applications or ~/Applications."
        # "open" hands the launch to launchd, so the app starts exactly as from Finder.
        try:
            subprocess.run(["open", "-a", exe], capture_output=True, timeout=20, check=False)
            return True, "Desktop client started."
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"Could not start the client: {exc}"
    if platform.is_windows():
        if not os.path.isfile(exe):
            return False, "nextcloud.exe not found in Program Files."
        try:
            subprocess.Popen([exe], close_fds=True)
            return True, "Desktop client started."
        except OSError as exc:
            return False, f"Could not start the client: {exc}"
    try:
        subprocess.Popen([exe], close_fds=True)
        return True, "Desktop client started."
    except OSError as exc:
        return False, f"Could not start the client: {exc}"


class Handler(BaseHTTPRequestHandler):
    server_version = "SyncDoctor"
    protocol_version = "HTTP/1.1"
    session: Session = None  # type: ignore[assignment]
    token: str = ""

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter console
        if os.environ.get("NCSD_DEBUG"):
            super().log_message(fmt, *args)

    def _authorized(self, query: dict) -> bool:
        supplied = self.headers.get("X-NCSD-Token") or (query.get("token", [""])[0])
        return bool(supplied) and secrets.compare_digest(supplied, self.token)

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HttpError("Invalid JSON body")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if not self._authorized(query):
                    raise HttpError("Not authorised", 403)
                return self._api(path, method, query)
            return self._static(path)
        except HttpError as exc:
            self._json({"error": str(exc)}, exc.status)
        except ActionError as exc:
            self._json({"error": str(exc)}, 400)
        except DavError as exc:
            self._json({"error": str(exc), "status": exc.status}, 502)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - never kill the server
            self._json({"error": f"{exc.__class__.__name__}: {exc}"}, 500)

    # -- static ------------------------------------------------------------
    def _static(self, path: str):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not full.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(full):
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- api ---------------------------------------------------------------
    def _api(self, path: str, method: str, query: dict):
        session = self.session
        if path == "/api/bootstrap" and method == "GET":
            return self._bootstrap()
        if path == "/api/scan" and method == "POST":
            return self._scan()
        if path == "/api/localcheck" and method == "POST":
            return self._local_check()
        if path == "/api/deepscan":
            if method == "POST":
                body = self._body()
                started = session.start_deep_scan(body.get("scope", ""))
                return self._json({"started": started, "status": session.deep_state.to_dict()})
            return self._json({"status": session.deep_state.to_dict()})
        if path == "/api/plan" and method == "POST":
            body = self._body()
            plan = session.resolver.plan(body["action"], body["path"], body.get("params") or {})
            return self._json({"plan": plan.to_dict(), "label": ACTION_LABELS.get(plan.action, plan.action)})
        if path == "/api/apply" and method == "POST":
            body = self._body()
            action = body["action"]
            if action == "dismiss":
                keys = body.get("keys") or [body.get("key")]
                store = session.store
                if body.get("undismiss"):
                    count = store.undismiss(keys)
                    return self._json({"ok": True, "message": f"Restored {count} entr(y/ies)."})
                count = store.dismiss([k for k in keys if k], note=body.get("note", ""))
                return self._json({"ok": True, "message": f"Hid {count} entr(y/ies)."})
            outcome = session.resolver.apply(action, body["path"], body.get("params") or {})
            return self._json({"outcome": outcome.to_dict()})
        if path == "/api/versions" and method == "GET":
            rel = query.get("path", [""])[0]
            item = session.dav.stat(rel)
            if item is None:
                raise HttpError("File not found on the server", 404)
            # Nextcloud lists the current revision as a pseudo version whose id is
            # the file's mtime; it cannot be downloaded (the file itself is it).
            current_id = str(int(item.mtime)) if item.mtime else ""
            versions = [v for v in session.dav.versions(item.fileid) if v["version"] != current_id]
            return self._json({"fileid": item.fileid, "versions": versions})
        if path == "/api/preview" and method == "GET":
            return self._preview(query)
        if path == "/api/diff" and method == "GET":
            return self._diff(query)
        if path == "/api/raw" and method == "GET":
            return self._raw(query)
        if path == "/api/open" and method == "POST":
            return self._open()
        if path == "/api/audit" and method == "GET":
            return self._json({
                "entries": session.store.recent_audit(200),
                "backups": session.store.backups_size(),
                "data_dir": session.store.data_dir,
            })
        if path == "/api/settings":
            if method == "POST":
                values = self._body()
                return self._json({"settings": session.store.update_settings(values)})
            return self._json({"settings": session.store.settings})
        if path == "/api/client" and method == "POST":
            body = self._body()
            if getattr(session, "demo", False):
                return self._json({"ok": False, "message": "Demo mode: the client is not controlled.", "running": False})
            op = body.get("op")
            if op == "quit":
                ok, message = _quit_client()
            elif op == "start":
                ok, message = _start_client()
            elif op == "restart":
                ok, message = _quit_client()
                if ok:
                    time.sleep(1.0)
                    ok, message = _start_client()
            else:
                raise HttpError("Unknown client operation")
            return self._json({"ok": ok, "message": message, "running": _client_running()})
        raise HttpError(f"Unknown endpoint {path}", 404)

    def _bootstrap(self):
        session = self.session
        folder = session.folder
        account = session.account
        is_demo = bool(getattr(session, "demo", False))
        last_sync = 0
        journal_entries = 0
        if session.journal_obj is not None:
            last_sync = session.journal_obj.last_sync
            journal_entries = len(session.journal_obj.rows)
        # In demo mode never expose a real path (the store really lives in a temp folder).
        data_dir = platform.demo_data_dir_hint() if is_demo else session.store.data_dir
        return self._json({
            "app": "Nextcloud Sync Doctor",
            "version": __version__,
            "demo": is_demo,
            "account": {
                "url": account.url,
                "user": account.user,
                "display_name": account.display_name,
                "server_version": account.server_version,
                "label": account.label,
            },
            "folder": {
                "local": folder.local_root,
                "remote": folder.target_path,
                "journal": os.path.basename(folder.journal_path),
                "journal_entries": journal_entries,
                "virtual_files": folder.virtual_files_mode,
                "paused": folder.paused,
                "journal_error": session.journal_error,
                "note": folder.note,
                "source": folder.source,
                "platform": {} if is_demo else _folder_platform_facts(folder),
            },
            "client": {
                "config_version": session.cfg.client_version,
                "running": False if is_demo else _client_running(),
            },
            "journal_last_sync": last_sync,
            "settings": session.store.settings,
            "kinds": {k: v for k, v in KINDS.items()},
            "data_dir": data_dir,
            "backups_dir": os.path.join(data_dir, "backups"),
            "auth_source": session.credential.source,
            "deep": {
                **session.deep_state.to_dict(),
                "age": (time.time() - session.deep_finished_at) if session.deep_finished_at else 0,
                "results": len(session.deep_results),
            },
        })

    def _local_check(self):
        """Walk the folder and check names against the client's rules (no server needed)."""
        session = self.session
        if getattr(session, "demo", False):
            raise HttpError("Demo mode: the files are fictional.", 400)
        body = self._body()
        root = session.folder.local_root
        roots = [root]
        skipped: list[str] = []
        if not body.get("all"):
            try:
                entries = sorted(os.listdir(root))
            except OSError as exc:
                raise HttpError(f"Cannot list the sync folder: {exc.strerror or exc}", 403) from None
            roots = []
            for name in entries:
                path = os.path.join(root, name)
                if os.path.isdir(path):
                    if name.lower() in platform.CHECK_DEFAULT_SKIP:
                        skipped.append(name)
                        continue
                    roots.append(path)
        timeout = min(float(body.get("timeout") or 120), 600)
        state = detect.local_name_problems(roots, max_seconds=timeout, base=root)
        for problem in state["problems"]:
            problem["label"] = KINDS.get(problem["kind"], {}).get("label", problem["kind"])
            actions = detect._actions_for(
                problem["kind"], detect.FileSide(exists=True), None, has_remote=False
            )
            if problem["kind"] == "filename_invalid" and "rename_local" not in actions:
                # A bad local name is fixed by renaming the file locally.
                actions.insert(0, "rename_local")
            problem["actions"] = actions
        return self._json({"state": state, "skipped": skipped, "timeout": timeout})

    def _scan(self):
        body = self._body()
        session = self.session
        include_junk = body.get("junk")
        # Keep using this session's deep-scan findings on later plain rescans;
        # otherwise pressing "Rescan" would silently drop what the crawl found.
        use_deep = bool(body.get("deep")) or bool(session.deep_results)
        issues = session.scan(
            include_junk=bool(include_junk) if include_junk is not None else None,
            use_deep=use_deep,
        )
        return self._json({
            "issues": [i.to_dict() for i in issues],
            "counts": {
                "blocking": sum(1 for i in issues if i.severity == "blocking" and not i.dismissed and not i.resolved),
                "warning": sum(1 for i in issues if i.severity == "warning" and not i.dismissed and not i.resolved),
                "noise": sum(1 for i in issues if i.severity == "noise" and not i.dismissed and not i.resolved),
                "dismissed": sum(1 for i in issues if i.dismissed),
                "resolved": sum(1 for i in issues if i.resolved and not i.dismissed),
            },
            "deep": session.deep_state.to_dict(),
        })

    # -- content -----------------------------------------------------------
    def _read_local_text(self, rel: str) -> tuple[str, bool]:
        full = self.session.local_path(rel)
        if os.path.isdir(full):
            raise HttpError("This is a folder, not a file - there is nothing to compare.", 400)
        if not os.path.exists(full):
            raise HttpError("No local file", 404)
        if os.path.getsize(full) > MAX_TEXT_BYTES:
            raise HttpError("File is too large to compare", 413)
        try:
            with open(full, "rb") as fh:
                data = fh.read(MAX_TEXT_BYTES)
        except OSError as exc:
            raise HttpError(f"Cannot read the local file: {exc.strerror or exc}", 403) from None
        return data.decode("utf-8", "replace"), True

    def _read_remote_text(self, rel: str) -> tuple[str, bool]:
        item = self.session.dav.stat(rel)
        if item is None:
            raise HttpError("No server file", 404)
        if item.is_dir:
            raise HttpError("This is a folder, not a file - there is nothing to compare.", 400)
        if item.size > MAX_TEXT_BYTES:
            raise HttpError("File is too large to compare", 413)
        with self.session.dav.open_download(rel) as resp:
            data = resp.read(MAX_TEXT_BYTES)
        return data.decode("utf-8", "replace"), True

    def _diff(self, query: dict):
        rel = query.get("path", [""])[0]
        side = query.get("side", ["both"])[0]
        if side == "local":
            text, _ = self._read_local_text(rel)
            return self._json({"text": text, "lines": text.count("\n") + 1})
        if side == "remote":
            text, _ = self._read_remote_text(rel)
            return self._json({"text": text, "lines": text.count("\n") + 1})
        local_text = remote_text = ""
        try:
            local_text, _ = self._read_local_text(rel)
        except HttpError:
            local_text = ""
        try:
            remote_text, _ = self._read_remote_text(rel)
        except HttpError:
            remote_text = ""
        if len(local_text) > MAX_TEXT_BYTES or len(remote_text) > MAX_TEXT_BYTES:
            raise HttpError("File is too large to compare", 413)
        local_lines = local_text.splitlines(keepends=True)
        remote_lines = remote_text.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            local_lines, remote_lines,
            fromfile="your local version", tofile="server version", n=3,
        ))
        changed = sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
        return self._json({
            "identical": local_text == remote_text,
            "changed_lines": changed,
            "diff": "".join(diff[-4000:]),
            "local_lines": len(local_lines),
            "remote_lines": len(remote_lines),
        })

    def _preview(self, query: dict):
        rel = query.get("path", [""])[0]
        item = self.session.dav.stat(rel)
        full = self.session.local_path(rel)
        local_exists = os.path.exists(full)
        hint = ""
        if local_exists and platform.is_macos():
            st = os.lstat(full)
            if not os.path.isdir(full) and (st.st_flags & detect.UF_DATAVAULT):
                hint = ("This file is not downloaded yet (a cloud placeholder). Opening the "
                        "preview downloads it into the sync folder.")
        return self._json({
            "path": rel,
            "name": names.basename(rel),
            "local_exists": local_exists,
            "remote": item.to_dict() if item else None,
            "kind": preview_kind(names.basename(rel)),
            "hint": hint,
        })

    def _raw(self, query: dict):
        rel = query.get("path", [""])[0]
        side = query.get("side", ["local"])[0]
        version = query.get("version", [""])[0]
        target = self.session.local_path(rel)
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"

        if version:
            item = self.session.dav.stat(rel)
            if item is None:
                raise HttpError("File not found on the server", 404)
            tmp = os.path.join(self.session.store.data_dir, "tmp", f"version-{secrets.token_hex(6)}")
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            try:
                self.session.dav.download_version(item.fileid, version, tmp)
                data = open(tmp, "rb").read(MAX_RAW_BYTES)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return self._send_bytes(data, ctype, inline_name=f"{names.basename(rel)} (v{version})")

        if side == "local":
            if os.path.isdir(target):
                raise HttpError("This is a folder - there is no content to show.", 400)
            if not os.path.isfile(target):
                raise HttpError("No local file", 404)
            if os.path.getsize(target) > MAX_RAW_BYTES:
                raise HttpError("File too large to preview", 413)
            with open(target, "rb") as fh:
                return self._send_bytes(fh.read(), ctype, inline_name=names.basename(rel))

        item = self.session.dav.stat(rel)
        if item is None:
            raise HttpError("File not found on the server", 404)
        if item.is_dir:
            raise HttpError("This is a folder - there is no content to show.", 400)
        if item.size > MAX_RAW_BYTES:
            raise HttpError("File too large to preview", 413)
        with self.session.dav.open_download(rel) as resp:
            return self._send_bytes(resp.read(), item.content_type or ctype, inline_name=names.basename(rel))

    def _send_bytes(self, data: bytes, ctype: str, inline_name: str = ""):
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if inline_name:
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(inline_name)}")
        self.end_headers()
        self.wfile.write(data)

    def _open(self):
        body = self._body()
        if getattr(self.session, "demo", False):
            raise HttpError("Demo mode: nothing is opened, the files are fictional.", 400)
        rel = body.get("path", "")
        side = body.get("side", "local")
        if side == "local":
            full = self.session.local_path(rel)
            if not os.path.exists(full):
                raise HttpError("Local file does not exist", 404)
            subprocess.Popen(platform.reveal_command(full), close_fds=True)
            message = ("Revealed in Finder." if platform.is_macos()
                       else "Opened the containing folder.")
            return self._json({"ok": True, "message": message})
        item = self.session.dav.stat(rel) if rel else None
        parent = names.parent_rel(rel) if rel else ""
        base = self.session.account.base_url
        if item is not None and item.fileid:
            url = f"{base}/index.php/f/{urllib.parse.quote(item.fileid)}"
        else:
            url = (f"{base}/index.php/apps/files/?dir=/{urllib.parse.quote(parent)}"
                   f"&openfile=/{urllib.parse.quote(names.basename(rel))}")
        webbrowser.open(url)
        return self._json({"ok": True, "message": "Opened Nextcloud in your browser.", "url": url})


def _free_port(preferred: int) -> int:
    if preferred:
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(account_index: int = 0, folder_index: int = 0, port: int = 0,
          open_browser: bool = True, password: str | None = None, demo: bool = False,
          config_path: str | None = None) -> int:
    if demo:
        from . import demo as demo_module

        session = demo_module.build_session()
    else:
        from .cli import build_session  # local import avoids a cycle

        cfg, account, folder, dav = build_session(account_index, folder_index, password, config_path)
        store = Store()

        # Fail fast with a readable message instead of a broken UI.
        dav.stat("")

        credential = creds.find_account_credential(account, explicit_password=password)
        session = Session(cfg, account, folder, credential, dav, store)

    Handler.session = session
    Handler.token = os.environ.get("NCSD_TOKEN") or secrets.token_urlsafe(18)

    port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{port}/?token={Handler.token}"

    print(f"Nextcloud Sync Doctor {__version__}")
    if demo:
        print("  mode    : DEMO - synthetic data, nothing will be downloaded or changed")
        print(f"  account : {session.account.label} (fictional)")
        print(f"  folder  : {session.folder.local_root} (fictional)")
    else:
        print(f"  account : {session.account.label}")
        print(f"  folder  : {session.folder.local_root}")
    print(f"  open    : {url}")
    print("  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0
