# Nextcloud Sync Doctor

[![CI](https://github.com/frischeDaten/nextcloud-sync-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/frischeDaten/nextcloud-sync-doctor/actions/workflows/ci.yml)
[![License: GPL-2.0-or-later](https://img.shields.io/badge/license-GPL--2.0--or--later-blue)](LICENSE)

A per-file sync problem resolver for the Nextcloud Desktop Client on Windows.

The client tells you *"Some files could not be synced!"* and sends you to an activity
list. That list is not filtered, has no per-file resolution for anything that is not a
textbook conflict, is not persisted, and cannot show you what the server actually has.
For one whole class of failures — names created on macOS that Windows cannot store — no
GUI action helps at all, because the file cannot exist locally.

This tool reads the client's own data, talks to your server with the client's own
credentials, and gives you a list of the files that are actually broken, with the local
and server version side by side and a decision to make for each one:

* keep the **local** version (upload, overwrite the server copy)
* keep the **server** version (download, overwrite the local copy)
* keep **both** (your file moves aside under a new name, the server version comes in)
* **rename** it on the server so Windows can store it at all
* **roll back** to an earlier server version, or pull one out as a copy
* **delete** one side (server deletes go to the trash)
* **ignore** it forever, or just hide the entry

Every destructive step is backed up locally and logged.

![The issue list](docs/ui.png)

![One file, both versions](docs/ui-detail.png)

![Choosing a Windows-safe name](docs/ui-rename.png)

All screenshots come from demo mode (`--demo`) with fictional accounts and files — no real
hostnames, user names or paths.

## Install

**Windows, nothing to install.** Needs Python 3.10+ (the launcher looks on `PATH` and in
`%LOCALAPPDATA%\Programs\Python`):

1. Make sure the Nextcloud desktop client has run at least once for the account.
2. Double-click **`Nextcloud Sync Doctor.cmd`**.
3. A console window opens with a URL containing a security token; the browser opens
   automatically. Keep the console open while you work.
4. Press **Rescan**. Fix things. Press **Rescan** again (or restart the client from the
   **Client…** button) to watch the list empty out.

**As a Python package** (from a checkout, or `pipx install git+https://…` later):

```bash
pip install -e .
ncsd serve
```

**Demo mode** — fictional data, no client, no server, nothing on your machine is read or
changed:

```bash
ncsd --demo serve        # the UI with sample issues
ncsd --demo scan         # the CLI with sample issues
```

There are no runtime dependencies beyond the Python standard library.

## What it finds

| Class | Meaning | Typical cause |
|---|---|---|
| **Cannot be created on Windows** | Server name uses `* ? < > " \| :` , a reserved device name (`CON`, `AUX`, `LPT1`…), a trailing dot/space, or is longer than 254 chars | A file created on macOS, Linux or in the web UI |
| **Name clash (upper/lower case)** | Two server files differ only by capitalisation | `Report.pdf` and `report.pdf` from different machines |
| **Both sides changed** | The client kept a conflicted copy | Editing the same file offline on two machines |
| **Sync error** | Any other failure the client reported (permissions, locks, storage, IO) | Revoked share, full quota, file locked |
| **Path too long for Windows** | Full local path beyond the classic 259 chars | Deep folder trees; Explorer and most apps fail, the client copes |
| **Ignored file** | Excluded by the client's ignore list but *reported as a sync error* | The 2026 regression that floods the activity list |
| **macOS / Windows junk** | `._*` resource forks, `.DS_Store`, `Thumbs.db`, `~$*.docx` lock files | Syncing between macOS and Windows |

The first three are real problems. The last two are noise — the client counts them as
errors, which is why an exclamation mark rarely tells you anything.

### It stays honest about what is still broken

Log lines and journal rows describe the **past**. Every scan re-checks both sides, so an
entry leaves the list as soon as it stops being a problem:

* if neither a local nor a server file exists at that path any more, the entry is marked
  **✓ resolved** and moves out of the way (the *Resolved* chip keeps the history);
* if the server file was renamed to a Windows-safe name, it even says what it is called
  now — `Fixed: the server file now exists as 'pricing_grid_(star).pdf'.`;
* log lines older than seven days are ignored unless the file is still there, so an old
  rotated log cannot resurrect a problem you already fixed.

![Resolved entries](docs/ui-resolved.png)

### Where the data comes from

1. **The client's log files** — the only place the client records the errors it shows.
   The activity list itself is memory-only and disappears on restart.
2. **The sync journal** (`.sync_<hash>.db`) — read from a copied snapshot, read-only.
   Its `blacklist` table is the client's own persisted list of failed items, with the
   exact error message; the `conflicts`/`caseconflicts` tables map conflict copies to
   their base file.
3. **A deep server scan** (opt-in) — `PROPFIND` over the folder tree, looking for names
   and paths Windows cannot represent. This finds files the client skipped once and
   then never mentioned again, because they never entered the journal at all.

   ![Deep scan progress](docs/ui-deepscan.png)

   It costs one request per folder (~50–60 folders/second against a remote server), so it
   only runs when you press the button.

## Actions

| Action | What it does | Safety |
|---|---|---|
| Keep local | Uploads your file over the server copy, preserving the modification time | Previous server version downloaded to the backup folder first |
| Keep server | Writes the server content over your file and sets the server mtime | Your local file is copied to the backup folder first |
| Keep both | Renames your file to `name (local copy <date> <time>).ext`, then brings the server version in | Nothing is destroyed; both files sync |
| Rename on server | `MOVE` on the server to a Windows-safe name (suggested automatically) | Rename back any time; the client then downloads it |
| Rename local / Download as… | Local rename, or pull the server copy in under another name | — |
| Download version as copy | Fetches an older server revision into the sync folder | Server untouched |
| Roll back server | Moves the server revision into Nextcloud's restore endpoint, then downloads it | The server keeps the replaced content as a version |
| Delete on server | `DELETE` (Nextcloud moves it to the trash) | Recoverable from the server trash |
| Delete local | Copies to the backup folder, then removes it locally | Backup + server copy remain |
| Ignore file | Appends a literal path pattern to the folder's `.sync-exclude.lst` | The client never downloads it again |
| Hide | Only hides the row in this tool | Nothing touched |

Notes:
* "Keep both" deliberately does **not** use the client's `(conflicted copy …)` naming:
  the client refuses to upload those by default, so the copy would stay local forever.
* Server-side writes check nothing implicitly — if your share is read-only the request
  fails and the error is shown instead of silently doing nothing.

## Safety model

* The sync journal is **never written**. It is copied (`db` + `wal` + `shm`) and opened
  read-only, so the client can keep running.
* Local overwrites and deletions copy the file to
  `%LOCALAPPDATA%\Nextcloud\sync-doctor\backups\<timestamp>\` first.
* Server overwrites download the current server version to that same backup folder first
  (configurable size limit, default 256 MB).
* Server deletions go through Nextcloud, i.e. into the server trash bin.
* Every applied action is appended to `%LOCALAPPDATA%\Nextcloud\sync-doctor\audit.jsonl`
  and visible under **History** in the UI.
* The UI listens on `127.0.0.1` only and requires the random token from the console URL.
* No password is stored. The tool reads the app password the client already keeps in the
  Windows Credential Manager (`Nextcloud_<user>_app-password:<url>:<id>`). It never
  modifies or deletes that entry. Use `--password` to supply your own app password
  (Nextcloud → Settings → Security → App passwords) and skip the Credential Manager
  entirely.

## CLI

```
python -m ncsd scan            # human readable report
python -m ncsd scan --json     # machine readable
python -m ncsd scan --junk     # include .DS_Store / ._* noise
python -m ncsd scan --deep     # also crawl the server for Windows-invalid names
python -m ncsd serve           # the web UI
python -m ncsd report          # audit trail + backup size
```

Global options: `--account N`, `--folder N` (for multi-account setups),
`--password <app-password>`, and `--demo` to use the synthetic fixtures instead of your
client and server.

## Limitations

* Windows only for real use: the credential reuse, the config discovery and the filename
  rules are Windows-specific. The WebDAV half works anywhere, and demo mode runs on any OS
  (that is what CI builds against).
* One account and one sync folder per session.
* A deep scan costs roughly one `PROPFIND` per folder (about 50–60 folders/second against
  a remote server, so a 10k-folder tree takes a few minutes). Scope it if the tree is
  huge. Folders that fail (timeouts, no permission) are retried once and then counted
  under "folder(s) could not be read".
* The tool does not edit the sync journal, so it cannot force the client to re-evaluate a
  blacklisted item immediately. The client picks up changes on its next sync; use
  **Client… → Restart** to force one.
* Actions run while the client is running can race with an in-flight sync. The outcome is
  still consistent (both sides end up with the same content), but re-run **Rescan** to see
  the settled state.
* "Keep server"/"Download" preserve the server mtime so the client sees the file as up to
  date; if the client still uploads it once, the content is identical.

## Where things live

| What | Path |
|---|---|
| Backups | `%LOCALAPPDATA%\Nextcloud\sync-doctor\backups\` |
| Audit log | `%LOCALAPPDATA%\Nextcloud\sync-doctor\audit.jsonl` |
| Hidden entries | `%LOCALAPPDATA%\Nextcloud\sync-doctor\dismissals.json` |
| Journal snapshots | `%LOCALAPPDATA%\Nextcloud\sync-doctor\snapshots\` |
| Client config read | `%APPDATA%\Nextcloud\nextcloud.cfg` |
| Client logs read | `%APPDATA%\Nextcloud\logs\` |

## Troubleshooting

* **"No stored Nextcloud credential found"** — the client has never stored an app
  password for this account, or it was invalidated. Add an app password in Nextcloud →
  Settings → Security → App passwords and pass it with `--password`.
* **HTTP 401 / 403 from the server** — the stored token was revoked. Same fix.
* **"Port already in use"** — another instance is already running; use the URL from its
  console window.
* **Nothing found but the client still shows a warning** — that is almost certainly the
  ignored-file regression; switch on **Show junk/noise** to confirm.

## Security and privacy

The doctor reads the app password the desktop client already stored in the Windows
Credential Manager (read-only, never modified) or takes one via `--password`. The token is
kept in memory and only ever sent to your own server. The UI binds to `127.0.0.1` and
requires a random per-run token. No telemetry, no third-party dependencies.
Details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The
quickest way to work on it is `ncsd --demo serve`, which needs no Nextcloud account.

[docs/upstream-contribution.md](docs/upstream-contribution.md) documents the underlying
desktop-client behaviour (and bugs) this tool works around, together with what a proper
upstream fix would have to touch.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE). That matches the licence of the Nextcloud
desktop client whose name rules this tool reimplements, and keeps any part of it usable in
an upstream contribution. (If you would rather publish it under MIT, change `LICENSE` and
the `license` field in `pyproject.toml`; nothing in the code depends on the licence.)
