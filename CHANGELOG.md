# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-13

macOS support, and a way to check the folder itself when the client's log and journal
are unavailable.

### Added

- **macOS support** (official client 35.0.3, sandboxed FileProvider mode):
  - configuration discovery in `~/Library/Containers/com.nextcloud.desktopclient/Data`,
    with a plain-language explanation when macOS blocks the file (Full Disk Access);
  - the client's app password is read from the login keychain (service `Nextcloud`,
    account `<user>_app-password:<url>`). Stored accounts are listed from the keychain's
    non-secret attribute table, so finding an account never prompts;
  - **FileProvider folders are found**, not configured: a macOS FileProvider account
    stores no folder path in `nextcloud.cfg`, so the folder is located in
    `~/Library/CloudStorage` by its name and reported in the UI;
  - macOS-only findings: Finder-trash leftovers in the folder's `.Trash`, numbered
    `.DS_Store` copies and `._*` sidecars, cloud placeholders (with a warning before a
    preview downloads them), quarantined files, and a warning for a sync folder inside
    iCloud Drive;
  - Unicode normalisation (NFC) for name comparisons and case clashes;
  - Finder integration, client quit/start via `osascript`/`open`, and a
    `Nextcloud Sync Doctor.command` launcher that checks Full Disk Access up front.
- **`ncsd check`** and a **Check the folder itself** button in the UI: walks the folder
  and checks every name against the client's rules without the server, journal or log.
  Breadth-first, skips dependency folders (`venv`, `node_modules`, `.git`, …) and large
  archive folders unless `--all` is given, and stops at a time limit instead of hanging.
- **`--config` / `NCSD_CONFIG`** to read a specific `nextcloud.cfg`.
- `tools/macos-diagnostics.sh`: a read-only probe for where the client keeps its data.
- A separate **macOS ZIP** for every release, built and tested on macOS, next to the
  Windows ZIP.
- The empty state now says what was checked (log, journal, deep scan) and what was not.

### Changed

- `ncsd/platform.py` holds every OS-specific path and rule; `config`, `creds`, `journal`
  and `names` are platform-neutral.
- Conflict copies made by the client are classified as **noise** (`conflict_copy`) instead
  of a problem.
- Launchers pass subcommands through, so `"Nextcloud Sync Doctor.command" --demo scan`
  works instead of always starting the UI.
- CI runs on macOS as well, with a dedicated macOS job; the release bundle ships the
  tests and tools.
- Invalid-name messages explain why a Mac cares about Windows rules.

### Fixed

- Folder keys written without the account prefix (`Folders\1\localPath`) are parsed.
- An empty issue list no longer implies "everything is fine" without saying what was
  checked.

## [1.0.1] - 2026-11-05

- Report only current failures; folders are shown as folders.

## [1.0.0] - 2026-11-05

- First release: per-file sync issue list, resolution actions, backups, audit trail.
