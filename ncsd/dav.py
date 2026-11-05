"""Small, dependency-free WebDAV client for Nextcloud.

Only the handful of verbs the doctor needs: PROPFIND, GET, PUT, MOVE, DELETE,
plus Nextcloud's file-version endpoints.
"""

from __future__ import annotations

import base64
import os
import random
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

DAV = "DAV:"
OC = "http://owncloud.org/ns"
NC = "http://nextcloud.org/ns"

PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
    "<d:prop>"
    "<d:getlastmodified/><d:getcontentlength/><d:getcontenttype/><d:getetag/><d:resourcetype/>"
    "<oc:fileid/><oc:checksums/><oc:permissions/><oc:size/><oc:owner-id/>"
    "<nc:has-preview/>"
    "</d:prop></d:propfind>"
)

CHUNK = 1024 * 256


class DavError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: bytes = b""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class RemoteItem:
    path: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0
    etag: str = ""
    fileid: str = ""
    checksums: str = ""
    permissions: str = ""
    content_type: str = ""
    has_preview: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "mtime": self.mtime,
            "etag": self.etag,
            "fileid": self.fileid,
            "checksums": self.checksums,
            "permissions": self.permissions,
            "content_type": self.content_type,
            "has_preview": self.has_preview,
        }


def quote_path(rel_path: str) -> str:
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel:
        return ""
    return "/" + "/".join(urllib.parse.quote(seg, safe="") for seg in rel.split("/"))


def _parse_http_date(value: str, default: float = 0.0) -> float:
    if not value:
        return default
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return default


def _text(node, tag: str) -> str:
    if node is None:
        return ""
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


