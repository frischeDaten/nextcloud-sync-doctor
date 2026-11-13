# Fixing Nextcloud sync problems: all the options, and how to contribute one

This document answers two questions: *what can actually be done about this*, and *what
would it take to land it upstream*. It is based on the desktop client source (master at
`6edb4f7`, 2026‑11‑05; `stable-33.0/34.0/35.0` compared), the server's WebDAV/versions
implementation, and the open issue tracker.

---

## 1. What is actually happening

The exclamation mark, "Resolve", and the activity-list dead end are **three separate
problems stacked on top of each other**.

**(a) The alert is mostly fake.** Since commit `0f1baf17c1` (2026‑06‑22) every item whose
`_errorString` is non-empty becomes a `SyncError` in `User::processCompletedSyncItem`
(`src/gui/tray/usermodel.cpp:1357`). Ignored files (`._.DS_Store`, `.DS_Store`,
`~$*.xlsx`, `venv/lib/…`) carry an error string, so they land in the list — and
`ActivityListModel::addErrorToActivityList` (`src/gui/activity/activitylistmodel.cpp:758`)
has no status filter. The tray alert `Some files could not be synced!` is raised by
`User::buildAccountAlert()` (`usermodel.cpp:1553`) from the strongest item, so
ignore-list noise alone produces the warning. Tracked as
[#11118](https://github.com/nextcloud/desktop/issues/11118) and
[#11047](https://github.com/nextcloud/desktop/issues/11047). Fixed on master by
`f67183de` ([PR #11207](https://github.com/nextcloud/desktop/pull/11207), 2026‑11‑02);
the backport to `stable-35.0` ([PR #11244](https://github.com/nextcloud/desktop/pull/11244))
is **still open**, and the flag is not in `stable-34.0`/`stable-35.0`. So on 34.0.x/35.0.x
(including 35.0.2) the flood is still there.

**(b) The "Resolve" entry cannot reach the resolver.** A real per-file resolver exists
since 3.9.0 — `ResolveConflictsDialog.qml` + `SyncConflictsModel` + `ConflictSolver`, with
keep local / keep server / keep both ([PR #5635](https://github.com/nextcloud/desktop/pull/5635),
2023‑05‑17). But the tray menu entry is built in `populateAccountMenu()`
(`src/gui/trayaccountpopup_qt.cpp:762‑771`) and calls `openActivitiesForUser()` — it only
opens the Activities window. The resolver is a *second* button in that window's header
(`src/gui/activity/qml/SyncStatus.qml:98‑136`), rendered **only if** `hasSyncConflicts` is
true; otherwise that slot is **"Sync now"**. With no entries in the journal's `conflicts`
table (as in this case) you get exactly what was reported: activity list, "Sync now", no
resolver.

**(c) Half the failures have no resolution path at all.** The resolver only handles
`Conflict`/`FileNameClash`. A `FileNameInvalid` item gets a *"Rename file"* link
(`RENAME_LOCAL_FILE`) in the activity list — which is meaningless when the file cannot
exist locally, because Windows rejects the name. The client has no way to rename it on the
server, and its dialog shows only file name + mtime + size — no path, no diff, no preview,
no server versions ([#3276](https://github.com/nextcloud/desktop/issues/3276),
[#5823](https://github.com/nextcloud/desktop/issues/5823),
[#8024](https://github.com/nextcloud/desktop/issues/8024),
[#8661](https://github.com/nextcloud/desktop/issues/8661)).

There is also **no other tool**: every public "Nextcloud conflict resolver"
(`nextcloud-solve-conflicts`, `deconflict`, `nextcloud-sync-fix`, …) is a filename scanner
for ` (conflicted copy …)` files. None reads the journal, none talks WebDAV, none knows
about Windows-invalid names.

---

## 2. The options

| # | Option | What you get | Cost | Verdict |
|---|---|---|---|---|
| 1 | **Live with it / update** | 35.0.2 is available | 5 min | Does **not** fix (a) — the backport is not in 35.0.x |
| 2 | **Server-side prevention** — enable Windows-compatible filenames (`forbidden_filename_*`, NC 31+; web UI *Administration → Basic settings → Files compatibility*, or `occ files:windows-filenames enable`), then `occ files:sanitize-filenames` to rename existing offenders ([docs](https://docs.nextcloud.com/server/35/admin_manual/configuration_files/windows_compatible_filenames.html)) | macOS can no longer create names Windows can't store; one command fixes what exists | 10 min, needs server admin + writable `config.php` | **Do this too.** It is the root-cause fix. It does not help case clashes, conflicts, or permissions. |
| 3 | **Standalone resolver** — this repository | Full per-file view of what is broken, local vs server, versions, keep local/server/both/rename/delete/ignore, backups + audit | built | **Works today.** External process, no upstream dependency. |
| 4 | **Upstream patch: de-noise + route the alert** | Tray "Resolve" opens the resolver; ignored files stop being errors | ~1–2 days | **Best value/effort ratio.** Small, reviewable, closes two open issues |
| 5 | **Upstream patch: make non-conflict failures actionable** | A "sync issues" model/dialog that handles `FileNameInvalid`, case clashes, permissions — with server rename/versions | ~1–3 weeks | **The real feature.** Needs design sign-off and a new model |
| 6 | **Upstream patch: richer conflict dialog** | Path, preview/diff, "open location", server versions | ~1 week | Well-scoped, `good first issue` territory for #8661 |
| 7 | **Switch to rclone bisync** | Mature conflict policy flags (`--conflict-resolve`, `--conflict-loser`, `--backup-dir`) | high | No — a second sync engine fighting the client; loses VFS, shares, locks |

The tool in this repository covers 3 completely, 4 and 5 conceptually, and complements 2.
It is deliberately *not* a replacement for the client: it is the missing resolution UI.

---

## 3. Recommended path

1. **Now:** use this tool for the current problems, and enable Windows-compatible
   filenames on the server (option 2) so the class stops recurring.
2. **Contribute (small, high value):** option 4 — separate ignored files from real errors
   and route the tray alert to the existing resolver. It fixes two confirmed issues and
   needs no new UI design.
3. **Then (the actual feature):** option 5 — a per-file "sync issues" view for failures
   that are not conflicts. This is where a redesign pays off, and this tool's data model
   (path, local side, server side, versions, actions) is a working specification for it.

---

## 4. Contribution plan in detail

### 4.1 De-noise the error list (option 4a)

* Reference: [#11118](https://github.com/nextcloud/desktop/issues/11118),
  [#11047](https://github.com/nextcloud/desktop/issues/11047), FR
  [#10962](https://github.com/nextcloud/desktop/issues/10962),
  [PR #11207](https://github.com/nextcloud/desktop/pull/11207) (master),
  [PR #11244](https://github.com/nextcloud/desktop/pull/11244) (backport, open).
* Touch: `User::processCompletedSyncItem` (`src/gui/tray/usermodel.cpp:1357`) and/or
  `ActivityListModel::addErrorToActivityList` (`src/gui/activity/activitylistmodel.cpp:758`)
  — skip `SyncFileItem::FileIgnored` (and `_isUploadForbiddenItemKept`) when building
  `SyncError` entries; keep them as informational activities at most.
* Cheapest useful contribution right now: **review/test the open backport** and report in
  [#11118](https://github.com/nextcloud/desktop/issues/11118) that 35.0.x still reproduces
  it. That is a real, welcome contribution with zero build risk.

### 4.2 Route the alert to the resolver (option 4b)

* Touch: `populateAccountMenu()` (`src/gui/trayaccountpopup_qt.cpp:762‑771`) — call
  `Systray::createResolveConflictsDialog()` instead of `openActivitiesForUser()` when the
  alert is caused by conflicts. The macOS tray menu is an `NSMenu`, not that Qt menu, so
  it needs the same branch: the "Resolve" row is built in `nctraypopup.mm` (the action
  around `222‑233`) and the alert row that carries it in `NCAlertBoxRow`
  (`src/gui/macOS/trayaccountpopup/ncalertboxrow.mm:40`); without it, macOS users get the
  activity list too →
  `nctraypopup.mm:222‑233`.
* Needs `User::buildAccountAlert()` to expose *why* the alert fired (conflicts vs generic
  errors) so the menu can branch — one extra role in the `QVariantMap`.
* Also: `SyncStatus.qml` currently hides the resolver button when `hasSyncConflicts` is
  false; that is correct, but the *menu* should not pretend "Resolve" is a resolver.

### 4.3 Make non-conflict failures actionable (option 5)

* New: `SyncIssuesModel` (path, kind, local meta, server meta, allowed actions) fed from
  `SyncFileItem::Status` — `showInIssuesTab()` already defines "is a problem"
  (`src/libsync/syncfileitem.h`).
* Server operations need a small client-side helper around `remote.php/dav`:
  `MOVE` rename (Destination + `Overwrite: F`) and version listing/restore
  (`remote.php/dav/versions/<user>/versions/<fileid>`, restore = `MOVE` into
  `…/versions/<user>/restore/`). The client uses **none** of the versions API today, so
  this is genuinely new — and it also unlocks "compare with the server version" in the
  conflict dialog.
* UI: extend `ResolveConflictsDialog.qml` or add a sibling dialog under
  `src/gui/activity/qml/`, which is where the activity area already lives.
* Tests exist and are expected: `test/testsyncconflictsmodel.cpp`,
  `test/testactivitylistmodel.cpp`, `test/qml/activitieswindow/…`.

### 4.4 Richer conflict dialog (option 6, good first issue)

* [#8661](https://github.com/nextcloud/desktop/issues/8661) ("open files in file manager in
  both dialogs") is labelled `good first issue`; camilasan asks for before/after
  screenshots and a ping to `@nextcloud/designers`.
* Touch: `ResolveConflictsDialog.qml`, `ConflictDelegate.qml`,
  `ConflictItemFileInfo.qml` (add a path row + an "open location" action), and
  `conflictdialog.{ui,cpp}` for the single-file case.

### 4.5 Process requirements (from CONTRIBUTING.md and the review wiki)

* DCO: `git commit -s`; conventional-commit style; `clang-format`.
* **Two** desktop-team approvals, green CI, tests; **extra design-team approval for any UI
  change**; before/after screenshots in the PR.
* Triage flow: `0. Needs triage` → `1. to develop` → PR
  ([#7778](https://github.com/nextcloud/desktop/issues/7778)). No RFC/design repo exists;
  the de-facto RFC is an accepted enhancement issue plus designer sign-off.
* Windows build: KDE Craft (`craftmaster.ini`, `nextcloud/craft-blueprints-nextcloud`),
  `cmake -S . -B build -DNEXTCLOUD_DEV=ON`; CI job
  `.github/workflows/windows-build-and-test.yml`.
* AI-assisted contributions must be disclosed in the PR (see CONTRIBUTING.md rules).

### 4.6 macOS-specific failures (relevant to option 5)

The Mac client adds failure modes that Windows does not have. They belong in the same
"sync issues" model as the Windows name rules:

* **Finder deletions in a FileProvider folder.** Deleting a file in Finder on a
  `~/Library/CloudStorage/…` Nextcloud folder leaves a local `.Trash/<name>` item; the
  server copy is usually deleted already, so the item can only ever fail with
  "not found in local storage" until it is removed locally. Upstream: the FileProvider
  extension should drop the trash item once the delete is confirmed (or the client should
  recognise `.Trash` as its own and clean it).
* **Cloud placeholders.** A FileProvider item is "dataless" (`UF_DATAVAULT`) until opened.
  Anything that reads the file (a preview, a hash, an editor) silently downloads it, which
  is a sync action the user did not ask for. A proper UI should say so before it happens.
* **iCloud Drive.** A Nextcloud sync folder inside `~/Library/Mobile Documents` is managed
  by two providers; each renames and re-creates files the other holds open. This produces
  conflicts neither client can explain and should be detected and refused at setup.
* **Unicode normalisation.** macOS stores names decomposed (NFD); the server and Windows
  use composed (NFC). Any name comparison upstream (case clashes, ignore rules, conflict
  detection) must normalise first, or the same name looks like two different files.
* **Sandboxed data.** The sandboxed client's config, log and journal live in
  `~/Library/Containers/com.nextcloud.desktopclient/Data`, readable only by the client
  itself or by processes with Full Disk Access. A "report a problem" feature that needs
  those files has to export them from inside the client.

---

## 5. What this tool proves is feasible

The local API prototype in `ncsd/` is a working reference for the upstream feature:

| Capability | Implementation here | Upstream equivalent |
|---|---|---|
| Enumerate real failures, not noise | `detect.py` — client logs + journal `blacklist`/`conflicts` + name rules | `ActivityListModel` fed from `SyncFileItem` (already there, just unfiltered) |
| Find names Windows cannot store | `names.py` (mirrors `csync_exclude.cpp` rules) + deep `PROPFIND` scan | new `SyncIssuesModel` pass, or reuse `SyncEngine`'s discovery result |
| Local vs server metadata | journal row + `PROPFIND` (`getetag`, `oc:fileid`, `oc:checksums`, `oc:permissions`) | new helper in `src/libsync` |
| Resolve per file | `actions.py` (`MOVE`, `PUT` with `X-OC-MTime`, `DELETE`, versions) | `ConflictSolver` is the natural home |
| Never corrupt the journal | snapshot copy, read-only (`journal.py`) | `SyncJournalDb` in-process |

Two details worth carrying into any upstream work:

* Nextcloud lists the **current** revision as a pseudo version whose id equals the file's
  mtime; it is not downloadable (`GET` → 404). Filter it out.
* `X-OC-MTime` on upload and setting the local mtime after a download are what keep the
  client from bouncing the file back and forth.