class Dav:
    def __init__(self, base_url: str, user: str, token: str, timeout: int = 90, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self._auth = "Basic " + base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        self.timeout = timeout
        self.retries = retries
        self._dav_prefix = f"/remote.php/dav/files/{urllib.parse.quote(user, safe='')}"

    # -- low level ---------------------------------------------------------
    def file_url(self, rel_path: str) -> str:
        return self.base_url + self._dav_prefix + quote_path(rel_path)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Authorization": self._auth,
            "User-Agent": "nextcloud-sync-doctor/1.0",
            "Accept-Encoding": "identity",
        }
        if extra:
            headers.update(extra)
        return headers

    def _open(self, method: str, url: str, data=None, headers=None, head=False):
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
        return urllib.request.urlopen(req, timeout=self.timeout)

    def request(self, method: str, rel_path: str, data=None, headers=None) -> bytes:
        url = self.file_url(rel_path)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self._open(method, url, data, headers) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:
                    pass
                if exc.code in (429, 423, 502, 503, 504) and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    last_error = DavError(f"{method} {rel_path} -> HTTP {exc.code}", exc.code, body)
                    continue
                raise DavError(f"{method} {rel_path} -> HTTP {exc.code} {exc.reason}", exc.code, body) from None
            except urllib.error.URLError as exc:
                last_error = DavError(f"{method} {rel_path} -> {exc.reason}")
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise last_error from None
        raise last_error or DavError(f"{method} {rel_path} failed")

    # -- properties --------------------------------------------------------
    def propfind(self, rel_path: str, depth: str = "0") -> list[RemoteItem]:
        body = PROPFIND_BODY.encode("utf-8")
        raw = self.request(
            "PROPFIND",
            rel_path,
            data=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        return self._parse_multistatus(raw)

    def _parse_multistatus(self, raw: bytes) -> list[RemoteItem]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise DavError(f"Could not parse WebDAV response: {exc}") from None
        items: list[RemoteItem] = []
        for response in root.findall(f"{{{DAV}}}response"):
            href = _text(response, f"{{{DAV}}}href")
            props: dict[str, object] = {}
            for propstat in response.findall(f"{{{DAV}}}propstat"):
                status = _text(propstat, f"{{{DAV}}}status")
                if status and "200" not in status:
                    continue
                prop = propstat.find(f"{{{DAV}}}prop")
                if prop is None:
                    continue
                for child in prop:
                    props[child.tag] = child
            if not props:
                continue

            def value(tag: str) -> str:
                node = props.get(tag)
                return (node.text or "").strip() if node is not None else ""

            resourcetype = props.get(f"{{{DAV}}}resourcetype")
            is_dir = resourcetype is not None and resourcetype.find(f"{{{DAV}}}collection") is not None
            size_text = value(f"{{{OC}}}size") or value(f"{{{DAV}}}getcontentlength")
            item = RemoteItem(
                path=self._href_to_rel(href),
                is_dir=is_dir,
                size=int(size_text) if size_text.isdigit() else 0,
                mtime=_parse_http_date(value(f"{{{DAV}}}getlastmodified")),
                etag=value(f"{{{DAV}}}getetag").strip('"'),
                fileid=value(f"{{{OC}}}fileid"),
                checksums="; ".join(
                    (c.text or "").strip()
                    for c in props.get(f"{{{OC}}}checksums", [])
                ) if f"{{{OC}}}checksums" in props else "",
                permissions=value(f"{{{OC}}}permissions"),
                content_type=value(f"{{{DAV}}}getcontenttype"),
                has_preview=value(f"{{{NC}}}has-preview").lower() == "true",
            )
            items.append(item)
        return items

    def _href_to_rel(self, href: str) -> str:
        path = urllib.parse.unquote(href)
        if self._dav_prefix in path:
            path = path.split(self._dav_prefix, 1)[1]
        path = path.strip("/")
        return path

    def stat(self, rel_path: str) -> RemoteItem | None:
        try:
            items = self.propfind(rel_path, "0")
        except DavError as exc:
            if exc.status == 404:
                return None
            raise
        if not items:
            return None
        item = items[0]
        item.path = rel_path.strip("/")
        return item

    def listdir(self, rel_path: str) -> list[RemoteItem]:
        items = self.propfind(rel_path, "1")
        target = rel_path.strip("/")
        children = []
        for item in items:
            if item.path.strip("/") == target:
                continue
            children.append(item)
        return children

    # -- transfers ---------------------------------------------------------
    def download(self, rel_path: str, dest_path: str, expected_size: int | None = None) -> int:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        tmp = dest_path + f".ncsd-{_rand()}.part"
        written = 0
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self._open("GET", self.file_url(rel_path)) as resp, open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                if expected_size is not None and written != expected_size:
                    raise DavError(
                        f"Truncated download for {rel_path}: got {written} of {expected_size} bytes"
                    )
                os.replace(tmp, dest_path)
                return written
            except (DavError, urllib.error.URLError, OSError) as exc:
                last_error = exc
                written = 0
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        raise DavError(f"Download failed for {rel_path}: {last_error}")

    def open_download(self, rel_path: str):
        """Return an open response object for streaming (used for in-browser preview)."""
        return self._open("GET", self.file_url(rel_path))

    def upload(self, rel_path: str, src_path: str, mtime: float | None = None) -> RemoteItem:
        size = os.path.getsize(src_path)
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        if mtime:
            headers["X-OC-MTime"] = str(int(mtime))
        with open(src_path, "rb") as fh:
            self.request("PUT", rel_path, data=fh, headers=headers)
        return self.stat(rel_path) or RemoteItem(path=rel_path, size=size, mtime=mtime or 0)

    def move(self, src_rel: str, dst_rel: str, overwrite: bool = False) -> None:
        destination = self.file_url(dst_rel)
        self.request(
            "MOVE",
            src_rel,
            headers={"Destination": destination, "Overwrite": "T" if overwrite else "F"},
        )

    def delete(self, rel_path: str) -> None:
        self.request("DELETE", rel_path)

    def mkdir(self, rel_path: str) -> None:
        self.request("MKCOL", rel_path)

    # -- versions ----------------------------------------------------------
    def versions(self, fileid: str) -> list[dict]:
        if not fileid:
            return []
        url = (
            f"{self.base_url}/remote.php/dav/versions/{urllib.parse.quote(self.user, safe='')}"
            f"/versions/{urllib.parse.quote(str(fileid), safe='')}"
        )
        body = PROPFIND_BODY.encode("utf-8")
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="PROPFIND",
                headers=self._headers({"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}),
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405, 501):
                return []
            raise DavError(f"Version listing failed for fileid {fileid}: HTTP {exc.code}", exc.code) from None
        except urllib.error.URLError as exc:
            raise DavError(f"Version listing failed: {exc.reason}") from None

        out = []
        for item in self._parse_multistatus_generic(raw):
            version_id = item["href"].rstrip("/").split("/")[-1]
            if not version_id or version_id == str(fileid):
                continue
            out.append(
                {
                    "version": version_id,
                    "size": item.get("size", 0),
                    "mtime": item.get("mtime", 0.0),
                    "etag": item.get("etag", ""),
                }
            )
        out.sort(key=lambda v: v["mtime"], reverse=True)
        return out

    def _parse_multistatus_generic(self, raw: bytes) -> list[dict]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        out = []
        for response in root.findall(f"{{{DAV}}}response"):
            href = _text(response, f"{{{DAV}}}href")
            entry = {"href": href, "size": 0, "mtime": 0.0, "etag": ""}
            for propstat in response.findall(f"{{{DAV}}}propstat"):
                prop = propstat.find(f"{{{DAV}}}prop")
                if prop is None:
                    continue
                length = prop.find(f"{{{DAV}}}getcontentlength")
                if length is not None and length.text and length.text.strip().isdigit():
                    entry["size"] = int(length.text.strip())
                modified = prop.find(f"{{{DAV}}}getlastmodified")
                if modified is not None:
                    entry["mtime"] = _parse_http_date(modified.text or "")
                etag = prop.find(f"{{{DAV}}}getetag")
                if etag is not None and etag.text:
                    entry["etag"] = etag.text.strip().strip('"')
            out.append(entry)
        return out

    def _version_url(self, fileid: str, version: str = "") -> str:
        user = urllib.parse.quote(self.user, safe="")
        base = f"{self.base_url}/remote.php/dav/versions/{user}/versions/{urllib.parse.quote(str(fileid), safe='')}"
        return base + (f"/{urllib.parse.quote(version, safe='')}" if version else "")

    def restore_version(self, fileid: str, version: str) -> None:
        """Roll the server file back (Nextcloud's MOVE-into-restore endpoint)."""
        user = urllib.parse.quote(self.user, safe="")
        destination = f"{self.base_url}/remote.php/dav/versions/{user}/restore/{version}"
        url = self._version_url(fileid, version)
        try:
            with self._open("MOVE", url, headers={"Destination": destination, "Overwrite": "T"}):
                pass
        except urllib.error.HTTPError as exc:
            raise DavError(f"Restoring version {version} failed: HTTP {exc.code}", exc.code) from None
        except urllib.error.URLError as exc:
            raise DavError(f"Restoring version {version} failed: {exc.reason}") from None

    def download_version(self, fileid: str, version: str, dest_path: str) -> int:
        url = self._version_url(fileid, version)
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        tmp = dest_path + f".ncsd-{_rand()}.part"
        try:
            with self._open("GET", url) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
            os.replace(tmp, dest_path)
            return os.path.getsize(dest_path)
        except urllib.error.HTTPError as exc:
            raise DavError(f"Version download failed: HTTP {exc.code}", exc.code) from None
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def _rand(n: int = 8) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))
